import os
import random
import tempfile
from datetime import UTC, datetime, timedelta
from io import StringIO
from types import SimpleNamespace
from unittest import TestCase as SimpleTestCase
from unittest.mock import MagicMock, patch

from django.contrib.auth.models import User
from django.core.files.base import ContentFile
from django.core.management import call_command
from django.http import HttpResponse
from django.test import RequestFactory, override_settings
from django.urls import reverse
from django.utils import translation

from oioioi.base.tests import TestCase
from oioioi.contests.models import Contest, ContestPermission, ProblemInstance, Round
from oioioi.contests.scores import IntegerScore
from oioioi.llmdetect import features as F
from oioioi.llmdetect.controllers import LLMDetectContestControllerMixin
from oioioi.llmdetect.explain import HABIT_LABELS, SIGNAL_LABELS, describe, habit_value
from oioioi.llmdetect.generation import PROMPTS, build_prompt, extract_code
from oioioi.llmdetect.habits import extract_habits
from oioioi.llmdetect.middleware import ProblemViewMiddleware
from oioioi.llmdetect.models import GenerationJob, LLMDetectConfig, LLMReferenceSolution, ProblemViewEvent, ReviewDecision, RiskAssessment, SubmissionFeatures
from oioioi.llmdetect.scoring import FLAG_THRESHOLD, assess_contest, compute_features, needs_update, user_rankings
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
            call_command("llmdetect_import_references", "camp", root, stdout=StringIO(), stderr=StringIO())
        reference = LLMReferenceSolution.objects.get()
        self.assertEqual((reference.model_name, reference.prompt_variant), ("gpt", "short"))

        call_command("llmdetect_update", "camp", stdout=StringIO())
        self.assertTrue(RiskAssessment.objects.exists())
        ReviewDecision.objects.create(contest=self.contest, user=self.cheater, verdict=ReviewDecision.VERDICT_TALK, note="x")

        call_command("llmdetect_purge", "camp", stdout=StringIO())
        self.assertFalse(RiskAssessment.objects.exists())
        self.assertFalse(SubmissionFeatures.objects.exists())
        decision = ReviewDecision.objects.get()
        self.assertIsNone(decision.user)
        self.assertEqual(decision.note, "")


PROVIDERS = {
    "free": {"type": "openai", "base_url": "https://example.com/v1/", "api_key": "k", "models": ["small:free", "big:free"]},
    "claude": {"type": "anthropic", "api_key": "k", "models": ["claude-opus-5-5"]},
}


def _chat_response(text, status=200):
    response = MagicMock(status_code=status, text=text, headers={})
    response.json.return_value = {"choices": [{"message": {"content": text}}]}
    return response


class TestGenerationHelpers(SimpleTestCase):
    def test_extract_code(self):
        answer = "Here is the solution:\n```cpp\nint main() {}\n```\nAnd a test:\n```\n1 2\n```"
        self.assertEqual(extract_code(answer), "int main() {}\n")
        self.assertEqual(extract_code("int main() {}"), "int main() {}\n")

    def test_prompts_contain_the_statement(self):
        for variant in PROMPTS:
            prompt = build_prompt(variant, "Given n, print n+1.")
            self.assertTrue(prompt.endswith("Given n, print n+1."))
            self.assertIn("C++", prompt)


@override_settings(LLMDETECT_PROVIDERS=PROVIDERS)
class TestGeneration(LLMDetectTestCase):
    def _job(self, **kwargs):
        defaults = {
            "problem_instance": self.pis[0],
            "provider": "free",
            "model": "small:free",
            "prompt_variant": "plain",
            "count": 2,
            "statement": "Add two numbers.",
        }
        return GenerationJob.objects.create(**{**defaults, **kwargs})

    def _url(self, **params):
        url = reverse("llmdetect_references", kwargs={"contest_id": self.contest.id})
        return url + ("?" + "&".join(f"{k}={v}" for k, v in params.items()) if params else "")

    def test_generate_form_queues_jobs_with_the_edited_statement(self):
        self.assertTrue(self.client.login(username="test_admin"))
        response = self.client.post(
            self._url(),
            {
                "action": "generate",
                "problem_instance": self.pis[0].id,
                "statement": "Edited statement.",
                "models": ["free:small:free", "claude:claude-opus-5-5"],
                "prompts": ["plain", "student"],
                "count": 3,
            },
        )
        self.assertEqual(response.status_code, 302)
        jobs = GenerationJob.objects.all()
        self.assertEqual(jobs.count(), 4)
        self.assertEqual({(j.provider, j.model) for j in jobs}, {("free", "small:free"), ("claude", "claude-opus-5-5")})
        self.assertTrue(all(j.statement == "Edited statement." and j.count == 3 and j.status == GenerationJob.QUEUED for j in jobs))
        page = self.client.get(self._url(problem=self.pis[0].id))
        self.assertContains(page, "small:free")

    def test_load_statement(self):
        self.assertTrue(self.client.login(username="test_admin"))
        with patch("oioioi.llmdetect.views.statement_text", return_value="Dane jest n."):
            response = self.client.get(self._url(problem=self.pis[0].id, statement=1))
        self.assertEqual(response.context["generate_form"].initial["statement"], "Dane jest n.")
        with patch("oioioi.llmdetect.views.statement_text", return_value=""):
            response = self.client.get(self._url(problem=self.pis[0].id, statement=1))
        self.assertContains(response, "Could not read the statement")

    def test_statement_text_formats(self):
        from oioioi.llmdetect.generation import _text_of

        self.assertEqual(_text_of(b"<html><body><h1>Zadanie</h1><p>Dane jest n.</p></body></html>", ".html"), "Zadanie\nDane jest n.")
        self.assertEqual(_text_of(b"Dane\r\n\r\n\r\n\r\njest n.", ".txt"), "Dane\n\njest n.")
        self.assertEqual(_text_of(b"\x00", ".ps"), "")

    def test_openai_compatible_job_creates_references(self):
        job = self._job()
        with patch("oioioi.llmdetect.generation.requests.post", return_value=_chat_response("```cpp\n" + LLM_BFS + "```")) as post:
            call_command("llmdetect_generator", "--once", stdout=StringIO())
        job.refresh_from_db()
        self.assertEqual((job.status, job.produced), (GenerationJob.DONE, 2))
        (url,) = post.call_args.args
        self.assertEqual(url, "https://example.com/v1/chat/completions")
        body = post.call_args.kwargs["json"]
        self.assertEqual(body["model"], "small:free")
        # Only the instruction and the statement are sent.
        self.assertEqual(body["messages"], [{"role": "user", "content": build_prompt("plain", "Add two numbers.")}])
        self.assertEqual(post.call_args.kwargs["headers"]["Authorization"], "Bearer k")
        references = LLMReferenceSolution.objects.filter(problem_instance=self.pis[0])
        self.assertEqual(references.count(), 2)
        self.assertEqual(references.first().source, LLM_BFS)
        self.assertEqual(references.first().model_name, "small:free")
        self.assertTrue(references.first().fingerprints)
        self.assertTrue(LLMDetectConfig.objects.get(contest=self.contest).dirty)

    def test_rate_limit_is_retried_and_errors_fail_the_job(self):
        job = self._job(count=1)
        limited = MagicMock(status_code=429, text="slow down", headers={"retry-after": "0"})
        with patch("oioioi.llmdetect.generation.requests.post", side_effect=[limited, _chat_response("int main(){}")]):
            call_command("llmdetect_generator", "--once", stdout=StringIO())
        job.refresh_from_db()
        self.assertEqual(job.status, GenerationJob.DONE)
        failing = self._job(count=1)
        with patch("oioioi.llmdetect.generation.requests.post", return_value=MagicMock(status_code=401, text="bad key", headers={})):
            call_command("llmdetect_generator", "--once", stdout=StringIO())
        failing.refresh_from_db()
        self.assertEqual(failing.status, GenerationJob.FAILED)
        self.assertIn("HTTP 401", failing.error)

    def test_claude_job_uses_the_sdk_with_fallback(self):
        job = self._job(provider="claude", model="claude-opus-5-5", count=1)
        message = MagicMock(stop_reason="end_turn", content=[MagicMock(type="text", text="```cpp\nint main(){}\n```")])
        with patch("anthropic.Anthropic") as client_class:
            client_class.return_value.beta.messages.create.return_value = message
            call_command("llmdetect_generator", "--once", stdout=StringIO())
        job.refresh_from_db()
        self.assertEqual(job.status, GenerationJob.DONE, job.error)
        kwargs = client_class.return_value.beta.messages.create.call_args.kwargs
        self.assertEqual((kwargs["model"], kwargs["fallbacks"], kwargs["betas"]), ("claude-opus-5-5", "default", ["server-side-fallback-2026-07-01"]))
        self.assertEqual(LLMReferenceSolution.objects.get().source, "int main(){}\n")

    def test_claude_refusal_fails_the_job(self):
        job = self._job(provider="claude", model="claude-opus-5-5", count=1)
        with patch("anthropic.Anthropic") as client_class:
            client_class.return_value.beta.messages.create.return_value = MagicMock(stop_reason="refusal", content=[])
            call_command("llmdetect_generator", "--once", stdout=StringIO())
        job.refresh_from_db()
        self.assertEqual(job.status, GenerationJob.FAILED)
        self.assertIn("declined", job.error)

    def test_jobs_left_running_are_resumed(self):
        job = self._job(count=2, status=GenerationJob.RUNNING, produced=1)
        with patch("oioioi.llmdetect.generation.requests.post", return_value=_chat_response("int main(){}")) as post:
            call_command("llmdetect_generator", "--once", stdout=StringIO())
        job.refresh_from_db()
        self.assertEqual((job.status, job.produced, post.call_count), (GenerationJob.DONE, 2, 1))

    @override_settings(LLMDETECT_PROVIDERS={})
    def test_no_providers(self):
        self.assertTrue(self.client.login(username="test_admin"))
        self.assertContains(self.client.get(self._url()), "No models are configured")
