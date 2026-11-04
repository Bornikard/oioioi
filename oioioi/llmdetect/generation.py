"""Generating LLM reference solutions from problem statements.

Only the statement text (as shown to and edited by the staff) and a fixed
instruction are sent to the models, never participants' code.

Providers are configured in ``settings.LLMDETECT_PROVIDERS``::

    LLMDETECT_PROVIDERS = {
        "openrouter": {
            "type": "openai",  # any OpenAI-compatible chat completions API
            "base_url": "https://openrouter.ai/api/v1",
            "api_key": os.environ.get("OPENROUTER_API_KEY", ""),
            "models": ["<model id>", ...],
            "min_interval": 4,  # seconds between requests (free tiers)
        },
        "anthropic": {
            "type": "anthropic",
            "api_key": os.environ.get("ANTHROPIC_API_KEY", ""),
            "models": ["claude-opus-5-5"],
        },
    }
"""

import io
import logging
import re
import zipfile
from datetime import UTC, datetime, timedelta

import requests
from django.conf import settings
from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from django.utils.translation import pgettext_lazy

from oioioi.llmdetect.models import GenerationJob, LLMReferenceSolution, ProviderState, mark_dirty
from oioioi.problems.utils import query_statement

logger = logging.getLogger(__name__)

MAX_TOKENS = 16000
REQUEST_TIMEOUT = 600
MAX_RETRIES = 3
# Claude models that accept the server-side fallback on a declined request.
CLAUDE_FALLBACK_MODELS = frozenset({"claude-fable-5-1", "claude-opus-5-5", "claude-opus-5", "claude-sonnet-5-5"})

_TASK = "Solve this competitive programming problem in C++. Read the input from standard input and write the output to standard output."

# How participants might ask. The answers differ in style, and the
# references should cover what participants paste.
PROMPTS = {
    "plain": (pgettext_lazy("llmdetect", "Plain"), _TASK),
    "polish": (
        _("In Polish"),
        "Rozwiąż to zadanie w C++. Wejście czytaj ze standardowego wejścia, wynik wypisz na standardowe wyjście.",
    ),
    "student": (
        _("Like a high-school student"),
        _TASK + " Write the code like a high-school student at an olympiad would: short, no comments, short variable names.",
    ),
    "olympiad": (
        _("Like an olympiad competitor"),
        _TASK + " Write it the way an experienced olympiad competitor would: bits/stdc++.h, using namespace std, macros, fast IO, global arrays.",
    ),
    "disguised": (
        _("Disguised"),
        _TASK + " Then make the code look hand-written: rename the variables, remove all comments and do not explain anything.",
    ),
}

_CODE_BLOCK_RE = re.compile(r"```[ \t]*(?:c\+\+|cpp|cc|c|cxx)?[ \t]*\n(.*?)```", re.S | re.I)


class GenerationError(Exception):
    pass


def providers():
    return getattr(settings, "LLMDETECT_PROVIDERS", {}) or {}


def model_choices():
    """``("provider:model", "provider – model")`` for every configured model."""
    return [(f"{name}:{model}", f"{name} – {model}") for name, config in providers().items() for model in config.get("models", [])]


def statement_text(problem_instance):
    """Text of the statement of a problem, for the staff to check before
    sending it. Empty if there is no statement in a readable format.
    """
    statement = query_statement(problem_instance.problem)
    if statement is None:
        return ""
    statement.content.open("rb")
    try:
        data = statement.content.read()
    finally:
        statement.content.close()
    return _text_of(data, statement.extension)


def _text_of(data, extension):
    if extension == ".pdf":
        from pdfminer.high_level import extract_text

        return _clean(extract_text(io.BytesIO(data)))
    if extension in (".html", ".htm"):
        from bs4 import BeautifulSoup

        return _clean(BeautifulSoup(data, "html.parser").get_text("\n"))
    if extension == ".zip":
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            names = sorted(archive.namelist(), key=lambda name: (not name.endswith("index.html"), name))
            for name in names:
                inner = "." + name.rsplit(".", 1)[-1].lower() if "." in name else ""
                if inner in (".html", ".htm", ".pdf", ".txt"):
                    return _text_of(archive.read(name), inner)
        return ""
    if extension == ".txt":
        return _clean(data.decode("utf-8", "replace"))
    return ""


def _clean(text):
    text = text.replace("\r\n", "\n").replace("\x0c", "\n")
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def build_prompt(variant, statement):
    _label, instruction = PROMPTS[variant]
    return f"{instruction}\n\n{statement}"


def extract_code(answer):
    """The C++ code of an answer: its longest code block, or all of it."""
    blocks = _CODE_BLOCK_RE.findall(answer)
    if blocks:
        return max(blocks, key=len).strip() + "\n"
    return answer.strip() + "\n"


def code_of_chat_answer(text):
    """The code of a whole chat answer pasted by hand (prose and code
    fences dropped); text without code fences is returned as it is.
    """
    return extract_code(text) if _CODE_BLOCK_RE.search(text) else text


class QuotaError(GenerationError):
    """The provider refused for now (rate or daily limit)."""

    def __init__(self, message, retry_after=None):
        super().__init__(message)
        self.retry_after = retry_after


_DURATION_RE = re.compile(r"(?:(\d+(?:\.\d+)?)h)?(?:(\d+(?:\.\d+)?)m(?!s))?(?:(\d+(?:\.\d+)?)s)?$")


def _seconds(value, now=None):
    """Seconds from a rate-limit header: seconds, a duration like "2m59.5s",
    an HTTP date, or an epoch timestamp (seconds or milliseconds).
    """
    value = (value or "").strip()
    if not value:
        return None
    try:
        number = float(value)
    except ValueError:
        match = _DURATION_RE.match(value)
        if match and any(match.groups()):
            hours, minutes, seconds = (float(g) if g else 0.0 for g in match.groups())
            return hours * 3600 + minutes * 60 + seconds
        try:
            from email.utils import parsedate_to_datetime

            return max(0.0, (parsedate_to_datetime(value) - (now or timezone.now())).total_seconds())
        except (TypeError, ValueError):
            return None
    if number > 1e11:  # epoch in milliseconds
        number /= 1000
    if number > 1e9:  # epoch in seconds
        return max(0.0, number - (now or timezone.now()).timestamp())
    return number


def _retry_after(response):
    for header in ("retry-after", "x-ratelimit-reset-requests", "x-ratelimit-reset"):
        seconds = _seconds(response.headers.get(header))
        if seconds is not None:
            return seconds
    return None


def _openai_compatible(config, model, prompt):
    url = config["base_url"].rstrip("/") + "/chat/completions"
    headers = {"Content-Type": "application/json"}
    if config.get("api_key"):
        headers["Authorization"] = f"Bearer {config['api_key']}"
    headers.update(config.get("headers", {}))
    body = {"model": model, "messages": [{"role": "user", "content": prompt}], "max_tokens": config.get("max_tokens", MAX_TOKENS)}
    try:
        response = requests.post(url, json=body, headers=headers, timeout=config.get("timeout", REQUEST_TIMEOUT))
    except requests.RequestException as error:
        raise GenerationError(f"Connection error: {error}") from error
    if response.status_code == 429:
        raise QuotaError(f"HTTP 429: {response.text[:200]}", _retry_after(response))
    if response.status_code >= 400:
        raise GenerationError(f"HTTP {response.status_code}: {response.text[:300]}")
    try:
        content = response.json()["choices"][0]["message"]["content"]
    except (ValueError, KeyError, IndexError, TypeError) as error:
        raise GenerationError(f"Unexpected answer: {response.text[:300]}") from error
    if isinstance(content, list):  # some APIs return content parts
        content = "".join(part.get("text", "") for part in content if isinstance(part, dict))
    if not content:
        raise GenerationError("Empty answer")
    return content


def _anthropic(config, model, prompt):
    import anthropic

    client = anthropic.Anthropic(api_key=config.get("api_key") or None, timeout=config.get("timeout", REQUEST_TIMEOUT), max_retries=MAX_RETRIES)
    kwargs = {"model": model, "max_tokens": config.get("max_tokens", MAX_TOKENS), "messages": [{"role": "user", "content": prompt}]}
    if config.get("effort"):
        kwargs["output_config"] = {"effort": config["effort"]}
    try:
        if model in CLAUDE_FALLBACK_MODELS:
            # A declined request is retried server-side on the model Anthropic recommends.
            response = client.beta.messages.create(betas=["server-side-fallback-2026-07-01"], fallbacks="default", **kwargs)
        else:
            response = client.messages.create(**kwargs)
    except anthropic.RateLimitError as error:
        raise QuotaError(f"HTTP 429: {error.message}", _seconds(error.response.headers.get("retry-after"))) from error
    except anthropic.APIStatusError as error:
        raise GenerationError(f"HTTP {error.status_code}: {error.message}") from error
    except anthropic.APIConnectionError as error:
        raise GenerationError(f"Connection error: {error}") from error
    if response.stop_reason == "refusal":
        raise GenerationError("The model declined the request")
    text = "".join(block.text for block in response.content if block.type == "text")
    if not text:
        raise GenerationError(f"Empty answer (stop reason: {response.stop_reason})")
    return text


CLIENTS = {"openai": _openai_compatible, "anthropic": _anthropic}


def ask(provider, model, prompt):
    config = providers().get(provider)
    if config is None:
        raise GenerationError(f"Unknown provider {provider}")
    client = CLIENTS.get(config.get("type", "openai"))
    if client is None:
        raise GenerationError(f"Unknown provider type {config.get('type')}")
    return client(config, model, prompt)


# Scheduling: one request at a time, from any provider that may be asked
# now, so that free tiers of several providers are used side by side.

# A provider refusing this many times in a row waits for its daily reset.
MAX_STRIKES = 4


def _quota_day(config, now):
    """The provider's quota day: days start at ``reset_utc_hour`` (UTC)."""
    return (now - timedelta(hours=config.get("reset_utc_hour", 0))).astimezone(UTC).date()


def next_reset(config, now):
    hour = config.get("reset_utc_hour", 0)
    day = _quota_day(config, now) + timedelta(days=1)
    return datetime(day.year, day.month, day.day, tzinfo=UTC) + timedelta(hours=hour)


def provider_state(name, config, now=None):
    now = now or timezone.now()
    state, _created = ProviderState.objects.get_or_create(provider=name, defaults={"day": _quota_day(config, now)})
    day = _quota_day(config, now)
    if state.day != day:
        state.day = day
        state.requests_today = 0
        state.save(update_fields=["day", "requests_today"])
    return state


def ready_at(config, state, now):
    """When the provider may be asked next; ``None`` means now."""
    times = []
    if state.paused_until and state.paused_until > now:
        times.append(state.paused_until)
    limit = config.get("daily_limit")
    if limit and state.requests_today >= limit:
        times.append(next_reset(config, now))
    interval = config.get("min_interval", 0)
    if interval and state.last_request_at and state.last_request_at + timedelta(seconds=interval) > now:
        times.append(state.last_request_at + timedelta(seconds=interval))
    return max(times) if times else None


def next_request(now=None):
    """The oldest queued job whose provider may be asked now, or ``None``."""
    now = now or timezone.now()
    configured = providers()
    states = {}
    for job in GenerationJob.objects.filter(status=GenerationJob.QUEUED).order_by("id"):
        config = configured.get(job.provider)
        if config is None:
            _finish(job, GenerationJob.FAILED, f"Provider {job.provider} is not configured")
            continue
        if job.provider not in states:
            states[job.provider] = provider_state(job.provider, config, now)
        if ready_at(config, states[job.provider], now) is None:
            return job
    return None


def _finish(job, status, error=""):
    job.status = status
    job.error = error[:1000]
    job.finished_at = timezone.now()
    job.save(update_fields=["status", "error", "finished_at"])


def perform(job):
    """Asks for one solution of ``job`` and stores it."""
    config = providers()[job.provider]
    now = timezone.now()
    state = provider_state(job.provider, config, now)
    state.requests_today += 1
    state.last_request_at = now
    state.save(update_fields=["requests_today", "last_request_at"])
    job.status = GenerationJob.RUNNING
    job.started_at = job.started_at or now
    job.save(update_fields=["status", "started_at"])
    try:
        answer = ask(job.provider, job.model, build_prompt(job.prompt_variant, job.statement))
    except QuotaError as error:
        _pause(job.provider, config, state, error)
        job.status = GenerationJob.QUEUED
        job.error = str(error)[:1000]
        job.save(update_fields=["status", "error"])
        return
    except Exception as error:
        logger.warning("LLM reference generation job %s failed", job.id, exc_info=True)
        state.last_error = str(error)[:500]
        state.save(update_fields=["last_error"])
        _finish(job, GenerationJob.FAILED, str(error))
        return
    LLMReferenceSolution.objects.create(
        problem_instance=job.problem_instance,
        model_name=job.model,
        prompt_variant=job.prompt_variant,
        filename=f"{job.provider}__{job.prompt_variant}__{job.id}_{job.produced + 1}.cpp",
        source=extract_code(answer),
        added_by=job.created_by,
    )
    mark_dirty(job.problem_instance.contest)
    state.strikes = 0
    state.paused_until = None
    state.save(update_fields=["strikes", "paused_until"])
    job.produced += 1
    job.error = ""
    job.save(update_fields=["produced", "error"])
    if job.produced >= job.count:
        _finish(job, GenerationJob.DONE)
    else:
        job.status = GenerationJob.QUEUED
        job.save(update_fields=["status"])


def _pause(name, config, state, error):
    now = timezone.now()
    state.strikes += 1
    reset = next_reset(config, now)
    if error.retry_after is not None:
        until = now + timedelta(seconds=min(error.retry_after, 26 * 3600))
    elif state.strikes >= MAX_STRIKES:
        until = reset
    else:
        until = min(reset, now + timedelta(seconds=60 * 2 ** (state.strikes - 1)))
    state.paused_until = until
    state.last_error = str(error)[:500]
    state.save(update_fields=["strikes", "paused_until", "last_error"])
    logger.info("LLM provider %s paused until %s: %s", name, until, error)


def provider_rows(now=None):
    """Usage of each configured provider today, for the panel."""
    now = now or timezone.now()
    rows = []
    for name, config in providers().items():
        state = provider_state(name, config, now)
        rows.append(
            {
                "name": name,
                "models": len(config.get("models", [])),
                "requests": state.requests_today,
                "limit": config.get("daily_limit"),
                "ready_at": ready_at(config, state, now),
                "reset": next_reset(config, now),
                "error": state.last_error,
            }
        )
    return rows
