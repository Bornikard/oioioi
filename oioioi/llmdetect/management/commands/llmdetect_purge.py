from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from oioioi.contests.models import Contest
from oioioi.llmdetect.models import LLMDetectConfig, ProblemViewEvent, ReviewDecision, RiskAssessment, SubmissionFeatures


class Command(BaseCommand):
    help = (
        "Deletes the LLM detection data of a contest once the camp is over. Decisions are kept anonymized "
        "(without the participant, reviewer and note) for calibrating the weights, unless --all is given."
    )

    def add_arguments(self, parser):
        parser.add_argument("contest_id")
        parser.add_argument("--all", action="store_true", help="Delete the decisions too.")

    @transaction.atomic
    def handle(self, *args, **options):
        try:
            contest = Contest.objects.get(id=options["contest_id"])
        except Contest.DoesNotExist as error:
            raise CommandError(f"No contest {options['contest_id']}") from error
        RiskAssessment.objects.filter(contest=contest).delete()
        SubmissionFeatures.objects.filter(submission__problem_instance__contest=contest).delete()
        ProblemViewEvent.objects.filter(problem_instance__contest=contest).delete()
        decisions = ReviewDecision.objects.filter(contest=contest)
        if options["all"]:
            decisions.delete()
        else:
            # Keep only what calibration needs: the verdict, the risk and the signals.
            decisions.update(user=None, note="", reviewer=None, submission=None)
        LLMDetectConfig.objects.filter(contest=contest).update(dirty=True, computed_at=None)
        self.stdout.write(f"Purged LLM detection data of {contest.id}")
