import json

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from oioioi.problembase.models import ProblemDescription, ProblemFamilyMember, ensure_families
from oioioi.problembase.taxonomy import OBJECT_NAMES, TAG_NAMES


class Command(BaseCommand):
    help = "Load descriptions written by tools/describe.py. Descriptions written by hand or taken from a package are never overwritten."

    def add_arguments(self, parser):
        parser.add_argument("input", help="JSONL from tools/describe.py")
        parser.add_argument("--dry-run", action="store_true", help="only report what would change")

    def handle(self, *args, **options):
        ensure_families()
        try:
            with open(options["input"], encoding="utf-8") as f:
                records = [json.loads(line) for line in f if line.strip()]
        except (OSError, ValueError) as e:
            raise CommandError(e) from e
        latest = {}
        for record in records:  # later lines win (re-runs are appended)
            latest[record["problem_id"]] = record
        family_of = dict(ProblemFamilyMember.objects.filter(problem_id__in=latest).values_list("problem_id", "family_id"))
        counts = {"created": 0, "updated": 0, "kept": 0, "missing": 0}
        with transaction.atomic():
            for problem_id, record in latest.items():
                family_id = family_of.get(problem_id)
                if family_id is None:
                    counts["missing"] += 1
                    self.stderr.write(f"problem {problem_id} ({record.get('short_name')}) not found")
                    continue
                description = ProblemDescription.objects.filter(family_id=family_id).first()
                if description is not None and description.source != "llm":
                    counts["kept"] += 1
                    continue
                counts["updated" if description else "created"] += 1
                if options["dry_run"]:
                    continue
                description = description or ProblemDescription(family_id=family_id)
                a = record["analysis"]
                description.statement = a["statement"]
                description.solution = a["solution"]
                description.rating = a["rating"]
                description.rating_reason = a["rating_reason"]
                description.main_tags = [t for t in a["main_tags"] if t in TAG_NAMES]
                description.side_tags = [t for t in a["side_tags"] if t in TAG_NAMES]
                description.problem_objects = [o for o in a.get("objects", []) if o in OBJECT_NAMES]
                description.model_name = record.get("model", "")
                description.source = "llm"
                description.save()
        self.stdout.write(
            f"{counts['created']} created, {counts['updated']} updated, {counts['kept']} kept (written by hand or from a package), "
            f"{counts['missing']} problems not found" + (" (dry run)" if options["dry_run"] else "")
        )
