"""Human-readable names and explanations of the signals."""

from django.utils.translation import gettext, pgettext_lazy
from django.utils.translation import gettext_lazy as _

from oioioi.llmdetect.features import STYLE_FEATURES

SIGNAL_LABELS = {
    "llm_reference": _("Close to an LLM solution"),
    "llm_style": _("LLM-like style"),
    "style_shift": _("Style shift towards LLM"),
    "deviation": _("Unlike own style"),
    "fast_solve": _("Unusually fast"),
    "burst": _("Much code at once"),
    "rewrite": _("Rewritten from scratch"),
    "first_try": _("Hard problem at first try"),
    "score_surprise": _("Above own level"),
    "pasted": _("Pasted, usually uploads files"),
    "peer_similarity": _("Similar to another participant"),
    "chat_leftovers": _("Leftovers of a chat answer"),
    "paste_characters": _("Characters from a web page"),
    "habit_break": _("Broken habits"),
    "vocabulary": _("Unfamiliar names"),
}

HABIT_LABELS = {
    "template": pgettext_lazy("llmdetect", "template"),
    "macros": pgettext_lazy("llmdetect", "macros"),
    "includes": pgettext_lazy("llmdetect", "includes"),
    "namespace": pgettext_lazy("llmdetect", "std namespace"),
    "ll_alias": pgettext_lazy("llmdetect", "long long alias"),
    "fast_io": pgettext_lazy("llmdetect", "fast IO"),
    "storage": pgettext_lazy("llmdetect", "arrays"),
    "size_constant": pgettext_lazy("llmdetect", "size constant"),
    "main": pgettext_lazy("llmdetect", "main"),
    "test_loop": pgettext_lazy("llmdetect", "test cases"),
    "line_endings": pgettext_lazy("llmdetect", "line endings"),
    "indent": pgettext_lazy("llmdetect", "indentation"),
    "braces": pgettext_lazy("llmdetect", "braces"),
    "else": pgettext_lazy("llmdetect", "else"),
    "keyword_space": pgettext_lazy("llmdetect", "space after if/for"),
    "operator_space": pgettext_lazy("llmdetect", "spaces around operators"),
    "comma_space": pgettext_lazy("llmdetect", "space after commas"),
    "naming": pgettext_lazy("llmdetect", "naming"),
    "function_names": pgettext_lazy("llmdetect", "function names"),
}

# Habit values that are descriptions, not code.
HABIT_VALUE_LABELS = {
    "separate": pgettext_lazy("llmdetect", "separate headers"),
    "global arrays": pgettext_lazy("llmdetect", "global arrays"),
    "vectors": pgettext_lazy("llmdetect", "local vectors"),
    "none": pgettext_lazy("llmdetect", "none"),
    "plain": pgettext_lazy("llmdetect", "single test"),
    "solve()": pgettext_lazy("llmdetect", "solve()"),
    "test loop in main": pgettext_lazy("llmdetect", "test loop in main"),
    "solve() in a test loop": pgettext_lazy("llmdetect", "solve() in a test loop"),
    "tabs": pgettext_lazy("llmdetect", "tabs"),
    "own line": pgettext_lazy("llmdetect", "on their own line"),
    "same line": pgettext_lazy("llmdetect", "on the same line"),
    "mixed": pgettext_lazy("llmdetect", "mixed"),
    "spaced": pgettext_lazy("llmdetect", "spaces"),
    "tight": pgettext_lazy("llmdetect", "no spaces"),
    "short names": pgettext_lazy("llmdetect", "short names"),
    "camelCase": "camelCase",
    "snake_case": "snake_case",
    "plain words": pgettext_lazy("llmdetect", "plain words"),
    "descriptive": pgettext_lazy("llmdetect", "descriptive"),
    "short": pgettext_lazy("llmdetect", "short"),
    "words": pgettext_lazy("llmdetect", "words"),
}

CHAT_LABELS = {
    "markdown fence": _("a ``` code fence"),
    "here is": _('"Here is"'),
    "explanation": _('"Explanation:"'),
    "complexity": _('"Time complexity"'),
    "big O": _("O(...) notation"),
    "step N": _('"Step 1:"'),
    "example usage": _('"Example usage"'),
    "markdown": _("markdown formatting"),
    "approach": _('"Approach:"'),
    "edge case": _('"edge cases"'),
}

PASTE_CHAR_LABELS = {
    "no-break space": _("no-break spaces"),
    "narrow no-break space": _("narrow no-break spaces"),
    "figure space": _("figure spaces"),
    "zero-width space": _("zero-width spaces"),
    "zero-width non-joiner": _("zero-width characters"),
    "zero-width joiner": _("zero-width characters"),
    "word joiner": _("zero-width characters"),
    "zero-width no-break space": _("zero-width characters"),
    "invisible character": _("invisible characters"),
    "typographic quote": _("typographic quotes"),
    "em dash": _("em dashes"),
    "en dash": _("en dashes"),
    "arrow": _("arrows"),
    "≤ sign": _("≤ signs"),
    "≥ sign": _("≥ signs"),
}

FEATURE_LABELS = {
    "comment_ratio": _("comments per line"),
    "prose_comment_ratio": _("prose comments"),
    "sentence_comment_ratio": _("full-sentence comments"),
    "english_comment_ratio": _("English comments"),
    "commented_code_ratio": _("commented-out code"),
    "comment_words_per_100": _("words in comments"),
    "ident_mean_length": _("identifier length"),
    "long_ident_ratio": _("long identifiers"),
    "multiword_ident_ratio": _("multi-word identifiers"),
    "short_ident_ratio": _("one- or two-letter identifiers"),
    "upper_ident_ratio": _("upper-case identifiers"),
    "std_qualified_per_100": _("std:: prefixes"),
    "using_namespace_std": _("using namespace std"),
    "bits_header": _("bits/stdc++.h"),
    "includes_per_100": _("includes"),
    "defines_per_100": _("macros"),
    "type_aliases_per_100": _("type aliases"),
    "fast_io": _("fast IO"),
    "debug_traces_per_100": _("debug output"),
    "return_zero": _("return 0 at the end"),
    "endl_ratio": _("endl instead of \\n"),
    "printf_ratio": _("printf/scanf"),
    "const_per_100": _("const"),
    "auto_per_100": _("auto"),
    "long_long_spelled_per_100": _("long long spelled out"),
    "reference_params_per_100": _("const references"),
    "brace_own_line_ratio": _("braces on own line"),
    "tab_indent_ratio": _("tab indentation"),
    "indent_unit": _("indentation width"),
    "blank_line_ratio": _("blank lines"),
    "mean_line_length": _("line length"),
    "spaced_operator_ratio": _("spaces around operators"),
    "multi_statement_line_ratio": _("several statements per line"),
    "trailing_space_ratio": _("trailing spaces"),
}


def feature_label(name):
    return FEATURE_LABELS.get(name, name)


def habit_label(name):
    return HABIT_LABELS.get(name, name)


def habit_value(value, quote=False):
    """A habit value for display: lists joined, descriptions translated,
    code quoted if ``quote``.
    """
    if value is None:
        return "–"
    if isinstance(value, list):
        if not value:
            return "–"
        return " · ".join(f'"{v}"' if quote else v for v in value)
    if value.endswith(" spaces") and value[:1].isdigit():
        return gettext("%(width)s spaces") % {"width": value.split()[0]}
    if value in HABIT_VALUE_LABELS:
        return str(HABIT_VALUE_LABELS[value])
    return f'"{value}"' if quote else value


def describe_habit(row):
    if row["habit"] in ("template", "macros"):
        return gettext("%(habit)s: kept %(share)s of the usual lines (%(usual)s)") % {
            "habit": habit_label(row["habit"]),
            "share": _percent(row["share"]),
            "usual": habit_value(row["usual"][:3], quote=True),
        }
    return gettext("%(habit)s: usually %(usual)s (%(share)s), now %(current)s") % {
        "habit": habit_label(row["habit"]),
        "usual": habit_value(row["usual"], quote=True),
        "share": _percent(row["share"]),
        "current": habit_value(row["current"], quote=True),
    }


def _percent(value):
    return f"{100 * value:.0f}%"


def _z(value):
    return "10+" if value > 10 else f"{value:.1f}"


def describe(name, detail, users=None):
    """One sentence about why the signal fired, for the staff."""
    users = users or {}
    d = detail or {}
    if name == "llm_reference":
        text = gettext("%(similarity)s of the code matches an LLM reference (participants usually: %(baseline)s).") % {
            "similarity": _percent(d.get("similarity", 0)),
            "baseline": _percent(d.get("baseline", 0)),
        }
        if d.get("model"):
            text += " " + gettext("Model: %(model)s.") % {"model": d["model"]}
        return text
    if name == "llm_style":
        reasons = ", ".join(str(feature_label(reason)) for reason in d.get("reasons", []))
        if "score" not in d:  # computed before the score was stored
            return gettext("Typical of LLM code: %(reasons)s.") % {"reasons": reasons} if reasons else ""
        parts = [gettext("LLM-likeness of the code: %(score)s.") % {"score": _percent(d["score"])}]
        if reasons:
            parts.append(gettext("Like LLM code: %(reasons)s.") % {"reasons": reasons})
        against = ", ".join(str(feature_label(reason)) for reason in d.get("against", []))
        if against:
            parts.append(gettext("Unlike LLM code: %(reasons)s.") % {"reasons": against})
        return " ".join(parts)
    if name == "style_shift":
        return gettext("Far from own style (z = %(z)s) and closer to LLM style (%(style)s, usually %(profile)s).") % {
            "z": _z(d.get("z", 0)),
            "style": _percent(d.get("style", 0)),
            "profile": _percent(d.get("profile_style", 0)),
        }
    if name == "deviation":
        return gettext("Far from own style (z = %(z)s), profile confidence %(confidence)s.") % {
            "z": _z(d.get("z", 0)),
            "confidence": _percent(d.get("confidence", 0)),
        }
    if name == "fast_solve":
        return gettext("Solved after %(minutes).0f min; others needed %(median).0f min (median).") % d
    if name == "burst":
        return gettext("%(lines)d new lines in %(minutes).0f min.") % d
    if name == "rewrite":
        return gettext("Only %(similarity)s in common with the previous, failed version.") % {"similarity": _percent(d.get("similarity", 0))}
    if name == "first_try":
        return gettext("Full score at the first submission; %(rate)s of participants solved it.") % {"rate": _percent(d.get("solve_rate", 0))}
    if name == "score_surprise":
        return gettext("Solved by %(rate)s of participants; on other problems this participant is better than %(strength)s of them.") % {
            "rate": _percent(d.get("solve_rate", 0)),
            "strength": _percent(d.get("strength", 0)),
        }
    if name == "pasted":
        return gettext("Pasted into the form; pastes only %(rate)s of other submissions.") % {"rate": _percent(d.get("rate", 0))}
    if name == "chat_leftovers":
        return gettext("Found: %(found)s.") % {"found": ", ".join(str(CHAT_LABELS.get(f, f)) for f in d.get("found", []))}
    if name == "paste_characters":
        return gettext("Found: %(found)s.") % {"found": ", ".join(sorted({str(PASTE_CHAR_LABELS.get(f, f)) for f in d.get("found", [])}))}
    if name == "habit_break":
        return "; ".join(describe_habit(row) for row in d.get("broken", [])) + "."
    if name == "vocabulary":
        return gettext("%(overlap)s of the names were used before by this participant (usually %(median)s). New: %(new)s.") % {
            "overlap": _percent(d.get("overlap", 0)),
            "median": _percent(d.get("median", 0)),
            "new": ", ".join(d.get("new", [])),
        }
    if name == "peer_similarity":
        user = users.get(d.get("user"))
        return gettext("%(similarity)s in common with a submission of %(user)s.") % {
            "similarity": _percent(d.get("similarity", 0)),
            "user": user.get_full_name() or user.username if user else "?",
        }
    return ""


def signal_rows(assessment, users=None):
    """Signals of a :class:`RiskAssessment`, largest contribution first."""
    rows = []
    for name, signal in assessment.signals.items():
        rows.append(
            {
                "name": name,
                "label": SIGNAL_LABELS.get(name, name),
                "value": signal["value"],
                "contribution": signal["contribution"],
                "text": describe(name, signal.get("detail"), users),
                "detail": signal.get("detail") or {},
            }
        )
    rows.sort(key=lambda row: -row["contribution"])
    return rows


def habit_rows(assessment):
    """Every habit compared for a submission, broken ones first."""
    rows = []
    for row in assessment.context.get("habits", []):
        rows.append(
            {
                "label": habit_label(row["habit"]),
                "usual": habit_value(row["usual"]),
                "share": row["share"],
                "current": habit_value(row["current"]),
                "broken": row["broken"],
                "counted": bool(row.get("strength")),
                "problem": row.get("problem", False),
                "set": row["habit"] in ("template", "macros"),
            }
        )
    rows.sort(key=lambda row: (not row["broken"], str(row["label"])))
    return rows


def profile_features(assessment):
    top = assessment.context.get("profile", {}).get("top", [])
    return [feature_label(STYLE_FEATURES[j]) for j in top if j < len(STYLE_FEATURES)]
