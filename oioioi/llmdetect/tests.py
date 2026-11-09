import importlib.util
import json
import os
import random
import tempfile
import time
from datetime import UTC, datetime, timedelta
from io import StringIO
from types import SimpleNamespace
from unittest import TestCase as SimpleTestCase
from unittest.mock import MagicMock, patch

from django.contrib.auth.models import User
from django.core.files.base import ContentFile, File
from django.core.management import call_command
from django.http import HttpResponse
from django.test import RequestFactory
from django.urls import reverse
from django.utils import translation

from oioioi.base.tests import TestCase
from oioioi.contests.models import Contest, ContestPermission, ProblemInstance, Round
from oioioi.contests.scores import IntegerScore
from oioioi.llmdetect import features as F
from oioioi.llmdetect.controllers import LLMDetectContestControllerMixin
from oioioi.llmdetect.explain import HABIT_LABELS, SIGNAL_LABELS, describe, habit_value
from oioioi.llmdetect.habits import extract_habits
from oioioi.llmdetect.middleware import ProblemViewMiddleware
from oioioi.llmdetect.models import (
    LLMDetectConfig,
    LLMReferenceSolution,
    ProblemViewEvent,
    ReviewDecision,
    RiskAssessment,
    SubmissionFeatures,
    get_config,
)
from oioioi.llmdetect.prompts import PROMPTS, PROMPTS_FORMAT, build_prompt, extract_code, parse_references
from oioioi.llmdetect.scoring import FLAG_THRESHOLD, assess_contest, compute_features, history_submissions, needs_update, read_source, user_rankings
from oioioi.problems.models import Problem
from oioioi.programs.models import ProgramSubmission

T0 = datetime(2026, 8, 1, 9, 0, tzinfo=UTC)

LLM_BFS = """#include <iostream>
#include <vector>
#include <queue>

int main() {
    std::ios::sync_with_stdio(false);
    std::cin.tie(nullptr);

    int numVertices, numEdges;
    std::cin >> numVertices >> numEdges;

    // Build the adjacency list for the undirected graph.
    std::vector<std::vector<int>> adjacency(numVertices + 1);
    for (int i = 0; i < numEdges; ++i) {
        int from, to;
        std::cin >> from >> to;
        adjacency[from].push_back(to);
        adjacency[to].push_back(from);
    }

    // Run BFS from vertex 1 to compute the shortest distances.
    std::vector<long long> distance(numVertices + 1, -1);
    std::queue<int> pending;
    distance[1] = 0;
    pending.push(1);
    while (!pending.empty()) {
        int current = pending.front();
        pending.pop();
        for (const int& neighbor : adjacency[current]) {
            if (distance[neighbor] == -1) {
                distance[neighbor] = distance[current] + 1;
                pending.push(neighbor);
            }
        }
    }

    // Print the distance to every vertex.
    for (int v = 1; v <= numVertices; ++v) {
        std::cout << distance[v] << (v == numVertices ? '\\n' : ' ');
    }
    return 0;
}
"""

HUMAN_BFS = """#include <bits/stdc++.h>
using namespace std;
#define pb push_back
const int N=2e5+7;
vector<int> g[N];
long long d[N];int n,m;
int main(){
    ios_base::sync_with_stdio(0);cin.tie(0);
    cin>>n>>m;
    for(int i=0;i<m;i++){int a,b;cin>>a>>b;g[a].pb(b);g[b].pb(a);}
    //cerr<<"ok"<<endl;
    queue<int> q;q.push(1);
    for(int i=1;i<=n;i++)d[i]=-1;
    d[1]=0;
    while(!q.empty()){int v=q.front();q.pop();
        for(int u:g[v])if(d[u]==-1){d[u]=d[v]+1;q.push(u);}
    }
    for(int i=1;i<=n;i++)cout<<d[i]<<' ';
}
"""

# A beginner's solution: clearly not LLM-like.
SCHOOL_CODE = """#include <iostream>
using namespace std;

int main()
{
    int n;
    cin >> n;

    int a[30000];

    for (int i = 0; i < n; i++)
    {
        cin >> a[i];
    }

    int ile = 0;

    for (int i = 1; i < n - 1; i++)
    {
        if (a[i] > a[i - 1] && a[i] > a[i + 1])
        {
            ile++;
        }
    }

    cout << ile;

    return 0;
}
"""

_STATEMENTS = [
    "for(int i=0;i<n;i++)a[i]{op}=b[i];",
    "if(x{cmp}y)swap(x,y);",
    "while(k{cmp}n){{k{op}=2;c++;}}",
    "s{op}=a[i]*b[j];",
    "for(int j=n-1;j>=0;j--)if(a[j]{cmp}m)m=a[j];",
    "ans=max(ans,dp[i]{op}w[i]);",
    "dp[i][j]=min(dp[i-1][j],dp[i][j-1]){op}1;",
    "cout<<ans<<'\\n';",
    "if(!vis[v]){{vis[v]=1;dfs(v);}}",
    "sort(a,a+n);",
    "r=(l+r)/2;",
    "cnt[a[i]]{op}=1;",
]


def human_code(seed):
    """A short program in a typical olympiad style, different for each seed."""
    rng = random.Random(seed)
    lines = ["#include <bits/stdc++.h>", "using namespace std;", "#define ll long long", "int n,m,k,a[100005],b[100005];", "int main(){", "    cin>>n;"]
    for _ in range(18):
        template = rng.choice(_STATEMENTS)
        lines.append("    " + template.format(op=rng.choice("+-*^|"), cmp=rng.choice(["<", ">", "<=", "==", "!="])))
    if rng.random() < 0.5:
        lines.append('    //cerr<<"dbg "<<ans<<endl;')
    lines.append("}")
    return "\n".join(lines) + "\n"


class _CreatedSubmission:
    def create_submission(self, request, problem_instance, form_data, **kwargs):
        return self.submission


class _PastingController(LLMDetectContestControllerMixin, _CreatedSubmission):
    pass


class TestFeatures(SimpleTestCase):
    def test_llm_style_separates_typical_code(self):
        llm_score, reasons = F.llm_style(F.extract_features(LLM_BFS, F.C_FAMILY))
        human_score, _reasons = F.llm_style(F.extract_features(HUMAN_BFS, F.C_FAMILY))
        self.assertGreater(llm_score, 0.8)
        self.assertLess(human_score, 0.1)
        self.assertIn("sentence_comment_ratio", [name for name, _c in reasons])

    def test_using_namespace_std_is_not_a_std_prefix(self):
        features = F.extract_features(SCHOOL_CODE, F.C_FAMILY)
        self.assertEqual(features["std_qualified_per_100"], 0)
        self.assertEqual(features["using_namespace_std"], 1)
        self.assertGreater(F.extract_features("int main() { std::cout << 1; std::cin.tie(0); }", F.C_FAMILY)["std_qualified_per_100"], 0)

    def test_llm_style_explained_both_ways(self):
        score, reasons = F.llm_style(F.extract_features(SCHOOL_CODE, F.C_FAMILY))
        self.assertLess(score, 0.1)
        detail = {
            "score": score,
            "reasons": [name for name, c in reasons if c > 0][:3],
            "against": [name for name, c in reasons if c < 0][:3],
        }
        text = describe("llm_style", detail)
        self.assertIn("LLM-likeness of the code: ", text)
        self.assertIn("Unlike LLM code: using namespace std, one- or two-letter identifiers", text)
        self.assertNotIn("std:: prefixes", text)

    def test_fingerprints_survive_renaming_and_comment_removal(self):
        disguised = LLM_BFS
        for old, new in [("numVertices", "n"), ("numEdges", "m"), ("adjacency", "g"), ("distance", "d"), ("pending", "q"), ("current", "v")]:
            disguised = disguised.replace(old, new)
        disguised = "\n".join(line for line in disguised.splitlines() if not line.strip().startswith("//"))
        original = F.fingerprints(LLM_BFS, F.C_FAMILY)
        self.assertGreater(F.containment(F.fingerprints(disguised, F.C_FAMILY), original), 0.9)
        self.assertLess(F.containment(F.fingerprints(HUMAN_BFS, F.C_FAMILY), original), 0.4)

    def test_lexer(self):
        tokens, comments = F.lex('#include <x>\nint a = 5; // one\nchar* s = "// not a comment"; /* two */\n', F.C_FAMILY)
        self.assertEqual([c.strip() for c in comments], ["one", "two"])
        self.assertIn(("str", "// not a comment"), tokens)
        self.assertEqual(tokens[0], ("pp", "#include <x>"))
        tokens, comments = F.lex('def f():\n    """Docstring."""\n    return "x"  # note\n', F.PYTHON)
        self.assertEqual([c.strip() for c in comments], ["Docstring.", "note"])

    def test_polish_translations(self):
        with translation.override("pl"):
            self.assertEqual(str(SIGNAL_LABELS["llm_style"]), "Styl typowy dla LLM")
            self.assertEqual(str(HABIT_LABELS["includes"]), "nagłówki")
            self.assertEqual(habit_value("none"), "brak")

    def test_language_and_style_vector(self):
        self.assertEqual(F.language_of("__pasted_code.py"), F.PYTHON)
        self.assertEqual(F.language_of("a.cpp"), F.C_FAMILY)
        vector = F.style_vector(F.extract_features(HUMAN_BFS, F.C_FAMILY))
        self.assertEqual(len(vector), len(F.STYLE_FEATURES))


class TestHabits(SimpleTestCase):
    def test_habits_of_typical_code(self):
        human = extract_habits(HUMAN_BFS, F.C_FAMILY)["values"]
        llm = extract_habits(LLM_BFS, F.C_FAMILY)["values"]
        expected = {
            "includes": ("bits/stdc++.h", "separate"),
            "namespace": ("using namespace std;", "std::"),
            "fast_io": ("ios_base::sync_with_stdio(0); cin.tie(0)", "std::ios::sync_with_stdio(false); std::cin.tie(nullptr)"),
            "storage": ("global arrays", "vectors"),
            "size_constant": ("N = 1eX+c", None),
            "keyword_space": ("if(", "if ("),
            "operator_space": ("tight", "spaced"),
            "naming": ("short names", "camelCase"),
            "main": ("int main()", "int main()"),
            "line_endings": ("LF", "LF"),
        }
        for habit, (human_value, llm_value) in expected.items():
            self.assertEqual((human[habit], llm[habit]), (human_value, llm_value), habit)
        self.assertEqual(extract_habits(HUMAN_BFS, F.C_FAMILY)["macros"], ["pb"])

    def test_aliases_tests_and_functions(self):
        code = "#include <bits/stdc++.h>\ntypedef long long ll;\nvoid solve(){ll x;}\nsigned main(){int t;cin>>t;while(t--)solve();}\n"
        values = extract_habits(code, F.C_FAMILY)["values"]
        self.assertEqual(values["ll_alias"], "typedef long long ll;")
        self.assertEqual(values["test_loop"], "solve() in a test loop")
        self.assertEqual(values["main"], "signed main()")
        self.assertEqual(values["function_names"], "short")
        template = extract_habits(code, F.C_FAMILY)["template"]
        self.assertEqual(template, ["#include<bits/stdc++.h>", "typedef long long ll;"])

    def test_line_endings_and_indentation(self):
        code = "int main() {\r\n\tint a;\r\n\tint b;\r\n\treturn 0;\r\n}\r\n"
        self.assertEqual(extract_habits(code, F.C_FAMILY)["values"]["line_endings"], "CRLF")
        self.assertEqual(extract_habits(code, F.C_FAMILY)["values"]["indent"], "tabs")
        # A browser sends a pasted form field with CRLF, whatever the author uses.
        self.assertNotIn("line_endings", extract_habits(code, F.C_FAMILY, pasted=True)["values"])

    def test_chat_leftovers_and_paste_characters(self):
        answer = "Here is the solution:\n```cpp\n// Time complexity: O(n log n)\nint main() {\n    int\u00a0n;\n}\n```\n"
        habits = extract_habits(answer, F.C_FAMILY)
        self.assertEqual(set(habits["chat"]), {"markdown fence", "complexity", "big O"})
        self.assertEqual(habits["paste_chars"], ["no-break space"])
        clean = extract_habits("\ufeff" + HUMAN_BFS, F.C_FAMILY)
        self.assertEqual((clean["chat"], clean["paste_chars"]), ([], []))
        # Prose in code is not a chat leftover, comments are searched.
        self.assertEqual(extract_habits("// here is the dp\nint main(){}", F.C_FAMILY)["chat"], ["here is"])


class LLMDetectTestCase(TestCase):
    fixtures = ["test_users"]

    def setUp(self):
        super().setUp()
        self.contest = Contest.objects.create(id="camp", name="Camp", controller_name="oioioi.programs.controllers.ProgrammingContestController")
        self.rounds = [
            Round.objects.create(contest=self.contest, name=f"Day {day}", start_date=T0 + timedelta(days=day), end_date=T0 + timedelta(days=day, hours=5))
            for day in range(4)
        ]
        self.pis = [self._problem(round, name) for round, name in zip(self.rounds, ["aaa", "bbb", "ccc", "ddd"], strict=False)]
        self.humans = [User.objects.create_user(f"human{i}", first_name="Human", last_name=str(i)) for i in range(7)]
        self.cheater = User.objects.get(username="test_user")
        self.admin = User.objects.get(username="test_admin")

    def _problem(self, round, short_name):
        controller = "oioioi.programs.controllers.ProgrammingProblemController"
        problem = Problem.objects.create(legacy_name=short_name, short_name=short_name, controller_name=controller)
        return ProblemInstance.objects.create(problem=problem, round=round, contest=self.contest, short_name=short_name)

    def _submit(self, user, pi, code, minutes, score=100, pasted=False):
        date = pi.round.start_date + timedelta(minutes=minutes)
        submission = ProgramSubmission(user=user, problem_instance=pi, date=date, score=IntegerScore(score), max_score=IntegerScore(100))
        submission.source_file.save("__pasted_code.cpp" if pasted else "sol.cpp", ContentFile(code.encode()), save=False)
        submission.save()
        if pasted:
            # As the submission form does.
            controller = _PastingController()
            controller.submission = submission
            controller.create_submission(None, pi, {"file": None, "code": code})
        return submission

    def _camp(self, cheater_code=LLM_BFS, pasted=False):
        """Everybody writes in their own style on the first three days; on
        the last one the cheater pastes an LLM solution, quickly.
        """
        for i, user in enumerate(self.humans + [self.cheater]):
            for day, pi in enumerate(self.pis[:3]):
                self._submit(user, pi, human_code(f"{i}-{day}"), minutes=90 + 7 * i)
        for i, user in enumerate(self.humans):
            self._submit(user, self.pis[3], human_code(f"{i}-3"), minutes=100 + 9 * i)
        return self._submit(self.cheater, self.pis[3], cheater_code, minutes=6, pasted=pasted)


class TestAssessment(LLMDetectTestCase):
    def test_pasted_llm_solution_is_flagged(self):
        LLMReferenceSolution.objects.create(problem_instance=self.pis[3], model_name="chat", source=LLM_BFS)
        disguised = LLM_BFS.replace("numVertices", "n").replace("adjacency", "g")
        submission = self._camp(disguised, pasted=True)
        assess_contest(self.contest)

        flagged = RiskAssessment.objects.get(submission=submission)
        self.assertGreaterEqual(flagged.risk, 0.9)
        for name in ("llm_reference", "llm_style", "style_shift", "fast_solve", "pasted"):
            self.assertIn(name, flagged.signals)
        others = RiskAssessment.objects.filter(contest=self.contest).exclude(submission=submission)
        self.assertEqual(others.count(), 4 * 7 + 3)
        self.assertLess(max(a.risk for a in others), FLAG_THRESHOLD)

        rows = user_rankings(self.contest)
        self.assertEqual(rows[0]["user_id"], self.cheater.id)
        self.assertEqual(rows[0]["flagged"], 1)

    def test_style_alone_without_references(self):
        submission = self._camp()
        assess_contest(self.contest)
        assessment = RiskAssessment.objects.get(submission=submission)
        self.assertNotIn("llm_reference", assessment.signals)
        self.assertIn("style_shift", assessment.signals)
        self.assertEqual(user_rankings(self.contest)[0]["user_id"], self.cheater.id)
        self.assertGreater(assessment.context["profile"]["confidence"], 0.3)

    def test_staff_submissions_are_left_out(self):
        self._submit(self.admin, self.pis[0], LLM_BFS, minutes=1)
        contest_admin = User.objects.get(username="test_user2")
        ContestPermission.objects.create(user=contest_admin, contest=self.contest, permission="contests.contest_basicadmin")
        self._submit(contest_admin, self.pis[0], LLM_BFS, minutes=1)
        self._submit(self.humans[0], self.pis[0], HUMAN_BFS, minutes=50)
        assess_contest(self.contest)
        self.assertEqual(list(RiskAssessment.objects.values_list("user_id", flat=True)), [self.humans[0].id])

    def test_needs_update(self):
        self.assertTrue(needs_update(self.contest))
        assess_contest(self.contest)
        self.assertFalse(needs_update(self.contest))
        self._submit(self.humans[0], self.pis[0], HUMAN_BFS, minutes=50)
        self.assertTrue(needs_update(self.contest))
        assess_contest(self.contest)
        LLMReferenceSolution.objects.create(problem_instance=self.pis[0], source=LLM_BFS)
        config = LLMDetectConfig.objects.get(contest=self.contest)
        config.dirty = True
        config.save()
        self.assertTrue(needs_update(self.contest))

    def test_trusted_rounds_weigh_more(self):
        self._camp()
        assess_contest(self.contest)
        before = RiskAssessment.objects.get(user=self.cheater, submission__problem_instance=self.pis[3]).context["profile"]["weight"]
        config = LLMDetectConfig.objects.get(contest=self.contest)
        config.trusted_rounds.add(self.rounds[0])
        assess_contest(self.contest)
        after = RiskAssessment.objects.get(user=self.cheater, submission__problem_instance=self.pis[3]).context["profile"]["weight"]
        self.assertGreater(after, before)

    def test_rewrite_and_peer_similarity(self):
        self._camp()
        self._submit(self.cheater, self.pis[2], HUMAN_BFS, minutes=200, score=10)
        second = self._submit(self.cheater, self.pis[2], LLM_BFS, minutes=204)
        copy = self._submit(self.humans[0], self.pis[2], LLM_BFS.replace("pending", "todo"), minutes=230)
        assess_contest(self.contest)
        signals = RiskAssessment.objects.get(submission=second).signals
        self.assertIn("rewrite", signals)
        self.assertIn("peer_similarity", RiskAssessment.objects.get(submission=copy).signals)

    def test_after_round_only_code_signals(self):
        self._camp()
        # After the round both write the solution discussed at the lecture.
        late = [self._submit(user, self.pis[2], HUMAN_BFS, minutes=8 * 60) for user in self.humans[:2]]
        assess_contest(self.contest)
        for submission in late:
            assessment = RiskAssessment.objects.get(submission=submission)
            self.assertFalse(assessment.context["during_round"])
            self.assertEqual(assessment.context["peer"]["similarity"], 1.0)
            self.assertNotIn("peer_similarity", assessment.signals)
            self.assertNotIn("burst", assessment.signals)

    def test_broken_habits_and_new_names(self):
        submission = self._camp()
        assess_contest(self.contest)
        assessment = RiskAssessment.objects.get(submission=submission)
        broken = {row["habit"] for row in assessment.signals["habit_break"]["detail"]["broken"]}
        self.assertTrue({"includes", "namespace", "ll_alias", "template", "naming"} <= broken)
        self.assertIn("includes: usually", describe("habit_break", assessment.signals["habit_break"]["detail"]))
        self.assertIn("numVertices", assessment.signals["vocabulary"]["detail"]["new"])
        others = RiskAssessment.objects.filter(contest=self.contest).exclude(submission=submission)
        self.assertFalse([a for a in others if "habit_break" in a.signals or "vocabulary" in a.signals])

    def test_habit_changed_by_everybody_on_a_problem_is_not_counted(self):
        for i, user in enumerate(self.humans):
            for day, pi in enumerate(self.pis[:3]):
                self._submit(user, pi, human_code(f"{i}-{day}"), minutes=90)
            # E.g. the statement suggests a template with tabs.
            self._submit(user, self.pis[3], human_code(f"{i}-3").replace("    ", "\t"), minutes=100)
        assess_contest(self.contest)
        for assessment in RiskAssessment.objects.filter(submission__problem_instance=self.pis[3]):
            self.assertNotIn("habit_break", assessment.signals)
            indent = next(row for row in assessment.context["habits"] if row["habit"] == "indent")
            self.assertTrue(indent["broken"] and indent["problem"])

    def test_chat_answer_pasted_as_a_whole(self):
        answer = "Here is the solution:\n```cpp\n" + LLM_BFS + "```\nExplanation: BFS.\n"
        submission = self._camp(answer.replace("int current", "int\u00a0current"))
        assess_contest(self.contest)
        signals = RiskAssessment.objects.get(submission=submission).signals
        self.assertEqual(signals["chat_leftovers"]["value"], 1.0)
        self.assertGreater(signals["paste_characters"]["value"], 0.8)


class TestHooks(LLMDetectTestCase):
    def test_features_computed_when_judged(self):
        submission = self._submit(self.humans[0], self.pis[0], HUMAN_BFS, minutes=10)
        self.contest.controller.submission_judged(submission)
        features = SubmissionFeatures.objects.get(submission=submission)
        self.assertFalse(features.pasted)
        self.assertEqual(features.language, F.C_FAMILY)

    def test_pasted_code_is_remembered(self):
        submission = self._submit(self.humans[0], self.pis[0], HUMAN_BFS, minutes=10, pasted=True)
        self.assertTrue(SubmissionFeatures.objects.get(submission=submission).pasted)
        # Judging later keeps it, and so does recomputing the features.
        self.contest.controller.submission_judged(submission)
        compute_features(submission)
        self.assertTrue(SubmissionFeatures.objects.get(submission=submission).pasted)

    def test_middleware_logs_first_statement_view(self):
        factory = RequestFactory()
        middleware = ProblemViewMiddleware(lambda request: HttpResponse())
        for minutes in (5, 30):
            request = factory.get("/")
            request.user = self.humans[0]
            request.contest = self.contest
            request.timestamp = T0 + timedelta(minutes=minutes)
            request.resolver_match = SimpleNamespace(url_name="problem_statement", kwargs={"problem_instance": "aaa"})
            middleware(request)
        event = ProblemViewEvent.objects.get()
        self.assertEqual(event.date, T0 + timedelta(minutes=5))
        self.assertEqual(event.problem_instance, self.pis[0])


class TestViews(LLMDetectTestCase):
    def _url(self, name, **kwargs):
        return reverse(name, kwargs={"contest_id": self.contest.id, **kwargs})

    def test_permissions(self):
        self._camp()
        # Risk scores are sensitive: basic admins do not see them either.
        ContestPermission.objects.create(user=self.humans[0], contest=self.contest, permission="contests.contest_basicadmin")
        self.assertTrue(self.client.login(username="human0"))
        self.assertEqual(self.client.get(self._url("llmdetect_panel")).status_code, 403)
        self.assertTrue(self.client.login(username="test_user2"))
        for url in (self._url("llmdetect_panel"), self._url("llmdetect_user", user_id=self.cheater.id), self._url("llmdetect_references")):
            self.assertEqual(self.client.get(url).status_code, 403)
        self.assertEqual(self.client.post(self._url("llmdetect_recompute")).status_code, 403)

    def test_panel_and_user_page(self):
        LLMReferenceSolution.objects.create(problem_instance=self.pis[3], model_name="chat", source=LLM_BFS)
        submission = self._camp()
        self.assertTrue(self.client.login(username="test_admin"))
        response = self.client.get(self._url("llmdetect_panel"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["rows"][0]["user"], self.cheater)
        response = self.client.get(self._url("llmdetect_user", user_id=self.cheater.id))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["selected"].submission_id, submission.id)
        self.assertContains(response, "numVertices")  # the submission and the reference
        self.assertIsNotNone(response.context["typical_source"])

    def test_submission_links(self):
        submission = self._camp()
        self.assertTrue(self.client.login(username="test_admin"))
        code_url = reverse("submission", kwargs={"contest_id": self.contest.id, "submission_id": submission.id})
        analysis_url = self._url("llmdetect_user", user_id=self.cheater.id) + f"?submission={submission.id}"
        # The queue leads to the analysis...
        panel = self.client.get(self._url("llmdetect_panel"))
        self.assertContains(panel, f'href="{analysis_url}"')
        # ...which is above the list of submissions, whose numbers open the code.
        page = self.client.get(analysis_url)
        content = page.content.decode()
        self.assertLess(content.index("Nearest LLM reference"), content.index('<h2 class="h4 mt-4">Submissions</h2>'))
        self.assertContains(page, f'<a href="{code_url}" title="open the submission">#{submission.id}</a>', html=True)
        self.assertEqual(self.client.get(code_url).status_code, 200)

    def test_disqualify_button(self):
        from oioioi.disqualification.models import Disqualification

        submission = self._camp()
        self.assertTrue(self.client.login(username="test_admin"))
        url = self._url("llmdetect_user", user_id=self.cheater.id) + f"?submission={submission.id}"
        page = self.client.get(url)
        self.assertContains(page, "Disqualify this submission")
        form_url = page.context["disqualify_url"]
        self.assertIn(f"submission={submission.id}", form_url)
        self.assertIn(f"user={self.cheater.id}", form_url)
        self.assertTrue(form_url.startswith(f"/c/{self.contest.id}/admin/"), form_url)
        form = self.client.get(form_url)
        self.assertEqual(form.status_code, 200)
        initial = form.context["adminform"].form.initial
        self.assertEqual(str(initial["submission"]), str(submission.id))
        self.assertIn(f"#{submission.id}", initial["content"])
        Disqualification.objects.create(contest=self.contest, user=self.cheater, submission=submission, title="LLM", content="Generated.")
        page = self.client.get(url)
        self.assertNotContains(page, "Disqualify this submission")
        self.assertTrue(page.context["selected_disqualified"])

    def test_confirmed_decision_becomes_reference(self):
        submission = self._camp()
        self.assertTrue(self.client.login(username="test_admin"))
        url = self._url("llmdetect_user", user_id=self.cheater.id)
        response = self.client.post(url, {"verdict": ReviewDecision.VERDICT_CONFIRMED, "submission": submission.id, "note": "Could not explain it."})
        self.assertEqual(response.status_code, 302)
        decision = ReviewDecision.objects.get()
        self.assertEqual(decision.reviewer, self.admin)
        self.assertIsNotNone(decision.risk)
        reference = LLMReferenceSolution.objects.get()
        self.assertEqual(reference.kind, LLMReferenceSolution.KIND_CONFIRMED)
        self.assertEqual(reference.problem_instance, self.pis[3])
        self.assertTrue(LLMDetectConfig.objects.get(contest=self.contest).dirty)

    def test_add_and_delete_references(self):
        self.assertTrue(self.client.login(username="test_admin"))
        url = self._url("llmdetect_references")
        files = [ContentFile(LLM_BFS.encode(), name="a.cpp"), ContentFile(HUMAN_BFS.encode(), name="b.cpp")]
        response = self.client.post(url, {"problem_instance": self.pis[1].id, "model_name": "m", "prompt_variant": "short", "files": files})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(LLMReferenceSolution.objects.filter(problem_instance=self.pis[1], model_name="m").count(), 2)
        self.assertTrue(LLMReferenceSolution.objects.first().fingerprints)
        response = self.client.post(url, {"problem_instance": self.pis[1].id})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(LLMReferenceSolution.objects.count(), 2)
        reference = LLMReferenceSolution.objects.first()
        self.client.post(self._url("llmdetect_delete_reference", reference_id=reference.id))
        self.assertEqual(LLMReferenceSolution.objects.count(), 1)

    def test_chat_answers_added_by_hand_keep_only_the_code(self):
        self.assertTrue(self.client.login(username="test_admin"))
        url = self._url("llmdetect_references")
        answer = "Here is the solution:\n\n```cpp\n" + LLM_BFS + "```\n\nExplanation: we run BFS from vertex 1.\n"
        self.client.post(url, {"problem_instance": self.pis[1].id, "code": answer})
        self.client.post(url, {"problem_instance": self.pis[1].id, "files": [ContentFile(answer.encode(), name="chat.txt")]})
        self.client.post(url, {"problem_instance": self.pis[1].id, "code": HUMAN_BFS})
        sources = list(LLMReferenceSolution.objects.order_by("id").values_list("source", flat=True))
        # Plain code is kept as it is (the form only trims surrounding whitespace).
        self.assertEqual(sources, [LLM_BFS, LLM_BFS, HUMAN_BFS.strip()])

    def test_settings(self):
        self.assertTrue(self.client.login(username="test_admin"))
        self.client.post(self._url("llmdetect_settings"), {"trusted_rounds": [self.rounds[0].id]})
        self.assertEqual(list(LLMDetectConfig.objects.get(contest=self.contest).trusted_rounds.all()), [self.rounds[0]])


class TestCommands(LLMDetectTestCase):
    def test_import_update_purge(self):
        self._camp()
        with tempfile.TemporaryDirectory() as root:
            os.makedirs(os.path.join(root, "ddd"))
            os.makedirs(os.path.join(root, "nope"))
            with open(os.path.join(root, "ddd", "gpt__short__1.cpp"), "w") as file:
                file.write(LLM_BFS)
            with open(os.path.join(root, "ddd", "gemini__plain__1.txt"), "w") as file:
                file.write("Sure! ```cpp\n" + HUMAN_BFS + "``` This reads the graph.")
            call_command("llmdetect_import_references", "camp", root, stdout=StringIO(), stderr=StringIO())
        reference = LLMReferenceSolution.objects.get(model_name="gpt")
        self.assertEqual((reference.prompt_variant, reference.source), ("short", LLM_BFS))
        self.assertEqual(LLMReferenceSolution.objects.get(model_name="gemini").source, HUMAN_BFS)

        call_command("llmdetect_update", "camp", stdout=StringIO())
        self.assertTrue(RiskAssessment.objects.exists())
        ReviewDecision.objects.create(contest=self.contest, user=self.cheater, verdict=ReviewDecision.VERDICT_TALK, note="x")

        call_command("llmdetect_purge", "camp", stdout=StringIO())
        self.assertFalse(RiskAssessment.objects.exists())
        self.assertFalse(SubmissionFeatures.objects.exists())
        decision = ReviewDecision.objects.get()
        self.assertIsNone(decision.user)
        self.assertEqual(decision.note, "")


def _load_script():
    path = os.path.join(os.path.dirname(__file__), "tools", "llm_references.py")
    spec = importlib.util.spec_from_file_location("llm_references", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


script = _load_script()


class TestPrompts(SimpleTestCase):
    def test_extract_code(self):
        answer = "Here is the solution:\n```cpp\nint main() {}\n```\nAnd a test:\n```\n1 2\n```"
        self.assertEqual(extract_code(answer), "int main() {}\n")
        self.assertEqual(extract_code("int main() {}"), "int main() {}\n")
        self.assertEqual(script.extract_code(answer), extract_code(answer))

    def test_prompts_contain_the_statement(self):
        for variant in PROMPTS:
            prompt = build_prompt(variant, "Given n, print n+1.\n")
            self.assertTrue(prompt.endswith("\n\nGiven n, print n+1."))
            self.assertIn("C++", prompt)

    def test_statement_text_formats(self):
        from oioioi.llmdetect.prompts import _text_of

        self.assertEqual(_text_of(b"<html><body><h1>Zadanie</h1><p>Dane jest n.</p></body></html>", ".html"), "Zadanie\nDane jest n.")
        self.assertEqual(_text_of(b"Dane\r\n\r\n\r\n\r\njest n.", ".txt"), "Dane\n\njest n.")
        self.assertEqual(_text_of(b"\x00", ".ps"), "")


class TestBulkFormat(SimpleTestCase):
    problems = {"abc": "pi-abc", "xyz": "pi-xyz"}

    def test_parse(self):
        text = (
            '### reference problem=abc model="ChatGPT free" prompt=polish\n'
            "int main(){}\n"
            "\n"
            "### Reference problem=xyz model=groq/openai/gpt-oss-120b prompt=student n=2 extra=ignored\n"
            "Here is the solution:\n```cpp\nint main() { return 0; }\n```\nExplanation: none.\n"
            "### reference model=duck.ai\n"
            "int f(){}\n"
        )
        references, errors = parse_references(text, self.problems, default_problem="pi-default")
        self.assertEqual(errors, [])
        self.assertEqual(
            [(r["problem_instance"], r["model"], r["prompt"], r["source"], r["line"]) for r in references],
            [
                ("pi-abc", "ChatGPT free", "polish", "int main(){}\n", 1),
                ("pi-xyz", "groq/openai/gpt-oss-120b", "student", "int main() { return 0; }\n", 4),
                ("pi-default", "duck.ai", "", "int f(){}\n", 10),
            ],
        )

    def test_errors(self):
        text = "stray text\n### reference problem=nope\nint main(){}\n### reference problem=abc\n\n### reference\nint g(){}\n"
        references, errors = parse_references(text, self.problems)
        self.assertEqual(len(errors), 4)
        self.assertIn("before the first", errors[0])
        self.assertIn("no problem nope", errors[1])
        self.assertIn("Line 4: no code", errors[2])
        self.assertIn("Line 6: no problem given", errors[3])
        self.assertEqual(parse_references("int main(){}", self.problems)[1], ['No "### reference" lines found.'])


class TestScript(SimpleTestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.output = os.path.join(self.directory.name, "references.txt")
        self.now = [0.0]
        self.slept = []
        patches = [
            patch.object(script, "clock", lambda: self.now[0]),
            patch.object(script, "sleep", self._sleep),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def tearDown(self):
        self.directory.cleanup()

    def _sleep(self, seconds):
        self.slept.append(round(seconds, 3))
        self.now[0] += seconds

    def _providers(self, **configs):
        return [script.Provider(name, config) for name, config in configs.items()]

    prompts = [{"problem": "abc", "prompt": "plain", "text": "P1"}, {"problem": "xyz", "prompt": "student", "text": "P2"}]

    def test_output_is_the_bulk_format_and_providers_alternate(self):
        asked = []

        def ask(config, model, prompt):
            asked.append((config["name"], model, prompt))
            return f"Sure:\n```cpp\n// {model} {prompt}\nint main(){{}}\n```"

        providers = self._providers(a={"name": "a", "models": ["m1"], "min_interval": 10}, b={"name": "b", "models": ["m2", "m3"], "min_interval": 10})
        with patch.object(script, "ask", ask):
            result = script.run(self.prompts, providers, count=2, output=self.output, log=lambda *a: None)
        self.assertEqual(result, {"done": 12, "failed": 0, "left": 0})
        # a and b take turns; neither waits for the other's interval.
        self.assertEqual([name for name, _m, _p in asked[:4]], ["a", "b", "a", "b"])
        with open(self.output, encoding="utf-8") as file:
            text = file.read()
        references, errors = parse_references(text, {"abc": "pi-abc", "xyz": "pi-xyz"})
        self.assertEqual(errors, [])
        self.assertEqual(len(references), 12)
        self.assertEqual(references[0]["source"], "// m1 P1\nint main(){}\n")
        self.assertEqual({r["model"] for r in references}, {"a/m1", "b/m2", "b/m3"})

    def test_resume_and_quota(self):
        calls = {"n": 0}

        def ask(config, model, prompt):
            calls["n"] += 1
            if config["name"] == "b":
                raise script.QuotaError("HTTP 429: daily limit", retry_after=3600)
            return "int main(){}"

        providers = self._providers(a={"name": "a", "models": ["m"]}, b={"name": "b", "models": ["m"]})
        with patch.object(script, "ask", ask):
            result = script.run(self.prompts, providers, count=1, output=self.output, log=lambda *a: None)
        # b is out of quota: left for the next run, a finished.
        self.assertEqual(result, {"done": 2, "failed": 0, "left": 2})
        self.assertEqual(providers[1].stopped.startswith("out of quota"), True)
        # The next run asks only for what is missing.
        providers = self._providers(a={"name": "a", "models": ["m"]}, b={"name": "b", "models": ["m"]})
        with patch.object(script, "ask", lambda config, model, prompt: "int g(){}"):
            result = script.run(self.prompts, providers, count=1, output=self.output, log=lambda *a: None)
        self.assertEqual(result, {"done": 2, "failed": 0, "left": 0})
        self.assertEqual(len(script.done_keys(self.output)), 4)

    def test_short_waits_and_daily_limit(self):
        answers = iter([script.QuotaError("HTTP 429", retry_after=20), "int a(){}", "int b(){}", "int c(){}"])

        def ask(config, model, prompt):
            answer = next(answers)
            if isinstance(answer, Exception):
                raise answer
            return answer

        providers = self._providers(a={"name": "a", "models": ["m"], "daily_limit": 2})
        with patch.object(script, "ask", ask):
            result = script.run(self.prompts, providers, count=2, output=self.output, log=lambda *a: None)
        self.assertIn(20, self.slept)
        # Two requests (one refused, one answered), then the daily limit.
        self.assertEqual(result, {"done": 1, "failed": 0, "left": 3})
        self.assertIn("daily_limit", providers[0].stopped)

    def test_unknown_model_is_skipped(self):
        def ask(config, model, prompt):
            if model == "gone":
                raise script.ModelError("HTTP 404: no such model")
            return "int main(){}"

        providers = self._providers(a={"name": "a", "models": ["gone", "ok"]})
        with patch.object(script, "ask", ask) as _ask:
            result = script.run(self.prompts, providers, count=2, output=self.output, log=lambda *a: None)
        self.assertEqual(result, {"done": 4, "failed": 4, "left": 0})

    def test_providers_without_a_key_are_skipped(self):
        config = os.path.join(self.directory.name, "providers.json")
        with open(config, "w") as file:
            json.dump(
                {"x": {"api_key_env": "LLMDETECT_TEST_UNSET", "models": ["m"]}, "local": {"base_url": "http://h/v1", "models": ["m"]}, "empty": {"models": []}},
                file,
            )
        logged = []
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("LLMDETECT_TEST_UNSET", None)
            providers = script.load_providers(config, log=logged.append)
        self.assertEqual([p.name for p in providers], ["local"])
        self.assertIn("LLMDETECT_TEST_UNSET is not set", logged[0])

    def test_openai_request(self):
        response = MagicMock()
        response.read.return_value = json.dumps({"choices": [{"message": {"content": "int main(){}"}}]}).encode()
        response.__enter__.return_value = response
        with patch.object(script.urllib.request, "urlopen", return_value=response) as urlopen, patch.dict(os.environ, {"LLMDETECT_TEST_KEY": "k"}):
            answer = script.ask({"base_url": "https://example.com/v1/", "api_key_env": "LLMDETECT_TEST_KEY", "max_tokens": 100}, "m", "prompt")
        self.assertEqual(answer, "int main(){}")
        request = urlopen.call_args.args[0]
        self.assertEqual(request.full_url, "https://example.com/v1/chat/completions")
        self.assertEqual(request.get_header("Authorization"), "Bearer k")
        self.assertEqual(json.loads(request.data), {"model": "m", "messages": [{"role": "user", "content": "prompt"}], "max_tokens": 100})

    def test_rate_limit_headers(self):
        self.assertEqual(script.seconds("30"), 30)
        self.assertEqual(script.seconds("2m59.5s"), 179.5)
        self.assertEqual(script.seconds("1h"), 3600)
        self.assertEqual(script.seconds(str(int((1_800_000_000 + 90) * 1000)), now=1_800_000_000), 90)
        self.assertIsNone(script.seconds("soon"))
        self.assertEqual(script.header(problem="abc", model="ChatGPT free", n=1), '### reference problem=abc model="ChatGPT free" n=1')


class TestReferencesPage(LLMDetectTestCase):
    def _url(self, name="llmdetect_references", **params):
        url = reverse(name, kwargs={"contest_id": self.contest.id})
        return url + ("?" + "&".join(f"{k}={v}" for k, v in params.items()) if params else "")

    def setUp(self):
        super().setUp()
        self.assertTrue(self.client.login(username="test_admin"))

    def test_load_statement_and_copy_buttons(self):
        with patch("oioioi.llmdetect.views.statement_text", return_value="Dane jest n."):
            response = self.client.get(self._url(problem=self.pis[0].id, statement=1))
        self.assertEqual(response.context["prompts_form"].initial["statement"], "Dane jest n.")
        for _label, instruction in PROMPTS.values():
            self.assertContains(response, f'data-instruction="{instruction}"')
        with patch("oioioi.llmdetect.views.statement_text", return_value=""):
            response = self.client.get(self._url(problem=self.pis[0].id, statement=1))
        self.assertContains(response, "Could not read the statement")

    def test_download_prompts_of_one_problem(self):
        response = self.client.post(
            self._url(), {"action": "prompts", "problem_instance": self.pis[0].id, "statement": "Edited.", "prompts": ["plain", "student"]}
        )
        self.assertEqual(response["Content-Type"], "application/json")
        self.assertIn("prompts-camp-aaa.json", response["Content-Disposition"])
        data = json.loads(response.content)
        self.assertEqual(data["format"], PROMPTS_FORMAT)
        self.assertEqual(data["prompts"], [{"problem": "aaa", "prompt": v, "text": build_prompt(v, "Edited.")} for v in ("plain", "student")])

    def test_download_prompts_of_all_problems_and_the_script(self):
        with patch("oioioi.llmdetect.views.statement_text", side_effect=lambda pi: "" if pi.short_name == "bbb" else f"Statement {pi.short_name}"):
            data = json.loads(self.client.get(self._url("llmdetect_all_prompts")).content)
        self.assertEqual({p["problem"] for p in data["prompts"]}, {"aaa", "ccc", "ddd"})
        self.assertEqual(len(data["prompts"]), 3 * len(PROMPTS))
        response = self.client.get(self._url("llmdetect_script"))
        self.assertIn("llm_references.py", response["Content-Disposition"])
        self.assertIn(b"def main(argv=None):", response.content)

    def test_add_many(self):
        text = "### reference problem=aaa model=x prompt=plain\nint a(){}\n### reference problem=bbb model=y\n```cpp\nint b(){}\n```\n"
        response = self.client.post(self._url(), {"action": "bulk", "text": text})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(
            sorted(LLMReferenceSolution.objects.values_list("problem_instance__short_name", "model_name", "source")),
            [("aaa", "x", "int a(){}\n"), ("bbb", "y", "int b(){}\n")],
        )
        self.assertTrue(LLMDetectConfig.objects.get(contest=self.contest).dirty)
        # The same again, as a file, with one more: duplicates are skipped.
        upload = ContentFile((text + "### reference problem=aaa\nint c(){}\n").encode(), name="references.txt")
        self.client.post(self._url(), {"action": "bulk", "file": upload}, follow=True)
        self.assertEqual(LLMReferenceSolution.objects.count(), 3)

    def test_add_many_with_errors_adds_nothing(self):
        text = "### reference problem=aaa\nint a(){}\n### reference problem=nope\nint b(){}\n"
        response = self.client.post(self._url(), {"action": "bulk", "text": text})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "no problem nope")
        self.assertEqual(LLMReferenceSolution.objects.count(), 0)
        self.client.post(self._url(), {"action": "bulk", "text": "### reference\nint a(){}\n", "default_problem": self.pis[2].id})
        self.assertEqual(LLMReferenceSolution.objects.get().problem_instance, self.pis[2])


class TestHostileCode(SimpleTestCase):
    """Participants control the code that is analysed on every submission
    (in the web request for pasted code, in the evaluation worker after
    judging): no input may make the analysis slow or its stored result big.
    """

    N = 100 * 1024  # the default submission size limit

    def _analyse(self, source, filename="a.cpp"):
        analysis = F.analyze_source(source, filename)
        return analysis, extract_habits(source, analysis["language"])

    def test_no_quadratic_patterns(self):
        n = self.N
        cases = {
            "big O in a comment": "// " + "O(n" * (n // 3),
            "calls in a comment": "// " + "f(x)" * (n // 4),
            "open parentheses in a comment": "// " + "a(" * (n // 2),
            "block comment": "/*" + "f(x)" * (n // 4) + "*/",
            "main( repeated": "int main(" * (n // 9),
            "const without &": "const a " * (n // 8),
            "for( without ;": "for(" * (n // 4),
            "Python comment": "# " + "O(n" * (n // 3),
        }
        for name, source in cases.items():
            start = time.monotonic()
            self._analyse(source, "a.py" if name.startswith("Python") else "a.cpp")
            # Each took 2-11 s before the patterns were made linear; ~0.1 s now.
            self.assertLess(time.monotonic() - start, 2.0, name)

    def test_stored_analysis_is_bounded(self):
        n = self.N
        for source in ("a\n" * (n // 2), " ".join(f"v{i}" for i in range(n // 7)), "".join(f"int g{i};\n" for i in range(n // 10))):
            analysis, habits = self._analyse(source)
            size = sum(len(json.dumps(value)) for value in (analysis["fingerprints"], analysis["line_hashes"], analysis["features"], habits))
            self.assertLess(size, 100 * 1024)
        self.assertLessEqual(len(F.fingerprints(" ".join(f"x{i} + {i};" for i in range(20000)), F.C_FAMILY)), F.MAX_FINGERPRINTS)

    def test_unclosed_code_fences(self):
        start = time.monotonic()
        # The fences pair up into empty blocks, as with the previous regex.
        self.assertEqual(extract_code("```cpp\n" * 300_000), "\n")
        self.assertEqual(extract_code("Here:\n```cpp\nint a;\n```\nand\n```\nint bb;\n```"), "int bb;\n")
        self.assertLess(time.monotonic() - start, 2.0)

    def test_bulk_upload_limit(self):
        from oioioi.llmdetect import prompts

        with patch.object(prompts, "MAX_BULK_REFERENCES", 3):
            references, errors = parse_references("### reference problem=abc\nint a;\n" * 4, {"abc": "pi"})
        self.assertEqual(references, [])
        self.assertIn("More than 3 references", errors[0])


class TestPanelExplainsEmptyQueue(LLMDetectTestCase):
    def setUp(self):
        super().setUp()
        self.assertTrue(self.client.login(username="test_admin"))
        self.url = reverse("llmdetect_panel", kwargs={"contest_id": self.contest.id})

    def test_summary(self):
        self._submit(self.humans[0], self.pis[0], HUMAN_BFS, minutes=10)
        self._submit(self.admin, self.pis[0], HUMAN_BFS, minutes=10)
        response = self.client.get(self.url)
        self.assertEqual(response.context["summary"], {"total": 2, "counted": 1, "staff": 1, "analysed": 1, "history": 0})
        self.assertContains(response, "Analysed 1 of 1 submissions by participants.")
        self.assertContains(response, "1 submissions by staff")

    def test_only_staff_submissions(self):
        self._submit(self.admin, self.pis[0], HUMAN_BFS, minutes=10)
        self.assertContains(self.client.get(self.url), "All submissions of this contest are by staff")

    def test_unreadable_sources_are_reported_and_retried(self):
        submission = self._submit(self.humans[0], self.pis[0], HUMAN_BFS, minutes=10)
        with patch("oioioi.llmdetect.scoring.read_source", side_effect=FileNotFoundError("no such file")):
            response = self.client.get(self.url)
        self.assertContains(response, "1 submission could not be analysed")
        self.assertContains(response, f"#{submission.id}: FileNotFoundError: no such file")
        self.assertEqual(response.context["rows"], [])
        # Once the file can be read, Recompute brings it in.
        self.client.post(reverse("llmdetect_recompute", kwargs={"contest_id": self.contest.id}))
        response = self.client.get(self.url)
        self.assertNotContains(response, "could not be analysed")
        self.assertEqual(len(response.context["rows"]), 1)


class _StreamWithoutClose:
    """Like filetracker's ``_FileLikeFromResponse``: it can only be read."""

    def __init__(self, data):
        self.data = data

    def read(self, size=None):
        data, self.data = self.data, b""
        return data


class TestNewAnalysisVersion(LLMDetectTestCase):
    def test_older_analysis_is_recomputed(self):
        submission = self._submit(self.humans[0], self.pis[0], HUMAN_BFS, minutes=10)
        assess_contest(self.contest)
        self.assertFalse(needs_update(self.contest))
        SubmissionFeatures.objects.filter(submission=submission).update(version=F.FEATURES_VERSION - 1)
        self.assertTrue(needs_update(self.contest))
        assess_contest(self.contest)
        self.assertEqual(SubmissionFeatures.objects.get(submission=submission).version, F.FEATURES_VERSION)
        self.assertFalse(needs_update(self.contest))


class TestFiletrackerSources(LLMDetectTestCase):
    def test_source_without_close_is_read(self):
        submission = self._submit(self.humans[0], self.pis[0], HUMAN_BFS, minutes=10)
        submission = ProgramSubmission.objects.get(id=submission.id)
        # As FiletrackerStorage._open returns it.
        submission.source_file.file = File(_StreamWithoutClose(HUMAN_BFS.encode()), submission.source_file.name)
        self.assertEqual(read_source(submission), HUMAN_BFS)


class TestLineEndingsOfOldSubmissions(LLMDetectTestCase):
    """Pasted code always arrives with CRLF. Submissions analysed long after
    they were sent (an older contest) may have been pasted or uploaded, so
    their line endings must not count as a habit.
    """

    CRLF = HUMAN_BFS.replace("\n", "\r\n")

    def test_unknown_paste_has_no_line_endings_habit(self):
        self.assertNotIn("line_endings", extract_habits(self.CRLF, F.C_FAMILY, paste_known=False)["values"])
        self.assertEqual(extract_habits(self.CRLF, F.C_FAMILY)["values"]["line_endings"], "CRLF")

    def test_backfilled_and_judged_submissions(self):
        old = self._submit(self.humans[0], self.pis[0], self.CRLF, minutes=10)
        assess_contest(self.contest)  # analyses it after the fact
        features = SubmissionFeatures.objects.get(submission=old)
        self.assertFalse(features.paste_known)
        self.assertNotIn("line_endings", features.habits["values"])
        new = self._submit(self.humans[1], self.pis[0], self.CRLF, minutes=10)
        self.contest.controller.submission_judged(new)  # analysed when judged: an uploaded file
        features = SubmissionFeatures.objects.get(submission=new)
        self.assertTrue(features.paste_known)
        self.assertEqual(features.habits["values"]["line_endings"], "CRLF")
        # Recomputing keeps what is known.
        compute_features(old)
        self.assertFalse(SubmissionFeatures.objects.get(submission=old).paste_known)

    def test_no_false_habit_break_in_an_older_contest(self):
        user = self.humans[0]
        for day, pi in enumerate(self.pis[:3]):
            self._submit(user, pi, human_code(f"0-{day}"), minutes=60)  # uploaded, LF
        self._submit(user, self.pis[3], human_code("0-3").replace("\n", "\r\n"), minutes=60)  # pasted: CRLF
        for i, other in enumerate(self.humans[1:]):
            for day, pi in enumerate(self.pis):
                self._submit(other, pi, human_code(f"{i + 1}-{day}"), minutes=60)
        assess_contest(self.contest)
        assessment = RiskAssessment.objects.get(user=user, submission__problem_instance=self.pis[3])
        self.assertNotIn("line_endings", [row["habit"] for row in assessment.context.get("habits", [])])
        self.assertNotIn("habit_break", assessment.signals)

    def test_migration_marks_backfilled_features(self):
        import importlib

        from django.apps import apps

        migration = importlib.import_module("oioioi.llmdetect.migrations.0004_paste_known")
        sent_and_judged = self._submit(self.humans[0], self.pis[0], HUMAN_BFS, minutes=10)
        backfilled = self._submit(self.humans[1], self.pis[0], HUMAN_BFS, minutes=10)
        pasted = self._submit(self.humans[2], self.pis[0], HUMAN_BFS, minutes=10, pasted=True)
        for submission in (sent_and_judged, backfilled, pasted):
            compute_features(submission)
        SubmissionFeatures.objects.update(paste_known=True)
        SubmissionFeatures.objects.filter(submission=sent_and_judged).update(computed_at=sent_and_judged.date + timedelta(minutes=5))
        SubmissionFeatures.objects.filter(submission__in=[backfilled, pasted]).update(computed_at=backfilled.date + timedelta(days=30))
        migration.mark_backfilled(apps, None)
        known = dict(SubmissionFeatures.objects.values_list("submission_id", "paste_known"))
        self.assertEqual(known, {sent_and_judged.id: True, backfilled.id: False, pasted.id: True})


class TestHistoryContests(LLMDetectTestCase):
    """Submissions of the same participants in earlier contests (camps)."""

    def setUp(self):
        super().setUp()
        self.old = Contest.objects.create(id="old", name="Old camp", controller_name="oioioi.programs.controllers.ProgrammingContestController")
        start = T0 - timedelta(days=90)
        self.old_rounds = [
            Round.objects.create(contest=self.old, name=f"Day {day}", start_date=start + timedelta(days=day), end_date=start + timedelta(days=day, hours=5))
            for day in range(3)
        ]
        self.old_pis = [self._old_problem(round, name) for round, name in zip(self.old_rounds, ["xxx", "yyy", "zzz"], strict=True)]
        self.config = get_config(self.contest)

    def _old_problem(self, round, short_name, problem=None):
        if problem is None:
            controller = "oioioi.programs.controllers.ProgrammingProblemController"
            problem = Problem.objects.create(legacy_name=short_name, short_name=short_name, controller_name=controller)
        return ProblemInstance.objects.create(problem=problem, round=round, contest=self.old, short_name=short_name)

    def _first_day_with_cheater(self):
        """Everybody wrote their own code at the earlier camp; this camp has
        only had its first day, on which the cheater pastes an LLM solution.
        """
        for i, user in enumerate(self.humans + [self.cheater]):
            for day, pi in enumerate(self.old_pis):
                self._submit(user, pi, human_code(f"{i}-old-{day}"), minutes=90 + 7 * i)
        for i, user in enumerate(self.humans):
            self._submit(user, self.pis[0], human_code(f"{i}-0"), minutes=100 + 9 * i)
        return self._submit(self.cheater, self.pis[0], LLM_BFS, minutes=95)

    def test_habits_known_from_an_earlier_contest(self):
        submission = self._first_day_with_cheater()
        assess_contest(self.contest)
        # On the first day nobody has a history in this contest.
        alone = RiskAssessment.objects.get(submission=submission)
        self.assertNotIn("habit_break", alone.signals)
        self.config.history_contests.add(self.old)
        assess_contest(self.contest)
        assessment = RiskAssessment.objects.get(submission=submission)
        broken = {row["habit"] for row in assessment.signals["habit_break"]["detail"]["broken"]}
        self.assertTrue({"includes", "namespace", "ll_alias"} <= broken)
        self.assertGreater(assessment.risk, alone.risk)
        profile = assessment.context["profile"]
        self.assertAlmostEqual(profile["history"], 3 * self.config.history_weight, places=3)
        self.assertEqual(profile["weight"], profile["history"])
        self.assertTrue(self.client.login(username="test_admin"))
        page = self.client.get(reverse("llmdetect_user", kwargs={"contest_id": self.contest.id, "user_id": self.cheater.id}))
        self.assertContains(page, "1.5 from earlier contests")
        # Earlier submissions are only part of profiles: not analysed again.
        self.assertFalse(RiskAssessment.objects.filter(submission__problem_instance__contest=self.old).exists())
        others = RiskAssessment.objects.filter(contest=self.contest).exclude(submission=submission)
        self.assertFalse([a for a in others if "habit_break" in a.signals])

    def test_weight(self):
        self._first_day_with_cheater()
        self.config.history_contests.add(self.old)
        self.config.history_weight = 0.2
        self.config.save()
        assess_contest(self.contest)
        assessment = RiskAssessment.objects.filter(contest=self.contest).first()
        self.assertAlmostEqual(assessment.context["profile"]["history"], 0.6, places=3)

    def test_same_problem_in_an_earlier_contest_is_left_out(self):
        again = self._old_problem(self.old_rounds[0], "aaa", problem=self.pis[0].problem)
        submission = self._submit(self.humans[0], self.pis[0], HUMAN_BFS, minutes=10)
        self._submit(self.humans[0], again, HUMAN_BFS, minutes=10)
        self.config.history_contests.add(self.old)
        assess_contest(self.contest)
        self.assertEqual(RiskAssessment.objects.get(submission=submission).context["profile"]["history"], 0)

    def test_only_participants_of_this_contest(self):
        stranger = User.objects.create_user("stranger")
        self._submit(stranger, self.old_pis[0], HUMAN_BFS, minutes=10)
        self._submit(self.humans[0], self.old_pis[0], HUMAN_BFS, minutes=10)
        self._submit(self.humans[0], self.pis[0], HUMAN_BFS, minutes=10)
        self.config.history_contests.add(self.old)
        self.assertEqual([s.user for s in history_submissions(self.contest, [self.humans[0].id])], [self.humans[0]])
        # The summary counts those of the participants of this contest only.
        self.assertTrue(self.client.login(username="test_admin"))
        response = self.client.get(reverse("llmdetect_panel", kwargs={"contest_id": self.contest.id}))
        self.assertEqual(response.context["summary"]["history"], 1)
        self.assertContains(response, "Profiles also use 1 submissions of these participants from earlier contests.")

    def test_settings_list_only_administered_contests(self):
        secret = Contest.objects.create(id="secret", name="Secret", controller_name="oioioi.programs.controllers.ProgrammingContestController")
        admin = self.humans[6]
        for contest in (self.contest, self.old):
            ContestPermission.objects.create(user=admin, contest=contest, permission="contests.contest_admin")
        # Chosen earlier by an admin of both.
        self.config.history_contests.add(secret)
        self.assertTrue(self.client.login(username=admin.username))
        url = reverse("llmdetect_settings", kwargs={"contest_id": self.contest.id})
        form = self.client.get(reverse("llmdetect_panel", kwargs={"contest_id": self.contest.id})).context["settings_form"]
        self.assertEqual(list(form.fields["history_contests"].queryset), [self.old])
        self.assertEqual(form.hidden_history, [secret])
        # Contests the admin cannot see cannot be chosen...
        self.client.post(url, {"history_contests": [secret.id], "history_weight": "0.3"})
        self.config.refresh_from_db()
        self.assertEqual(self.config.history_weight, 0.5)
        # ...nor removed.
        self.client.post(url, {"history_contests": [self.old.id], "history_weight": "0.3"})
        self.config.refresh_from_db()
        self.assertEqual(set(self.config.history_contests.all()), {self.old, secret})
        self.assertEqual(self.config.history_weight, 0.3)
        self.assertTrue(self.config.dirty)
