from django.core.management.base import BaseCommand, CommandError

from oioioi.contests.models import Contest
from oioioi.llmdetect.scoring import assess_contest, needs_update


class Command(BaseCommand):
    help = "Computes the LLM detection features and risks of the submissions of contests (e.g. from cron during a camp)."

    def add_arguments(self, parser):
        parser.add_argument("contest_ids", nargs="+", metavar="CONTEST_ID")
        parser.add_argument("--force", action="store_true", help="Recompute even if nothing changed.")

    def handle(self, *args, **options):
        for contest_id in options["contest_ids"]:
            try:
                contest = Contest.objects.get(id=contest_id)
            except Contest.DoesNotExist as error:
                raise CommandError(f"No contest {contest_id}") from error
            # The same claim as on the panel: concurrent runs are serialised.
            items = assess_contest(contest, force=options["force"]) if options["force"] or needs_update(contest) else None
            if items is not None:
                self.stdout.write(f"{contest_id}: {len(items)} submissions assessed")
            else:
                self.stdout.write(f"{contest_id}: up to date")
