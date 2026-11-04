import os

from django.core.management.base import BaseCommand, CommandError

from oioioi.contests.models import Contest, ProblemInstance
from oioioi.llmdetect.models import LLMReferenceSolution, mark_dirty


class Command(BaseCommand):
    help = (
        "Imports LLM reference solutions from DIR/<problem short name>/<file>. A file named "
        "<model>__<prompt>__<anything>.<ext> gets the model and the prompt from its name."
    )

    def add_arguments(self, parser):
        parser.add_argument("contest_id")
        parser.add_argument("directory")
        parser.add_argument("--replace", action="store_true", help="Delete the generated references of the imported problems first.")

    def handle(self, *args, **options):
        try:
            contest = Contest.objects.get(id=options["contest_id"])
        except Contest.DoesNotExist as error:
            raise CommandError(f"No contest {options['contest_id']}") from error
        root = options["directory"]
        if not os.path.isdir(root):
            raise CommandError(f"{root} is not a directory")
        total = 0
        for short_name in sorted(os.listdir(root)):
            path = os.path.join(root, short_name)
            if not os.path.isdir(path):
                continue
            pi = ProblemInstance.objects.filter(contest=contest, short_name=short_name).first()
            if pi is None:
                self.stderr.write(f"Skipped {short_name}: no such problem in {contest.id}")
                continue
            if options["replace"]:
                LLMReferenceSolution.objects.filter(problem_instance=pi, kind=LLMReferenceSolution.KIND_GENERATED).delete()
            for filename in sorted(os.listdir(path)):
                file_path = os.path.join(path, filename)
                if not os.path.isfile(file_path):
                    continue
                with open(file_path, encoding="utf-8", errors="replace") as file:
                    source = file.read()
                parts = os.path.splitext(filename)[0].split("__")
                LLMReferenceSolution.objects.create(
                    problem_instance=pi,
                    model_name=parts[0] if len(parts) > 1 else "",
                    prompt_variant=parts[1] if len(parts) > 2 else "",
                    filename=filename,
                    source=source,
                )
                total += 1
        mark_dirty(contest)
        self.stdout.write(f"Imported {total} references")
