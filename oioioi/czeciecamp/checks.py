"""Deployment checks of the camp server, run by ``manage.py check --deploy``.

Django's own deployment checks cover DEBUG, cookies and HTTPS; these cover
the SIO2 settings that decide whether participants' code is contained.
"""

from django.conf import settings
from django.core.checks import Error, Tags, Warning, register

# The SECRET_KEY default in oioioi/default_settings.py, public in the repository.
PUBLIC_SECRET_KEYS = {"eca76a75-2b9f-4e09-8f88-86671acbed8b", "__SECRET__", ""}
# Apps that let users who are not trusted staff upload problem packages.
UNTRUSTED_UPLOADER_APPS = ("oioioi.teachers", "oioioi.usercontests")


@register(Tags.security, deploy=True)
def check_sandbox(app_configs, **kwargs):
    if getattr(settings, "USE_UNSAFE_EXEC", False):
        return [
            Error(
                "USE_UNSAFE_EXEC is True: participants' programs run without the sio2jail sandbox.",
                hint="Set USE_UNSAFE_EXEC = False in settings.py. The worker container runs with all capabilities, so "
                "an unsandboxed program can take over the worker and its host.",
                id="czeciecamp.E001",
            )
        ]
    return []


@register(Tags.security, deploy=True)
def check_secret_key(app_configs, **kwargs):
    if getattr(settings, "SECRET_KEY", "") in PUBLIC_SECRET_KEYS:
        return [
            Error(
                "SECRET_KEY is a value published in the repository.",
                hint="Set a long random SECRET_KEY in settings.py, e.g. from: python3 -c 'import secrets; print(secrets.token_urlsafe(50))'. "
                "Changing it logs everybody out.",
                id="czeciecamp.E002",
            )
        ]
    return []


@register(Tags.security, deploy=True)
def check_package_makefiles(app_configs, **kwargs):
    untrusted = [app for app in UNTRUSTED_UPLOADER_APPS if app in settings.INSTALLED_APPS]
    if getattr(settings, "USE_SINOLPACK_MAKEFILES", False) and untrusted:
        return [
            Warning(
                f"USE_SINOLPACK_MAKEFILES is True while {', '.join(untrusted)} let non-staff users upload problem packages: "
                "a package's makefile runs unsandboxed on the server.",
                hint="Set USE_SINOLPACK_MAKEFILES = False in settings.py, or remove those apps.",
                id="czeciecamp.W001",
            )
        ]
    return []


@register(Tags.security, deploy=True)
def check_allowed_hosts(app_configs, **kwargs):
    if "*" in getattr(settings, "ALLOWED_HOSTS", []):
        return [
            Warning(
                "ALLOWED_HOSTS contains '*'.",
                hint="List the host names the site is served under, e.g. ALLOWED_HOSTS = ['sio.example.org'].",
                id="czeciecamp.W002",
            )
        ]
    return []
