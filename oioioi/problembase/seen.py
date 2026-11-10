"""Who has already seen a problem family.

A user has seen a family if they submitted to any of its problems in a contest,
or are a participant of a contest in which a round with one of its problems
has already started (trial rounds count too).
"""

import re
from collections import defaultdict
from dataclasses import dataclass

from django.contrib.auth.models import User
from django.utils import timezone

from oioioi.contests.models import ProblemInstance, Submission
from oioioi.participants.models import Participant
from oioioi.problembase.models import ProblemFamilyMember

SUBMITTED = "submitted"
PARTICIPANT = "participant"


@dataclass
class Ban:
    contests: list
    users: list  # User objects
    unknown_logins: list

    def __bool__(self):
        return bool(self.users)


def build_ban(contests, logins_text):
    logins = [login for login in re.split(r"[\s,;]+", logins_text or "") if login]
    found = {u.username: u for u in User.objects.filter(username__in=logins)}
    users = {u.id: u for u in found.values()}
    if contests:
        for participant in Participant.objects.filter(contest__in=contests).select_related("user"):
            users[participant.user_id] = participant.user
    ordered = sorted(users.values(), key=lambda u: (u.last_name.lower(), u.first_name.lower(), u.username))
    return Ban(list(contests), ordered, [login for login in logins if login not in found])


def seen_by(user_ids, family_ids=None):
    """family_id -> user_id -> {contest_id: SUBMITTED | PARTICIPANT}."""
    result = defaultdict(lambda: defaultdict(dict))
    if not user_ids:
        return result
    members = ProblemFamilyMember.objects.all()
    if family_ids is not None:
        members = members.filter(family_id__in=family_ids)
    family_of = dict(members.values_list("problem_id", "family_id"))

    users_in_contest = defaultdict(set)
    for user_id, contest_id in Participant.objects.filter(user_id__in=user_ids).values_list("user_id", "contest_id"):
        users_in_contest[contest_id].add(user_id)
    started = ProblemInstance.objects.filter(
        contest_id__in=users_in_contest.keys(),
        round__start_date__lte=timezone.now(),
    ).values_list("problem_id", "contest_id")
    for problem_id, contest_id in started:
        family_id = family_of.get(problem_id)
        if family_id is None:
            continue
        for user_id in users_in_contest[contest_id]:
            result[family_id][user_id].setdefault(contest_id, PARTICIPANT)

    submitted = (
        Submission.objects.filter(user_id__in=user_ids, problem_instance__contest__isnull=False)
        .values_list("user_id", "problem_instance__problem_id", "problem_instance__contest_id")
        .distinct()
    )
    for user_id, problem_id, contest_id in submitted:
        family_id = family_of.get(problem_id)
        if family_id is not None:
            result[family_id][user_id][contest_id] = SUBMITTED
    return result
