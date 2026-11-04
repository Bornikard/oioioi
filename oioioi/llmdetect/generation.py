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
import time
import zipfile

import requests
from django.conf import settings
from django.db import connection, transaction
from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from django.utils.translation import pgettext_lazy

from oioioi.llmdetect.models import GenerationJob, LLMReferenceSolution, mark_dirty
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


def _openai_compatible(config, model, prompt):
    url = config["base_url"].rstrip("/") + "/chat/completions"
    headers = {"Content-Type": "application/json"}
    if config.get("api_key"):
        headers["Authorization"] = f"Bearer {config['api_key']}"
    headers.update(config.get("headers", {}))
    body = {"model": model, "messages": [{"role": "user", "content": prompt}], "max_tokens": config.get("max_tokens", MAX_TOKENS)}
    for attempt in range(MAX_RETRIES + 1):
        response = requests.post(url, json=body, headers=headers, timeout=config.get("timeout", REQUEST_TIMEOUT))
        if response.status_code == 429 and attempt < MAX_RETRIES:
            time.sleep(_retry_after(response, attempt))
            continue
        if response.status_code >= 400:
            raise GenerationError(f"HTTP {response.status_code}: {response.text[:300]}")
        break
    try:
        content = response.json()["choices"][0]["message"]["content"]
    except (ValueError, KeyError, IndexError, TypeError) as error:
        raise GenerationError(f"Unexpected answer: {response.text[:300]}") from error
    if isinstance(content, list):  # some APIs return content parts
        content = "".join(part.get("text", "") for part in content if isinstance(part, dict))
    if not content:
        raise GenerationError("Empty answer")
    return content


def _retry_after(response, attempt):
    try:
        return min(120.0, float(response.headers.get("retry-after", "")))
    except ValueError:
        return 10.0 * 2**attempt


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


def run_job(job):
    """Asks the model ``job.count`` times and stores the answers as references."""
    config = providers().get(job.provider, {})
    interval = config.get("min_interval", 0)
    prompt = build_prompt(job.prompt_variant, job.statement)
    for i in range(job.produced, job.count):
        if i and interval:
            time.sleep(interval)
        answer = ask(job.provider, job.model, prompt)
        LLMReferenceSolution.objects.create(
            problem_instance=job.problem_instance,
            model_name=job.model,
            prompt_variant=job.prompt_variant,
            filename=f"{job.provider}__{job.prompt_variant}__{job.id}_{i + 1}.cpp",
            source=extract_code(answer),
            added_by=job.created_by,
        )
        job.produced = i + 1
        job.save(update_fields=["produced"])
        mark_dirty(job.problem_instance.contest)


def claim_job():
    """The oldest queued job, marked as running, or ``None``."""
    with transaction.atomic():
        queued = GenerationJob.objects.filter(status=GenerationJob.QUEUED).order_by("id")
        if connection.features.has_select_for_update_skip_locked:
            queued = queued.select_for_update(skip_locked=True)
        job = queued.first()
        if job is None:
            return None
        job.status = GenerationJob.RUNNING
        job.started_at = timezone.now()
        job.save(update_fields=["status", "started_at"])
    return job


def process(job):
    try:
        run_job(job)
    except Exception as error:
        logger.warning("LLM reference generation job %s failed", job.id, exc_info=True)
        job.status = GenerationJob.FAILED
        job.error = str(error)[:1000]
    else:
        job.status = GenerationJob.DONE
        job.error = ""
    job.finished_at = timezone.now()
    job.save(update_fields=["status", "error", "finished_at"])
