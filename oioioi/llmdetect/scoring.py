"""Turns the features of all submissions of a contest into risk scores.

Everything is recomputed for the whole contest at once: at camp scale
(hundreds of participants, thousands of submissions) this takes seconds,
and recomputing keeps the results consistent when rounds, scores or
references change. Only extracting features from the code is cached.

Each signal is a number in [0, 1] with the facts behind it; the risk is
``sigmoid(bias + sum(weight * signal))``, so every signal can only raise
it and the staff sees how much each one did.
"""

import contextlib
import logging
import math
import statistics
from collections import Counter, defaultdict
from dataclasses import dataclass, field

from django.conf import settings
from django.contrib.auth.models import User
from django.db import transaction
from django.utils import timezone

from oioioi.contests.models import ContestPermission
from oioioi.llmdetect.features import FEATURES_VERSION, analyze_source, containment, llm_style, style_vector
from oioioi.llmdetect.habits import CATEGORICAL_HABITS, LLM_VALUES, extract_habits
from oioioi.llmdetect.models import (
    LLMDetectConfig,
    LLMReferenceSolution,
    ProblemViewEvent,
    ReviewDecision,
    RiskAssessment,
    SubmissionFeatures,
    get_config,
    mark_dirty,
)
from oioioi.programs.models import ProgramSubmission

logger = logging.getLogger(__name__)

SUBMISSION_KINDS = ("NORMAL", "IGNORED")
# Name of a pasted submission before SIO2 renames the stored file.
PASTED_PREFIX = "__pasted_code."

DEFAULT_WEIGHTS = {
    "llm_reference": 3.5,
    "llm_style": 1.5,
    "style_shift": 2.5,
    "deviation": 0.5,
    "fast_solve": 1.2,
    "burst": 0.8,
    "rewrite": 1.0,
    "first_try": 0.3,
    "score_surprise": 1.0,
    "pasted": 0.5,
    "peer_similarity": 0.8,
    "chat_leftovers": 3.0,
    "paste_characters": 2.0,
    "habit_break": 2.5,
    "vocabulary": 1.0,
}
BIAS = -3.0
# After a round its solutions are discussed, and rewriting the presented one
# is not cheating: signals comparing with the author's own style count half.
AFTER_ROUND_HALVED = frozenset({"style_shift", "deviation", "habit_break", "vocabulary"})
# Submissions at or above this risk are flagged.
FLAG_THRESHOLD = 0.5
# How many submissions the population prior of a profile is worth.
PROFILE_PRIOR_WEIGHT = 5.0
# Weight of a submission in the profile, per day of distance in time.
PROFILE_DECAY_PER_DAY = 0.9
# Weight of submissions from supervised rounds in the profile.
TRUSTED_WEIGHT = 3.0
# A fingerprint in submissions of more than this fraction of the
# participants of a problem is boilerplate (input reading, fast IO...).
BOILERPLATE_FRACTION = 0.4
MIN_VARIANCE = 0.02
# A profile uses at most this many (the latest) submissions per problem.
PROFILE_PER_PROBLEM = 3
# A profile uses at most this many (the latest) submissions from earlier contests.
HISTORY_PER_USER = 40
# Submissions of other participants shown as sharing code with one: at most
# this many (one per participant), with at least this share of its code.
PEERS_SHOWN = 5
PEERS_SHOWN_FROM = 0.2
# A user's risk is the sum of their this many worst problems.
USER_TOP_PROBLEMS = 3
# Habits that change together: an editor that formats the code changes all
# of "formatting" at once, another computer without the usual template all of
# "template". Formatting is weak evidence: editors and camp computers differ.
HABIT_GROUP = {
    **dict.fromkeys(("template", "macros", "includes", "namespace", "ll_alias", "fast_io", "main"), "template"),
    **dict.fromkeys(("indent", "braces", "else", "keyword_space", "operator_space", "comma_space", "line_endings"), "formatting"),
    **dict.fromkeys(("storage", "size_constant", "test_loop", "naming", "function_names"), "structure"),
}
HABIT_GROUP_WEIGHT = {"template": 1.0, "formatting": 0.6, "structure": 1.0}
# A habit counts when kept in this share of at least this many other submissions.
HABIT_STABILITY = 0.8
HABIT_MIN_HISTORY = 3
# How sure each chat leftover or pasted character alone is.
CHAT_STRENGTH = {
    "markdown fence": 1.0,
    "here is": 0.9,
    "explanation": 0.9,
    "complexity": 0.8,
    "example usage": 0.8,
    "approach": 0.8,
    "big O": 0.5,
    "step N": 0.5,
    "markdown": 0.4,
    "edge case": 0.4,
}
PASTE_CHAR_STRENGTH = {
    "no-break space": 0.9,
    "narrow no-break space": 0.9,
    "figure space": 0.9,
    "zero-width space": 0.9,
    "zero-width non-joiner": 0.9,
    "zero-width joiner": 0.9,
    "word joiner": 0.9,
    "zero-width no-break space": 0.9,
    "invisible character": 0.7,
    # Typed by some people too (e.g. Polish quotes in comments).
    "typographic quote": 0.3,
    "em dash": 0.4,
    "en dash": 0.3,
    "arrow": 0.4,
    "≤ sign": 0.4,
    "≥ sign": 0.4,
}


def weights():
    return {**DEFAULT_WEIGHTS, **getattr(settings, "LLMDETECT_WEIGHTS", {})}


def clamp(value, low=0.0, high=1.0):
    return max(low, min(high, value))


def sigmoid(x):
    return 1.0 / (1.0 + math.exp(-x))


def read_source(submission):
    """The code of a submission, read the way SIO2 itself reads it."""
    data = submission.source_file.read()
    # Filetracker's stream has no close(); local storages do.
    with contextlib.suppress(AttributeError):
        submission.source_file.close()
    return data.decode("utf-8", "replace") if isinstance(data, bytes) else data


def compute_features(submission, source=None, pasted=None, backfill=False):
    """Computes and stores the features of a :class:`ProgramSubmission`.

    Whether the code was pasted into the form is only known when the
    submission is created (the stored file is renamed), so it is kept from
    then on unless given. ``backfill`` means the submission was sent before
    this module analysed it (e.g. an older contest): then nobody knows, and
    habits that depend on it (line endings: pasted code always has CRLF) are
    left out.
    """
    if source is None:
        source = read_source(submission)
    filename = submission.source_file.name.rsplit("/", 1)[-1]
    paste_known = True
    if pasted is None:
        existing = SubmissionFeatures.objects.filter(submission=submission).only("pasted", "paste_known").first()
        if existing is not None:
            pasted, paste_known = existing.pasted, existing.paste_known
        elif backfill:
            pasted, paste_known = False, False
        else:
            pasted = filename.startswith(PASTED_PREFIX)
    analysis = analyze_source(source, filename)
    habits = extract_habits(source, analysis["language"], pasted=pasted, paste_known=paste_known)
    features, _created = SubmissionFeatures.objects.update_or_create(
        submission=submission,
        defaults={
            "version": FEATURES_VERSION,
            "language": analysis["language"],
            "pasted": pasted,
            "paste_known": paste_known,
            "features": analysis["features"],
            "fingerprints": analysis["fingerprints"],
            "line_hashes": analysis["line_hashes"],
            "code_lines": analysis["code_lines"],
            "habits": habits,
            "computed_at": timezone.now(),
        },
    )
    return features


def staff_user_ids(contest):
    ids = set(ContestPermission.objects.filter(contest=contest).values_list("user_id", flat=True))
    ids |= set(User.objects.filter(is_superuser=True).values_list("id", flat=True))
    return ids


def contest_submissions(contest):
    return (
        ProgramSubmission.objects.filter(problem_instance__contest=contest, kind__in=SUBMISSION_KINDS, user__isnull=False)
        .exclude(user_id__in=staff_user_ids(contest))
        .select_related("problem_instance__round")
        .order_by("date", "id")
    )


def history_submissions(contest, user_ids):
    """Submissions of ``user_ids`` in the earlier contests chosen for ``contest``:
    they only add to the participants' profiles.
    """
    contests = get_config(contest).history_contests.exclude(id=contest.id)
    return (
        ProgramSubmission.objects.filter(problem_instance__contest__in=contests, kind__in=SUBMISSION_KINDS, user_id__in=user_ids)
        .select_related("problem_instance__round")
        .order_by("date", "id")
    )


def ensure_features(submissions, errors=None):
    """Features of ``submissions`` by submission id, computing the missing ones.

    Submissions that cannot be analysed are left out; ``(submission id,
    message)`` of each is appended to ``errors`` if given.
    """
    submissions = list(submissions)
    stored = {f.submission_id: f for f in SubmissionFeatures.objects.filter(submission__in=submissions, version=FEATURES_VERSION)}
    for submission in submissions:
        if submission.id not in stored:
            try:
                # Not analysed when it was sent (or the analysis failed then).
                stored[submission.id] = compute_features(submission, backfill=True)
            except Exception as error:
                logger.warning("Cannot compute LLM detection features of submission %s", submission.id, exc_info=True)
                if errors is not None:
                    errors.append((submission.id, f"{type(error).__name__}: {error}"[:300]))
    return stored


def needs_update(contest, config=None):
    if config is None:
        config = get_config(contest)
    if config.dirty or config.computed_at is None:
        return True
    # The analysis of the code changed (FEATURES_VERSION) since it was stored.
    if SubmissionFeatures.objects.filter(submission__problem_instance__contest=contest).exclude(version=FEATURES_VERSION).exists():
        return True
    return contest_submissions(contest).filter(id__gt=config.last_submission_id).exists()


@dataclass(eq=False)
class Item:
    submission: ProgramSubmission
    features: SubmissionFeatures
    vector: list
    style: float
    style_reasons: list
    fingerprints: set
    habits: dict
    trusted: bool
    score: float | None
    start: object = None
    signals: dict = field(default_factory=dict)
    context: dict = field(default_factory=dict)
    # Risk from the signals that do not depend on the author's profile.
    independent: float = 0.0
    # From an earlier contest: only part of the author's profile.
    history: bool = False

    def __post_init__(self):
        # Plain attributes: these are read in the innermost loops.
        self.user_id = self.submission.user_id
        self.pi_id = self.submission.problem_instance_id
        self.problem_id = self.submission.problem_instance.problem_id
        self.date = self.submission.date
        # After a round, its problems are discussed and solved again, so
        # similar code, speed or solving above one's level mean nothing.
        round = self.submission.problem_instance.round
        self.during_round = round is None or round.end_date is None or round.start_date <= self.date <= round.end_date


def _item(submission, features, trusted=False, history=False):
    style, reasons = llm_style(features.features)
    return Item(
        submission=submission,
        features=features,
        vector=style_vector(features.features),
        style=style,
        style_reasons=reasons,
        fingerprints=set(features.fingerprints),
        habits=features.habits or {},
        trusted=trusted,
        score=_score_fraction(submission),
        history=history,
    )


def _score_fraction(submission):
    if submission.score is None:
        return None
    try:
        points = submission.score.to_int()
        maximum = submission.max_score.to_int() if submission.max_score is not None else None
    except (AttributeError, TypeError, ValueError):
        return None
    if maximum:
        return clamp(points / maximum)
    # Without the maximum, assume problems are worth 100 points, as in OI.
    return clamp(points / 100.0)


def _population(vectors):
    dims = len(vectors[0])
    means = [statistics.fmean(v[j] for v in vectors) for j in range(dims)]
    variances = [max(MIN_VARIANCE, statistics.fmean((v[j] - means[j]) ** 2 for v in vectors)) for j in range(dims)]
    return means, variances


def _robust_scores(values):
    """Robust z-scores of ``values`` (median and MAD)."""
    if len(values) < 3:
        return [0.0] * len(values)
    median = statistics.median(values)
    mad = statistics.median(abs(v - median) for v in values) * 1.4826
    scale = mad if mad > 1e-9 else (statistics.pstdev(values) or 1.0)
    return [(v - median) / scale for v in values]


class ContestAssessor:
    """Assesses all submissions of a contest.

    Several requests (two staff members on the panel, the panel and
    ``llmdetect_update``) may want to assess a contest at once. The
    computation takes seconds to minutes, so it runs without a lock; only
    claiming it and saving its results lock the contest's
    :class:`LLMDetectConfig` row, briefly:

    * claiming checks again whether the results are out of date (another
      run may have just saved them) and clears ``dirty``, so that a change
      made during the computation (a reference, a decision) sets it again
      and is not lost;
    * saving keeps the results of the run that started last: a run that
      started earlier computed from older data and does not overwrite them.

    The views that assess must not run in the request's transaction
    (``transaction.non_atomic_requests``), or the lock and the new results
    would be held until the end of the request.
    """

    def __init__(self, contest):
        self.contest = contest
        self.config = get_config(contest)
        self.weights = weights()

    def _locked_config(self):
        return LLMDetectConfig.objects.select_for_update().get(pk=self.config.pk)

    def run(self, force=True):
        """Assesses the contest; with ``force=False`` only if the results are
        out of date. Returns the assessed items, or ``None`` when the results
        were up to date or a run that started later saved newer ones.
        """
        with transaction.atomic():
            config = self._locked_config()
            if not force and not needs_update(self.contest, config):
                return None
            self.started = timezone.now()
            if config.dirty:
                config.dirty = False
                config.save(update_fields=["dirty"])
            self.config = config
        try:
            items = self._compute()
        except BaseException:
            # The claim cleared the flag: the results are still out of date.
            mark_dirty(self.contest)
            raise
        return items if self._save(items) else None

    def _compute(self):
        submissions = list(contest_submissions(self.contest))
        self.last_submission_id = max((s.id for s in submissions), default=0)
        self.errors = []
        features = ensure_features(submissions, self.errors)
        trusted_rounds = set(self.config.trusted_rounds.values_list("id", flat=True))
        self.items = [
            _item(submission, features[submission.id], trusted=submission.problem_instance.round_id in trusted_rounds)
            for submission in submissions
            if submission.id in features
        ]
        if not self.items:
            return []
        self.by_user = defaultdict(list)
        self.by_pi = defaultdict(list)
        for item in self.items:
            self.by_user[item.user_id].append(item)
            self.by_pi[item.pi_id].append(item)
        self.history_weight = self.config.history_weight
        self.history = self._history_items()
        self.profile_pool = self._profile_pools(self.history)
        contests = [self.contest.id, *self.config.history_contests.values_list("id", flat=True)]
        self.confirmed = set(
            ReviewDecision.objects.filter(contest__in=contests, verdict=ReviewDecision.VERDICT_CONFIRMED, submission__isnull=False).values_list(
                "submission_id", flat=True
            )
        )
        self._set_start_times()
        self._similarity_signals()
        self._artifact_signals()
        for item in self.items:
            # Both sides, so that a low score is not explained by its few LLM-like features alone.
            towards = [name for name, contribution in item.style_reasons if contribution > 0][:3]
            against = [name for name, contribution in item.style_reasons if contribution < 0][:3]
            item.signals["llm_style"] = (item.style, {"score": item.style, "reasons": towards, "against": against})
            reference = item.signals.get("llm_reference", (0.0, {}))[0]
            artifacts = max(item.signals.get(name, (0.0, {}))[0] for name in ("chat_leftovers", "paste_characters"))
            item.independent = max(reference, item.style, artifacts)
            item.context["trusted"] = item.trusted
            item.context["during_round"] = item.during_round
        self._profile_signals()
        self._habit_signals()
        self._vocabulary_signals()
        self._behaviour_signals()
        self._score_signals()
        for item in self.items:
            self._combine(item)
        return self.items

    # Time when the participant could start working on a problem: the
    # start of its round, or the first time they opened the statement.
    def _set_start_times(self):
        """When the participant could start working on each problem: the start
        of its round, or (if the setting says statements are read in SIO2)
        the first time they opened the statement, if later.
        """
        views = {}
        if self.config.time_from_statement:
            events = ProblemViewEvent.objects.filter(problem_instance__contest=self.contest)
            views = {(user_id, pi_id): date for user_id, pi_id, date in events.values_list("user_id", "problem_instance_id", "date")}
        for item in self.items:
            round = item.submission.problem_instance.round
            start = round.start_date if round is not None else None
            viewed = views.get((item.user_id, item.pi_id))
            if viewed is not None and (start is None or viewed > start):
                start = viewed
            item.start = start

    def _personal_code(self):
        """Per participant and problem, the fingerprints of the code they also
        wrote for other problems (here or in earlier contests): their own
        template and library, which says nothing about copying.
        """
        per_problem = defaultdict(lambda: defaultdict(set))
        for item in self.items:
            per_problem[item.user_id][item.problem_id] |= item.fingerprints
        for user_id, items in self.history.items():
            for item in items:
                per_problem[user_id][item.problem_id] |= item.fingerprints
        personal = {}
        for user_id, problems in per_problem.items():
            counts = Counter(f for prints in problems.values() for f in prints)
            for problem_id, prints in problems.items():
                # Present for another problem too.
                personal[user_id, problem_id] = {f for f in prints if counts[f] > 1}
        return personal

    def _similarity_signals(self):
        references = defaultdict(list)
        for reference in LLMReferenceSolution.objects.filter(problem_instance__contest=self.contest).select_related("submission"):
            references[reference.problem_instance_id].append(reference)
        personal = self._personal_code()
        for pi_id, items in self.by_pi.items():
            users = {item.user_id for item in items}
            common = Counter()
            for user_id in users:
                fingerprints = set().union(*(item.fingerprints for item in items if item.user_id == user_id))
                common.update(fingerprints)
            boilerplate = set()
            if len(users) >= 5:
                boilerplate = {f for f, count in common.items() if count > BOILERPLATE_FRACTION * len(users)}
            for item in items:
                # Without boilerplate, and without the author's own template.
                item.specific = item.fingerprints
                for removed in (boilerplate, personal.get((item.user_id, item.problem_id), set())):
                    specific = item.specific - removed
                    if len(specific) >= 5:
                        item.specific = specific
            self._peer_similarity(items)
            self._reference_similarity(items, references.get(pi_id, []), boilerplate)

    def _peer_similarity(self, items):
        index = defaultdict(list)
        for position, item in enumerate(items):
            for fingerprint in item.specific:
                index[fingerprint].append(position)
        users = [item.user_id for item in items]
        for item in items:
            if not item.specific:
                continue
            counts = Counter()
            for fingerprint in item.specific:
                counts.update(index[fingerprint])
            user_id = item.user_id
            # The closest submission of each other participant, the closest first.
            peers = []
            seen = {user_id}
            for position, shared in counts.most_common():
                if users[position] in seen:
                    continue
                seen.add(users[position])
                similarity = shared / len(item.specific)
                if similarity < PEERS_SHOWN_FROM or len(peers) == PEERS_SHOWN:
                    break
                other = items[position]
                peers.append({"submission": other.submission.id, "user": other.user_id, "similarity": similarity, "during_round": other.during_round})
            best = next(((position, shared) for position, shared in counts.most_common() if users[position] != user_id), None)
            if best is None:
                continue
            item.context["peers"] = peers
            other, shared = items[best[0]], best[1]
            similarity = shared / len(item.specific)
            value = clamp((similarity - 0.5) / 0.4)
            item.context["peer"] = {"submission": other.submission.id, "user": other.user_id, "similarity": similarity}
            if value > 0 and item.during_round and other.during_round:
                item.signals["peer_similarity"] = (value, {"similarity": similarity, "user": other.user_id, "submission": other.submission.id})

    def _reference_similarity(self, items, references, boilerplate):
        if not references:
            return
        reference_prints = []
        for reference in references:
            prints = set(reference.fingerprints)
            specific = prints - boilerplate
            reference_prints.append((reference, specific if len(specific) >= 5 else prints))
        best = {}
        for item in items:
            # A confirmed case may have been copied from an honest participant:
            # code sent before it (and the case itself) is not compared with it.
            usable = [
                (reference, prints)
                for reference, prints in reference_prints
                if reference.submission_id is None or (reference.submission_id != item.submission.id and item.date >= reference.submission.date)
            ]
            if not usable:
                continue
            similarity, reference = max(((containment(item.specific, prints), reference) for reference, prints in usable), key=lambda pair: pair[0])
            best[item] = (similarity, reference)
        if not best:
            return
        # Solutions of the same problem share some code anyway; compare with
        # how close participants usually get (most of them write their own).
        per_user = defaultdict(float)
        for item, (similarity, _reference) in best.items():
            per_user[item.user_id] = max(per_user[item.user_id], similarity)
        baseline = statistics.median(per_user.values()) if len(per_user) >= 5 else 0.0
        threshold = max(0.35, baseline + 0.15)
        for item, (similarity, reference) in best.items():
            item.context["reference"] = {"id": reference.id, "similarity": similarity, "baseline": baseline}
            value = clamp((similarity - threshold) / max(0.9 - threshold, 0.05))
            if value > 0:
                item.signals["llm_reference"] = (
                    value,
                    {"similarity": similarity, "baseline": baseline, "reference": reference.id, "model": reference.model_name},
                )

    # Chat leftovers and characters of a web page: no history needed.
    def _artifact_signals(self):
        for item in self.items:
            chat = item.habits.get("chat", [])
            if chat:
                value = 1.0 - math.prod(1.0 - CHAT_STRENGTH.get(name, 0.5) for name in chat)
                item.signals["chat_leftovers"] = (value, {"found": chat})
            pasted_chars = item.habits.get("paste_chars", [])
            if pasted_chars:
                value = 1.0 - math.prod(1.0 - PASTE_CHAR_STRENGTH.get(name, 0.4) for name in pasted_chars)
                item.signals["paste_characters"] = (value, {"found": pasted_chars})

    def _history_items(self):
        """Items of the participants' submissions in the earlier contests
        chosen in the settings, by user. They are not scored, only added to
        profiles; code that already looks like LLM code is gated out the same
        way as in this contest.
        """
        submissions = list(history_submissions(self.contest, list(self.by_user)))
        features = ensure_features(submissions, self.errors)
        history = defaultdict(list)
        for submission in submissions:
            if submission.id in features:
                item = _item(submission, features[submission.id], history=True)
                artifacts = 1.0 - math.prod(1.0 - CHAT_STRENGTH.get(name, 0.5) for name in item.habits.get("chat", []))
                item.independent = max(item.style, artifacts)
                history[item.user_id].append(item)
        return history

    def _profile_pools(self, history):
        """Per participant, the submissions their style profile is built from:
        the last PROFILE_PER_PROBLEM on each problem (items are sorted by date),
        and of those from earlier contests, the last HISTORY_PER_USER.
        Many submissions to one problem add little to a profile, and without
        this bound each participant would cost the square of their submissions.
        """
        pools = {}
        for user_id, user_items in self.by_user.items():
            pool = []
            for items, limit in ((history.get(user_id, []), HISTORY_PER_USER), (user_items, None)):
                per_problem = defaultdict(list)
                for item in items:
                    per_problem[item.problem_id].append(item)
                latest = sorted((item for items in per_problem.values() for item in items[-PROFILE_PER_PROBLEM:]), key=lambda item: item.date)
                pool += latest[-limit:] if limit else latest
            pools[user_id] = pool
        return pools

    def _history(self, item):
        """The participant's other submissions with their profile weights."""
        return [(other, weight) for other in self.profile_pool[item.user_id] if (weight := self._profile_weight(item, other)) > 0]

    @staticmethod
    def _usual_lines(history, key, threshold=0.6):
        """Lines (or names) present in most of the weighted ``history``."""
        total = sum(weight for _other, weight in history)
        counts = defaultdict(float)
        for other, weight in history:
            for line in set(other.habits.get(key, [])):
                counts[line] += weight
        return {line for line, count in counts.items() if count >= threshold * total} if total else set()

    def _habit_signals(self):
        """Habits a participant keeps (>= HABIT_STABILITY of their other
        submissions, at least HABIT_MIN_HISTORY of them) and this
        submission breaks. A break weighs more when participants rarely
        break that habit, and is ignored when many break it on the same
        problem (the problem forces it, e.g. a multi-test input).
        """
        observations = []  # (item, habit, row)
        for item in self.items:
            history = self._history(item)
            if len(history) < HABIT_MIN_HISTORY:
                continue
            total = sum(weight for _other, weight in history)
            values = item.habits.get("values", {})
            for habit in CATEGORICAL_HABITS:
                weights = defaultdict(float)
                count = 0
                for other, weight in history:
                    value = other.habits.get("values", {}).get(habit)
                    if value is not None:
                        weights[value] += weight
                        count += 1
                if count < HABIT_MIN_HISTORY:
                    continue
                usual, usual_weight = max(weights.items(), key=lambda pair: pair[1])
                share = usual_weight / sum(weights.values())
                current = values.get(habit)
                if share < HABIT_STABILITY or current is None or usual == "mixed":
                    continue
                row = {"habit": habit, "usual": usual, "share": share, "current": current, "broken": current not in (usual, "mixed")}
                observations.append((item, habit, row))
            for habit, key, minimum in (("template", "template", 3), ("macros", "macros", 2)):
                usual = self._usual_lines(history, key)
                if len(usual) < minimum or not total:
                    continue
                current = set(item.habits.get(key, []))
                if key == "template" and not current and not item.habits.get("values", {}).get("main"):
                    continue
                kept = len(usual & current) / len(usual)
                row = {"habit": habit, "usual": sorted(usual)[:8], "share": kept, "current": sorted(current - usual)[:8], "broken": kept < 0.5}
                observations.append((item, habit, row))
        cases = Counter()
        breaks = Counter()
        problem_cases = Counter()
        problem_breaks = Counter()
        for item, habit, row in observations:
            cases[habit] += 1
            breaks[habit] += row["broken"]
            problem_cases[habit, item.pi_id] += 1
            problem_breaks[habit, item.pi_id] += row["broken"]
        for item, habit, row in observations:
            item.context.setdefault("habits", []).append(row)
            if not row["broken"]:
                continue
            local_cases = problem_cases[habit, item.pi_id]
            if local_cases >= 4 and problem_breaks[habit, item.pi_id] / local_cases >= 0.3:
                row["problem"] = True
                continue
            rate = (breaks[habit] + 1) / (cases[habit] + 5)
            strength = clamp(math.log(1 / rate) / 3)
            current = row["current"]
            towards_llm = habit in ("template", "macros") or (isinstance(current, str) and current in LLM_VALUES.get(habit, ()))
            strength = min(1.0, strength * 1.3) if towards_llm else strength * 0.6
            row["strength"] = strength
        for item in self.items:
            broken = [row for row in item.context.get("habits", []) if row.get("strength")]
            if not broken:
                continue
            # Habits that change together (an editor formats the code, another
            # computer lacks the template) count as one change per group.
            groups = defaultdict(list)
            for row in broken:
                groups[HABIT_GROUP.get(row["habit"], "structure")].append(row["strength"])
            values = {}
            for group, strengths in groups.items():
                strongest = max(strengths)
                values[group] = min(1.0, strongest + 0.25 * (sum(strengths) - strongest)) * HABIT_GROUP_WEIGHT[group]
            value = 1.0 - math.prod(1.0 - 0.8 * v for v in values.values())
            detail = [
                {**{key: row[key] for key in ("habit", "usual", "current", "share")}, "group": HABIT_GROUP.get(row["habit"], "structure")}
                for row in sorted(broken, key=lambda r: -r["strength"])
            ]
            item.signals["habit_break"] = (value, {"broken": detail})

    def _vocabulary_signals(self):
        """Names never used before by the participant, compared with how
        many new names others use on the same problem.
        """
        overlaps = {}
        new_names = {}
        for item in self.items:
            history = self._history(item)
            if len(history) < HABIT_MIN_HISTORY:
                continue
            known = set()
            for other, _weight in history:
                known.update(other.habits.get("identifiers", []))
            current = {name for name in item.habits.get("identifiers", []) if len(name) >= 2}
            if len(current) < 4:
                continue
            overlaps[item] = len(current & known) / len(current)
            new_names[item] = sorted(current - known, key=lambda name: (-len(name), name))[:6]
        for items in self.by_pi.values():
            group = [item for item in items if item in overlaps]
            if len(group) < 5:
                group = list(overlaps)
            if len(group) < 3:
                continue
            scores = dict(zip(group, _robust_scores([overlaps[item] for item in group]), strict=False))
            median = statistics.median(overlaps[item] for item in group)
            for item in items:
                if item not in overlaps:
                    continue
                value = clamp((-scores[item] - 1.5) / 2.5)
                item.context["vocabulary"] = {"overlap": overlaps[item], "median": median}
                if value > 0:
                    item.signals["vocabulary"] = (value, {"overlap": overlaps[item], "median": median, "new": new_names[item]})

    def _profile_weight(self, item, other):
        """Weight of ``other`` in the style profile used to judge ``item``."""
        # The same problem may come back in another contest.
        if other.problem_id == item.problem_id or other.pi_id == item.pi_id or other.submission.id in self.confirmed:
            return 0.0
        # Code that already looks like LLM code does not get to define
        # what the author's style is (protects against poisoned profiles).
        gate = clamp((0.8 - other.independent) / 0.5)
        if other.history:
            # Months may have passed, so no decay: a fixed, lower weight.
            return gate * self.history_weight
        days = abs((item.date - other.date).total_seconds()) / 86400.0
        return gate * (TRUSTED_WEIGHT if other.trusted else 1.0) * PROFILE_DECAY_PER_DAY**days

    def _profile_signals(self):
        means, variances = _population([item.vector for item in self.items])
        mean_style = statistics.fmean(item.style for item in self.items)
        dims = len(means)
        kappa = PROFILE_PRIOR_WEIGHT
        distances = {}
        for user_items in self.by_user.values():
            for item in user_items:
                total = 0.0
                sums = [0.0] * dims
                squares = [0.0] * dims
                style_sum = 0.0
                history = 0.0
                for other in self.profile_pool[item.user_id]:
                    weight = self._profile_weight(item, other)
                    if weight <= 0:
                        continue
                    total += weight
                    history += weight * other.history
                    style_sum += weight * other.style
                    for j, x in enumerate(other.vector):
                        sums[j] += weight * x
                        squares[j] += weight * x * x
                profile_mean = [(sums[j] + kappa * means[j]) / (total + kappa) for j in range(dims)]
                profile_var = [
                    max(MIN_VARIANCE, (squares[j] - 2 * profile_mean[j] * sums[j] + total * profile_mean[j] ** 2 + kappa * variances[j]) / (total + kappa))
                    for j in range(dims)
                ]
                distance = statistics.fmean((item.vector[j] - profile_mean[j]) ** 2 / profile_var[j] for j in range(dims))
                deviations = sorted(((item.vector[j] - profile_mean[j]) / math.sqrt(profile_var[j]), j) for j in range(dims))
                distances[item] = distance
                item.context["profile"] = {
                    "weight": total,
                    "history": history,
                    "confidence": total / (total + kappa),
                    "style": (style_sum + kappa * mean_style) / (total + kappa),
                    "distance": distance,
                    "top": [j for _z, j in sorted(deviations, key=lambda pair: -abs(pair[0]))[:3]],
                }
        # Some problems force unusual code; compare with how far other
        # participants strayed from their own style on the same problem.
        for items in self.by_pi.values():
            group = items if len(items) >= 5 else self.items
            scores = dict(zip(group, _robust_scores([distances[item] for item in group]), strict=False))
            for item in items:
                z = scores[item]
                profile = item.context["profile"]
                deviation = clamp((z - 1.0) / 3.0)
                profile["z"] = z
                if deviation <= 0:
                    continue
                confidence = profile["confidence"]
                toward = clamp((item.style - profile["style"]) / 0.4)
                closeness = max(toward, item.signals.get("llm_reference", (0.0, {}))[0])
                item.signals["deviation"] = (deviation * confidence, {"z": z, "confidence": confidence})
                if closeness > 0:
                    item.signals["style_shift"] = (
                        deviation * closeness * confidence,
                        {"z": z, "style": item.style, "profile_style": profile["style"], "confidence": confidence},
                    )

    def _behaviour_signals(self):
        for user_items in self.by_user.values():
            pasted = [item.features.pasted for item in user_items]
            previous_by_pi = {}
            for item in user_items:  # sorted by date
                previous = previous_by_pi.get(item.pi_id)
                previous_by_pi[item.pi_id] = item
                if item.during_round:
                    self._burst(item, previous)
                self._rewrite(item, previous)
                others = len(pasted) - 1
                if item.features.pasted and others >= 3:
                    rate = (sum(pasted) - 1) / others
                    if rate < 0.3:
                        item.signals["pasted"] = (1.0 - rate / 0.3, {"rate": rate})

    def _minutes_since_start(self, item):
        if item.start is None:
            return None
        return (item.date - item.start).total_seconds() / 60.0

    def _burst(self, item, previous):
        if previous is None:
            new_lines = item.features.code_lines
            since = item.start
        else:
            previous_lines = Counter(previous.features.line_hashes)
            new_lines = sum((Counter(item.features.line_hashes) - previous_lines).values())
            since = previous.date
        if since is None or new_lines < 20:
            return
        minutes = max((item.date - since).total_seconds() / 60.0, 0.5)
        rate = new_lines / minutes
        value = clamp((rate - 4.0) / 8.0)
        if value > 0:
            item.signals["burst"] = (value, {"lines": new_lines, "minutes": minutes, "first": previous is None})

    def _rewrite(self, item, previous):
        if previous is None or item.features.code_lines < 15 or previous.features.code_lines < 15:
            return
        if previous.score is not None and previous.score >= 1.0:
            return
        similarity = (containment(item.fingerprints, previous.fingerprints) + containment(previous.fingerprints, item.fingerprints)) / 2
        value = clamp((0.25 - similarity) / 0.25)
        if value > 0:
            item.signals["rewrite"] = (value, {"similarity": similarity, "previous": previous.submission.id})

    def _score_signals(self):
        best = defaultdict(float)  # (user, pi) -> best score fraction
        for item in self.items:
            if item.score is not None:
                key = (item.user_id, item.pi_id)
                best[key] = max(best[key], item.score)
        users = list(self.by_user)
        user_totals = Counter()
        for (user_id, _pi_id), score in best.items():
            user_totals[user_id] += score
        problems = {}
        # How fast each participant was on each problem among those who got
        # half the points during the round: 1 = the fastest.
        speed = {}
        for pi_id, items in self.by_pi.items():
            tried = {item.user_id for item in items}
            first_good = {}
            first_any = {}
            for item in items:
                first_any.setdefault(item.user_id, item)
                if item.score is not None and item.score >= 0.5:
                    first_good.setdefault(item.user_id, item)
            first_good = {user_id: item for user_id, item in first_good.items() if item.during_round}
            first_any = {user_id: item for user_id, item in first_any.items() if item.during_round}
            times = {user_id: self._minutes_since_start(item) for user_id, item in first_good.items()}
            known = {user_id: t for user_id, t in times.items() if t is not None and t >= 0}
            for user_id, minutes in known.items():
                if len(known) >= 5:
                    speed[user_id, pi_id] = sum(t > minutes for t in known.values()) / (len(known) - 1)
            problems[pi_id] = (tried, first_good, first_any, times)
        for pi_id, (tried, first_good, first_any, times) in problems.items():
            solved = {user_id for user_id in tried if best[(user_id, pi_id)] >= 0.9}
            solve_rate = len(solved) / len(tried)
            # Strength of each participant on the other problems, as a percentile.
            totals = {user_id: user_totals[user_id] - best.get((user_id, pi_id), 0.0) for user_id in users}
            ordered = sorted(totals.values())

            def strength(user_id, ordered=ordered, totals=totals):
                below = sum(1 for total in ordered if total < totals[user_id])
                return below / max(len(ordered) - 1, 1)

            for user_id, item in first_good.items():
                minutes = times[user_id]
                others = [t for u, t in times.items() if u != user_id and t is not None and t >= 0]
                if minutes is not None and minutes >= 0 and len(others) >= 4 and item.features.code_lines >= 15:
                    median = statistics.median(others)
                    value = clamp((0.5 * median - minutes) / (0.5 * median)) if median > 0 else 0.0
                    # Fast compared with others means little for someone who is
                    # always fast: compare with the participant's own pace too.
                    own = [speed[user_id, other] for other in problems if other != pi_id and (user_id, other) in speed]
                    usual = statistics.median(own) if len(own) >= 2 else None
                    rank = speed.get((user_id, pi_id))
                    if usual is None or rank is None:
                        value *= 0.5
                    else:
                        value *= clamp((rank - usual) / 0.4)
                    if value > 0:
                        item.signals["fast_solve"] = (value, {"minutes": minutes, "median": median, "usual_rank": usual, "rank": rank})
                if item.score >= 0.9 and solve_rate < 0.5 and len(users) >= 5:
                    participant_strength = strength(user_id)
                    value = clamp((1.0 - participant_strength) * (1.0 - solve_rate) / 0.5)
                    if value > 0:
                        item.signals["score_surprise"] = (value, {"strength": participant_strength, "solve_rate": solve_rate})
            for user_id, item in first_any.items():
                if item.score is not None and item.score >= 1.0 and solve_rate < 0.3 and len(tried) >= 5:
                    # Strong participants solve hard problems at once.
                    participant_strength = strength(user_id)
                    value = (1.0 - solve_rate / 0.3) * (1.0 - participant_strength)
                    if value > 0:
                        item.signals["first_try"] = (value, {"solve_rate": solve_rate, "strength": participant_strength})

    def _combine(self, item):
        logit = BIAS
        signals = {}
        for name, (value, detail) in item.signals.items():
            if not item.during_round and name in AFTER_ROUND_HALVED:
                value *= 0.5
                detail = {**(detail or {}), "after_round": True}
            contribution = self.weights.get(name, 0.0) * value
            logit += contribution
            signals[name] = {"value": round(value, 4), "contribution": round(contribution, 4), "detail": detail}
        item.risk = sigmoid(logit)
        item.signals = signals

    def _save(self, items):
        """Stores the results, unless a run that started later already
        stored newer ones. Returns whether they were stored.
        """
        with transaction.atomic():
            config = self._locked_config()
            if config.computed_at is not None and config.computed_at > self.started:
                return False
            RiskAssessment.objects.filter(contest=self.contest).delete()
            # The lock serialises the saves; should an assessment of one of
            # these submissions still exist (e.g. saved under another
            # contest), it is replaced instead of breaking the unique key.
            RiskAssessment.objects.bulk_create(
                [
                    RiskAssessment(
                        submission_id=item.submission.id,
                        contest=self.contest,
                        user_id=item.user_id,
                        risk=item.risk,
                        signals=item.signals,
                        context=_round_floats(item.context),
                        computed_at=self.started,
                    )
                    for item in items
                ],
                batch_size=500,
                update_conflicts=True,
                unique_fields=["submission"],
                update_fields=["contest", "user", "risk", "signals", "context", "computed_at"],
            )
            # The time of the data, not of the end of the computation, so that
            # the results of a later run are recognised as newer.
            config.computed_at = self.started
            config.last_submission_id = self.last_submission_id
            # Shown on the panel, so that the staff know why submissions are missing.
            config.read_errors = len(self.errors)
            config.last_error = f"#{self.errors[0][0]}: {self.errors[0][1]}" if self.errors else ""
            config.save(update_fields=["computed_at", "last_submission_id", "read_errors", "last_error"])
            self.config = config
        return True


def _round_floats(value):
    if isinstance(value, float):
        return round(value, 4)
    if isinstance(value, dict):
        return {k: _round_floats(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_round_floats(v) for v in value]
    return value


def assess_contest(contest, force=True):
    """Recomputes the risk of all submissions of ``contest`` (with
    ``force=False`` only if out of date); see :class:`ContestAssessor`.
    """
    return ContestAssessor(contest).run(force=force)


def update_if_needed(contest):
    # Checked without the lock first: opening the panel is the common case,
    # and the results are usually up to date.
    if needs_update(contest):
        assess_contest(contest, force=False)


def user_rankings(contest):
    """Participants by risk: the sum of the risks of their worst
    :data:`USER_TOP_PROBLEMS` problems (one submission per problem).
    """
    worst = defaultdict(dict)
    flagged = Counter()
    for assessment in RiskAssessment.objects.filter(contest=contest).select_related("submission"):
        per_problem = worst[assessment.user_id]
        pi_id = assessment.submission.problem_instance_id
        if pi_id not in per_problem or assessment.risk > per_problem[pi_id].risk:
            per_problem[pi_id] = assessment
        flagged[assessment.user_id] += assessment.risk >= FLAG_THRESHOLD
    rows = []
    for user_id, per_problem in worst.items():
        top = sorted(per_problem.values(), key=lambda a: -a.risk)[:USER_TOP_PROBLEMS]
        rows.append({"user_id": user_id, "score": sum(a.risk for a in top), "top": top, "flagged": flagged[user_id]})
    rows.sort(key=lambda row: -row["score"])
    return rows
