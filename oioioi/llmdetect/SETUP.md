# LLM detection: updating a running server and setting it up

This guide takes a server that runs this repository with `docker-compose.yml`
(the Czecie camp production setup) to the version with `oioioi.llmdetect`, and
then shows how the staff get LLM reference solutions on their own computers.

The server itself never contacts any LLM and needs no API keys. It gives the
staff the prompts, and takes the answers back: pasted, or uploaded in a simple
text format that the included script writes.

Server commands are run on the server, in the directory with
`docker-compose.yml` and `.env`.

## What the update changes

- A new Django app, `oioioi.llmdetect`, with 2 migrations (new tables only;
  existing tables are not touched).
- An "LLM detection" button in the camp panel and an item in the contest admin
  menu, both for full contest admins only (not basic admins).
- No new dependencies, programs or settings beyond enabling the app.

## 1. Already deployed the earlier version with the server-side generator?

Skip this section if you never ran the version of this branch whose page had
a "Generate" button and whose settings had `LLMDETECT_PROVIDERS`.

That version had two more migrations (`0003_generationjob`,
`0004_providerstate`) that no longer exist. Remove their tables **before**
pulling the new code, while the old code is still there:

```bash
docker compose exec web ./manage.py migrate llmdetect 0002
```

Then, from `$SIO_DATA_DIR/settings.py`, delete the `LLMDETECT_PROVIDERS` block,
and from `.env` delete the provider API keys (`OPENROUTER_API_KEY`,
`GEMINI_API_KEY`, ...). The server does not need them any more; keep the keys
for the script on your own computer (section 7).

## 2. Before you start

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

## 3. Get the new code

If the pull request is merged into `czecie-camp`:

```bash
git checkout czecie-camp
```

```bash
git pull origin czecie-camp
```

## 4. Enable the module in settings

Edit `settings.py` in your data directory (`$SIO_DATA_DIR/settings.py`, the
directory set in `.env`). It is mounted into the container, so the rebuild
does not change it.

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

## 5. Rebuild, migrate, restart

1. Build the new image (the code is copied into it):

   ```bash
   docker compose build web
   ```

2. Apply the migrations:

   ```bash
   docker compose run --rm web ./manage.py migrate
   ```

3. Start the new containers (`web` and `worker` share the image):

   ```bash
   docker compose up -d
   ```

4. If the camp already has submissions, analyse them once (otherwise the first
   opening of the panel does it, which can take a while):

   ```bash
   docker compose exec web ./manage.py llmdetect_update <contest_id>
   ```

## 6. Check it works

1. Log in as a superuser or a full contest admin of a camp.
2. Open the camp panel → **LLM detection** (in Polish: **Wykrywanie LLM**), or
   the same item in the contest admin menu.
3. Open **Referencje LLM**, pick a problem and press **Wczytaj treść**: the text
   of its statement should appear in the box.

## 7. Getting reference solutions

References are LLM solutions of each problem; the closer a participant's code
is to one of them, the higher the risk. Get tens per problem, from the models
participants use, in several prompt styles, before each round. Keep wrong
answers too: their mistakes are a strong fingerprint.

On the **Referencje LLM** page, first **Wczytaj treść** (load the statement),
check the text and remove anything that must not leave the camp. Then use one
or both of the ways below.

### A. By hand, from chat websites

For chats without an API (free ChatGPT, Claude, Gemini, Duck.ai, ...):

1. Press one of the **copy** buttons (one per prompt style): the exact prompt
   (instruction and statement) goes to the clipboard.
2. Paste it into the chat.
3. Paste the whole answer back, either under **Dodaj jedno** (one solution,
   with the chat's name in "Model"), or collect many in a text file in the
   format below and add them at once under **Dodaj wiele**:

   ```
   ### reference problem=abc model="ChatGPT free" prompt=polish
   <the whole answer; only its code block is kept>
   ### reference problem=abc model=duck.ai prompt=student
   <the whole answer>
   ```

   `problem` is the short name of the problem (or choose a default problem on
   the form and leave it out); `model` and `prompt` are labels for you. Values
   with spaces go in quotes.

### B. With the script, on your own computer

The script asks the free (and, if you want, paid) APIs of LLM providers with
your own keys, and writes the answers in the format above. It uses the free
limits of all your providers side by side, and continues where it stopped
when run again.

#### Once: install and configure

1. Install Python 3.10 or newer. For Claude only, also run `pip install anthropic`.
2. On the **Referencje LLM** page, press **Pobierz skrypt** and save
   `llm_references.py` in a new folder.
3. In that folder, create the configuration:

   ```bash
   python3 llm_references.py --example-config > providers.json
   ```

4. Create accounts and API keys at the providers you want (each free; one
   account per provider; several accounts on one provider to get more quota
   usually breaks its terms). Most need no credit card, some ask for a phone
   number:

   | Provider | Where to get a key | Environment variable |
   |---|---|---|
   | OpenRouter | openrouter.ai → Settings → Keys | `OPENROUTER_API_KEY` |
   | Google Gemini | aistudio.google.com → Get API key | `GEMINI_API_KEY` |
   | Groq | console.groq.com → API Keys | `GROQ_API_KEY` |
   | Mistral | console.mistral.ai → free "Experiment" plan (phone verification) → API Keys | `MISTRAL_API_KEY` |
   | Cerebras | cloud.cerebras.ai → API Keys (free trial) | `CEREBRAS_API_KEY` |
   | GitHub Models | github.com → Settings → Developer settings → fine-grained token with "Models: read" | `GITHUB_MODELS_TOKEN` |
   | NVIDIA | build.nvidia.com → NVIDIA Developer account (phone verification) → API key | `NVIDIA_API_KEY` |
   | Cloudflare | dash.cloudflare.com → API token with the "Workers AI" permission | `CLOUDFLARE_API_TOKEN` |
   | Anthropic (paid) | platform.claude.com → API keys | `ANTHROPIC_API_KEY` |

   You do not need all of them; every one adds its daily quota. Providers whose
   variable is not set are skipped.

5. Set the keys as environment variables in the terminal where you run the
   script. Linux and macOS:

   ```bash
   export GROQ_API_KEY=gsk_...
   ```

   Windows (PowerShell):

   ```powershell
   $env:GROQ_API_KEY = "gsk_..."
   ```

   (To keep them between sessions, put the `export` lines in `~/.bashrc` or
   `~/.zshrc`, or use "Edit environment variables for your account" on
   Windows.) Instead of a variable, `providers.json` may hold the key itself
   as `"api_key": "..."`; then do not share that file.

6. For Cloudflare, replace `<ACCOUNT_ID>` in its `base_url` in
   `providers.json` with your account ID (shown in the Cloudflare dashboard).

7. Check the model names, which change often:

   ```bash
   python3 llm_references.py --list-models
   ```

   For every provider with a key it prints the models it offers: `*` marks the
   ones in `providers.json`, `!` marks names in `providers.json` the provider
   no longer offers. Fix the `"models"` lists in `providers.json`. Prefer
   bigger coding-capable models: they are what participants use.

   The options of a provider in `providers.json`:

   | Option | Meaning |
   |---|---|
   | `base_url` | Address of an OpenAI-compatible API. |
   | `api_key_env` / `api_key` | Name of the variable with the key / the key itself. |
   | `models` | Model names to ask; empty or missing skips the provider. |
   | `min_interval` | Seconds between two requests to this provider. |
   | `daily_limit` | Stop after this many requests in one run. |
   | `max_tokens` | Longest answer to ask for. |
   | `type` | `"anthropic"` for Claude; anything else is OpenAI-compatible. |

   A model on your own machine (Ollama, nothing leaves your computer) is
   `{"base_url": "http://localhost:11434/v1", "models": ["<model>"]}`.

#### Before each round: run it

1. On the **Referencje LLM** page, download the prompts:
   - **Pobierz prompty wszystkich zadań**: all problems at once, with the
     statements as read from their files;
   - or, after **Wczytaj treść** and checking the text, **Pobierz te prompty
     dla skryptu**: one problem with the statement as edited, and only the
     prompt styles ticked.
2. Run the script with the downloaded file (here 2 answers per model and
   prompt style):

   ```bash
   python3 llm_references.py prompts-<contest>.json -n 2
   ```

   It prints each answer as it comes and writes them to `references.txt`. Useful
   options: `--providers groq,gemini`, `--problems abc,xyz`,
   `--only-prompts plain,student`, `-o other.txt`.

3. On the **Referencje LLM** page, under **Dodaj wiele**, upload
   `references.txt` (or paste it if it is small) and press **Dodaj**. Answers
   already stored are skipped, so uploading the same file again after a later
   run adds only the new ones. If any entry has an error (e.g. an unknown
   problem), nothing is added and the errors are listed.

When a provider's free quota runs out, the script says so and leaves its
answers for the next run; run the same command the next day and it asks only
for what is missing. Ctrl+C stops it without losing what it wrote.

Rough free capacity with all the providers above: a few hundred to a thousand
answers a day. With 5 prompt styles and ~20 models, 2 answers each is about 200
answers per problem, so start a few days before the camp, or use fewer
models and styles per problem.

## 8. Using it at a camp

- In the panel settings, tick the rounds written under supervision without
  internet, if there are any: their code anchors each participant's style.
- Under **earlier contests**, tick previous camps of the same participants.
  Their submissions there are added to the profiles (not scored again), so
  habits are known from the first day of the camp. Each counts with the
  **weight of earlier contests** (0.5 by default; a submission of this camp
  counts 1 on the same day and less on later days). Only contests you
  administer are listed. The first **Przelicz** after ticking them analyses
  those submissions, which takes a while for big contests.
- Mention the automatic analysis in the camp rules (GDPR: purpose and basis;
  for minors also inform the parents).
- The **review queue** orders participants by risk; it is recomputed when the
  panel is opened and there are new submissions (**Przelicz** forces it).
- A participant's page explains each signal, shows their code next to the
  nearest LLM reference and their usual code, their habits and a timeline.
- Talk to the participant before deciding (e.g. "explain this part"); a high
  score is never a proof. Record the decision; a confirmed submission becomes a
  reference for its problem. Disqualifications go through the existing
  disqualification mechanism.
- After the camp, delete the data (decisions are kept anonymized):

  ```bash
  docker compose exec web ./manage.py llmdetect_purge <contest_id>
  ```

## 9. Privacy

- Participants' code is never sent anywhere. Only statements leave the camp,
  and only from the staff's computers, in prompts they can read.
- Free tiers may use what they get to train models (Google says so for the
  free Gemini API; Mistral's free plan requires agreeing to it). Statements of
  a camp are secret until it starts: for unpublished problems use paid tiers
  that do not train on your data, or a local model.
- Risk scores are visible only to full contest admins.

## 10. Troubleshooting

- **No "LLM detection" in the menu:** you are a basic admin, or
  `'oioioi.llmdetect'` is not in `INSTALLED_APPS` (restart `web` after editing
  `settings.py`: `docker compose restart web`).
- **"Problem statement views are not logged"** on the panel: the middleware
  line from section 4 is missing; timing then counts from the round start.
- **"Could not read the statement":** the statement is a scanned PDF or an
  unusual format; paste its text into the box.
- **Uploading fails or the page reports a too large request:** upload the file
  instead of pasting it.
- **The script says "skipped, ... is not set":** that provider's environment
  variable is not set in this terminal.
- **The script reports `HTTP 401`/`403`:** wrong key, or the key lacks a
  permission; `HTTP 404`: the model name does not exist (`--list-models`);
  `HTTP 400` mentioning tokens: lower `max_tokens` for that provider.
- **The panel is slow the first time:** it analyses all existing submissions
  once; run `llmdetect_update` (section 5) instead.

## 11. Going back

1. Optionally, delete the module's tables and data first, while it is still
   enabled:

   ```bash
   docker compose exec web ./manage.py migrate llmdetect zero
   ```

2. Remove `'oioioi.llmdetect'` and the middleware line from `settings.py`.
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
