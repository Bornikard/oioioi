from django import forms
from django.utils.translation import gettext_lazy as _

from oioioi.contests.models import Contest, ProblemInstance
from oioioi.llmdetect.models import LLMDetectConfig, ReviewDecision
from oioioi.llmdetect.prompts import HEADER_RE, PROMPTS, code_of_chat_answer

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


def administered_contests(user, exclude):
    """Contests other than ``exclude`` whose LLM detection ``user`` may see:
    the profiles of a contest show code from its history contests.
    """
    contests = Contest.objects.exclude(id=exclude.id).order_by("-creation_date")
    if user.is_superuser:
        return contests
    return contests.filter(id__in=[contest.id for contest in contests if user.has_perm("contests.contest_admin", contest)])


class SettingsForm(forms.ModelForm):
    class Meta:
        model = LLMDetectConfig
        fields = ["trusted_rounds", "history_contests", "history_weight", "time_from_statement"]
        widgets = {"trusted_rounds": forms.CheckboxSelectMultiple, "history_contests": forms.CheckboxSelectMultiple}

    def __init__(self, *args, contest, user, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["trusted_rounds"].queryset = contest.round_set.order_by("start_date")
        allowed = administered_contests(user, contest)
        self.fields["history_contests"].queryset = allowed
        self.fields["history_contests"].label_from_instance = lambda c: c.name
        self.fields["history_weight"].required = False
        # Chosen by another admin in contests this one cannot see: kept as they are.
        self.hidden_history = list(self.instance.history_contests.exclude(id__in=allowed.values("id"))) if self.instance.pk else []

    def clean_history_weight(self):
        weight = self.cleaned_data.get("history_weight")
        return self.instance.history_weight if weight is None else weight

    def save(self, commit=True):
        config = super().save(commit=commit)
        if commit and self.hidden_history:
            config.history_contests.add(*self.hidden_history)
        return config


class DecisionForm(forms.ModelForm):
    class Meta:
        model = ReviewDecision
        fields = ["verdict", "submission", "note"]
        widgets = {
            "verdict": forms.Select(attrs={"class": "form-select form-select-sm"}),
            "submission": forms.Select(attrs={"class": "form-select form-select-sm"}),
            "note": forms.Textarea(attrs={"rows": 2, "class": "form-control form-control-sm"}),
        }

    def __init__(self, *args, submissions, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["submission"].queryset = submissions
        self.fields["submission"].required = False
        self.fields["submission"].label_from_instance = lambda s: f"#{s.id} {s.problem_instance.short_name} {s.date:%d.%m %H:%M}"
        self.fields["submission"].help_text = _("A confirmed submission becomes an LLM reference of its problem.")


def problems_of(contest):
    return ProblemInstance.objects.filter(contest=contest).select_related("round").order_by("round__start_date", "short_name")


class StatementForm(forms.Form):
    """The statement of a problem, checked by the staff, and the prompts to download."""

    text = forms.CharField(
        widget=forms.Textarea(attrs={"rows": 18, "class": "form-control font-monospace small", "spellcheck": "false"}),
        label=_("Statement in the prompts"),
        help_text=_(
            "The prompts are this text after a short instruction. Check the limits and the examples, and remove anything that must not leave the camp."
        ),
        max_length=60000,
    )
    prompts = forms.MultipleChoiceField(widget=forms.CheckboxSelectMultiple, label=_("Prompts to download"), required=False)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["prompts"].choices = [(key, label) for key, (label, _instruction) in PROMPTS.items()]
        self.fields["prompts"].initial = list(PROMPTS)


MAX_BULK_SIZE = 20 * 1024 * 1024


class AnswersForm(forms.Form):
    """Answers of LLMs for one problem: a chat answer or code pasted as it
    is, files, or many answers in the bulk format (see prompts.py), which
    may also name other problems.
    """

    text = forms.CharField(
        widget=forms.Textarea(attrs={"rows": 10, "class": "form-control font-monospace small", "spellcheck": "false"}),
        required=False,
        label=_("Answers"),
        strip=False,
        help_text=_(
            'A whole chat answer (only its code is kept) or code. Many answers at once: start each with a "### reference" line, as the script writes them.'
        ),
    )
    files = MultipleFileField(required=False, label=_("Or upload files"))
    model_name = forms.CharField(max_length=100, required=False, label=_("Model"), help_text=_("E.g. ChatGPT free, Gemini 3 Flash."))
    prompt_variant = forms.ChoiceField(required=False, label=_("Prompt"))

    def __init__(self, *args, contest, problem_instance, **kwargs):
        super().__init__(*args, **kwargs)
        self.problem_instance = problem_instance
        self.problems = {pi.short_name: pi for pi in problems_of(contest)}
        self.fields["prompt_variant"].choices = [("", "–")] + [(key, label) for key, (label, _instruction) in PROMPTS.items()]

    def _answer(self, text, filename):
        from oioioi.llmdetect.prompts import parse_references

        if HEADER_RE.search(text):
            return parse_references(text, self.problems, self.problem_instance)
        source = code_of_chat_answer(text).strip("\n") + "\n"
        if len(source) > MAX_REFERENCE_SIZE:
            return [], [_("%(name)s is too large.") % {"name": filename}]
        reference = {
            "problem_instance": self.problem_instance,
            "model": self.cleaned_data.get("model_name", ""),
            "prompt": self.cleaned_data.get("prompt_variant", ""),
            "source": source,
            "filename": filename,
        }
        return [reference], []

    def clean(self):
        cleaned = super().clean()
        references, errors = [], []
        for upload in cleaned.get("files") or []:
            if upload.size > MAX_BULK_SIZE:
                errors.append(_("%(name)s is too large.") % {"name": upload.name})
                continue
            found, problems = self._answer(upload.read().decode("utf-8", "replace"), upload.name)
            references += found
            errors += [f"{upload.name}: {error}" for error in problems]
        if (cleaned.get("text") or "").strip():
            found, problems = self._answer(cleaned["text"], "solution.cpp")
            references += found
            errors += problems
        if errors:
            raise forms.ValidationError(errors[:20] + ([_("…and %(count)d more errors.") % {"count": len(errors) - 20}] if len(errors) > 20 else []))
        if not references:
            raise forms.ValidationError(_("Paste an answer or choose files."))
        cleaned["references"] = references
        return cleaned
