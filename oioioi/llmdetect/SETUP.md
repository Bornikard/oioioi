# LLM detection: updating a running server and setting it up

This guide takes a server that runs this repository with `docker-compose.yml`
(the Czecie camp production setup) to the version with `oioioi.llmdetect`. It
covers enabling the module, generating LLM reference solutions with free (and
optionally paid) models, and using it during a camp.

All commands are run on the server, in the directory with `docker-compose.yml`
and `.env`, unless said otherwise.

## What the update changes

- A new Django app, `oioioi.llmdetect`, with 4 migrations (new tables only;
  existing tables are not touched).
- A new Python dependency, `anthropic`, so the Docker image has to be rebuilt.
- A new background program, `llmdetect-generator`, started by supervisor in the
  `web` container when the app is enabled.
- `docker-compose.yml` passes the API keys of LLM providers from `.env` to the
  `web` container (empty when not set).
- A "LLM detection" button in the camp panel and an item in the contest admin
  menu, both for full contest admins only (not basic admins).

## 1. Before you start

1. Do it outside a running round: the site is down for a minute while the
   containers restart.
2. Note the current version, to be able to go back:

   ```bash
   git rev-parse HEAD
   ```

3. Back up the database:

   ```bash
   docker compose exec -T db pg_dump -U oioioi oioioi > ~/oioioi-backup-$(date +%F).sql
   ```

## 2. Get the new code

If the pull request is merged into `czecie-camp`:

```bash
git checkout czecie-camp
```

```bash
git pull origin czecie-camp
```

## 3. Enable the module in settings

Edit `settings.py` in your data directory (`$SIO_DATA_DIR/settings.py`, the
directory set in `.env`). It is mounted into the container, so it is not part
of the image and the rebuild does not change it.

1. In `INSTALLED_APPS`, next to `'oioioi.czeciecamp',`, add:

   ```python
       'oioioi.llmdetect',
   ```

2. In the `MIDDLEWARE += (...)` block, add the line below. It records when a
   participant first opens a problem statement, for the timing signals. It is
   optional; without it, time is counted from the start of the round.

   ```python
      'oioioi.llmdetect.middleware.ProblemViewMiddleware',
   ```

3. At the end of the file, add the providers that generate reference
   solutions (section 4 explains the accounts and keys). Keep only the
   providers you will have a key for; a provider without a key fails every
   request with "HTTP 401". The model ids were current in November 2026;
   section 6 shows how to check them.

   ```python
   # LLM detection: models that generate reference solutions from problem statements.
   # Check the model ids with:  docker compose exec web ./manage.py llmdetect_models
   LLMDETECT_PROVIDERS = {
       # ~20 requests/min, ~50/day (1000/day after a one-time $10 top-up).
       "openrouter": {
           "type": "openai",
           "base_url": "https://openrouter.ai/api/v1",
           "api_key": os.environ.get("OPENROUTER_API_KEY", ""),
           "models": [
               "cohere/north-mini-code:free",
               "poolside/laguna-m.1:free",
               "openrouter/free",  # a random free model each time
           ],
           "min_interval": 4,
           "max_tokens": 8000,
           "daily_limit": 50,  # 1000 after topping up
       },
       # Free tier on Flash models; Google may use free-tier input to train models.
       "gemini": {
           "type": "openai",
           "base_url": "https://generativelanguage.googleapis.com/v1beta/openai",
           "api_key": os.environ.get("GEMINI_API_KEY", ""),
           "models": ["gemini-3.8-flash", "gemini-3.5-flash-lite", "gemma-4-31b-it"],
           "min_interval": 6,
           "reset_utc_hour": 8,  # midnight Pacific
       },
       "groq": {
           "type": "openai",
           "base_url": "https://api.groq.com/openai/v1",
           "api_key": os.environ.get("GROQ_API_KEY", ""),
           "models": ["openai/gpt-oss-120b", "qwen/qwen3.8-27b", "llama-3.3-70b-versatile"],
           "min_interval": 3,
           "max_tokens": 8000,
       },
       # Free "Experiment" plan (~2 requests/min); you must allow Mistral to train on your data.
       "mistral": {
           "type": "openai",
           "base_url": "https://api.mistral.ai/v1",
           "api_key": os.environ.get("MISTRAL_API_KEY", ""),
           "models": ["codestral-latest", "devstral-medium-latest", "mistral-medium-latest"],
           "min_interval": 35,
       },
       # A time-limited free trial, ~5 requests/min.
       "cerebras": {
           "type": "openai",
           "base_url": "https://api.cerebras.ai/v1",
           "api_key": os.environ.get("CEREBRAS_API_KEY", ""),
           "models": ["gpt-oss-120b", "qwen-3.8-27b"],
           "min_interval": 13,
       },
       # ~50/day for these models, 8k input / 4k output tokens.
       "github": {
           "type": "openai",
           "base_url": "https://models.github.ai/inference",
           "api_key": os.environ.get("GITHUB_MODELS_TOKEN", ""),
           "models": ["openai/gpt-4.1", "openai/gpt-5-mini"],
           "min_interval": 7,
           "max_tokens": 4000,
           "daily_limit": 50,
       },
       # ~40 requests/min shared by all models.
       "nvidia": {
           "type": "openai",
           "base_url": "https://integrate.api.nvidia.com/v1",
           "api_key": os.environ.get("NVIDIA_API_KEY", ""),
           "models": ["moonshotai/kimi-k2.6", "meta/llama-3.3-70b-instruct", "qwen/qwen2.5-coder-32b-instruct"],
           "min_interval": 2,
       },
       # 10,000 "neurons" a day (reset 00:00 UTC); big models use them up in a few dozen requests.
       "cloudflare": {
           "type": "openai",
           "base_url": "https://api.cloudflare.com/client/v4/accounts/" + os.environ.get("CLOUDFLARE_ACCOUNT_ID", "") + "/ai/v1",
           "api_key": os.environ.get("CLOUDFLARE_API_TOKEN", ""),
           "models": ["@cf/openai/gpt-oss-120b", "@cf/qwen/qwen3.8-27b", "@cf/moonshotai/kimi-k2.7-code"],
       },
       # Paid, if you want them (they do not train on your data by default):
       # "openai": {
       #     "type": "openai",
       #     "base_url": "https://api.openai.com/v1",
       #     "api_key": os.environ.get("OPENAI_API_KEY", ""),
       #     "models": [],
       # },
       # "anthropic": {
       #     "type": "anthropic",
       #     "api_key": os.environ.get("ANTHROPIC_API_KEY", ""),
       #     "models": ["claude-opus-5-5"],
       # },
   }
   ```

   `settings.py` already imports `os.path`, so `os.environ` works there.

   What the per-provider options mean:

   | Option | Meaning |
   |---|---|
   | `models` | Model ids to offer on the page; an empty list means the provider is never used. |
   | `min_interval` | Seconds between two requests to this provider (to stay under per-minute limits). |
   | `daily_limit` | Optional: stop after this many requests per quota day. |
   | `reset_utc_hour` | Hour (UTC) when the provider's quota day starts; default 0. |
   | `max_tokens` | Longest answer to ask for; some free models allow less than the default 16000. |

   You do not need `daily_limit` to be exact: a provider that answers "too
   many requests" is paused until its retry time or its daily reset anyway,
   and its jobs go on by themselves afterwards.

## 4. Create the provider accounts and keys

Each free API needs its own account; the free quota belongs to that account.
Most need no credit card, some ask for a phone number. Make one account per
provider: several accounts on one provider to get more quota usually breaks its
terms. You do not need all of them; every one adds its daily quota.

| Provider | Where to get a key | Variable in `.env` |
|---|---|---|
| OpenRouter | openrouter.ai → Settings → Keys | `OPENROUTER_API_KEY` |
| Google Gemini | aistudio.google.com → Get API key (Google account) | `GEMINI_API_KEY` |
| Groq | console.groq.com → API Keys | `GROQ_API_KEY` |
| Mistral | console.mistral.ai → choose the free "Experiment" plan (phone verification) → API Keys | `MISTRAL_API_KEY` |
| Cerebras | cloud.cerebras.ai → API Keys (free trial) | `CEREBRAS_API_KEY` |
| GitHub Models | github.com → Settings → Developer settings → fine-grained token with "Models: read" | `GITHUB_MODELS_TOKEN` |
| NVIDIA | build.nvidia.com → NVIDIA Developer account (phone verification) → API key | `NVIDIA_API_KEY` |
| Cloudflare | dash.cloudflare.com → API token with the "Workers AI" permission; the account ID is shown in the dashboard | `CLOUDFLARE_API_TOKEN`, `CLOUDFLARE_ACCOUNT_ID` |
| OpenAI (paid) | platform.openai.com → API keys | `OPENAI_API_KEY` |
| Anthropic (paid) | platform.claude.com → API keys | `ANTHROPIC_API_KEY` |

Add the keys to `.env` (next to `docker-compose.yml`; never commit it), e.g.:

```
GROQ_API_KEY=gsk_...
GEMINI_API_KEY=...
```

`.env.example` lists all the variables. `docker-compose.yml` passes them to the
`web` container, where the generator runs.

## 5. Rebuild, migrate, restart

1. Build the new image (the code and the `anthropic` dependency are baked in):

   ```bash
   docker compose build web
   ```

2. Apply the migrations with the new image before starting it, so that the
   generator finds its tables when it starts:

   ```bash
   docker compose run --rm web ./manage.py migrate
   ```

3. Start the new containers (`web` and `worker` share the image):

   ```bash
   docker compose up -d
   ```

4. Check that everything runs. `llmdetect-generator` should be `RUNNING`:

   ```bash
   docker compose exec web ./manage.py supervisor status
   ```

   Its logs are in the container: one line per request in
   `llmdetect-generator.log`, errors in `llmdetect-generator-err.log`:

   ```bash
   docker compose exec web tail -n 50 /sio2/deployment/logs/llmdetect-generator.log
   ```

   ```bash
   docker compose exec web tail -n 50 /sio2/deployment/logs/llmdetect-generator-err.log
   ```

## 6. Check the model ids

```bash
docker compose exec web ./manage.py llmdetect_models
```

It prints, for every configured provider, the models it offers now: `*` marks
the ones in your settings, `!` marks configured ids the provider no longer
offers. Replace the `!` ids with ones from the list (for OpenRouter add `--free`
to see only free models), then restart `web` so the settings are read again:

```bash
docker compose restart web
```

A provider that prints "cannot list models" has a wrong key or address.

## 7. First test

1. Log in as a superuser or a full contest admin of a camp.
2. Open the camp panel → **LLM detection** (or the contest admin menu →
   **Wykrywanie LLM**).
3. Go to **Referencje LLM**.
4. Pick a problem whose statement is already public, press **Wczytaj treść**,
   tick **all** models, one prompt style, count 1, and press **Generuj**.
5. Refresh after a minute or two: each job should be `gotowe` and references
   should appear below. A failed job shows the provider's answer:
   - `HTTP 401` / `403`: wrong key, or the key lacks a permission.
   - `HTTP 404`: the model id does not exist (see section 6).
   - `HTTP 400` mentioning tokens: lower `max_tokens` for that provider.
   - waiting until a time: the provider's limit is used up; it goes on by itself.

The table above the jobs shows, for each provider, its requests today and when
it can be asked next.

If the camp already has submissions, analyse them once (otherwise the first
opening of the panel does it, which can take a while):

```bash
docker compose exec web ./manage.py llmdetect_update <contest_id>
```

## 8. Using it at a camp

Before the camp:

- In **Referencje LLM**, generate references for every problem, several per
  model and prompt style (plain, in Polish, like a high-school student, like an
  olympiad competitor, disguised). Free limits come to a few hundred to a
  thousand requests a day across all providers; start a few days ahead.
- For chats without an API (free ChatGPT, Duck.ai, ...): the **Copy** buttons
  put the exact prompt on the clipboard; paste the whole answer under
  **Dodaj ręcznie** (only its code is kept) and name the chat in "Model".
- In the panel settings, tick the rounds written under supervision without
  internet, if there are any: their code anchors each participant's style.
- Mention the automatic analysis in the camp rules (GDPR: purpose and basis;
  for minors also inform the parents).

During the camp:

- The **review queue** orders participants by risk; it is recomputed when the
  panel is opened and there are new submissions (**Przelicz** forces it).
- A participant's page explains each signal, shows their code next to the
  nearest LLM reference and their usual code, their habits and a timeline.
- Talk to the participant before deciding (e.g. "explain this part"); a high
  score is never a proof. Record the decision; a confirmed submission becomes a
  reference for its problem. Disqualifications go through the existing
  disqualification mechanism.

After the camp, delete the data (decisions are kept anonymized):

```bash
docker compose exec web ./manage.py llmdetect_purge <contest_id>
```

## 9. Privacy

- Participants' code is never sent anywhere; only the statement text you see
  in the box (and a one-line instruction) goes to the models.
- Free tiers may use what they get to train models (Google says so for the
  free Gemini API; Mistral's free plan requires agreeing to it). Statements of
  a camp are secret until it starts: for unpublished problems use paid tiers
  that do not train on your data, or a model on your own machine (Ollama:
  `"type": "openai"`, `"base_url": "http://<host>:11434/v1"`).
- Risk scores are visible only to full contest admins.

## 10. Troubleshooting

- **No "LLM detection" in the menu:** you are a basic admin, or
  `'oioioi.llmdetect'` is not in `INSTALLED_APPS` (restart `web` after editing
  `settings.py`).
- **"Problem statement views are not logged"** on the panel: the middleware
  line from section 3 is missing; timing then counts from the round start.
- **"Could not read the statement":** the statement is a scanned PDF or an
  unusual format; paste its text into the box.
- **Jobs stay "w kolejce" and nothing happens:** check that the generator runs
  (section 5, step 4). Start it with:

  ```bash
  docker compose exec web ./manage.py supervisor start llmdetect-generator
  ```

- **The panel is slow the first time:** it analyses all existing submissions
  once; run `llmdetect_update` (section 7) instead.

## 11. Going back

1. Optionally, delete the module's tables and data first, while it is still
   enabled:

   ```bash
   docker compose exec web ./manage.py migrate llmdetect zero
   ```

2. Remove `'oioioi.llmdetect'`, the middleware line and `LLMDETECT_PROVIDERS`
   from `settings.py`.
3. Return to the noted version and rebuild:

   ```bash
   git checkout <noted commit>
   ```

   ```bash
   docker compose build web
   ```

   ```bash
   docker compose up -d
   ```

If you skip step 1, the module's tables stay in the database unused; nothing
else reads them.
