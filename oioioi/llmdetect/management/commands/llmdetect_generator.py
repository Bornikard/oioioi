import logging
import time

from django.core.management.base import BaseCommand
from django.db import DatabaseError, close_old_connections

from oioioi.llmdetect.generation import next_request, perform
from oioioi.llmdetect.models import GenerationJob

logger = logging.getLogger(__name__)

# Seconds to wait after an unexpected error, e.g. the database not being
# migrated yet or being restarted.
ERROR_PAUSE = 30.0


class Command(BaseCommand):
    help = (
        "Generates the LLM reference solutions requested in the panel, one request at a time from whichever provider may be "
        "asked, so that the limits of several providers are used side by side. Runs as a daemon under supervisor; --once "
        "stops when no provider can be asked now."
    )

    def add_arguments(self, parser):
        parser.add_argument("--once", action="store_true", help="Stop when no queued job can be done now.")
        parser.add_argument("--interval", type=float, default=2.0, help="Seconds between checks when nothing can be done.")

    def handle(self, *args, **options):
        resumed = False
        while True:
            close_old_connections()
            try:
                if not resumed:
                    # Jobs left running by a killed daemon would never finish otherwise.
                    GenerationJob.objects.filter(status=GenerationJob.RUNNING).update(status=GenerationJob.QUEUED)
                    resumed = True
                job = next_request()
                if job is not None:
                    perform(job)
                    job.refresh_from_db()
                    self.stdout.write(
                        f"Job {job.id} ({job.provider} {job.model}, {job.prompt_variant}): {job.produced}/{job.count} {job.status} {job.error}".rstrip()
                    )
                    continue
            except DatabaseError:
                # Supervisor does not restart this program, so it must survive
                # e.g. starting before the migrations were applied.
                if options["once"]:
                    raise
                logger.warning("LLM reference generator: database error, retrying in %s s", ERROR_PAUSE, exc_info=True)
                time.sleep(ERROR_PAUSE)
                continue
            if options["once"]:
                return
            time.sleep(options["interval"])
