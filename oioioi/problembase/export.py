"""Exporting what the offline description tool needs (tools/describe.py).

One record per family: statement (LaTeX source from the package, or text
from the PDF), editorial if the package has one, config.yml, up to three
model solutions, limits, and the contests the problem was used in with
results (to calibrate the rating). Tests are never read.
"""

import io
import json
import logging
import re
import shutil
import tarfile
import tempfile
import zipfile
from collections import defaultdict

from django.db.models import Count, Max, Sum
from django.utils import timezone

from oioioi.contests.models import ProblemInstance, UserResultForProblem
from oioioi.contests.scores import IntegerScore
from oioioi.participants.models import Participant
from oioioi.problembase.fingerprint import pdf_text
from oioioi.problembase.models import ProblemFamilyMember, ProblemFingerprint
from oioioi.problembase.rejection import rejected_problem_ids
from oioioi.problems.models import Problem, ProblemPackage, ProblemStatement
from oioioi.programs.models import ModelSolution, Test

logger = logging.getLogger(__name__)

MAX_SOLUTIONS = 3
MAX_SOURCE_CHARS = 30000
MAX_TEXT_CHARS = 60000
SOLUTION_EXTENSIONS = ["cpp", "cc", "cxx", "c", "py", "java", "pas"]
EDITORIAL_WORDS = ("opr", "roz", "sol", "editorial")


def decode(data):
    for encoding in ("utf-8", "cp1250", "iso-8859-2"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def _package_for(problem):
    try:
        from oioioi.sinolpack.models import OriginalPackage

        original = OriginalPackage.objects.filter(problem=problem, problem_package__isnull=False).select_related("problem_package").order_by("-id").first()
        if original is not None:
            return original.problem_package
    except ImportError:
        pass
    return ProblemPackage.objects.filter(problem=problem, status="OK").order_by("-creation_date").first()


def _relative(name):
    """Path inside the package without the top-level directory (``abc/doc/x.tex`` -> ``doc/x.tex``)."""
    parts = name.replace("\\", "/").strip("/").split("/")
    if len(parts) > 1 and parts[1] in ("doc", "prog", "in", "out", "config.yml", "config.yaml", "attachments", "dlazaw", "makefile.in"):
        parts = parts[1:]
    return "/".join(parts)


def _wanted(relative):
    lower = relative.lower()
    if lower in ("config.yml", "config.yaml"):
        return True
    if lower.startswith("doc/") and "/" not in lower[4:]:
        return lower.endswith(".tex") or (lower.endswith(".pdf") and any(word in lower[4:] for word in EDITORIAL_WORDS))
    return False


def read_package_files(field_file):
    """{relative path: bytes} for config.yml and statement/editorial files in doc/."""
    with tempfile.TemporaryFile() as tmp:
        with field_file.open("rb") as source:
            shutil.copyfileobj(source, tmp)
        tmp.seek(0)
        files = {}
        if zipfile.is_zipfile(tmp):
            tmp.seek(0)
            with zipfile.ZipFile(tmp) as archive:
                for info in archive.infolist():
                    relative = _relative(info.filename)
                    if not info.is_dir() and _wanted(relative):
                        files[relative] = archive.read(info)
            return files
        tmp.seek(0)
        with tarfile.open(fileobj=tmp) as archive:
            for member in archive:
                relative = _relative(member.name)
                if member.isfile() and _wanted(relative):
                    files[relative] = archive.extractfile(member).read()
        return files


def _split_doc(files, short_name):
    """(statement tex, editorial text) from the doc/ files of a package."""
    statement, editorial = "", ""
    tex = {name: decode(data) for name, data in files.items() if name.lower().endswith(".tex")}
    statement_names = sorted((n for n in tex if "zad" in n.lower()), key=lambda n: (short_name.lower() not in n.lower(), n))
    if not statement_names:
        statement_names = sorted(n for n in tex if not any(word in n.lower() for word in EDITORIAL_WORDS))
    if statement_names:
        statement = tex[statement_names[0]]
    editorial_names = sorted(n for n in tex if any(word in n.lower()[4:] for word in EDITORIAL_WORDS) and "zad" not in n.lower())
    if editorial_names:
        editorial = tex[editorial_names[0]]
    else:
        for name, data in sorted(files.items()):
            if name.lower().endswith(".pdf"):
                try:
                    editorial = pdf_text(data)
                except Exception as e:
                    logger.info("Cannot read editorial %s: %s", name, e)
                break
    return statement[:MAX_TEXT_CHARS], editorial[:MAX_TEXT_CHARS]


def _solution_key(solution, short_name):
    base, _, extension = solution.name.rpartition(".")
    match = re.fullmatch(re.escape(short_name) + r"(\d*)", base)
    number = int(match.group(1) or 0) if match else 10**6
    rank = SOLUTION_EXTENSIONS.index(extension) if extension in SOLUTION_EXTENSIONS else len(SOLUTION_EXTENSIONS)
    return (match is None, rank, number, solution.name)


def model_solutions(problem):
    solutions = sorted(ModelSolution.objects.filter(problem=problem, kind="NORMAL"), key=lambda s: _solution_key(s, problem.short_name))
    result, errors = [], []
    for solution in solutions[:MAX_SOLUTIONS]:
        try:
            result.append({"name": solution.name, "source": decode(solution.source_file.read())[:MAX_SOURCE_CHARS]})
        except Exception as e:
            errors.append(f"{solution.name}: {e}")
    return result, errors


def _pdf_statement(problem):
    fingerprint = ProblemFingerprint.objects.filter(problem=problem).first()
    if fingerprint and fingerprint.statement_text:
        return fingerprint.statement_text
    statement = next((s for s in ProblemStatement.objects.filter(problem=problem) if s.extension == ".pdf"), None)
    return pdf_text(statement.content.read()) if statement else ""


def contest_uses(problem_ids):
    """Contests the problems were used in, with results, newest first."""
    instances = list(ProblemInstance.objects.filter(problem_id__in=problem_ids, contest__isnull=False).select_related("contest", "round"))
    max_points = dict(
        Test.objects.filter(problem_instance__in=instances, is_active=True)
        .values_list("problem_instance_id")
        .annotate(total=Sum("max_score"))
        .values_list("problem_instance_id", "total")
    )
    participant_count = dict(
        Participant.objects.filter(contest_id__in={pi.contest_id for pi in instances})
        .values("contest_id")
        .annotate(n=Count("id"))
        .values_list("contest_id", "n")
    )
    results = defaultdict(list)
    for pi_id, score in UserResultForProblem.objects.filter(problem_instance__in=instances).values_list("problem_instance_id", "score"):
        if isinstance(score, IntegerScore):
            results[pi_id].append(score.value)

    uses = []
    for pi in instances:
        date = pi.round.start_date if pi.round else pi.contest.creation_date
        scores, top = results.get(pi.id, []), max_points.get(pi.id)
        uses.append(
            {
                "contest": pi.contest.name,
                "contest_id": pi.contest_id,
                "round": pi.round.name if pi.round else "",
                "date": date.date().isoformat() if date else None,
                "participants": participant_count.get(pi.contest_id, 0),
                "submitted": len(scores),
                "full_score": sum(1 for s in scores if top and s >= top) if top else None,
                "average_percent": round(100 * sum(scores) / (len(scores) * top)) if scores and top else None,
            }
        )
    uses.sort(key=lambda use: use["date"] or "", reverse=True)
    return uses


def _limits(problem):
    tests = Test.objects.filter(problem_instance__problem=problem, problem_instance__contest__isnull=True, is_active=True)
    limits = tests.aggregate(time=Max("time_limit"), memory=Max("memory_limit"))
    return {"time_ms": limits["time"], "memory_kb": limits["memory"], "tests": tests.count()}


def export_family(family_id, problem_ids):
    problems = list(Problem.objects.filter(id__in=problem_ids).prefetch_related("names").order_by("id"))
    representative = problems[-1]
    errors = []
    statement_tex = editorial = config = ""
    package = _package_for(representative)
    if package is not None:
        try:
            files = read_package_files(package.package_file)
            statement_tex, editorial = _split_doc(files, representative.short_name)
            config_name = next((n for n in files if n.lower() in ("config.yml", "config.yaml")), None)
            config = decode(files[config_name])[:MAX_TEXT_CHARS] if config_name else ""
        except Exception as e:
            errors.append(f"package: {e}")
    else:
        errors.append("package: not found")
    statement_text = ""
    if not statement_tex:
        try:
            statement_text = _pdf_statement(representative)
        except Exception as e:
            errors.append(f"statement: {e}")
    solutions, solution_errors = model_solutions(representative)
    return {
        "family_id": family_id,
        "problem_id": representative.id,
        "problem_ids": [p.id for p in problems],
        "name": representative.name,
        "short_name": representative.short_name,
        "statement_tex": statement_tex,
        "statement_text": statement_text,
        "editorial": editorial,
        "config_yml": config,
        "solutions": solutions,
        "limits": _limits(representative),
        "contests": contest_uses([p.id for p in problems]),
        "errors": errors + solution_errors,
    }


def families_to_export(family_ids=None):
    """family_id -> problem ids (rejected packages left out, rejected-only families skipped)."""
    rejected = rejected_problem_ids()
    families = defaultdict(list)
    members = ProblemFamilyMember.objects.all()
    if family_ids:
        members = members.filter(family_id__in=family_ids)
    for problem_id, family_id in members.values_list("problem_id", "family_id").order_by("problem_id"):
        if problem_id not in rejected:
            families[family_id].append(problem_id)
    return dict(families)


def write_export(path, records, meta):
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        buffer = io.StringIO()
        for record in records:
            buffer.write(json.dumps(record, ensure_ascii=False) + "\n")
        archive.writestr("families.jsonl", buffer.getvalue())
        archive.writestr("meta.json", json.dumps({**meta, "exported": timezone.now().isoformat()}, ensure_ascii=False, indent=2))
