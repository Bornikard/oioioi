"""What the latest rejudge of a problem has changed.

Only the moment of the rejudge is stored. Everything else comes from reports,
which are kept forever: a submission is compared if it has a result from
before that moment and a newer one, so submissions sent afterwards are skipped.
Their scores still count when working out the results of users, which tells
how the ranking would look like today if the rejudge had not happened.
"""

from django.contrib.auth.models import User
from django.utils.dateparse import parse_datetime

from oioioi.contests.models import Submission, SubmissionReport
from oioioi.czeciecamp.controllers import RESULT_REPORT_KINDS, best_submissions
from oioioi.czeciecamp.models import SCORING_LAST, CampRanking, get_camp_config


def _compared_submissions(problem_instance, started_at):
    """Returns the old scores of submissions rejudged so far, by submission id."""
    reports = (
        SubmissionReport.objects.filter(
            submission__problem_instance=problem_instance,
            submission__kind="NORMAL",
            submission__user__isnull=False,
            kind__in=RESULT_REPORT_KINDS,
        )
        .prefetch_related("scorereport_set")
        .order_by("creation_date", "id")
    )
    latest_before = {}
    rejudged = set()
    for report in reports:
        if report.creation_date < started_at:
            latest_before[report.submission_id] = report
        else:
            rejudged.add(report.submission_id)

    old_scores = {}
    for submission_id in rejudged & set(latest_before):
        report = latest_before[submission_id]
        old_scores[submission_id] = report.score_report.score if report.kind == "NORMAL" and report.score_report else None
    return old_scores


def _result_text(result):
    return "—" if result is None else result[0]


def _ranking_points(daily, stale, ranking):
    if daily is None and stale is None:
        return None
    return ranking.daily_weight * (daily[0] if daily else 0) + ranking.stale_weight * (stale[0] if stale else 0)


def _ranking_part(name, old, new):
    return {
        "name": name,
        "old": _result_text(old),
        "new": _result_text(new),
        "old_submission": old[1] if old else None,
        "new_submission": new[1] if new else None,
        "changed": (old[0] if old else None) != (new[0] if new else None),
    }


def _ranking_changes(contest, problem_instance, ranking, user_ids, old_scores):
    """For each user, how the rejudge has changed the problem's cell in
    ``ranking``. Freezing is ignored, like in the view for admins.
    """
    submissions = list(Submission.objects.filter(problem_instance=problem_instance, user__in=user_ids, kind="NORMAL").order_by("date", "id"))
    pi_by_id = {problem_instance.id: problem_instance}
    counting_end = get_camp_config(contest).counting_end_date
    last_daily = ranking.daily_scoring == SCORING_LAST

    def best(score_of):
        return best_submissions(submissions, pi_by_id, counting_end, counting_end, last_daily, score_of)

    new_daily, new_stale = best(lambda submission: submission.score)
    old_daily, old_stale = best(lambda submission: old_scores.get(submission.id, submission.score))

    changes = {}
    for user_id in user_ids:
        key = (user_id, problem_instance.id)
        daily = _ranking_part("Dzienne", old_daily.get(key), new_daily.get(key))
        stale = _ranking_part("Stałe", old_stale.get(key), new_stale.get(key))
        changes[user_id] = {
            # Shown even with a zero weight: a ranking of a round usually has
            # one for Stałe, which other rankings of the camp do count.
            "parts": [part for part in (daily, stale) if part["changed"]],
            "changed": (ranking.daily_weight and daily["changed"]) or (ranking.stale_weight and stale["changed"]),
            "old": _ranking_points(old_daily.get(key), old_stale.get(key), ranking),
            "new": _ranking_points(new_daily.get(key), new_stale.get(key), ranking),
        }
    return changes


def build_rejudge_report(contest, problem_instance):
    """Returns the report of the latest rejudge of ``problem_instance`` from
    its page, or ``None`` if there has not been any.
    """
    rejudge = get_camp_config(contest).last_rejudges.get(str(problem_instance.id))
    if rejudge is None:
        return None
    started_at = parse_datetime(rejudge["at"])

    old_scores = _compared_submissions(problem_instance, started_at)
    submissions = Submission.objects.filter(id__in=old_scores).select_related("user").order_by("id")
    changed = [submission for submission in submissions if submission.score != old_scores[submission.id]]
    changed_scores = {submission.id: old_scores[submission.id] for submission in changed}

    ranking = None
    if problem_instance.round_id is not None:
        ranking = CampRanking.objects.filter(contest=contest, round=problem_instance.round).first()

    user_ids = {submission.user_id for submission in changed}
    ranking_changes = {}
    if ranking is not None and user_ids:
        participants = contest.controller.ranking_controller().filter_users_for_ranking(None, User.objects.filter(id__in=user_ids))
        ranking_changes = _ranking_changes(contest, problem_instance, ranking, set(participants.values_list("id", flat=True)), changed_scores)

    rows = {}
    for submission in changed:
        row = rows.setdefault(
            submission.user_id,
            {"user": submission.user, "submissions": [], "ranking": ranking_changes.get(submission.user_id)},
        )
        row["submissions"].append({"id": submission.id, "old": old_scores[submission.id], "new": submission.score})
    rows = sorted(rows.values(), key=lambda row: (row["user"].last_name, row["user"].first_name, row["user"].username))
    rows.sort(key=lambda row: not row["ranking"]["changed"] if row["ranking"] else True)

    return {
        "started_at": started_at,
        "count": rejudge["count"],
        "rejudged": len(old_scores),
        "in_progress": len(old_scores) < rejudge["count"],
        "ranking": ranking,
        "ranking_changes_count": sum(1 for change in ranking_changes.values() if change["changed"]),
        "rows": rows,
    }
