from django.core.management.base import BaseCommand

from oioioi.problembase.fingerprint import compute_fingerprint
from oioioi.problems.models import Problem


class Command(BaseCommand):
    help = "Read model solutions and statements of problems from filetracker, to find re-uploaded packages."

    def add_arguments(self, parser):
        parser.add_argument("--all", action="store_true", help="recompute problems that already have a fingerprint")
        parser.add_argument("problem_ids", nargs="*", type=int)

    def handle(self, *args, **options):
        problems = Problem.objects.order_by("id")
        if options["problem_ids"]:
            problems = problems.filter(id__in=options["problem_ids"])
        elif not options["all"]:
            problems = problems.filter(fingerprint__isnull=True)
        total = problems.count()
        failed = 0
        for i, problem in enumerate(problems.iterator(), 1):
            fingerprint = compute_fingerprint(problem)
            if fingerprint.errors:
                failed += 1
                self.stderr.write(f"{problem.id} {problem.short_name}: {fingerprint.errors}")
            if i % 50 == 0 or i == total:
                self.stdout.write(f"{i}/{total}")
        self.stdout.write(f"done, {failed} with errors")
