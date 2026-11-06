from django import forms
from django.conf import settings
from django.urls import reverse
from django.utils.translation import gettext
from django.utils.translation import gettext_lazy as _

from oioioi.programs.models import ContestCompiler, ContestLanguagesConfig
from oioioi.programs.utils import get_submittable_languages


class CompilerInlineForm(forms.ModelForm):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.language_choices = [("", "Choose language")]
        default_compilers = settings.DEFAULT_COMPILERS
        model = self._meta.model
        submittable_languages = get_submittable_languages().items()
        if model == ContestCompiler:
            self.language_choices = [
                (
                    lang,
                    lang_info["display_name"] + gettext(" (default compiler: ") + default_compilers.get(lang) + ")",
                )
                for lang, lang_info in submittable_languages
            ]
        else:
            self.language_choices = [(lang, lang_info["display_name"]) for lang, lang_info in submittable_languages]

        final_compiler_choices = [("", "Choose language first")]
        if kwargs.get("instance"):
            instance = kwargs.get("instance")
            available_compilers = getattr(settings, "AVAILABLE_COMPILERS", {})
            compilers_for_lang = available_compilers.get(instance.language, [])
            # we can't just add all the compilers_for_lang, because first one
            # is the default option, so it has to be current compiler
            final_compiler_choices = [(instance.compiler, instance.compiler)] + [
                (compiler, compiler) for compiler in compilers_for_lang if compiler != instance.compiler
            ]
        self.fields["compiler"].widget = forms.Select(choices=final_compiler_choices)
        self.fields["language"] = forms.ChoiceField(choices=self.language_choices)
        self.fields["language"].widget.attrs.update({"data-compilerchoicesurl": reverse("get_compiler_hints")})

    class Media:
        js = ("common/choose_compiler.js",)


class ProblemCompilerInlineForm(CompilerInlineForm):
    def save(self, commit=True):
        instance = super(CompilerInlineForm, self).save(commit=False)
        instance.auto_created = False
        if commit:
            instance.save()
        return instance


class ProblemAllowedLanguageInlineForm(forms.ModelForm):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        submittable_languages = get_submittable_languages().items()
        self.language_choices = [(lang, lang_info["display_name"]) for lang, lang_info in submittable_languages]
        self.fields["language"] = forms.ChoiceField(choices=self.language_choices)
        self.fields["language"].widget.attrs.update({"data-compilerchoicesurl": reverse("get_compiler_hints")})


class ContestLanguagesConfigForm(forms.ModelForm):
    allowed_languages = forms.MultipleChoiceField(
        required=False,
        widget=forms.CheckboxSelectMultiple,
        label=_("Allowed languages"),
        help_text=_("Leave all unchecked to allow every language."),
    )

    class Meta:
        model = ContestLanguagesConfig
        fields = ["allowed_languages"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["allowed_languages"].choices = [(lang, info["display_name"]) for lang, info in get_submittable_languages().items()]
