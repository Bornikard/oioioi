#!/usr/bin/env python3
"""Generate problem descriptions with Gemini (Vertex AI) from a problembase export.

Runs on your own machine, without Django or the database: it reads the .zip
written by ``manage.py problembase_export`` and appends one JSON line per
family to the output file, which ``manage.py problembase_import`` loads.

Requirements: Python 3.10+, httpx; google-auth only when using Application
Default Credentials instead of an API key. The BazaSzkopul venv has both.

Configuration comes from the environment or a .env file (--env):
  VERTEX_PROJECT, VERTEX_LOCATION (default global), VERTEX_API_KEY (optional,
  otherwise Application Default Credentials), GEMINI_MODEL,
  GEMINI_THINKING_LEVEL, GEMINI_INPUT_PRICE_PER_MILLION,
  GEMINI_OUTPUT_PRICE_PER_MILLION.

Examples:
  describe.py export.zip out.jsonl --sample 6 --seed 1     # a few random problems, easy to hard
  describe.py export.zip out.jsonl --family 12 --family 40
  describe.py export.zip out.jsonl --all --workers 6 --max-cost 40
  describe.py export.zip out.jsonl --sample 1 --dry-run    # print the prompt, no request
  describe.py --show out.jsonl                             # read the results in the terminal
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import random
import re
import sys
import threading
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

PROMPT_VERSION = 1
DEFAULT_MODEL = "gemini-3.1-pro-preview"

_spec = importlib.util.spec_from_file_location("problembase_taxonomy", Path(__file__).resolve().parent.parent / "taxonomy.py")
taxonomy = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(taxonomy)

LEVEL_WORDS = [  # camp group names, weakest first
    (1, ("warsztat", "podstaw")),
    (2, ("średni", "sredni")),
    (3, ("zaawans",)),
    (4, ("mistrz",)),
    (5, ("ekstra", "olimp")),
]
STRATA = ["easy", "medium", "hard", "unknown"]


# ---------------------------------------------------------------- input


def load_dotenv(path):
    if not path or not Path(path).exists():
        return
    for line in Path(path).read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip("'\""))


def load_export(path):
    with zipfile.ZipFile(path) as archive:
        return [json.loads(line) for line in archive.read("families.jsonl").decode().splitlines() if line.strip()]


def load_done(path):
    done = {}
    if Path(path).exists():
        for line in Path(path).read_text().splitlines():
            if line.strip():
                record = json.loads(line)
                done[record["family_id"]] = record
    return done


def contest_level(name):
    lower = name.lower()
    return max((level for level, words in LEVEL_WORDS if any(word in lower for word in words)), default=None)


def stratum(record):
    """Rough difficulty from camp results and group names, only for sampling."""
    ratios = [use["full_score"] / use["submitted"] for use in record["contests"] if use.get("full_score") is not None and use["submitted"] >= 5]
    levels = [level for use in record["contests"] if (level := contest_level(use["contest"]))]
    if not ratios and not levels:
        return "unknown"
    ratio = min(ratios) if ratios else None
    level = max(levels) if levels else None
    if (ratio is not None and ratio < 0.2) or (level or 0) >= 4:
        return "hard"
    if (ratio is not None and ratio < 0.5) or level == 3:
        return "medium"
    return "easy"


def sample(records, n, seed):
    """n families spread evenly over the difficulty strata."""
    rng = random.Random(seed)
    groups = {name: [] for name in STRATA}
    for record in records:
        groups[stratum(record)].append(record)
    for group in groups.values():
        rng.shuffle(group)
    chosen = []
    while len(chosen) < n and any(groups.values()):
        for name in STRATA:
            if groups[name] and len(chosen) < n:
                chosen.append(groups[name].pop())
    return chosen


# ---------------------------------------------------------------- prompt


def tex_body(tex):
    match = re.search(r"\\begin\{document\}(.*?)(\\end\{document\}|$)", tex, re.S)
    body = match.group(1) if match else tex
    return re.sub(r"(?m)(?<!\\)%.*$", "", body).strip()


def contests_for_prompt(record):
    lines = []
    for use in record["contests"]:
        line = f"- {use['contest']}"
        if use["round"]:
            line += f", {use['round']}"
        if use["date"]:
            line += f" ({use['date'][:4]})"
        if use.get("submitted"):
            line += f": wysłało {use['submitted']} z {use['participants']} uczestników"
            if use.get("full_score") is not None:
                line += f", na 100% {use['full_score']}"
            if use.get("average_percent") is not None:
                line += f", średnio {use['average_percent']}%"
        lines.append(line)
    return "\n".join(lines) or "- brak danych"


def build_prompt(record):
    statement = tex_body(record["statement_tex"]) if record["statement_tex"] else record["statement_text"]
    parts = [
        f"""Jesteś doświadczonym jurorem olimpiad informatycznych. Budujesz katalog zadań dla instruktorów obozów
przygotowujących do Olimpiady Informatycznej. Instruktor zna algorytmy i chce w kilka sekund zrozumieć,
o co chodzi w zadaniu, jak się je rozwiązuje i jak jest trudne. Odpowiedz po polsku, w formacie JSON
zgodnym ze schematem.

## Pola odpowiedzi
- statement: istota zadania w 1-3 zdaniach (do ok. 400 znaków), językiem matematyki i informatyki
  (ciąg, graf, drzewo, przedziały, plansza, napis), bez fabuły i postaci. Na końcu w nawiasie najważniejsze
  ograniczenia, zwłaszcza rozmiar danych (np. $n \\le 10^6$). Bez formatu wejścia i wyjścia.
  Przykład: „Dla każdego $k$ znaleźć maksymalną sumę spójnego fragmentu długości $k$ w ciągu
  $n$ liczb ($n \\le 10^6$).”
- solution: rozwiązanie wzorcowe w 2-4 zdaniach (do ok. 500 znaków): kluczowa obserwacja, technika
  i złożoność. Opisz rozwiązanie z załączonego kodu wzorcówki i opracowania (jeśli są), nie inne.
  Przykład: „Idziemy od lewej i drzewem Fenwicka liczymy, ile wcześniejszych elementów jest większych
  od bieżącego; $O(n \\log n)$.”
- main_tags: 1-3 techniki stanowiące rdzeń rozwiązania na 100 pkt.
- side_tags: 0-4 techniki pomocnicze (np. sortowanie jako krok wstępny, kompresja współrzędnych).
  Każdy tag w jednej z list, tylko z listy poniżej. Wybieraj najbardziej szczegółowy pasujący tag
  i nie dodawaj ogólniejszego obok (przy „DP na drzewie” nie dawaj „programowanie dynamiczne”).
  „trudna implementacja” tylko wtedy, gdy główną trudnością jest kod; „obserwacja”, gdy kluczem jest
  pomysł niepasujący do żadnej techniki.
- objects: 1-2 obiekty, o których jest zadanie (z listy obiektów).
- rating: trudność rozwiązania na 100 pkt w skali rankingu Codeforces (800-3500, wielokrotność 100).
- rating_reason: jedno krótkie zdanie o tym, co decyduje o trudności.

## Skala rating (punkty odniesienia)
- 800: wypisanie tekstu, proste działania, jedna pętla lub warunek.
- 900-1100: pętle i tablice, prosta symulacja wprost, proste zliczanie.
- 1200-1400: sortowanie, proste zachłanne, sumy prefiksowe, proste wyszukiwanie binarne,
  BFS/DFS na prostym grafie, klasyczne proste DP (plecak, schody).
- 1500-1700: gąsienica z obserwacją, Dijkstra, find-union, sortowanie topologiczne,
  DP z nietrywialnym stanem, wyszukiwanie binarne po wyniku, hasze lub KMP w prostym użyciu.
- 1800-2000: drzewo przedziałowe lub Fenwicka jako główny pomysł, LCA, DP na drzewie,
  połączenie dwóch standardowych technik, wymagająca obserwacja.
- 2100-2400: leniwa propagacja z nietrywialną operacją, optymalizacje DP, centroidy, HLD,
  przepływy z modelowaniem, trudniejsze obserwacje.
- 2500-2900: kilka zaawansowanych technik naraz, struktury trwałe, zaawansowane teksty,
  głęboka obserwacja; trudne zadania finałów OI i PA.
- 3000-3500: najtrudniejsze zadania OI, CEOI, IOI.

Orientacyjnie: OIJ ~800-1400, OI etap I ~1300-2200, etap II ~1700-2700, etap III ~2000-3200.
Grupy na obozach od najsłabszej: warsztatowa/podstawowa, średnia, zaawansowana, mistrzowska, ekstraklasa
(Olimp to zwykle grupy najmocniejsze). Wyniki z obozów pomagają tylko częściowo: zależą od grupy,
czasu i tego, czy zadanie było na zawodach, czy do dorobienia.

## Lista tagów (slug: nazwa)
{taxonomy.for_prompt()}

## Formatowanie
Wzory w LaTeX: krótkie między $...$, dłuższe między $$...$$. Odpowiedź jest w JSON, więc backslashe
w LaTeX-u zapisuj podwójnie (\\\\le, \\\\frac). Opieraj się wyłącznie na materiałach poniżej.

## Zadanie
Nazwa: {record["name"]} (skrót: {record["short_name"]})
Limity: czas {record["limits"].get("time_ms")} ms, pamięć {record["limits"].get("memory_kb")} KB, testów {record["limits"].get("tests")}
Użycia na obozach i wyniki:
{contests_for_prompt(record)}
""",
        f"### Treść{' (LaTeX)' if record['statement_tex'] else ''}\n{statement or '(brak treści)'}\n",
    ]
    if record.get("config_yml"):
        parts.append(f"### config.yml (punktacja podzadań, limity)\n{record['config_yml']}\n")
    if record.get("editorial"):
        parts.append(f"### Opracowanie\n{tex_body(record['editorial'])}\n")
    for solution in record.get("solutions", []):
        parts.append(f"### Rozwiązanie wzorcowe {solution['name']}\n```\n{solution['source']}\n```\n")
    return "\n".join(parts)


RESPONSE_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "statement": {"type": "STRING"},
        "solution": {"type": "STRING"},
        "main_tags": {"type": "ARRAY", "items": {"type": "STRING", "enum": taxonomy.TAG_SLUGS}},
        "side_tags": {"type": "ARRAY", "items": {"type": "STRING", "enum": taxonomy.TAG_SLUGS}},
        "objects": {"type": "ARRAY", "items": {"type": "STRING", "enum": taxonomy.OBJECT_SLUGS}},
        "rating": {"type": "INTEGER"},
        "rating_reason": {"type": "STRING"},
    },
    "required": ["statement", "solution", "main_tags", "side_tags", "objects", "rating", "rating_reason"],
    "propertyOrdering": ["statement", "solution", "main_tags", "side_tags", "objects", "rating", "rating_reason"],
}


# ---------------------------------------------------------------- answer


def repair_latex(text):
    """Undo JSON escapes the model produced with single backslashes (\\times -> TAB + "imes")."""
    text = text.replace("\t", "\\t").replace("\b", "\\b").replace("\f", "\\f").replace("\r", "\\r")
    text = re.sub(r"\n(?=(?:eq|abla|ot|ewline|u|i)(?![a-zA-Z]))", r"\\n", text)
    return re.sub(r"\\n(?=\\n|[A-ZĄĆĘŁŃÓŚŹŻ0-9]|\s|$)", "\n", text)


def normalize(data):
    for key in RESPONSE_SCHEMA["required"]:
        if key not in data:
            raise ValueError(f"missing {key}")
    for key in ("statement", "solution", "rating_reason"):
        if not isinstance(data[key], str) or not data[key].strip():
            raise ValueError(f"{key} must be non-empty text")
        data[key] = repair_latex(data[key].strip())
    main = list(dict.fromkeys(t for t in data["main_tags"] if t in taxonomy.TAG_NAMES))[:3]
    if not main:
        raise ValueError("no valid main_tags")
    covered = taxonomy.expand(main)
    side = [t for t in dict.fromkeys(data["side_tags"]) if t in taxonomy.TAG_NAMES and t not in covered][:4]
    # drop a tag that is an ancestor of another chosen tag
    chosen = main + side
    ancestors = set().union(*(taxonomy.expand([t]) - {t} for t in chosen))
    data["main_tags"] = [t for t in main if t not in ancestors] or main
    data["side_tags"] = [t for t in side if t not in ancestors]
    data["objects"] = [o for o in dict.fromkeys(data["objects"]) if o in taxonomy.OBJECT_NAMES][:2]
    if not isinstance(data["rating"], int):
        raise ValueError("rating must be an integer")
    data["rating"] = int(round(max(800, min(3500, data["rating"])) / 100) * 100)
    return data


# ---------------------------------------------------------------- Gemini

_auth_lock = threading.Lock()
_credentials = None


def endpoint(model):
    project = os.environ.get("VERTEX_PROJECT")
    if not project:
        raise SystemExit("Set VERTEX_PROJECT (environment or --env file)")
    location = os.environ.get("VERTEX_LOCATION", "global")
    host = "aiplatform.googleapis.com" if location == "global" else f"{location}-aiplatform.googleapis.com"
    url = f"https://{host}/v1/projects/{project}/locations/{location}/publishers/google/models/{model}:generateContent"
    if os.environ.get("VERTEX_API_KEY"):
        return url, {}, {"key": os.environ["VERTEX_API_KEY"]}
    global _credentials
    import google.auth
    import google.auth.transport.requests

    with _auth_lock:
        if _credentials is None:
            _credentials, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
        if not _credentials.valid:
            _credentials.refresh(google.auth.transport.requests.Request())
        return url, {"Authorization": f"Bearer {_credentials.token}"}, {}


def call_gemini(client, prompt, model, thinking, retries=5):
    payload = {
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": {
            "temperature": 1.0,
            "responseMimeType": "application/json",
            "responseSchema": RESPONSE_SCHEMA,
            "maxOutputTokens": 24000,
            "thinkingConfig": {"thinkingLevel": thinking.upper()},
        },
    }
    for attempt in range(retries):
        try:
            url, headers, params = endpoint(model)
            response = client.post(url, headers=headers, params=params, json=payload)
            if response.is_error:
                raise RuntimeError(f"HTTP {response.status_code}: {response.text[:800]}")
            body = response.json()
            candidate = body["candidates"][0]
            if candidate.get("finishReason") not in (None, "STOP"):
                raise RuntimeError(f"finishReason={candidate.get('finishReason')}")
            text = "".join(part.get("text", "") for part in candidate["content"]["parts"] if not part.get("thought"))
            usage = body.get("usageMetadata", {})
            return normalize(json.loads(text)), {
                "input": int(usage.get("promptTokenCount", 0)),
                "output": int(usage.get("candidatesTokenCount", 0)) + int(usage.get("thoughtsTokenCount", 0)),
            }
        except Exception as error:  # network, HTTP, malformed or invalid answer: retry
            if isinstance(error, SystemExit):
                raise
            if attempt == retries - 1:
                raise RuntimeError(f"no valid answer: {error}") from error
            time.sleep(min(90, 5 * 2**attempt))
    raise AssertionError("unreachable")


# ---------------------------------------------------------------- CLI


def show(path):
    for record in load_done(path).values():
        a = record["analysis"]
        tags = ", ".join(taxonomy.TAG_NAMES[t] for t in a["main_tags"])
        side = ", ".join(taxonomy.TAG_NAMES[t] for t in a["side_tags"])
        print(f"\n## {record['name']} ({record['short_name']}, rodzina {record['family_id']})  rating {a['rating']}")
        print(f"Zadanie: {a['statement']}")
        print(f"Rozwiązanie: {a['solution']}")
        print(f"Tagi: {tags}" + (f" | poboczne: {side}" if side else "") + f" | obiekty: {', '.join(a['objects'])}")
        print(f"Trudność: {a['rating_reason']}")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("export", nargs="?", help="zip from manage.py problembase_export")
    parser.add_argument("output", nargs="?", help="JSONL to append results to")
    parser.add_argument("--show", metavar="JSONL", help="print results from a JSONL file and exit")
    parser.add_argument("--env", default=".env", help="file with VERTEX_* and GEMINI_* settings (default ./.env)")
    parser.add_argument("--family", type=int, action="append", help="describe this family (repeatable)")
    parser.add_argument("--sample", type=int, help="describe N random families, spread from easy to hard")
    parser.add_argument("--seed", type=int, default=None, help="random seed for --sample")
    parser.add_argument("--all", action="store_true", help="describe every family that is not done yet")
    parser.add_argument("--redo", action="store_true", help="describe chosen families again even if done with this prompt")
    parser.add_argument("--model", help=f"Gemini model (default GEMINI_MODEL or {DEFAULT_MODEL})")
    parser.add_argument("--thinking", choices=["low", "medium", "high"], help="thinking level (default GEMINI_THINKING_LEVEL or high)")
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--max-cost", type=float, default=5.0, help="stop after this many dollars (estimate, default 5)")
    parser.add_argument("--dry-run", action="store_true", help="print the prompt of the first chosen family, send nothing")
    args = parser.parse_args()

    if args.show:
        show(args.show)
        return
    if not args.export or not args.output:
        parser.error("export and output are required")
    load_dotenv(args.env)
    model = args.model or os.environ.get("GEMINI_MODEL", DEFAULT_MODEL)
    thinking = args.thinking or os.environ.get("GEMINI_THINKING_LEVEL", "high").lower()
    input_price = float(os.environ.get("GEMINI_INPUT_PRICE_PER_MILLION", "2"))
    output_price = float(os.environ.get("GEMINI_OUTPUT_PRICE_PER_MILLION", "12"))

    records = load_export(args.export)
    done = load_done(args.output)
    finished = {fid for fid, r in done.items() if r.get("prompt_version") == PROMPT_VERSION and not args.redo}
    if args.family:
        chosen = [r for r in records if r["family_id"] in set(args.family)]
    elif args.sample:
        chosen = sample([r for r in records if r["family_id"] not in finished], args.sample, args.seed)
    elif args.all:
        chosen = [r for r in records if r["family_id"] not in finished]
    else:
        parser.error("choose --family, --sample or --all")
    if not args.redo:
        chosen = [r for r in chosen if r["family_id"] not in finished or args.family]
    if not chosen:
        print("nothing to do")
        return
    if args.dry_run:
        prompt = build_prompt(chosen[0])
        print(prompt)
        print(f"\n--- {len(prompt)} chars, ~{len(prompt) // 3} tokens; {len(chosen)} families chosen", file=sys.stderr)
        return

    import httpx

    print(f"model {model}, thinking {thinking}, {len(chosen)} families, limit ${args.max_cost:.2f}")
    spent, lock, stop = 0.0, threading.Lock(), threading.Event()
    output = open(args.output, "a", encoding="utf-8")

    def work(record):
        if stop.is_set():
            return record, None, None
        return record, *call_gemini(client, build_prompt(record), model, thinking)

    with httpx.Client(timeout=600) as client, ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = [pool.submit(work, record) for record in chosen]
        for i, future in enumerate(as_completed(futures), 1):
            try:
                record, analysis, usage = future.result()
            except RuntimeError as error:
                print(f"[{i}/{len(chosen)}] error: {error}", file=sys.stderr)
                continue
            if analysis is None:
                continue
            cost = (usage["input"] * input_price + usage["output"] * output_price) / 1e6
            with lock:
                spent += cost
                result = {
                    "family_id": record["family_id"],
                    "problem_id": record["problem_id"],
                    "name": record["name"],
                    "short_name": record["short_name"],
                    "model": model,
                    "thinking": thinking,
                    "prompt_version": PROMPT_VERSION,
                    "taxonomy_version": taxonomy.VERSION,
                    "analysis": analysis,
                    "usage": usage,
                    "cost": round(cost, 5),
                }
                output.write(json.dumps(result, ensure_ascii=False) + "\n")
                output.flush()
                tags = ", ".join(analysis["main_tags"])
                print(f"[{i}/{len(chosen)}] {record['short_name']}: rating {analysis['rating']}, {tags} (${cost:.3f}, total ${spent:.2f})")
                if spent >= args.max_cost and not stop.is_set():
                    stop.set()
                    print(f"cost limit ${args.max_cost:.2f} reached, finishing requests in flight")
    output.close()
    print(f"done, ${spent:.2f}")


if __name__ == "__main__":
    main()
