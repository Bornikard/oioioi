"""The text of a problem statement for LLM prompts.

The best source is the LaTeX source of a Sinol package (``doc/<id>zad.tex``):
LLMs read LaTeX math exactly, so the limits stay right. It is turned into
plain text (sections, tables and examples) with the math kept as it is, and
without what must not leave the camp (the contest name, the date). Without
a package, the statement file is read: PDF (see :mod:`pdftext`), HTML, a zip
of them, or text.

Every function returns ``(text, source, warnings)``; the staff checks and
edits the text before it is used.
"""

import contextlib
import io
import logging
import os
import re
import tarfile
import tempfile
import zipfile

from django.utils.translation import gettext as _g

logger = logging.getLogger(__name__)

# A statement file, or a package member read into memory, at most this large.
MAX_STATEMENT_SIZE = 50 * 1024 * 1024
MAX_EXAMPLE_SIZE = 64 * 1024
# Packages are copied to a temporary file to be read; larger ones are skipped.
MAX_PACKAGE_SIZE = 2 * 1024 * 1024 * 1024

SOURCE_PACKAGE = "package"
SOURCE_FILE = "file"

SECTION_NAMES = {
    "pl": {
        "Input": "Wejście",
        "Output": "Wyjście",
        "Example": "Przykład",
        "Examples": "Przykłady",
        "Subtask": "Podzadanie",
        "Subtasks": "Podzadania",
        "Points": "Liczba punktów",
        "Conditions": "Ograniczenia",
        "Constraints": "Ograniczenia",
        "TimeLimit": "Limit czasu",
        "Interaction": "Interakcja",
        "Notes": "Uwagi",
        "Explanation": "Wyjaśnienie przykładu",
        # Names in older versions of sinol.cls.
        "Wejscie": "Wejście",
        "Wyjscie": "Wyjście",
        "Przyklad": "Przykład",
        "Uwagi": "Uwagi",
        "Zadanie": "Zadanie",
    },
    "en": {
        "Input": "Input",
        "Output": "Output",
        "Example": "Example",
        "Examples": "Examples",
        "Subtask": "Subtask",
        "Subtasks": "Subtasks",
        "Points": "Points",
        "Conditions": "Constraints",
        "Constraints": "Constraints",
        "TimeLimit": "Time limit",
        "Interaction": "Interaction",
        "Notes": "Notes",
        "Explanation": "Explanation of the example",
        "Wejscie": "Input",
        "Wyjscie": "Output",
        "Przyklad": "Example",
        "Uwagi": "Comments",
        "Zadanie": "Task",
    },
}
EXAMPLE_LABELS = {
    "pl": ("Dla danych wejściowych:", "poprawnym wynikiem jest:"),
    "en": ("For the input:", "the correct output is:"),
}
MEMORY_LABEL = {"pl": "Dostępna pamięć: %s MB.", "en": "Memory limit: %s MB."}

_RULE = "\x00rule\x00"
_TEX_MEMBER_RE = re.compile(r"(?:^|/)doc/([A-Za-z0-9_]+)zad\.tex$")
# One level of nested braces is enough for statements.
_ARG = r"\{((?:[^{}]|\{[^{}]*\})*)\}"


def read_field(field, limit=None):
    """Reads a stored file the way SIO2 does (Filetracker streams cannot be
    reopened or closed), at most ``limit`` bytes.
    """
    try:
        return field.read() if limit is None else field.read(limit)
    finally:
        with contextlib.suppress(AttributeError, ValueError):
            field.close()


def _clean(text):
    text = text.replace("\r\n", "\n").replace("\x0c", "\n")
    text = "\n".join(line.rstrip() for line in text.split("\n"))
    return re.sub(r"\n{3,}", "\n\n", text).strip()


# LaTeX sources.


def _strip_comments(tex):
    return "\n".join(re.sub(r"(?<!\\)%.*", "", line) for line in tex.split("\n"))


def _argument(tex, command):
    match = re.search(r"\\" + command + r"\s*" + _ARG, tex)
    return match.group(1).strip() if match else None


def _language(tex):
    options = re.search(r"\\documentclass\[([^\]]*)\]", tex)
    options = options.group(1).split(",") if options else []
    return "en" if "en" in [o.strip() for o in options] else "pl"


def _table(body):
    rows = []
    for row in re.split(r"\\\\", body):
        row = re.sub(r"\\(?:hline|cline\{[^}]*\}|toprule|midrule|bottomrule)", "", row).strip()
        if row:
            rows.append("| " + " | ".join(cell.strip() for cell in re.split(r"(?<!\\)&", row)) + " |")
    if len(rows) > 1:
        # A placeholder: the dashes would become an em dash below.
        rows.insert(1, "|" + _RULE * (rows[0].count(" | ") + 1))
    return "\n" + "\n".join(rows) + "\n"


def latex_statement(tex, example=None):
    """Plain text of a Sinol statement source, with the math as it is.

    ``example(name)`` returns ``(input, output)`` of an example test, for
    ``\\makeexample``, ``\\makecompactexample`` and ``\\makestandardexample``
    (test ``0`` unless given as an argument), or ``None``.
    Returns ``(text, warnings)``.
    """
    warnings = []
    language = _language(tex)
    names = SECTION_NAMES[language]
    tex = _strip_comments(tex)
    title = _argument(tex, "title")
    memory = _argument(tex, "RAM")
    body = re.search(r"\\begin\{document\}(.*?)(?:\\end\{document\}|$)", tex, re.S)
    body = body.group(1) if body else tex

    def make_example(match):
        name = (match.group(1) or "0").strip()
        data = example(name) if example else None
        if data is None:
            warnings.append(_g("Example %(name)s was not found in the package; add it to the text.") % {"name": name})
            return "\n[" + _g("example %(name)s") % {"name": name} + "]\n"
        before, after = EXAMPLE_LABELS[language]
        return f"\n{before}\n```\n{data[0].rstrip()}\n```\n{after}\n```\n{data[1].rstrip()}\n```\n"

    body = re.sub(r"\\make(?:compact|standard)?example\b(?:\s*\{([^{}]*)\})?", make_example, body)
    if re.search(r"\\includegraphics|\\begin\{(?:figure|tikzpicture)\}", body):
        warnings.append(_g("The statement has pictures; describe them in the text if they matter."))
    body = re.sub(r"\\includegraphics(?:\[[^\]]*\])?\{[^}]*\}", "[" + _g("picture") + "]", body)
    body = re.sub(r"\\begin\{(?:figure|tikzpicture)\}.*?\\end\{(?:figure|tikzpicture)\}", "[" + _g("picture") + "]", body, flags=re.S)
    body = re.sub(r"\\sinolSName(\w+)", lambda m: names.get(m.group(1), m.group(1)), body)
    body = re.sub(r"\\begin\{tabular\}\{[^}]*\}(.*?)\\end\{tabular\}", lambda m: _table(m.group(1)), body, flags=re.S)
    body = re.sub(r"\\(sub)?section\*?\s*" + _ARG, lambda m: "\n" + ("###" if m.group(1) else "##") + " " + m.group(2).strip() + "\n", body)
    for _repeat in range(3):  # nested formatting
        body = re.sub(r"\\(?:textbf|textit|texttt|emph|textrm|textsf|mbox|underline|text)\s*" + _ARG, r"\1", body)
    body = re.sub(r"\\item\s*", "\n- ", body)
    body = re.sub(r"\\(?:begin|end)\{(?:tasktext|center|flushleft|flushright|itemize|enumerate|minipage)\}(?:\[[^\]]*\])?(?:\{[^}]*\})?", "\n", body)
    body = re.sub(r"\\(?:vspace|hspace|setlength|addtolength|pagestyle|thispagestyle)\*?\s*(?:\{[^}]*\})+", "", body)
    body = re.sub(r"\\(?:noindent|medskip|bigskip|smallskip|newline|par|clearpage|newpage)\b", "\n", body)
    body = body.replace("{}", "")
    # Outside math: TeX spacing and dashes.
    parts = re.split(r"(\$\$.*?\$\$|\$[^$]*\$|\\\[.*?\\\])", body, flags=re.S)
    for i in range(0, len(parts), 2):
        text = parts[i]
        text = text.replace("~", " ").replace("\\,", " ").replace("---", "—").replace("--", "–").replace("\\ldots", "…")
        text = text.replace("\\\\", "\n").replace("\\%", "%").replace("\\&", "&").replace("\\_", "_").replace("\\#", "#")
        text = text.replace(",,", "„").replace("''", "”")
        parts[i] = text
    body = "".join(parts)
    leftover = sorted(set(re.findall(r"\\([A-Za-z]+)", re.sub(r"\$[^$]*\$", "", body))) - {"le", "ge", "cdot"})
    if leftover:
        warnings.append(_g("Unknown LaTeX commands left in the text: %(commands)s.") % {"commands": ", ".join("\\" + c for c in leftover[:8])})
    header = []
    if title:
        header.append(f"# {title}")
    if memory:
        header.append(MEMORY_LABEL[language] % memory)
    # Paragraph lines joined: TeX source line breaks mean nothing.
    paragraphs = []
    for block in re.split(r"\n\s*\n", body):
        lines = [line.strip() for line in block.strip().split("\n")]
        if any(line.startswith(("|", "-", "#", "```")) for line in lines) or not lines:
            paragraphs.append("\n".join(lines))
        else:
            paragraphs.append(" ".join(line for line in lines if line))
    text = "\n".join(header) + "\n\n" + "\n\n".join(paragraphs)
    return _clean(text.replace(_RULE, "---|")), warnings


# Packages.


class _Package:
    """Read access to the members of a zip or tar package on disk."""

    def __init__(self, path):
        self.zip = self.tar = None
        if zipfile.is_zipfile(path):
            self.zip = zipfile.ZipFile(path)
        else:
            self.tar = tarfile.open(path)

    def names(self):
        if self.zip:
            return self.zip.namelist()
        return [member.name for member in self.tar.getmembers() if member.isfile()]

    def read(self, name, limit):
        if self.zip:
            info = self.zip.getinfo(name)
            if info.file_size > limit:
                return None
            return self.zip.read(name)
        member = self.tar.getmember(name)
        if member.size > limit:
            return None
        return self.tar.extractfile(member).read()

    def close(self):
        (self.zip or self.tar).close()


def _copy_to_temporary(field):
    """The stored package in a temporary file (archives need seeking)."""
    handle = tempfile.NamedTemporaryFile(suffix=".pkg", delete=False)
    total = 0
    try:
        while True:
            chunk = field.read(1024 * 1024)
            if not chunk:
                break
            total += len(chunk)
            if total > MAX_PACKAGE_SIZE:
                raise ValueError("package too large")
            handle.write(chunk)
    except BaseException:
        handle.close()
        os.unlink(handle.name)
        raise
    finally:
        with contextlib.suppress(AttributeError, ValueError):
            field.close()
    handle.close()
    return handle.name


def _decode(data):
    return data.decode("utf-8", "replace") if data is not None else None


def package_statement(problem):
    """The statement from the LaTeX source in the newest package of ``problem``,
    or ``None`` without one.
    """
    from oioioi.problems.models import ProblemPackage

    package = ProblemPackage.objects.filter(problem=problem, status="OK").order_by("-creation_date", "-id").first()
    if package is None or not package.package_file:
        return None
    path = _copy_to_temporary(package.package_file)
    try:
        archive = _Package(path)
        try:
            sources = [(m.group(1), name) for name in archive.names() if (m := _TEX_MEMBER_RE.search(name))]
            if not sources:
                return None
            # The statement of this problem, not of another one in the package.
            sources.sort(key=lambda item: (item[0] != problem.short_name, len(item[1])))
            problem_id, name = sources[0]
            tex = _decode(archive.read(name, MAX_STATEMENT_SIZE))
            if tex is None:
                return None
            root = name[: -len(f"doc/{problem_id}zad.tex")]
            members = set(archive.names())
            tex_id = _argument(tex, "id") or problem_id

            def example(test):
                found = []
                for kind, extension in (("in", ".in"), ("out", ".out")):
                    member = f"{root}{kind}/{tex_id}{test}{extension}"
                    data = archive.read(member, MAX_EXAMPLE_SIZE) if member in members else None
                    found.append(_decode(data))
                return tuple(found) if None not in found else None

            text, warnings = latex_statement(tex, example)
            return text, SOURCE_PACKAGE, warnings
        finally:
            archive.close()
    finally:
        os.unlink(path)


# Statement files.


def file_text(data, extension):
    """Text of a statement file (``.pdf``, ``.html``, ``.zip`` of them, ``.txt``)."""
    if extension == ".pdf":
        from oioioi.llmdetect.pdftext import pdf_text

        return _clean(pdf_text(data))
    if extension in (".html", ".htm"):
        from bs4 import BeautifulSoup

        return _clean(BeautifulSoup(data, "html.parser").get_text("\n"))
    if extension == ".zip":
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            names = sorted(archive.namelist(), key=lambda name: (not name.endswith("index.html"), name))
            for name in names:
                inner = "." + name.rsplit(".", 1)[-1].lower() if "." in name else ""
                # A member that large is not a statement (or is a zip bomb).
                if inner in (".html", ".htm", ".pdf", ".txt") and archive.getinfo(name).file_size <= MAX_STATEMENT_SIZE:
                    return file_text(archive.read(name), inner)
        return ""
    if extension == ".txt":
        return _clean(data.decode("utf-8", "replace"))
    return ""


def _suspicious(text):
    """Warnings about a text read from a file that is probably garbled."""
    warnings = []
    if len(text) < 200:
        warnings.append(_g("The text is very short; the statement may be a scan or a picture."))
    if text.count("?") > 20 and text.count("?") > len(text) / 50:
        warnings.append(_g("Some characters could not be read (shown as ?)."))
    return warnings


def statement_file_text(problem_instance):
    from oioioi.problems.utils import query_statement

    statement = query_statement(problem_instance.problem)
    if statement is None:
        return None
    data = read_field(statement.content, MAX_STATEMENT_SIZE + 1)
    if len(data) > MAX_STATEMENT_SIZE:
        return "", SOURCE_FILE, [_g("The statement file is too large.")]
    text = file_text(data, statement.extension)
    warnings = _suspicious(text) if text else []
    if statement.extension == ".pdf" and text:
        warnings.append(_g("Read from the PDF: check the formulas, the examples and the tables."))
    return text, SOURCE_FILE, warnings


def statement_text(problem_instance):
    """``(text, source, warnings)`` of the statement of a problem: from its
    package's LaTeX source if there is one, else from its statement file.
    ``("", None, [reason])`` when neither can be read.
    """
    errors = []
    for reader in (lambda: package_statement(problem_instance.problem), lambda: statement_file_text(problem_instance)):
        try:
            result = reader()
        except Exception as error:
            logger.warning("Cannot read the statement of %s", problem_instance, exc_info=True)
            errors.append(f"{type(error).__name__}: {error}"[:200])
            continue
        if result is not None and result[0]:
            return result
    reason = _g("No statement found for this problem.") if not errors else _g("The statement could not be read: %(error)s") % {"error": errors[-1]}
    return "", None, [reason]
