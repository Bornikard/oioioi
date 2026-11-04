from django.apps import AppConfig


class CzecieCampAppConfig(AppConfig):
    default_auto_field = "django.db.models.AutoField"
    name = "oioioi.czeciecamp"

    def ready(self):
        from oioioi.czeciecamp import checks  # noqa: F401  (registers the deployment checks)
