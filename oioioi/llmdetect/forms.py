from django import forms
from django.utils.translation import gettext_lazy as _
from django.utils.translation import pgettext_lazy

from oioioi.contests.models import ProblemInstance
from oioioi.llmdetect.models import LLMDetectConfig, ReviewDecision

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
    code = forms.CharField(widget=forms.Textarea(attrs={"rows": 6, "class": "font-monospace"}), required=False, label=_("Or paste code"))

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
            sources.append((file.name, file.read().decode("utf-8", "replace")))
        if cleaned.get("code", "").strip():
            sources.append(("solution.cpp", cleaned["code"]))
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


class GenerateForm(forms.Form):
    problem_instance = forms.ModelChoiceField(queryset=ProblemInstance.objects.none(), label=_("Problem"))
    statement = forms.CharField(
        widget=forms.Textarea(attrs={"rows": 12, "class": "font-monospace small"}),
        label=_("Statement sent to the models"),
        help_text=_("Exactly this text is sent, with a short instruction. Check it, and remove anything that must not leave the server."),
        max_length=60000,
    )
    models = forms.MultipleChoiceField(widget=forms.CheckboxSelectMultiple, label=pgettext_lazy("llmdetect", "Models"))
    prompts = forms.MultipleChoiceField(widget=forms.CheckboxSelectMultiple, label=_("Prompts"))
    count = forms.IntegerField(min_value=1, max_value=20, initial=2, label=_("Solutions per model and prompt"))

    def __init__(self, *args, contest, **kwargs):
        from oioioi.llmdetect.generation import PROMPTS, model_choices

        super().__init__(*args, **kwargs)
        problems = ProblemInstance.objects.filter(contest=contest).select_related("round")
        self.fields["problem_instance"].queryset = problems.order_by("round__start_date", "short_name")
        self.fields["models"].choices = model_choices()
        self.fields["prompts"].choices = [(key, label) for key, (label, _instruction) in PROMPTS.items()]
        self.fields["prompts"].initial = list(PROMPTS)
