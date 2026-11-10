from datetime import timedelta

from django.contrib.auth.models import User
from django.core.management import call_command
from django.urls import reverse
from django.utils import timezone

from oioioi.base.tests import TestCase
from oioioi.contests.models import Contest, ProblemInstance, Round, Submission
from oioioi.participants.models import Participant
from oioioi.problembase.fingerprint import compute_fingerprint, normalize_source, source_hash
from oioioi.problembase.models import (
    NotSameProblem,
    ProblemDescription,
    ProblemFamily,
    ProblemFamilyMember,
    ProblemFingerprint,
    ensure_families,
    merge_families,
    unlink_problem,
)
from oioioi.problembase.seen import PARTICIPANT, SUBMITTED, build_ban, seen_by
from oioioi.problembase.similarity import find_clusters
from oioioi.problems.models import Problem

SOURCE = """// author: someone, %s
#include <cstdio>
long long gcd(long long a, long long b) {
    return b == 0 ? a : gcd(b, a %% b);
}
int main() {
    long long a, b;
    scanf("%%lld %%lld", &a, &b);
    printf("%%lld\\n", a + b);
    return 0;
}
"""
STATEMENT = (
    "Bajtazar ma ciąg n liczb całkowitych a_1, a_2, ..., a_n. Jego przyjaciel Bitek zadaje mu q pytań. "
    "Każde pytanie jest postaci l r i oznacza prośbę o podanie sumy elementów ciągu o indeksach od l do r włącznie. "
    "Pomóż Bajtazarowi i napisz program, który szybko odpowie na wszystkie pytania. "
    "W pierwszym wierszu wejścia znajdują się dwie liczby n oraz q, a w drugim wierszu kolejne wyrazy ciągu. "
    "W kolejnych q wierszach znajdują się opisy pytań. Wyjście powinno zawierać q wierszy z odpowiedziami."
)


def family_of(problem):
    return ProblemFamilyMember.objects.get(problem=problem).family


class ProblemBaseTestCase(TestCase):
    fixtures = ["test_users"]

    def _problem(self, short_name, name=None):
        return Problem.objects.create(legacy_name=name or short_name, short_name=short_name, controller_name="oioioi.problems.controllers.ProblemController")

    def _fingerprint(self, problem, solution_hash=None, statement=""):
        hashes = [{"name": f"{problem.short_name}.cpp", "hash": solution_hash}] if solution_hash else []
        return ProblemFingerprint.objects.create(problem=problem, solution_hashes=hashes, statement_text=statement)


class TestFamilies(ProblemBaseTestCase):
    def test_every_problem_gets_a_family(self):
        a, b = self._problem("abc"), self._problem("xyz")
        ensure_families()
        self.assertNotEqual(family_of(a), family_of(b))
        ensure_families()
        self.assertEqual(ProblemFamily.objects.count(), 2)

    def test_merge_keeps_description(self):
        a, b, c = self._problem("abc"), self._problem("abd"), self._problem("abe")
        ensure_families()
        ProblemDescription.objects.create(family=family_of(c), statement="Sum $a+b$.")
        target = merge_families([family_of(a).id, family_of(b).id, family_of(c).id])
        self.assertEqual(ProblemFamily.objects.count(), 1)
        self.assertEqual(target.description.statement, "Sum $a+b$.")
        self.assertEqual(set(target.members.values_list("problem_id", flat=True)), {a.id, b.id, c.id})

    def test_unlink(self):
        a, b = self._problem("abc"), self._problem("abd")
        ensure_families()
        merge_families([family_of(a).id, family_of(b).id])
        unlink_problem(b)
        self.assertNotEqual(family_of(a), family_of(b))
        self.assertTrue(NotSameProblem.objects.filter(problem_a=a, problem_b=b).exists())

    def test_empty_families_are_removed(self):
        a = self._problem("abc")
        ensure_families()
        a.delete()
        ensure_families()
        self.assertFalse(ProblemFamily.objects.exists())


class TestFingerprint(TestCase):
    def test_comments_and_whitespace_ignored(self):
        self.assertIsNotNone(source_hash((SOURCE % "x").encode(), "a.cpp"))
        self.assertEqual(source_hash((SOURCE % "2019").encode(), "a.cpp"), source_hash((SOURCE % "2024").encode().replace(b"    ", b"\t"), "b.cpp"))
        self.assertNotEqual(source_hash((SOURCE % "x").encode(), "a.cpp"), source_hash((SOURCE % "x").replace("a + b", "a - b").encode(), "a.cpp"))

    def test_short_sources_are_not_hashed(self):
        self.assertIsNone(source_hash(b"int main(){}", "a.cpp"))

    def test_pascal_and_python(self):
        self.assertEqual(normalize_source("begin { hi } writeln(1); (* x *) end.", "a.pas"), "beginwriteln(1);end.")
        self.assertEqual(normalize_source("print(1)  # hi\n", "a.py"), "print(1)")


class TestUploadedPackage(TestCase):
    def test_fingerprint_of_uploaded_package(self):
        from oioioi.sinolpack.tests import get_test_filename

        call_command("addproblem", get_test_filename("test_simple_package.zip"))
        call_command("addproblem", get_test_filename("test_simple_package.zip"))
        first, second = Problem.objects.order_by("id")
        for problem in (first, second):
            compute_fingerprint(problem)
        self.assertEqual(first.fingerprint.errors, "")
        self.assertEqual([entry["name"] for entry in first.fingerprint.solution_hashes], ["tst.cpp"])
        self.assertIn("bajtocja", first.fingerprint.statement_text.lower())
        ensure_families()
        (cluster,) = find_clusters()
        self.assertEqual(set(cluster.family_ids), {family_of(first).id, family_of(second).id})
        self.assertTrue(cluster.certain)


class TestSuggestions(ProblemBaseTestCase):
    def setUp(self):
        super().setUp()
        self.a, self.b = self._problem("aaa", "Pierwsze"), self._problem("bbb", "Drugie")
        ensure_families()

    def test_identical_solution_is_certain(self):
        self._fingerprint(self.a, "h1")
        self._fingerprint(self.b, "h1")
        (cluster,) = find_clusters()
        self.assertTrue(cluster.certain)

    def test_similar_statement(self):
        self._fingerprint(self.a, statement=STATEMENT + " Limit n wynosi milion.")
        self._fingerprint(self.b, statement=STATEMENT + " Limit n wynosi tysiąc.")
        (cluster,) = find_clusters()
        self.assertFalse(cluster.certain)
        (evidence,) = cluster.edges.values()
        self.assertGreater(evidence.statement, 0.5)

    def test_same_name(self):
        c = self._problem("aaa", "Pierwsze")
        ensure_families()
        (cluster,) = find_clusters()
        self.assertEqual(set(cluster.family_ids), {family_of(self.a).id, family_of(c).id})

    def test_different_problems_not_suggested(self):
        self._fingerprint(self.a, "h1", STATEMENT)
        self._fingerprint(self.b, "h2", "Zupełnie inna treść o grafach i drzewach. " * 10)
        self.assertEqual(find_clusters(), [])

    def test_merged_and_dismissed_not_suggested(self):
        self._fingerprint(self.a, "h1")
        self._fingerprint(self.b, "h1")
        NotSameProblem.objects.create(problem_a=self.a, problem_b=self.b)
        self.assertEqual(find_clusters(), [])
        NotSameProblem.objects.all().delete()
        merge_families([family_of(self.a).id, family_of(self.b).id])
        self.assertEqual(find_clusters(), [])


class TestSeen(ProblemBaseTestCase):
    def setUp(self):
        super().setUp()
        now = timezone.now()
        self.contest = Contest.objects.create(id="c1", name="Camp", controller_name="oioioi.participants.tests.ParticipantsContestController")
        self.past = Round.objects.create(contest=self.contest, name="R1", start_date=now - timedelta(days=1))
        self.future = Round.objects.create(contest=self.contest, name="R2", start_date=now + timedelta(days=1))
        self.old, self.new, self.other = self._problem("old"), self._problem("new"), self._problem("oth")
        self.pi_old = ProblemInstance.objects.create(problem=self.old, contest=self.contest, round=self.past, short_name="old")
        ProblemInstance.objects.create(problem=self.new, contest=self.contest, round=self.future, short_name="new")
        self.user = User.objects.get(username="test_user")
        self.outsider = User.objects.get(username="test_user2")
        Participant.objects.create(contest=self.contest, user=self.user)
        ensure_families()

    def test_participant_of_started_round(self):
        seen = seen_by([self.user.id, self.outsider.id])
        self.assertEqual(seen[family_of(self.old).id], {self.user.id: {"c1": PARTICIPANT}})
        self.assertNotIn(family_of(self.new).id, seen)

    def test_submission(self):
        Submission.objects.create(user=self.outsider, problem_instance=self.pi_old)
        seen = seen_by([self.outsider.id])
        self.assertEqual(seen[family_of(self.old).id], {self.outsider.id: {"c1": SUBMITTED}})

    def test_seen_through_other_package(self):
        merge_families([family_of(self.old).id, family_of(self.other).id])
        self.assertIn(family_of(self.other).id, seen_by([self.user.id]))

    def test_build_ban(self):
        ban = build_ban([self.contest], "test_user2 nosuchuser")
        self.assertEqual({u.username for u in ban.users}, {"test_user", "test_user2"})
        self.assertEqual(ban.unknown_logins, ["nosuchuser"])


class TestViews(ProblemBaseTestCase):
    def setUp(self):
        super().setUp()
        self.a, self.b, self.c = self._problem("aaa", "Pierwsze"), self._problem("bbb", "Drugie"), self._problem("ccc", "Trzecie")
        ensure_families()
        self.assertTrue(self.client.login(username="test_admin"))

    def test_superuser_only(self):
        self.assertTrue(self.client.login(username="test_user"))
        self.assertEqual(self.client.get(reverse("problembase_list")).status_code, 403)

    def test_list_and_family(self):
        ProblemDescription.objects.create(family=family_of(self.a), statement="Policz $\\sum a_i$.", rating=1500, rating_reason="Proste sumy.")
        response = self.client.get(reverse("problembase_list"))
        self.assertContains(response, "Pierwsze")
        self.assertContains(response, "Proste sumy.")
        self.assertContains(response, "1500")
        response = self.client.get(reverse("problembase_list"), {"q": "bbb"})
        self.assertContains(response, "Drugie")
        self.assertNotContains(response, "Pierwsze")
        response = self.client.get(reverse("problembase_family", kwargs={"family_id": family_of(self.a).id}))
        self.assertContains(response, "Policz $\\sum a_i$.")

    def test_merge_and_unlink(self):
        ids = [family_of(self.a).id, family_of(self.b).id]
        response = self.client.post(reverse("problembase_merge"), {"family": ids})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(family_of(self.a), family_of(self.b))
        self.client.post(reverse("problembase_unlink", kwargs={"problem_id": self.b.id}))
        self.assertNotEqual(family_of(self.a), family_of(self.b))

    def test_resolve_merge_leaves_others(self):
        d = self._problem("ddd", "Czwarte")
        ensure_families()
        fa, fb, fc, fd = (family_of(p).id for p in (self.a, self.b, self.c, d))
        self.client.post(reverse("problembase_resolve"), {"family": [fa, fb], "action": "merge"})
        self.client.post(reverse("problembase_resolve"), {"family": [fc, fd], "action": "merge"})
        self.assertEqual(family_of(self.a), family_of(self.b))
        self.assertEqual(family_of(self.c), family_of(d))
        self.assertNotEqual(family_of(self.a), family_of(self.c))
        self.assertFalse(NotSameProblem.objects.exists())

    def test_resolve_different(self):
        fa, fb, fc = family_of(self.a).id, family_of(self.b).id, family_of(self.c).id
        self.client.post(reverse("problembase_resolve"), {"family": [fa, fb], "action": "different"})
        self.assertEqual(ProblemFamily.objects.count(), 3)
        self.assertEqual(list(NotSameProblem.objects.values_list("problem_a", "problem_b")), [(self.a.id, self.b.id)])
        self.assertNotIn(fc, {f for pair in NotSameProblem.objects.values_list("problem_a", "problem_b") for f in pair})

    def test_resolve_needs_two(self):
        self.client.post(reverse("problembase_resolve"), {"family": [family_of(self.a).id], "action": "merge"})
        self.assertEqual(ProblemFamily.objects.count(), 3)

    def test_merge_certain(self):
        self._fingerprint(self.a, "h1")
        self._fingerprint(self.b, "h1")
        self._fingerprint(self.c, statement=STATEMENT)
        self.assertContains(self.client.get(reverse("problembase_suggestions")), "Pierwsze")
        self.client.post(reverse("problembase_merge_certain"))
        self.assertEqual(family_of(self.a), family_of(self.b))
        self.assertEqual(ProblemFamily.objects.count(), 2)

    def test_edit_description(self):
        url = reverse("problembase_family", kwargs={"family_id": family_of(self.a).id})
        response = self.client.post(url, {"statement": "Krótko.", "solution": "", "rating": "1800", "rating_reason": ""})
        self.assertEqual(response.status_code, 302)
        description = ProblemDescription.objects.get(family=family_of(self.a))
        self.assertEqual((description.statement, description.rating, description.source), ("Krótko.", 1800, "manual"))

    def test_ban_hides_seen(self):
        contest = Contest.objects.create(id="c1", name="Camp", controller_name="oioioi.participants.tests.ParticipantsContestController")
        past = Round.objects.create(contest=contest, name="R1", start_date=timezone.now() - timedelta(days=1))
        ProblemInstance.objects.create(problem=self.a, contest=contest, round=past, short_name="aaa")
        Participant.objects.create(contest=contest, user=User.objects.get(username="test_user"))
        params = {"ban": "1", "ban_contest": "c1"}
        response = self.client.get(reverse("problembase_list"), params, follow=True)
        self.assertContains(response, "Pierwsze")
        response = self.client.get(reverse("problembase_list"), {**params, "hide_seen": "1"}, follow=True)
        self.assertNotContains(response, "Pierwsze")
        self.assertContains(response, "Drugie")


class TestSolutionStatus(ProblemBaseTestCase):
    def test_solution_status_of_suggestion(self):
        a, b, c = self._problem("aaa", "Pierwsze"), self._problem("aaa", "Pierwsze"), self._problem("aaa", "Pierwsze")
        ensure_families()
        self._fingerprint(a, "h1")
        self._fingerprint(b, "h2")
        (cluster,) = find_clusters()
        statuses = {pair: e.solution for pair, e in cluster.edges.items()}
        fa, fb, fc = family_of(a).id, family_of(b).id, family_of(c).id
        self.assertEqual(statuses[fa, fb], "different")
        self.assertEqual(statuses[fa, fc], "unknown")
        self.assertTrue(self.client.login(username="test_admin"))
        response = self.client.get(reverse("problembase_suggestions"))
        self.assertContains(response, "different model solution")
        self.assertContains(response, "aaa.cpp")


class TestNavbarAndLinks(ProblemBaseTestCase):
    def test_navbar_link_for_superusers_only(self):
        url = reverse("problembase_list")
        self.assertTrue(self.client.login(username="test_admin"))
        self.assertContains(self.client.get(reverse("problemset_main")), f'href="{url}"')
        self.assertTrue(self.client.login(username="test_user"))
        self.assertNotContains(self.client.get(reverse("problemset_main")), f'href="{url}"')

    def test_package_links_to_problem_page(self):
        from oioioi.problems.models import ProblemSite

        problem = self._problem("aaa", "Pierwsze")
        ProblemSite.objects.create(problem=problem, url_key="abcdef")
        ensure_families()
        self.assertTrue(self.client.login(username="test_admin"))
        response = self.client.get(reverse("problembase_family", kwargs={"family_id": family_of(problem).id}))
        self.assertContains(response, reverse("problem_site", kwargs={"site_key": "abcdef"}))


class TestRejection(ProblemBaseTestCase):
    def setUp(self):
        super().setUp()
        from oioioi.problembase.rejection import auto_reject, rejected_problem_ids

        self.auto_reject, self.rejected = auto_reject, rejected_problem_ids
        self.contest = Contest.objects.create(id="c1", name="Camp", controller_name="oioioi.participants.tests.ParticipantsContestController")
        self.used = self._problem("aaa", "Pierwsze")
        ProblemInstance.objects.create(problem=self.used, contest=self.contest, short_name="aaa")
        self.copy = self._problem("aaa", "Pierwsze")
        self.unique = self._problem("zzz", "Jedyne")
        ensure_families()

    def test_unused_copy_is_rejected(self):
        self.assertEqual(self.auto_reject(), 1)
        self.assertEqual(self.rejected(), {self.copy.id})
        self.assertEqual(find_clusters(), [])

    def test_copy_in_the_same_family_is_rejected(self):
        other = self._problem("bbb", "Inna nazwa")
        ensure_families()
        merge_families([family_of(self.used).id, family_of(other).id])
        self.auto_reject()
        self.assertIn(other.id, self.rejected())

    def test_reasons_to_keep(self):
        submitted, public = self._problem("aaa", "Pierwsze"), self._problem("aaa", "Pierwsze")
        main = ProblemInstance.objects.create(problem=submitted, short_name="aaa")
        Submission.objects.create(user=User.objects.get(username="test_user"), problem_instance=main)
        public.visibility = Problem.VISIBILITY_PUBLIC
        public.save()
        ensure_families()
        self.auto_reject()
        self.assertEqual(self.rejected(), {self.copy.id})

    def test_model_solution_submissions_do_not_keep(self):
        main = ProblemInstance.objects.create(problem=self.copy, short_name="aaa")
        Submission.objects.create(user=None, problem_instance=main)
        self.auto_reject()
        self.assertEqual(self.rejected(), {self.copy.id})

    def test_restored_is_never_rejected_again(self):
        self.auto_reject()
        self.assertTrue(self.client.login(username="test_admin"))
        self.client.post(reverse("problembase_restore", kwargs={"contest_id": "c1", "problem_id": self.copy.id}))
        self.auto_reject()
        self.assertEqual(self.rejected(), set())

    def test_rejection_ends_when_used(self):
        self.auto_reject()
        ProblemInstance.objects.create(problem=self.copy, contest=self.contest, short_name="aab")
        self.assertEqual(self.rejected(), set())

    def test_manual_reject_and_list(self):
        self.assertTrue(self.client.login(username="test_admin"))
        self.client.post(reverse("problembase_reject", kwargs={"contest_id": "c1", "problem_id": self.unique.id}))
        self.assertEqual(self.rejected(), {self.unique.id})
        response = self.client.get(reverse("problembase_list"), follow=True)
        self.assertNotContains(response, "Jedyne")
        self.assertContains(response, "Show 1 rejected problem")
        response = self.client.get(reverse("problembase_list"), {"show_rejected": "1"}, follow=True)
        self.assertContains(response, "Jedyne")

    def test_reject_from_suggestions(self):
        self.assertTrue(self.client.login(username="test_admin"))
        response = self.client.get(reverse("problembase_suggestions"), follow=True)
        self.assertContains(response, "1 rejected package is left out.")
        third = self._problem("zzz", "Jedyne")
        ensure_families()
        self.client.post(reverse("problembase_reject_family", kwargs={"contest_id": "c1", "family_id": family_of(third).id}))
        self.assertIn(third.id, self.rejected())
        self.assertNotIn(self.unique.id, self.rejected())


class TestContestOrder(ProblemBaseTestCase):
    def test_newest_contest_first(self):
        problem = self._problem("aaa", "Pierwsze")
        now = timezone.now()
        for i, name in enumerate(["Najstarszy", "Sredni", "Najnowszy", "Czwarty"]):
            contest = Contest.objects.create(id=f"c{i}", name=name, controller_name="oioioi.participants.tests.ParticipantsContestController")
            rnd = Round.objects.create(contest=contest, name="R", start_date=now - timedelta(days=100 - i * 10 if name != "Czwarty" else 200))
            ProblemInstance.objects.create(problem=problem, contest=contest, round=rnd, short_name="aaa")
        ensure_families()
        from oioioi.problembase.views import _family_rows

        (row,) = _family_rows([family_of(problem).id])
        self.assertEqual([use.contest.name for use in row.contests], ["Najnowszy", "Sredni", "Najstarszy", "Czwarty"])


class TestExport(TestCase):
    fixtures = ["test_users"]

    def test_export_uploaded_package(self):
        import json
        import tempfile
        import zipfile

        from oioioi.sinolpack.tests import get_test_filename

        call_command("addproblem", get_test_filename("test_simple_package.zip"))
        problem = Problem.objects.get()
        contest = Contest.objects.create(id="c1", name="Camp 2024", controller_name="oioioi.participants.tests.ParticipantsContestController")
        rnd = Round.objects.create(contest=contest, name="Dzień 1", start_date=timezone.now() - timedelta(days=3))
        ProblemInstance.objects.create(problem=problem, contest=contest, round=rnd, short_name="tst")
        Participant.objects.create(contest=contest, user=User.objects.get(username="test_user"))
        with tempfile.NamedTemporaryFile(suffix=".zip") as out:
            call_command("problembase_export", out.name)
            with zipfile.ZipFile(out.name) as archive:
                (record,) = [json.loads(line) for line in archive.read("families.jsonl").decode().splitlines()]
                meta = json.loads(archive.read("meta.json"))
        self.assertEqual(meta["families"], 1)
        self.assertEqual(record["problem_id"], problem.id)
        self.assertIn("\\documentclass", record["statement_tex"])
        self.assertEqual([s["name"] for s in record["solutions"]], ["tst.cpp"])
        self.assertIn("main", record["solutions"][0]["source"])
        self.assertEqual(record["contests"][0]["contest"], "Camp 2024")
        self.assertEqual(record["contests"][0]["participants"], 1)
        self.assertGreater(record["limits"]["tests"], 0)
        self.assertEqual(record["errors"], [])

    def test_solution_order(self):
        from oioioi.problembase.export import _solution_key

        class S:
            def __init__(self, name):
                self.name = name

        names = ["abc2.cpp", "abcs1.cpp", "abc.py", "abc.cpp", "abc1.cpp", "abc10.cpp"]
        ordered = [s.name for s in sorted(map(S, names), key=lambda s: _solution_key(s, "abc"))]
        self.assertEqual(ordered[:4], ["abc.cpp", "abc1.cpp", "abc2.cpp", "abc10.cpp"])


class TestTaxonomy(TestCase):
    def test_consistent(self):
        from oioioi.problembase import taxonomy

        self.assertEqual(len(taxonomy.TAG_SLUGS), len(set(taxonomy.TAG_SLUGS)))
        for slug, parent in taxonomy.TAG_PARENT.items():
            self.assertIn(parent, taxonomy.TAG_NAMES, slug)
        self.assertEqual(set(taxonomy.TAG_CATEGORY.values()) - set(taxonomy.CATEGORY_NAMES), set())
        self.assertEqual(taxonomy.expand(["rerooting"]), {"rerooting", "dp-trees", "dp"})


def load_describe_tool():
    import importlib.util
    from pathlib import Path

    import oioioi.problembase

    path = Path(oioioi.problembase.__file__).parent / "tools" / "describe.py"
    spec = importlib.util.spec_from_file_location("describe_tool", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def export_record(family_id, contests=(), **extra):
    record = {
        "family_id": family_id,
        "problem_id": family_id,
        "problem_ids": [family_id],
        "name": f"Zadanie {family_id}",
        "short_name": f"z{family_id}",
        "statement_tex": "\\documentclass{sinol}\\begin{document}Policz $n$ % komentarz\n\\end{document}",
        "statement_text": "",
        "editorial": "",
        "config_yml": "scores: {1: 50, 2: 50}",
        "solutions": [{"name": f"z{family_id}.cpp", "source": "int main() {}"}],
        "limits": {"time_ms": 1000, "memory_kb": 262144, "tests": 10},
        "contests": list(contests),
        "errors": [],
    }
    record.update(extra)
    return record


def use(contest, submitted=0, full=None):
    return {
        "contest": contest,
        "contest_id": "x",
        "round": "Dzień 1",
        "date": "2024-07-01",
        "participants": 20,
        "submitted": submitted,
        "full_score": full,
        "average_percent": None,
    }


class TestDescribeTool(TestCase):
    def setUp(self):
        self.tool = load_describe_tool()

    def test_normalize(self):
        data = self.tool.normalize(
            {
                "statement": "Ciąg \tilde",
                "solution": "DP",
                "main_tags": ["dp", "dp-trees", "nonexistent"],
                "side_tags": ["sorting", "dp"],
                "objects": ["tree", "tree", "bogus"],
                "rating": 1840,
                "rating_reason": "Stan DP.",
            }
        )
        self.assertEqual(data["main_tags"], ["dp-trees"])
        self.assertEqual(data["side_tags"], ["sorting"])
        self.assertEqual(data["objects"], ["tree"])
        self.assertEqual(data["rating"], 1800)
        self.assertEqual(data["statement"], "Ciąg \\tilde")

    def test_normalize_rejects_answers_without_tags(self):
        answer = {"statement": "a", "solution": "b", "main_tags": ["bogus"], "side_tags": [], "objects": [], "rating": 1000, "rating_reason": "c"}
        with self.assertRaises(ValueError):
            self.tool.normalize(answer)

    def test_strata_and_sample(self):
        records = [
            export_record(1, [use("Gródek Ekstraklasa")]),
            export_record(2, [use("Camp", submitted=20, full=2)]),
            export_record(3, [use("Grupa zaawansowana")]),
            export_record(4, [use("Grupa podstawowa", submitted=20, full=18)]),
            export_record(5),
        ]
        self.assertEqual([self.tool.stratum(r) for r in records], ["hard", "hard", "medium", "easy", "unknown"])
        chosen = self.tool.sample(records, 4, seed=1)
        self.assertEqual({self.tool.stratum(r) for r in chosen}, {"easy", "medium", "hard", "unknown"})
        self.assertEqual([r["family_id"] for r in chosen], [r["family_id"] for r in self.tool.sample(records, 4, seed=1)])

    def test_prompt(self):
        prompt = self.tool.build_prompt(export_record(7, [use("Gródek Mistrzowska", submitted=10, full=3)]))
        self.assertIn("Policz $n$", prompt)
        self.assertNotIn("documentclass", prompt)
        self.assertNotIn("komentarz", prompt)
        self.assertIn("z7.cpp", prompt)
        self.assertIn("na 100% 3", prompt)
        self.assertIn("rerooting: DP po wszystkich korzeniach", prompt)


class TestImport(ProblemBaseTestCase):
    def _write(self, records):
        import json
        import tempfile

        f = tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False, encoding="utf-8")
        for record in records:
            f.write(json.dumps(record) + "\n")
        f.close()
        return f.name

    def _result(self, problem, rating=1500):
        analysis = {
            "statement": "Policz $n$.",
            "solution": "Wzór.",
            "main_tags": ["combinatorics"],
            "side_tags": ["modular-arithmetic"],
            "objects": ["numbers"],
            "rating": rating,
            "rating_reason": "Prosty wzór.",
        }
        return {"family_id": 0, "problem_id": problem.id, "short_name": problem.short_name, "model": "gemini-x", "analysis": analysis}

    def test_import(self):
        a, b = self._problem("aaa"), self._problem("bbb")
        ensure_families()
        ProblemDescription.objects.create(family=family_of(b), statement="Ręczny opis.")
        path = self._write([self._result(a, 1400), self._result(a, 1600), self._result(b)])
        call_command("problembase_import", path)
        description = ProblemDescription.objects.get(family=family_of(a))
        self.assertEqual((description.rating, description.source, description.model_name), (1600, "llm", "gemini-x"))
        self.assertEqual(description.main_tag_names, ["kombinatoryka"])
        self.assertEqual(description.object_names, ["liczby"])
        self.assertEqual(ProblemDescription.objects.get(family=family_of(b)).statement, "Ręczny opis.")
        self.assertTrue(self.client.login(username="test_admin"))
        self.assertContains(self.client.get(reverse("problembase_list")), "kombinatoryka")
