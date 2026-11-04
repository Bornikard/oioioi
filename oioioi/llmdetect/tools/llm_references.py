#!/usr/bin/env python3
"""Ask LLMs for reference solutions of problems, on your own machine.

Part of oioioi.llmdetect. The SIO2 server never contacts any LLM: download
the prompts from the "LLM references" page, run this script with your own
API keys, and paste or upload its output on the same page.

  python3 llm_references.py --example-config > providers.json
  # edit providers.json; put the keys in environment variables
  python3 llm_references.py --list-models -c providers.json
  python3 llm_references.py prompts.json -c providers.json -o references.txt

Only the Python standard library is needed (plus the "anthropic" package
for Claude). Requests go to whichever provider may be asked next, within
each provider's limits, so the free quotas of several providers add up. The
output is written as it comes; interrupted or out of quota, run the same
command again later and it goes on where it stopped.
"""

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from collections import deque
from email.utils import parsedate_to_datetime

PROMPTS_FORMAT = "llmdetect-prompts/1"
DEFAULT_MAX_TOKENS = 16000
TIMEOUT = 600
# A provider refusing this many times in a row, or asking to wait longer
# than this, is left for the next run (its daily quota is probably used up).
MAX_STRIKES = 4
MAX_WAIT = 600
# Claude models that accept the server-side fallback on a declined request.
CLAUDE_FALLBACK_MODELS = {"claude-fable-5-1", "claude-opus-5-5", "claude-opus-5", "claude-sonnet-5-5"}
# Model lists that are not at <base_url>/models.
MODEL_LISTS = {"https://models.github.ai/inference": "https://models.github.ai/catalog/models"}

EXAMPLE_CONFIG = {
    "openrouter": {
        "base_url": "https://openrouter.ai/api/v1",
        "api_key_env": "OPENROUTER_API_KEY",
        "models": ["cohere/north-mini-code:free", "poolside/laguna-m.1:free", "openrouter/free"],
        "min_interval": 4,
        "max_tokens": 8000,
        "daily_limit": 50,
    },
    "gemini": {
        "base_url": "https://generativelanguage.googleapis.com/v1beta/openai",
        "api_key_env": "GEMINI_API_KEY",
        "models": ["gemini-3.8-flash", "gemini-3.5-flash-lite", "gemma-4-31b-it"],
        "min_interval": 6,
    },
    "groq": {
        "base_url": "https://api.groq.com/openai/v1",
        "api_key_env": "GROQ_API_KEY",
        "models": ["openai/gpt-oss-120b", "qwen/qwen3.8-27b", "llama-3.3-70b-versatile"],
        "min_interval": 3,
        "max_tokens": 8000,
    },
    "mistral": {
        "base_url": "https://api.mistral.ai/v1",
        "api_key_env": "MISTRAL_API_KEY",
        "models": ["codestral-latest", "devstral-medium-latest", "mistral-medium-latest"],
        "min_interval": 35,
    },
    "cerebras": {
        "base_url": "https://api.cerebras.ai/v1",
        "api_key_env": "CEREBRAS_API_KEY",
        "models": ["gpt-oss-120b", "qwen-3.8-27b"],
        "min_interval": 13,
    },
    "github": {
        "base_url": "https://models.github.ai/inference",
        "api_key_env": "GITHUB_MODELS_TOKEN",
        "models": ["openai/gpt-4.1", "openai/gpt-5-mini"],
        "min_interval": 7,
        "max_tokens": 4000,
        "daily_limit": 50,
    },
    "nvidia": {
        "base_url": "https://integrate.api.nvidia.com/v1",
        "api_key_env": "NVIDIA_API_KEY",
        "models": ["moonshotai/kimi-k2.6", "meta/llama-3.3-70b-instruct", "qwen/qwen2.5-coder-32b-instruct"],
        "min_interval": 2,
    },
    "cloudflare": {
        "base_url": "https://api.cloudflare.com/client/v4/accounts/<ACCOUNT_ID>/ai/v1",
        "api_key_env": "CLOUDFLARE_API_TOKEN",
        "models": ["@cf/openai/gpt-oss-120b", "@cf/qwen/qwen3.8-27b", "@cf/moonshotai/kimi-k2.7-code"],
    },
    "anthropic": {
        "type": "anthropic",
        "api_key_env": "ANTHROPIC_API_KEY",
        "models": ["claude-opus-5-5"],
    },
}

_HEADER_RE = re.compile(r"^###[ \t]*reference\b(.*)$", re.M | re.I)
_ATTRIBUTE_RE = re.compile(r'(\w+)=(?:"([^"]*)"|(\S+))')
_DURATION_RE = re.compile(r"(?:(\d+(?:\.\d+)?)h)?(?:(\d+(?:\.\d+)?)m(?!s))?(?:(\d+(?:\.\d+)?)s)?$")

# Replaced in tests.
clock = time.monotonic
sleep = time.sleep


class ProviderError(Exception):
    pass


class QuotaError(ProviderError):
    def __init__(self, message, retry_after=None):
        super().__init__(message)
        self.retry_after = retry_after


class ModelError(ProviderError):
    """The model cannot be used at all (e.g. it does not exist)."""


_FENCE_LANGUAGES = ("", "c++", "cpp", "cc", "c", "cxx")


def _code_blocks(text):
    """The contents of closed ``` code blocks (C++ or untagged), in linear time."""
    parts = text.split("```")
    blocks = []
    for i in range((len(parts) - 1) // 2):
        tag, newline, body = parts[2 * i + 1].partition("\n")
        if newline and tag.strip().lower() in _FENCE_LANGUAGES:
            blocks.append(body)
    return blocks


def extract_code(answer):
    """The C++ code of an answer: its longest code block, or all of it."""
    blocks = _code_blocks(answer)
    if blocks:
        return max(blocks, key=len).strip() + "\n"
    return answer.strip() + "\n"


def header(**attributes):
    parts = []
    for key, value in attributes.items():
        value = str(value)
        parts.append(f'{key}="{value}"' if (not value or re.search(r'[\s"]', value)) else f"{key}={value}")
    return "### reference " + " ".join(parts)


def done_keys(path):
    """(problem, prompt, model, n) of the references already in the output file."""
    if not os.path.exists(path):
        return set()
    with open(path, encoding="utf-8") as file:
        text = file.read()
    keys = set()
    for match in _HEADER_RE.finditer(text):
        attributes = {m.group(1).lower(): m.group(2) if m.group(2) is not None else m.group(3) for m in _ATTRIBUTE_RE.finditer(match.group(1))}
        keys.add((attributes.get("problem"), attributes.get("prompt"), attributes.get("model"), attributes.get("n")))
    return keys


def seconds(value, now=None):
    """Seconds from a rate-limit header: seconds, "2m59.5s", an HTTP date or an epoch."""
    value = (value or "").strip()
    if not value:
        return None
    try:
        number = float(value)
    except ValueError:
        match = _DURATION_RE.match(value)
        if match and any(match.groups()):
            hours, minutes, secs = (float(g) if g else 0.0 for g in match.groups())
            return hours * 3600 + minutes * 60 + secs
        try:
            return max(0.0, parsedate_to_datetime(value).timestamp() - (now if now is not None else time.time()))
        except (TypeError, ValueError):
            return None
    if number > 1e11:  # epoch in milliseconds
        number /= 1000
    if number > 1e9:  # epoch in seconds
        return max(0.0, number - (now if now is not None else time.time()))
    return number


def api_key(config):
    if config.get("api_key"):
        return config["api_key"]
    name = config.get("api_key_env")
    return os.environ.get(name, "") if name else ""


def _http(url, key, body=None, extra_headers=None):
    headers = {"Content-Type": "application/json", "User-Agent": "oioioi-llmdetect"}
    if key:
        headers["Authorization"] = f"Bearer {key}"
    headers.update(extra_headers or {})
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(url, data=data, headers=headers, method="POST" if body is not None else "GET")
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        text = error.read().decode("utf-8", "replace")[:300]
        if error.code == 429:
            retry = None
            for name in ("retry-after", "x-ratelimit-reset-requests", "x-ratelimit-reset"):
                retry = seconds(error.headers.get(name))
                if retry is not None:
                    break
            raise QuotaError(f"HTTP 429: {text}", retry) from error
        if error.code == 404:
            raise ModelError(f"HTTP 404: {text}") from error
        raise ProviderError(f"HTTP {error.code}: {text}") from error
    except (urllib.error.URLError, TimeoutError, OSError) as error:
        raise ProviderError(f"Connection error: {error}") from error
    except ValueError as error:
        raise ProviderError(f"Not a JSON answer: {error}") from error


def ask_openai(config, model, prompt):
    body = {"model": model, "messages": [{"role": "user", "content": prompt}], "max_tokens": config.get("max_tokens", DEFAULT_MAX_TOKENS)}
    data = _http(config["base_url"].rstrip("/") + "/chat/completions", api_key(config), body, config.get("headers"))
    try:
        content = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as error:
        raise ProviderError(f"Unexpected answer: {str(data)[:300]}") from error
    if isinstance(content, list):  # some APIs return content parts
        content = "".join(part.get("text", "") for part in content if isinstance(part, dict))
    if not content:
        raise ProviderError("Empty answer")
    return content


def ask_anthropic(config, model, prompt):
    try:
        import anthropic
    except ImportError as error:
        raise ProviderError("Claude needs the anthropic package: pip install anthropic") from error
    client = anthropic.Anthropic(api_key=api_key(config) or None, timeout=TIMEOUT)
    kwargs = {"model": model, "max_tokens": config.get("max_tokens", DEFAULT_MAX_TOKENS), "messages": [{"role": "user", "content": prompt}]}
    if config.get("effort"):
        kwargs["output_config"] = {"effort": config["effort"]}
    try:
        if model in CLAUDE_FALLBACK_MODELS:
            # A declined request is retried server-side on the model Anthropic recommends.
            response = client.beta.messages.create(betas=["server-side-fallback-2026-07-01"], fallbacks="default", **kwargs)
        else:
            response = client.messages.create(**kwargs)
    except anthropic.RateLimitError as error:
        raise QuotaError(f"HTTP 429: {error.message}", seconds(error.response.headers.get("retry-after"))) from error
    except anthropic.NotFoundError as error:
        raise ModelError(f"HTTP 404: {error.message}") from error
    except anthropic.APIStatusError as error:
        raise ProviderError(f"HTTP {error.status_code}: {error.message}") from error
    except anthropic.APIConnectionError as error:
        raise ProviderError(f"Connection error: {error}") from error
    if response.stop_reason == "refusal":
        raise ProviderError("The model declined the request")
    text = "".join(block.text for block in response.content if block.type == "text")
    if not text:
        raise ProviderError(f"Empty answer (stop reason: {response.stop_reason})")
    return text


def ask(config, model, prompt):
    return (ask_anthropic if config.get("type") == "anthropic" else ask_openai)(config, model, prompt)


def list_models(config):
    if config.get("type") == "anthropic":
        import anthropic

        return [model.id for model in anthropic.Anthropic(api_key=api_key(config) or None).models.list()]
    base_url = config["base_url"].rstrip("/")
    data = _http(MODEL_LISTS.get(base_url, base_url + "/models"), api_key(config))
    items = data.get("data", data.get("models", data.get("result", []))) if isinstance(data, dict) else data
    return [item["id"] if isinstance(item, dict) else str(item) for item in items if not isinstance(item, dict) or "id" in item]


class Provider:
    def __init__(self, name, config):
        self.name = name
        self.config = config
        self.tasks = deque()
        self.ready_at = 0.0
        self.requests = 0
        self.strikes = 0
        self.stopped = None  # why the provider is left for the next run

    @property
    def interval(self):
        return float(self.config.get("min_interval", 0))

    def available(self):
        if self.stopped or not self.tasks:
            return False
        limit = self.config.get("daily_limit")
        if limit and self.requests >= limit:
            self.stopped = f"daily_limit of {limit} requests reached"
            return False
        return True


def plan(prompts, providers, count, done):
    """Gives every provider its tasks: (prompt entry, model, n), not yet done."""
    for provider in providers:
        for entry in prompts:
            for model in provider.config.get("models", []):
                label = f"{provider.name}/{model}"
                for n in range(1, count + 1):
                    if (entry["problem"], entry["prompt"], label, str(n)) not in done:
                        provider.tasks.append((entry, model, n))


def run(prompts, providers, count, output, log=print):
    done = done_keys(output)
    plan(prompts, providers, count, done)
    total = sum(len(p.tasks) for p in providers)
    if not total:
        log("Nothing to do: every answer is already in " + output)
        return {"done": 0, "failed": 0, "left": 0}
    log(f"{total} answers to get from {len([p for p in providers if p.tasks])} providers; writing to {output}")
    finished = failed = 0
    with open(output, "a", encoding="utf-8") as out:
        while True:
            candidates = [p for p in providers if p.available()]
            if not candidates:
                break
            provider = min(candidates, key=lambda p: p.ready_at)
            wait = provider.ready_at - clock()
            if wait > 0:
                sleep(wait)
            entry, model, n = provider.tasks.popleft()
            provider.requests += 1
            started = clock()
            provider.ready_at = started + provider.interval
            label = f"{provider.name}/{model}"
            where = f"{entry['problem']} {entry['prompt']} #{n} {label}"
            try:
                answer = ask(provider.config, model, entry["text"])
            except QuotaError as error:
                provider.tasks.appendleft((entry, model, n))
                provider.strikes += 1
                pause = error.retry_after if error.retry_after is not None else 30.0 * 2 ** (provider.strikes - 1)
                if provider.strikes >= MAX_STRIKES or pause > MAX_WAIT:
                    provider.stopped = f"out of quota ({error})"
                    log(f"{provider.name}: out of quota for now, left for the next run")
                else:
                    provider.ready_at = clock() + pause
                    log(f"{provider.name}: too many requests, waiting {pause:.0f} s")
                continue
            except ModelError as error:
                skipped = [task for task in provider.tasks if task[1] == model]
                provider.tasks = deque(task for task in provider.tasks if task[1] != model)
                failed += 1 + len(skipped)
                log(f"{label}: cannot be used, its {1 + len(skipped)} answers skipped in this run ({error})")
                continue
            except ProviderError as error:
                failed += 1
                log(f"[failed] {where}: {error}")
                continue
            provider.strikes = 0
            out.write(header(problem=entry["problem"], model=label, prompt=entry["prompt"], n=n) + "\n")
            out.write(extract_code(answer))
            out.flush()
            finished += 1
            log(f"[{finished}/{total}] {where} ({clock() - started:.0f} s)")
    left = sum(len(p.tasks) for p in providers)
    for provider in providers:
        if provider.stopped and provider.tasks:
            log(f"{provider.name}: {len(provider.tasks)} answers left ({provider.stopped})")
    log(f"Done: {finished} answers, {failed} failed, {left} left for the next run.")
    if finished:
        log(f'Now paste or upload {output} on the "LLM references" page.')
    return {"done": finished, "failed": failed, "left": left}


def load_prompts(path, problems=None, variants=None):
    with open(path, encoding="utf-8") as file:
        data = json.load(file)
    if data.get("format") != PROMPTS_FORMAT:
        raise SystemExit(f"{path} is not a prompts file from the LLM references page")
    prompts = data["prompts"]
    if problems:
        prompts = [p for p in prompts if p["problem"] in problems]
    if variants:
        prompts = [p for p in prompts if p["prompt"] in variants]
    return prompts


def load_providers(path, only=None, log=print):
    with open(path, encoding="utf-8") as file:
        configured = json.load(file)
    providers = []
    for name, config in configured.items():
        if only and name not in only:
            continue
        if not config.get("models"):
            continue
        if config.get("api_key_env") and not api_key(config):
            log(f"{name}: skipped, {config['api_key_env']} is not set")
            continue
        providers.append(Provider(name, config))
    return providers


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("prompts", nargs="?", help="prompts file downloaded from the LLM references page")
    parser.add_argument("-c", "--config", default="providers.json", help="providers (default: providers.json)")
    parser.add_argument("-o", "--output", default="references.txt", help="output to paste or upload (default: references.txt)")
    parser.add_argument("-n", "--count", type=int, default=1, help="answers per model and prompt (default: 1)")
    parser.add_argument("--providers", help="only these providers, comma-separated")
    parser.add_argument("--problems", help="only these problems (short names), comma-separated")
    parser.add_argument("--only-prompts", help="only these prompt styles, comma-separated")
    parser.add_argument("--list-models", action="store_true", help="list the models of each provider and exit")
    parser.add_argument("--example-config", action="store_true", help="print an example providers.json and exit")
    args = parser.parse_args(argv)

    split = lambda value: set(value.split(",")) if value else None  # noqa: E731
    if args.example_config:
        print(json.dumps(EXAMPLE_CONFIG, indent=2))
        return 0
    if not os.path.exists(args.config):
        parser.error(f"no {args.config}; create one with --example-config")
    providers = load_providers(args.config, split(args.providers))
    if args.list_models:
        for provider in providers:
            print(f"== {provider.name}")
            try:
                ids = list_models(provider.config)
            except Exception as error:
                print(f"   cannot list models: {error}")
                continue
            chosen = set(provider.config.get("models", []))
            for model_id in sorted(ids):
                print(f" {'*' if model_id in chosen else ' '} {model_id}")
            for model_id in sorted(chosen - set(ids)):
                print(f" ! {model_id}  (in the config, but not offered)")
        return 0
    if not args.prompts:
        parser.error("give the prompts file downloaded from the LLM references page")
    if not providers:
        parser.error("no provider with models and a key in " + args.config)
    prompts = load_prompts(args.prompts, split(args.problems), split(args.only_prompts))
    try:
        result = run(prompts, providers, args.count, args.output)
    except KeyboardInterrupt:
        print(f"\nInterrupted. What is in {args.output} is kept; run the same command again to go on.")
        return 130
    return 0 if result["failed"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
