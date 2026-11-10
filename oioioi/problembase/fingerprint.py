"""Reading package files (from filetracker) into a :class:`ProblemFingerprint`."""

import hashlib
import io
import logging
import re

from oioioi.problembase.models import ProblemFingerprint
from oioioi.problems.models import ProblemStatement
from oioioi.programs.models import ModelSolution

logger = logging.getLogger(__name__)

# Shorter normalised sources are too generic to identify a task.
MIN_SOURCE_LENGTH = 100
STATEMENT_PAGES = 8
STATEMENT_MAX_CHARS = 30000

_C_COMMENTS = re.compile(r"//[^\n]*|/\*.*?\*/", re.DOTALL)
_PASCAL_COMMENTS = re.compile(r"//[^\n]*|\{.*?\}|\(\*.*?\*\)", re.DOTALL)
_PYTHON_COMMENTS = re.compile(r"#[^\n]*")
_WHITESPACE = re.compile(r"\s+")


def _decode(data):
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return data.decode("latin-1")


def normalize_source(source, filename):
    """Source without comments and whitespace, so a changed header or
    reformatting does not change the hash."""
    extension = filename.rsplit(".", 1)[-1].lower()
    if extension == "pas":
        source = _PASCAL_COMMENTS.sub("", source)
    elif extension == "py":
        source = _PYTHON_COMMENTS.sub("", source)
    else:
        source = _C_COMMENTS.sub("", source)
    return _WHITESPACE.sub("", source)


def source_hash(data, filename):
    normalized = normalize_source(_decode(data), filename)
    if len(normalized) < MIN_SOURCE_LENGTH:
        return None
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def pdf_text(data):
    from pdfminer.high_level import extract_text

    return extract_text(io.BytesIO(data), maxpages=STATEMENT_PAGES)[:STATEMENT_MAX_CHARS]


def _statement(problem):
    statements = [s for s in ProblemStatement.objects.filter(problem=problem) if s.extension == ".pdf"]
    statements.sort(key=lambda s: (s.language not in ("pl", None, ""), s.id))
    return statements[0] if statements else None


def compute_fingerprint(problem):
    errors = []
    hashes = []
    for solution in ModelSolution.objects.filter(problem=problem, kind="NORMAL").order_by("order_key", "id"):
        try:
            digest = source_hash(solution.source_file.read(), solution.name)
        except Exception as e:  # filetracker errors are not uniform
            errors.append(f"{solution.name}: {e}")
            continue
        if digest:
            hashes.append({"name": solution.name, "hash": digest})

    text = ""
    statement = _statement(problem)
    if statement is not None:
        try:
            text = pdf_text(statement.content.read())
        except Exception as e:
            errors.append(f"{statement.filename}: {e}")

    fingerprint, _ = ProblemFingerprint.objects.update_or_create(
        problem=problem,
        defaults={"solution_hashes": hashes, "statement_text": text, "errors": "\n".join(errors)},
    )
    return fingerprint
