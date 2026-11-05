from django import forms
from django.utils.translation import gettext_lazy as _

from oioioi.contests.models import ProblemInstance
from oioioi.llmdetect.models import LLMDetectConfig, ReviewDecision
from oioioi.llmdetect.prompts import PROMPTS, code_of_chat_answer

MAX_REFERENCE_SIZE = 256 * 1024


class MultipleFileInput(forms.ClearableFileInput):
    allow_multiple_selected = True


class MultipleFileField(forms.FileField):
    def __init__(self, *args, **kwargs):
        kwargs.setdefault("widget", MultipleFileInput())
        super().__init__(*args, **kwargs)

    def clean(self, data, initial=None):
        single = super().clean
        if isinstance(data, (list, tuple)):
            return [single(file, initial) for file in data]
        return [single(data, initial)] if data else []


class SettingsForm(forms.ModelForm):
    class Meta:
        model = LLMDetectConfig
        fields = ["trusted_rounds"]
        widgets = {"trusted_rounds": forms.CheckboxSelectMultiple}

    def __init__(self, *args, contest, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["trusted_rounds"].queryset = contest.round_set.order_by("start_date")


class ReferenceForm(forms.Form):
    problem_instance = forms.ModelChoiceField(queryset=ProblemInstance.objects.none(), label=_("Problem"))
    model_name = forms.CharField(max_length=100, required=False, label=_("Model"))
    prompt_variant = forms.CharField(max_length=100, required=False, label=_("Prompt"))
    files = MultipleFileField(required=False, label=_("Files"))
    code = forms.CharField(
        widget=forms.Textarea(attrs={"rows": 6, "class": "font-monospace"}),
        required=False,
        label=_("Or paste code"),
        help_text=_("A whole chat answer can be pasted: only its code block is kept."),
    )

    def __init__(self, *args, contest, **kwargs):
        super().__init__(*args, **kwargs)
        problems = ProblemInstance.objects.filter(contest=contest).select_related("round")
        self.fields["problem_instance"].queryset = problems.order_by("round__start_date", "short_name")

    def clean(self):
        cleaned = super().clean()
        sources = []
        for file in cleaned.get("files") or []:
            if file.size > MAX_REFERENCE_SIZE:
                raise forms.ValidationError(_("%(name)s is too large.") % {"name": file.name})
            sources.append((file.name, code_of_chat_answer(file.read().decode("utf-8", "replace"))))
        if cleaned.get("code", "").strip():
            sources.append(("solution.cpp", code_of_chat_answer(cleaned["code"])))
        if not sources:
            raise forms.ValidationError(_("Choose files or paste code."))
        cleaned["sources"] = sources
        return cleaned


class DecisionForm(forms.ModelForm):
    class Meta:
        model = ReviewDecision
        fields = ["verdict", "submission", "note"]
        widgets = {"note": forms.Textarea(attrs={"rows": 2})}

    def __init__(self, *args, submissions, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["submission"].queryset = submissions
        self.fields["submission"].required = False
        self.fields["submission"].label_from_instance = lambda s: f"#{s.id} {s.problem_instance.short_name} {s.date:%d.%m %H:%M}"
        self.fields["submission"].help_text = _("A confirmed submission becomes an LLM reference of its problem.")


def _problems(contest):
    return ProblemInstance.objects.filter(contest=contest).select_related("round").order_by("round__start_date", "short_name")


class PromptsForm(forms.Form):
    """The statement of a problem, checked by the staff, for the prompts."""

    problem_instance = forms.ModelChoiceField(queryset=ProblemInstance.objects.none(), label=_("Problem"))
    statement = forms.CharField(
        widget=forms.Textarea(attrs={"rows": 12, "class": "font-monospace small"}),
        label=_("Statement in the prompts"),
        help_text=_("The prompts are this text with a short instruction. Check it, and remove anything that must not be sent to an LLM."),
        max_length=60000,
    )
    prompts = forms.MultipleChoiceField(widget=forms.CheckboxSelectMultiple, label=_("Prompts"))

    def __init__(self, *args, contest, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["problem_instance"].queryset = _problems(contest)
        self.fields["prompts"].choices = [(key, label) for key, (label, _instruction) in PROMPTS.items()]
        self.fields["prompts"].initial = list(PROMPTS)


MAX_BULK_SIZE = 20 * 1024 * 1024


class BulkReferencesForm(forms.Form):
    """Many references at once, in the bulk format (see prompts.py)."""

    text = forms.CharField(
        widget=forms.Textarea(attrs={"rows": 10, "class": "font-monospace small"}),
        required=False,
        label=_("Paste"),
        strip=False,
    )
    file = forms.FileField(required=False, label=_("Or upload a file"))
    default_problem = forms.ModelChoiceField(
        queryset=ProblemInstance.objects.none(),
        required=False,
        label=_("Problem for entries without problem="),
    )

    def __init__(self, *args, contest, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["default_problem"].queryset = _problems(contest)
        self.problems = {pi.short_name: pi for pi in _problems(contest)}

    def clean(self):
        from oioioi.llmdetect.prompts import parse_references

        cleaned = super().clean()
        parts = []
        upload = cleaned.get("file")
        if upload:
            if upload.size > MAX_BULK_SIZE:
                raise forms.ValidationError(_("The file is too large."))
            parts.append(upload.read().decode("utf-8", "replace"))
        if (cleaned.get("text") or "").strip():
            parts.append(cleaned["text"])
        if not parts:
            raise forms.ValidationError(_("Paste the references or choose a file."))
        references, errors = parse_references("\n".join(parts), self.problems, cleaned.get("default_problem"))
        if errors:
            raise forms.ValidationError(errors[:20] + ([_("…and %(count)d more errors.") % {"count": len(errors) - 20}] if len(errors) > 20 else []))
        cleaned["references"] = references
        return cleaned
