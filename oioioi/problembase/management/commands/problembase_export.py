import time

from django.core.management.base import BaseCommand

from oioioi.problembase.export import export_family, families_to_export, write_export
from oioioi.problembase.models import ensure_families
from oioioi.problembase.taxonomy import VERSION


class Command(BaseCommand):
    help = "Export statements, model solutions and contest results of problem families for tools/describe.py."

    def add_arguments(self, parser):
        parser.add_argument("output", help="path of the .zip to write")
        parser.add_argument("--family", type=int, action="append", help="export only this family (repeatable)")
        parser.add_argument("--limit", type=int, help="export at most this many families")

    def handle(self, *args, **options):
        ensure_families()
        families = sorted(families_to_export(options["family"]).items())
        if options["limit"]:
            families = families[: options["limit"]]
        records, failed, start = [], 0, time.time()
        for i, (family_id, problem_ids) in enumerate(families, 1):
            record = export_family(family_id, problem_ids)
            records.append(record)
            if record["errors"]:
                failed += 1
                self.stderr.write(f"family {family_id} ({record['short_name']}): {'; '.join(record['errors'])}")
            if i % 25 == 0 or i == len(families):
                self.stdout.write(f"{i}/{len(families)} ({time.time() - start:.0f}s)")
        write_export(options["output"], records, {"families": len(records), "taxonomy_version": VERSION})
        self.stdout.write(f"wrote {len(records)} families to {options['output']}, {failed} with errors")
