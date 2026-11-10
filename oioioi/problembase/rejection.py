"""Rejecting packages that only get in the way: unused duplicates of a used package.

A package is rejected automatically when it is not in any contest, nobody but
the model solutions submitted to it, it is not public in the problemset, and
it has a "living twin": a kept package in the same family or connected to it
by a merge suggestion. A unique package (e.g. a task being prepared) is kept.
"""

from oioioi.contests.models import ProblemInstance, Submission
from oioioi.problembase.models import NotSameProblem, ProblemFamilyMember
from oioioi.problems.models import Problem

REJECTED = ("rejected", "auto")


def used_problem_ids(problem_ids=None):
    """Problems that must be kept: in a contest, with user submissions, or public."""
    instances = ProblemInstance.objects.filter(contest__isnull=False)
    submissions = Submission.objects.filter(user__isnull=False)
    public = Problem.objects.filter(visibility=Problem.VISIBILITY_PUBLIC)
    if problem_ids is not None:
        instances = instances.filter(problem_id__in=problem_ids)
        submissions = submissions.filter(problem_instance__problem_id__in=problem_ids)
        public = public.filter(id__in=problem_ids)
    used = set(instances.values_list("problem_id", flat=True))
    used |= set(submissions.values_list("problem_instance__problem_id", flat=True).distinct())
    used |= set(public.values_list("id", flat=True))
    return used


def rejected_problem_ids():
    statuses = dict(ProblemFamilyMember.objects.filter(status__in=REJECTED).values_list("problem_id", "status"))
    auto = {p for p, status in statuses.items() if status == "auto"}
    # An automatic rejection stops counting once the package gets used.
    auto -= used_problem_ids(auto) if auto else set()
    return {p for p, status in statuses.items() if status == "rejected"} | auto


def auto_reject():
    """Recompute automatic rejections; returns the number of rejected packages."""
    from oioioi.problembase.similarity import problem_pairs

    members = {p: (family, status) for p, family, status in ProblemFamilyMember.objects.values_list("problem_id", "family_id", "status")}
    keep = used_problem_ids() | {p for p, (_family, status) in members.items() if status == "kept"}
    keep &= members.keys()
    families_with_keeper = {members[p][0] for p in keep}
    dismissed = set(NotSameProblem.objects.values_list("problem_a_id", "problem_b_id"))

    twins = set()
    for a, b, _kind, _value in problem_pairs():
        if (a, b) in dismissed:
            continue
        if a in keep and b not in keep:
            twins.add(b)
        elif b in keep and a not in keep:
            twins.add(a)

    rejected = {p for p, (family, status) in members.items() if p not in keep and status != "rejected" and (family in families_with_keeper or p in twins)}
    currently = {p for p, (_family, status) in members.items() if status == "auto"}
    ProblemFamilyMember.objects.filter(problem_id__in=rejected - currently, status="").update(status="auto")
    ProblemFamilyMember.objects.filter(problem_id__in=currently - rejected, status="auto").update(status="")
    return len(rejected)
