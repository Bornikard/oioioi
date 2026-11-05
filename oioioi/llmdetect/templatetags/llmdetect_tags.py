from django import template

register = template.Library()


@register.filter
def percent(value):
    try:
        return f"{100 * float(value):.0f}%"
    except (TypeError, ValueError):
        return ""


@register.filter
def risk_class(value):
    """Bootstrap colour of a risk in [0, 1]."""
    value = float(value or 0)
    if value >= 0.7:
        return "danger"
    if value >= 0.5:
        return "warning"
    if value >= 0.25:
        return "info"
    return "secondary"


@register.filter
def bar_width(value, scale=4.0):
    """Width in percent of a bar for a contribution to the logit."""
    try:
        return f"{min(100.0, 100.0 * float(value) / float(scale)):.0f}"
    except (TypeError, ValueError):
        return "0"
