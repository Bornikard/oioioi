import time

from django.core.management.base import BaseCommand
from django.db import close_old_connections

from oioioi.llmdetect.generation import claim_job, process
from oioioi.llmdetect.models import GenerationJob


class Command(BaseCommand):
    help = "Generates the LLM reference solutions requested in the panel. Runs as a daemon under supervisor; --once processes the queue and exits."

    def add_arguments(self, parser):
        parser.add_argument("--once", action="store_true", help="Process the queued jobs and exit.")
        parser.add_argument("--interval", type=float, default=5.0, help="Seconds between checks of an empty queue.")

    def handle(self, *args, **options):
        # Jobs left running by a killed daemon would never finish otherwise.
        GenerationJob.objects.filter(status=GenerationJob.RUNNING).update(status=GenerationJob.QUEUED)
        while True:
            close_old_connections()
            job = claim_job()
            if job is not None:
                self.stdout.write(f"Job {job.id}: {job.count} x {job.provider} {job.model} ({job.prompt_variant})")
                process(job)
                job.refresh_from_db()
                self.stdout.write(f"Job {job.id}: {job.status}, {job.produced} references{': ' + job.error if job.error else ''}")
                continue
            if options["once"]:
                return
            time.sleep(options["interval"])
