import requests
from django.core.management.base import BaseCommand

from oioioi.llmdetect.generation import providers

# Lists of models that are not at <base_url>/models.
MODEL_LISTS = {
    "https://models.github.ai/inference": "https://models.github.ai/catalog/models",
}


class Command(BaseCommand):
    help = "Lists the models each provider in LLMDETECT_PROVIDERS offers, to choose their ids for the settings."

    def add_arguments(self, parser):
        parser.add_argument("provider", nargs="*", help="Only these providers.")
        parser.add_argument("--free", action="store_true", help="Only models whose id ends with ':free' (OpenRouter).")

    def handle(self, *args, **options):
        configured = providers()
        if not configured:
            self.stderr.write("LLMDETECT_PROVIDERS is empty.")
            return
        for name, config in configured.items():
            if options["provider"] and name not in options["provider"]:
                continue
            self.stdout.write(f"== {name}")
            try:
                ids = self._models(config)
            except Exception as error:  # report and go on with the other providers
                self.stdout.write(f"   cannot list models: {error}")
                continue
            if options["free"]:
                ids = [i for i in ids if i.endswith(":free")]
            chosen = set(config.get("models", []))
            for model_id in sorted(ids):
                self.stdout.write(f" {'*' if model_id in chosen else ' '} {model_id}")
            missing = chosen - set(ids)
            for model_id in sorted(missing):
                self.stdout.write(f" ! {model_id}  (configured, but not offered)")

    def _models(self, config):
        if config.get("type") == "anthropic":
            import anthropic

            return [model.id for model in anthropic.Anthropic(api_key=config.get("api_key") or None).models.list()]
        base_url = config["base_url"].rstrip("/")
        url = MODEL_LISTS.get(base_url, base_url + "/models")
        headers = {"Authorization": f"Bearer {config['api_key']}"} if config.get("api_key") else {}
        response = requests.get(url, headers=headers, timeout=30)
        response.raise_for_status()
        data = response.json()
        items = data.get("data", data.get("models", data.get("result", []))) if isinstance(data, dict) else data
        return [item["id"] if isinstance(item, dict) else str(item) for item in items if not isinstance(item, dict) or "id" in item]
