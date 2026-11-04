"""Human-readable names and explanations of the signals."""

from django.utils.translation import gettext
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
        return gettext("Typical of LLM code: %(reasons)s.") % {"reasons": reasons} if reasons else ""
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


def profile_features(assessment):
    top = assessment.context.get("profile", {}).get("top", [])
    return [feature_label(STYLE_FEATURES[j]) for j in top if j < len(STYLE_FEATURES)]
