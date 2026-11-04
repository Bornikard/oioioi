"""Coding habits: things a person writes the same way every time.

Each habit is extracted as a short value, mostly the idiom itself (e.g.
``cin.tie(0)``), so that a change can be shown to the staff as it is.
``None`` means the habit does not apply to the program (e.g. no fast IO
line, or not C++). Pure functions, no Django.
"""

import re
import unicodedata

from oioioi.llmdetect.features import C_FAMILY, LIBRARY_NAMES, keywords_of, lex

# Values typical of LLM code, for habits where there is one. A change
# towards them weighs more than a change in the other direction.
LLM_VALUES = {
    "includes": {"separate"},
    "namespace": {"std::"},
    "ll_alias": {"none"},
    "fast_io": {"ios::sync_with_stdio(false); cin.tie(nullptr)", "std::ios::sync_with_stdio(false); std::cin.tie(nullptr)"},
    "storage": {"vectors"},
    "main": {"int main()"},
    "test_loop": {"plain"},
    "indent": {"4 spaces"},
    "braces": {"same line"},
    "else": {"} else {"},
    "keyword_space": {"if ("},
    "operator_space": {"spaced"},
    "comma_space": {", "},
    "naming": {"camelCase", "snake_case"},
    "function_names": {"descriptive"},
}

CATEGORICAL_HABITS = (
    "includes",
    "namespace",
    "ll_alias",
    "fast_io",
    "storage",
    "size_constant",
    "main",
    "test_loop",
    "line_endings",
    "indent",
    "braces",
    "else",
    "keyword_space",
    "operator_space",
    "comma_space",
    "naming",
    "function_names",
)

# Leftovers of a chat answer. Searched in comments (and ``` anywhere).
CHAT_PATTERNS = (
    ("markdown fence", re.compile(r"```")),
    ("here is", re.compile(r"\bhere(?:'s| is)\b", re.I)),
    ("explanation", re.compile(r"\bexplanation\s*:", re.I)),
    ("complexity", re.compile(r"\b(?:time|space)\s+complexity\b", re.I)),
    ("big O", re.compile(r"\bO\((?:n|m|k|q|log|1|\w\s*[\*\+])[^)]*\)")),
    ("step N", re.compile(r"\bstep\s*\d+\s*[:.)]", re.I)),
    ("example usage", re.compile(r"\bexample usage\b", re.I)),
    ("markdown", re.compile(r"\*\*\w[^*]*\*\*|`\w[^`]*`")),
    ("approach", re.compile(r"\b(?:approach|intuition)\s*:", re.I)),
    ("edge case", re.compile(r"\bedge cases?\b", re.I)),
)

# Characters a web page or chat interface leaves in copied text.
PASTE_CHARACTERS = {
    " ": "no-break space",
    " ": "narrow no-break space",
    " ": "figure space",
    "​": "zero-width space",
    "‌": "zero-width non-joiner",
    "‍": "zero-width joiner",
    "⁠": "word joiner",
    "﻿": "zero-width no-break space",
    "“": "typographic quote",
    "”": "typographic quote",
    "‘": "typographic quote",
    "’": "typographic quote",
    "—": "em dash",
    "–": "en dash",
    "→": "arrow",
    "≤": "≤ sign",
    "≥": "≥ sign",
}

_FUNCTION_RE = re.compile(
    r"^\s*(?:template\s*<[^>]*>\s*)?(?:[A-Za-z_][\w:<>,]*[\s\*&]+)+\**&?([A-Za-z_]\w*)"
    r"\s*\(([^;{}]*)\)\s*(?:const\s*)?(?:->[^{;]*)?(?:\{.*)?$"
)
_GLOBAL_ARRAY_RE = re.compile(r"^\s*(?:static\s+)?(?:const\s+)?[A-Za-z_][\w:<>]*(?:\s+|\s*[\*&]\s*)[A-Za-z_]\w*\s*\[[^\]]+\]")
_SIZE_CONSTANT_RE = re.compile(
    r"^\s*(?:(?:static\s+)?(?:const|constexpr)\s+(?:int|long long|ll|long|unsigned|size_t)\s+([A-Za-z_]\w*)\s*=\s*([^;]+);"
    r"|#define\s+([A-Za-z_]\w*)\s+(\S+))"
)
_SPACING_RE = re.compile(r"\s*([^\w\s])\s*")
_NOT_FUNCTIONS = frozenset({"if", "for", "while", "switch", "return", "else", "do", "sizeof", "catch"})


def strip_comments_and_strings(source):
    """C-family code with comments blanked and string contents removed,
    keeping the line structure.
    """
    out = []
    i = 0
    n = len(source)
    while i < n:
        c = source[i]
        two = source[i : i + 2]
        if two == "//":
            end = source.find("\n", i)
            i = n if end == -1 else end
            continue
        if two == "/*":
            end = source.find("*/", i + 2)
            end = n if end == -1 else end + 2
            out.append("\n" * source.count("\n", i, end))
            i = end
            continue
        if c in "\"'":
            j = i + 1
            while j < n and source[j] != c and source[j] != "\n":
                j += 2 if source[j] == "\\" else 1
            out.append(c + c)
            i = j + 1
            continue
        out.append(c)
        i += 1
    return "".join(out)


def _majority(yes, no, labels):
    total = yes + no
    if not total:
        return None
    share = yes / total
    if share >= 0.7:
        return labels[0]
    if share <= 0.3:
        return labels[1]
    return "mixed"


def _top_level_lines(code):
    """(line, depth at its start) for every line of ``code``."""
    depth = 0
    result = []
    for line in code.split("\n"):
        result.append((line, depth))
        depth = max(0, depth + line.count("{") - line.count("}"))
    return result


def _functions(lines):
    """Names of the functions defined at the top level (or inside a
    namespace or struct), with the index of their first line.
    """
    found = []
    for index, (line, depth) in enumerate(lines):
        if depth > 1 or line.lstrip().startswith("#"):
            continue
        match = _FUNCTION_RE.match(line)
        if match and match.group(1) not in _NOT_FUNCTIONS:
            following = lines[index + 1][0].strip() if index + 1 < len(lines) else ""
            if "{" in line or following.startswith("{"):
                found.append((match.group(1), index))
    return found


def _ll_alias(code):
    if re.search(r"#define\s+int\s+long\s+long", code):
        return "#define int long long"
    if re.search(r"#define\s+\w+\s+long\s+long", code):
        return "#define ll long long"
    if re.search(r"typedef\s+long\s+long\s+\w+\s*;", code):
        return "typedef long long ll;"
    if re.search(r"using\s+\w+\s*=\s*long\s+long\s*;", code):
        return "using ll = long long;"
    return "none"


def _fast_io(code):
    sync = re.search(r"((?:std::)?ios(?:_base)?)::sync_with_stdio\s*\(\s*(\w+)\s*\)", code)
    tie = re.search(r"((?:std::)?cin)\s*\.\s*tie\s*\(\s*(\w+)\s*\)", code)
    if not sync and not tie:
        return None
    parts = []
    if sync:
        value = "false" if sync.group(2) in ("false", "False") else "0" if sync.group(2) == "0" else sync.group(2)
        parts.append(f"{sync.group(1)}::sync_with_stdio({value})")
    if tie:
        parts.append(f"{tie.group(1)}.tie({tie.group(2)})")
    if re.search(r"cout\s*\.\s*tie\s*\(", code):
        parts.append("cout.tie")
    return "; ".join(parts)


def _size_constant(lines):
    for line, depth in lines:
        if depth:
            continue
        match = _SIZE_CONSTANT_RE.match(line)
        if not match:
            continue
        name = match.group(1) or match.group(3)
        value = (match.group(2) or match.group(4) or "").replace(" ", "")
        if not re.search(r"\d", value) or name in ("ll", "int"):
            continue
        number = int(re.match(r"\d+", value).group(0)) if re.match(r"\d+(?![e.])", value) else None
        if "e" in value.lower() and re.match(r"\d+(?:\.\d+)?e\d", value.lower()):
            form = "1eX" + ("+c" if "+" in value else "")
        elif number is not None and number >= 1000:
            form = "digits" + ("+c" if "+" in value else "")
        else:
            continue
        shape = name if name in ("N", "MAXN", "MAX", "M", "MX", "MAX_N", "maxn", "nax", "LIM") else ("UPPER" if name.isupper() else "other")
        return f"{shape} = {form}"
    return None


def _main_signature(code):
    match = re.search(r"\b(int|signed|auto|void)\s+main\s*\(([^)]*)\)", code)
    if not match:
        return None
    args = match.group(2).strip()
    if args == "void":
        return f"{match.group(1)} main(void)"
    if args:
        return f"{match.group(1)} main(argc, argv)"
    return f"{match.group(1)} main()"


def _test_loop(code, function_names):
    solve = any(name in ("solve", "testcase", "test_case", "rozwiaz") for name in function_names)
    loop = bool(re.search(r"while\s*\(\s*\w+\s*--\s*\)|for\s*\([^;]*;\s*\w+\s*<=?\s*(?:t|T|tests|q|Q)\s*;", code))
    if solve and loop:
        return "solve() in a test loop"
    if solve:
        return "solve()"
    if loop:
        return "test loop in main"
    return "plain"


def _indent(raw_lines):
    tabs = spaces = 0
    widths = {}
    for line in raw_lines:
        if not line.strip() or not line[:1].isspace():
            continue
        if line.startswith("\t"):
            tabs += 1
            continue
        spaces += 1
        width = len(line) - len(line.lstrip(" "))
        for unit in (8, 4, 3, 2):
            if width % unit == 0:
                widths[unit] = widths.get(unit, 0) + 1
                break
    if tabs + spaces < 3:
        return None
    if tabs > spaces:
        return "tabs"
    unit = max(widths, key=widths.get) if widths else 1
    return f"{unit} spaces"


# Standard names that are not the author's choice, besides LIBRARY_NAMES.
_STANDARD_NAMES = frozenset(
    """
    ios_base nullptr NULL size_t int64_t uint64_t int32_t INT_MAX INT_MIN LLONG_MAX LLONG_MIN ULLONG_MAX EOF stdout stderr
    string_view unordered_multiset unordered_multimap iterator const_iterator value_type to_string push_front pop_front
    """.split()
)


def _identifiers(tokens, language):
    keywords = keywords_of(language)
    return [text for kind, text in tokens if kind == "id" and text not in keywords and text not in LIBRARY_NAMES and text not in _STANDARD_NAMES]


def _naming(identifiers):
    distinct = set(identifiers)
    if len(distinct) < 4:
        return None
    longer = [i for i in distinct if len(i) >= 3]
    camel = sum(bool(re.search(r"[a-z][A-Z]", i)) for i in longer)
    snake = sum("_" in i.strip("_") for i in longer)
    if len(longer) >= 3 and camel / len(longer) >= 0.25:
        return "camelCase"
    if len(longer) >= 3 and snake / len(longer) >= 0.25:
        return "snake_case"
    if sum(len(i) <= 2 for i in distinct) / len(distinct) >= 0.5:
        return "short names"
    return "plain words"


def _function_names(names):
    names = [n for n in names if n != "main"]
    if not names:
        return None
    multiword = sum(bool(re.search(r"[a-z][A-Z]", n)) or "_" in n.strip("_") for n in names)
    mean = sum(len(n) for n in names) / len(names)
    if multiword or mean >= 9:
        return "descriptive"
    if mean <= 5:
        return "short"
    return "words"


def chat_leftovers(comments, source):
    """Names of chat-answer leftovers found in the code."""
    found = []
    text = "\n".join(comments)
    for name, pattern in CHAT_PATTERNS:
        haystack = source if name == "markdown fence" else text
        if pattern.search(haystack):
            found.append(name)
    return found


def paste_characters(source):
    """Characters typical of text copied from a web page, as names.

    A byte order mark at the very start is an editor setting, not a paste.
    """
    body = source[1:] if source.startswith("﻿") else source
    found = []
    for char, name in PASTE_CHARACTERS.items():
        if char in body and name not in found:
            found.append(name)
    for char in set(body):
        if unicodedata.category(char) == "Cf" and char not in PASTE_CHARACTERS and char != "﻿":
            found.append("invisible character")
            break
    return found


def extract_habits(source, language, pasted=False):
    """Habits of a program: ``{"values": {habit: value}, "template": [...],
    "macros": [...], "identifiers": [...], "chat": [...], "paste_chars": [...]}``.
    """
    tokens, comments = lex(source, language)
    identifiers = _identifiers(tokens, language)
    result = {
        "values": {},
        "template": [],
        "macros": [],
        "identifiers": sorted(set(identifiers))[:400],
        "chat": chat_leftovers(comments, source),
        "paste_chars": paste_characters(source),
    }
    values = result["values"]
    raw_lines = source.split("\n")
    # Text pasted into a form always has CRLF line ends, whatever the author uses.
    if not pasted:
        crlf = source.count("\r\n")
        values["line_endings"] = "CRLF" if crlf > source.count("\n") / 2 else "LF"
    values["indent"] = _indent([line.rstrip("\r") for line in raw_lines])
    values["naming"] = _naming(identifiers)
    if language != C_FAMILY:
        return result

    code = strip_comments_and_strings(source.replace("\r\n", "\n"))
    lines = _top_level_lines(code)
    functions = _functions(lines)
    names = [name for name, _index in functions]
    first_function = functions[0][1] if functions else len(lines)
    template = []
    for line, _depth in lines[:first_function]:
        # Spacing is a habit of its own; here only the content counts.
        stripped = _SPACING_RE.sub(r"\1", " ".join(line.split()))
        if stripped and stripped not in ("{", "}", "};"):
            template.append(stripped)
    result["template"] = template[:60]
    result["macros"] = sorted({m.group(1) for m in re.finditer(r"#define\s+([A-Za-z_]\w*)", code)})

    pp = [line.strip() for line, _depth in lines if line.strip().startswith("#include")]
    if pp:
        values["includes"] = "bits/stdc++.h" if any("bits/stdc++" in line for line in pp) else "separate"
    if re.search(r"using\s+namespace\s+std\s*;", code):
        values["namespace"] = "using namespace std;"
    elif "std::" in code:
        values["namespace"] = "std::"
    values["ll_alias"] = _ll_alias(code) if re.search(r"long\s+long|\bll\b|int64_t", code) else None
    values["fast_io"] = _fast_io(code)
    global_arrays = sum(1 for line, depth in lines if depth == 0 and _GLOBAL_ARRAY_RE.match(line))
    uses_vectors = "vector<" in code.replace(" ", "")
    values["storage"] = "global arrays" if global_arrays else ("vectors" if uses_vectors else None)
    values["size_constant"] = _size_constant(lines)
    values["main"] = _main_signature(code)
    values["test_loop"] = _test_loop(code, names) if values["main"] else None
    opening = [line.strip() for line in code.split("\n") if "{" in line]
    if len(opening) >= 3:
        own = sum(line == "{" for line in opening)
        values["braces"] = _majority(own, len(opening) - own, ("own line", "same line"))
    same_else = len(re.findall(r"\}\s*else\b", code.replace("\n", " ").replace("\r", "")))
    next_line_else = len(re.findall(r"\}[ \t]*\n\s*else\b", code))
    if same_else:
        values["else"] = _majority(same_else - next_line_else, next_line_else, ("} else {", "}\\nelse"))
    spaced_kw = len(re.findall(r"\b(?:if|for|while|switch)\s+\(", code))
    tight_kw = len(re.findall(r"\b(?:if|for|while|switch)\(", code))
    values["keyword_space"] = _majority(spaced_kw, tight_kw, ("if (", "if("))
    spaced_ops = len(re.findall(r"\w\s+(?:==|!=|<=|>=|\+=|-=|=|\+|-|\*|/|%|<|>)\s+\w", code))
    tight_ops = len(re.findall(r"\w(?:==|!=|<=|>=|\+=|-=|=|\+|\*|/|%)\w", code))
    values["operator_space"] = _majority(spaced_ops, tight_ops, ("spaced", "tight"))
    spaced_commas = len(re.findall(r",[ \t]\S", code))
    tight_commas = len(re.findall(r",[^\s]", code))
    values["comma_space"] = _majority(spaced_commas, tight_commas, (", ", ","))
    values["function_names"] = _function_names(names)
    return result
