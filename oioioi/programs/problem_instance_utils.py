from django.conf import settings
from django.core.exceptions import ObjectDoesNotExist


def get_contest_allowed_languages(contest):
    """Languages the contest is restricted to, or ``None`` if unrestricted."""
    if contest is None:
        return None
    try:
        return contest.languages_config.allowed_languages or None
    except ObjectDoesNotExist:
        return None


def get_allowed_languages_dict(problem_instance):
    if not hasattr(problem_instance, "_cache"):
        lang_dict = getattr(settings, "SUBMITTABLE_EXTENSIONS", {})
        allowed_langs = problem_instance.problem.controller.get_allowed_languages_for_problem(problem_instance.problem)
        contest_langs = problem_instance.controller.get_allowed_languages()
        contest_restriction = get_contest_allowed_languages(problem_instance.contest)
        problem_instance._cache = {
            lang: lang_dict[lang]
            for lang in lang_dict
            if lang in allowed_langs and lang in contest_langs and (contest_restriction is None or lang in contest_restriction)
        }
    return problem_instance._cache


def get_allowed_languages_extensions(problem_instance):
    lang_exts = list(get_allowed_languages_dict(problem_instance).values())
    return [ext for lang in lang_exts for ext in lang]


def get_language_by_extension(problem_instance, ext):
    for lang, extension_list in get_allowed_languages_dict(problem_instance).items():
        if ext in extension_list:
            return lang
    return None
