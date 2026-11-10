from django.apps import AppConfig


class ProblemBaseAppConfig(AppConfig):
    default_auto_field = "django.db.models.AutoField"
    name = "oioioi.problembase"

    def ready(self):
        from oioioi.problembase import signals  # noqa: F401
