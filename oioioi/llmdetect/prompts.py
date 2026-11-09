"""Prompts for LLM reference solutions, and the format to add many at once.

The server never asks any LLM itself. The staff copy the prompts into a
chat, or download them for ``tools/llm_references.py``, a script run on
their own machine, and paste or upload the answers here in the *bulk
format*::

    ### reference problem=abc model=groq/openai/gpt-oss-120b prompt=student
    <the answer: code, or a whole chat answer with a ``` code block>
    ### reference problem=abc model="ChatGPT free" prompt=polish
    ...

Values with spaces are quoted. ``problem`` is the short name of the
problem (it may be left out when a default problem is chosen); ``model``
and ``prompt`` are labels. Other keys are ignored.
"""

import json
import re

from django.utils import timezone
from django.utils.translation import gettext as _g
from django.utils.translation import gettext_lazy as _
from django.utils.translation import pgettext_lazy

PROMPTS_FORMAT = "llmdetect-prompts/1"
# References added in one bulk upload at most.
MAX_BULK_REFERENCES = 5000

_TASK = "Solve this competitive programming problem in C++. Read the input from standard input and write the output to standard output."

# How participants might ask. The answers differ in style, and the
# references should cover what participants paste.
PROMPTS = {
    "plain": (pgettext_lazy("llmdetect", "Plain"), _TASK),
    "polish": (
        _("In Polish"),
        "Rozwiąż to zadanie w C++. Wejście czytaj ze standardowego wejścia, wynik wypisz na standardowe wyjście.",
    ),
    "student": (
        _("Like a high-school student"),
        _TASK + " Write the code like a high-school student at an olympiad would: short, no comments, short variable names.",
    ),
    "olympiad": (
        _("Like an olympiad competitor"),
        _TASK + " Write it the way an experienced olympiad competitor would: bits/stdc++.h, using namespace std, macros, fast IO, global arrays.",
    ),
    "disguised": (
        _("Disguised"),
        _TASK + " Then make the code look hand-written: rename the variables, remove all comments and do not explain anything.",
    ),
}

HEADER_RE = re.compile(r"^###[ \t]*reference\b(.*)$", re.M | re.I)
_ATTRIBUTE_RE = re.compile(r'(\w+)=(?:"([^"]*)"|(\S+))')


def build_prompt(variant, statement):
    _label, instruction = PROMPTS[variant]
    return f"{instruction}\n\n{statement.strip()}"


def prompts_file(contest, statements, variants):
    """The prompts for ``tools/llm_references.py``, as JSON text.

    ``statements`` maps problem short names to statement texts.
    """
    data = {
        "format": PROMPTS_FORMAT,
        "contest": contest.id,
        "created": timezone.now().isoformat(timespec="seconds"),
        "prompts": [
            {"problem": problem, "prompt": variant, "text": build_prompt(variant, statement)}
            for problem, statement in statements.items()
            for variant in variants
        ],
    }
    return json.dumps(data, ensure_ascii=False, indent=1)


_FENCE_LANGUAGES = ("", "c++", "cpp", "cc", "c", "cxx")


def _code_blocks(text):
    """The contents of closed ``` code blocks (C++ or untagged), in linear time."""
    parts = text.split("```")
    blocks = []
    for i in range((len(parts) - 1) // 2):
        tag, newline, body = parts[2 * i + 1].partition("\n")
        if newline and tag.strip().lower() in _FENCE_LANGUAGES:
            blocks.append(body)
    return blocks


def extract_code(answer):
    """The C++ code of an answer: its longest code block, or all of it."""
    blocks = _code_blocks(answer)
    if blocks:
        return max(blocks, key=len).strip() + "\n"
    return answer.strip() + "\n"


def code_of_chat_answer(text):
    """The code of a whole chat answer pasted by hand (prose and code
    fences dropped); text without code fences is returned as it is.
    """
    return extract_code(text) if _code_blocks(text) else text


def _attributes(text):
    return {m.group(1).lower(): m.group(2) if m.group(2) is not None else m.group(3) for m in _ATTRIBUTE_RE.finditer(text)}


def parse_references(text, problems, default_problem=None):
    """Parses the bulk format.

    ``problems`` maps short names to problem instances. Returns
    ``(references, errors)``: references are dicts with ``problem_instance``,
    ``model``, ``prompt``, ``source`` and ``line``; errors are messages.
    """
    text = text.replace("\r\n", "\n")
    headers = list(HEADER_RE.finditer(text))
    errors = []
    references = []
    if not headers:
        return [], [_g('No "### reference" lines found.')]
    if len(headers) > MAX_BULK_REFERENCES:
        return [], [_g("More than %(count)d references; split them into several uploads.") % {"count": MAX_BULK_REFERENCES}]
    if text[: headers[0].start()].strip():
        errors.append(_g('There is text before the first "### reference" line.'))
    for index, header in enumerate(headers):
        line = text.count("\n", 0, header.start()) + 1
        end = headers[index + 1].start() if index + 1 < len(headers) else len(text)
        body = text[header.end() : end].strip("\n")
        attributes = _attributes(header.group(1))
        short_name = attributes.get("problem")
        pi = problems.get(short_name) if short_name else default_problem
        if pi is None:
            if short_name:
                errors.append(_g("Line %(line)d: no problem %(problem)s in this contest.") % {"line": line, "problem": short_name})
            else:
                errors.append(_g("Line %(line)d: no problem given, and no default problem chosen.") % {"line": line})
            continue
        if not body.strip():
            errors.append(_g("Line %(line)d: no code after the line.") % {"line": line})
            continue
        source = code_of_chat_answer(body).strip("\n") + "\n"
        references.append(
            {
                "problem_instance": pi,
                "model": attributes.get("model", "")[:100],
                "prompt": attributes.get("prompt", "")[:100],
                "source": source,
                "line": line,
            }
        )
    return references, errors
