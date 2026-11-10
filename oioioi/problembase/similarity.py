"""Suggestions of problems that are probably the same task (re-uploaded packages)."""

import re
import unicodedata
import zlib
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from itertools import combinations

from oioioi.problembase.models import NotSameProblem, ProblemFamilyMember, ProblemFingerprint
from oioioi.problems.models import Problem
from oioioi.programs.models import Test

SHINGLE_WORDS = 5
MIN_SHINGLES = 30
# Shingles shared by many statements are boilerplate (headers, input/output format phrases).
MAX_SHINGLE_FREQUENCY = 25
STATEMENT_THRESHOLD = 0.5


@dataclass
class Evidence:
    same_solution: bool = False
    statement: float = 0.0  # Jaccard similarity of statement shingles
    same_name: bool = False
    tests: float | None = None  # Jaccard similarity of test names

    @property
    def certain(self):
        return self.same_solution


@dataclass
class Cluster:
    family_ids: list
    edges: dict = field(default_factory=dict)  # (family_a, family_b) -> Evidence

    @property
    def certain(self):
        """Every family is connected to the others through identical model solutions."""
        return _connected(self.family_ids, [pair for pair, evidence in self.edges.items() if evidence.certain])

    @property
    def strength(self):
        return max((2.0 if e.same_solution else e.statement) for e in self.edges.values())


def _normalize_words(text):
    text = unicodedata.normalize("NFKD", text.lower())
    text = "".join(c for c in text if not unicodedata.combining(c))
    return re.findall(r"\w+", text)


def statement_shingles(text):
    words = _normalize_words(text)
    return {zlib.crc32(" ".join(words[i : i + SHINGLE_WORDS]).encode()) for i in range(len(words) - SHINGLE_WORDS + 1)}


def _normalize_name(name):
    return " ".join(_normalize_words(name))


def _jaccard(a, b):
    return len(a & b) / len(a | b) if a and b else 0.0


def _connected(nodes, pairs):
    parent = {n: n for n in nodes}

    def find(n):
        while parent[n] != n:
            parent[n] = parent[parent[n]]
            n = parent[n]
        return n

    for a, b in pairs:
        parent[find(a)] = find(b)
    return len({find(n) for n in nodes}) == 1


def _test_names(problem_ids):
    names = defaultdict(set)
    rows = Test.objects.filter(problem_instance__problem_id__in=problem_ids, problem_instance__contest__isnull=True).values_list(
        "problem_instance__problem_id", "name"
    )
    for problem_id, name in rows:
        names[problem_id].add(name)
    return names


def _problem_pairs():
    """Yield (problem_a, problem_b, kind, value) for every signal between two problems."""
    fingerprints = list(ProblemFingerprint.objects.values_list("problem_id", "solution_hashes", "statement_text"))

    by_hash = defaultdict(set)
    for problem_id, hashes, _text in fingerprints:
        for entry in hashes:
            by_hash[entry["hash"]].add(problem_id)
    for problems in by_hash.values():
        for a, b in combinations(sorted(problems), 2):
            yield a, b, "solution", True

    shingles = {}
    for problem_id, _hashes, text in fingerprints:
        s = statement_shingles(text)
        if len(s) >= MIN_SHINGLES:
            shingles[problem_id] = s
    index = defaultdict(list)
    for problem_id, s in shingles.items():
        for shingle in s:
            index[shingle].append(problem_id)
    shared = Counter()
    for problems in index.values():
        if 1 < len(problems) <= MAX_SHINGLE_FREQUENCY:
            for a, b in combinations(sorted(problems), 2):
                shared[a, b] += 1
    for (a, b), common in shared.items():
        similarity = common / (len(shingles[a]) + len(shingles[b]) - common)
        if similarity >= STATEMENT_THRESHOLD:
            yield a, b, "statement", similarity

    by_name = defaultdict(set)
    for problem in Problem.objects.prefetch_related("names"):
        by_name[problem.short_name, _normalize_name(problem.name)].add(problem.id)
    for problems in by_name.values():
        for a, b in combinations(sorted(problems), 2):
            yield a, b, "name", True


def find_clusters():
    """Groups of families that look like one task, strongest first."""
    family_of = dict(ProblemFamilyMember.objects.values_list("problem_id", "family_id"))
    dismissed = set(NotSameProblem.objects.values_list("problem_a_id", "problem_b_id"))

    edges = defaultdict(Evidence)
    name_pairs = set()
    for a, b, kind, value in _problem_pairs():
        if (a, b) in dismissed or a not in family_of or b not in family_of:
            continue
        fa, fb = family_of[a], family_of[b]
        if fa == fb:
            continue
        evidence = edges[min(fa, fb), max(fa, fb)]
        if kind == "solution":
            evidence.same_solution = True
        elif kind == "statement":
            evidence.statement = max(evidence.statement, value)
        else:
            evidence.same_name = True
            name_pairs.add((a, b, min(fa, fb), max(fa, fb)))

    if name_pairs:
        tests = _test_names({p for a, b, _fa, _fb in name_pairs for p in (a, b)})
        for a, b, fa, fb in name_pairs:
            if tests[a] and tests[b]:
                evidence = edges[fa, fb]
                evidence.tests = max(evidence.tests or 0.0, _jaccard(tests[a], tests[b]))

    parent = {}

    def find(n):
        parent.setdefault(n, n)
        while parent[n] != n:
            parent[n] = parent[parent[n]]
            n = parent[n]
        return n

    for fa, fb in edges:
        parent[find(fa)] = find(fb)
    groups = defaultdict(list)
    for family_id in parent:
        groups[find(family_id)].append(family_id)

    clusters = []
    for family_ids in groups.values():
        members = set(family_ids)
        cluster = Cluster(sorted(family_ids), {pair: e for pair, e in edges.items() if pair[0] in members})
        clusters.append(cluster)
    clusters.sort(key=lambda c: (not c.certain, -c.strength, c.family_ids[0]))
    return clusters
