"""Results of problems in contests and difficulty statistics of contests."""

import statistics
from collections import defaultdict
from dataclasses import dataclass, field

from django.db.models import Sum

from oioioi.contests.models import Contest, ProblemInstance, UserResultForProblem
from oioioi.contests.scores import IntegerScore
from oioioi.problembase.models import ProblemDescription, ProblemFamilyMember
from oioioi.programs.models import Test


def instance_results(instances):
    """pi_id -> {"submitted", "full_score", "average_percent"} from the users' results.

    Only integer scores count; full score means the sum of the active tests' points.
    """
    max_points = dict(
        Test.objects.filter(problem_instance__in=instances, is_active=True)
        .values_list("problem_instance_id")
        .annotate(total=Sum("max_score"))
        .values_list("problem_instance_id", "total")
    )
    scores = defaultdict(list)
    for pi_id, score in UserResultForProblem.objects.filter(problem_instance__in=instances).values_list("problem_instance_id", "score"):
        if isinstance(score, IntegerScore):
            scores[pi_id].append(score.value)
    results = {}
    for pi in instances:
        values, top = scores.get(pi.id, []), max_points.get(pi.id)
        results[pi.id] = {
            "submitted": len(values),
            "full_score": sum(1 for v in values if v >= top) if top else None,
            "average_percent": round(100 * sum(values) / (len(values) * top)) if values and top else None,
        }
    return results


@dataclass
class Summary:
    """Rating statistics of a set of problems, and how participants did on them."""

    ratings: list = field(default_factory=list)
    count: int = 0
    submitted: int = 0
    full_score: int = 0

    def add(self, rating, result):
        self.count += 1
        if rating is not None:
            self.ratings.append(rating)
        if result and result["full_score"] is not None:
            self.submitted += result["submitted"]
            self.full_score += result["full_score"]

    @property
    def mean(self):
        return round(statistics.mean(self.ratings)) if self.ratings else None

    @property
    def median(self):
        return round(statistics.median(self.ratings)) if self.ratings else None

    @property
    def low(self):
        return min(self.ratings) if self.ratings else None

    @property
    def high(self):
        return max(self.ratings) if self.ratings else None

    @property
    def full_percent(self):
        return round(100 * self.full_score / self.submitted) if self.submitted else None


@dataclass
class ProblemRow:
    instance: ProblemInstance
    family_id: int | None
    rating: int | None
    result: dict


@dataclass
class RoundRow:
    round: object
    problems: list = field(default_factory=list)
    summary: Summary = field(default_factory=Summary)


@dataclass
class ContestRow:
    contest: Contest
    rounds: list = field(default_factory=list)
    summary: Summary = field(default_factory=Summary)
    start: object = None
    end: object = None


def contest_rows():
    """All contests with problems, newest first, with rating statistics per contest and round."""
    instances = list(ProblemInstance.objects.filter(contest__isnull=False).select_related("contest", "round", "problem").prefetch_related("problem__names"))
    family_of = dict(ProblemFamilyMember.objects.values_list("problem_id", "family_id"))
    ratings = dict(ProblemDescription.objects.filter(rating__isnull=False).values_list("family_id", "rating"))
    results = instance_results(instances)

    contests, rounds = {}, {}
    for pi in instances:
        contest = contests.setdefault(pi.contest_id, ContestRow(pi.contest))
        round_row = rounds.get((pi.contest_id, pi.round_id))
        if round_row is None:
            round_row = rounds[pi.contest_id, pi.round_id] = RoundRow(pi.round)
            contest.rounds.append(round_row)
        family_id = family_of.get(pi.problem_id)
        rating = ratings.get(family_id)
        result = results.get(pi.id)
        round_row.problems.append(ProblemRow(pi, family_id, rating, result))
        round_row.summary.add(rating, result)
        contest.summary.add(rating, result)

    for contest in contests.values():
        dates = [r.round.start_date for r in contest.rounds if r.round is not None]
        contest.start = min(dates, default=contest.contest.creation_date)
        contest.end = max(dates, default=contest.contest.creation_date)
        contest.rounds.sort(key=lambda r: (r.round is None, r.round.start_date if r.round else 0, r.round.id if r.round else 0))
        for round_row in contest.rounds:
            round_row.problems.sort(key=lambda p: p.instance.short_name)
    return sorted(contests.values(), key=lambda c: c.end, reverse=True)
