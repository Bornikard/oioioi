import os
from collections import Counter
from urllib.parse import urlencode

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.models import User
from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect
from django.template.response import TemplateResponse
from django.urls import reverse
from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from django.views.decorators.http import require_POST

from oioioi.base.permissions import enforce_condition
from oioioi.contests.menu import contest_admin_menu_registry
from oioioi.contests.models import ProblemInstance
from oioioi.contests.utils import contest_exists, is_contest_admin
from oioioi.llmdetect.explain import SIGNAL_LABELS, habit_rows, profile_features, signal_rows
from oioioi.llmdetect.forms import AnswersForm, DecisionForm, SettingsForm, StatementForm, problems_of
from oioioi.llmdetect.models import LLMReferenceSolution, LLMStatement, ProblemViewEvent, ReviewDecision, RiskAssessment, get_config, mark_dirty
from oioioi.llmdetect.prompts import PROMPTS, prompts_file
from oioioi.llmdetect.scoring import (
    FLAG_THRESHOLD,
    SUBMISSION_KINDS,
    assess_contest,
    contest_submissions,
    history_submissions,
    read_source,
    update_if_needed,
    user_rankings,
)
from oioioi.llmdetect.statements import statement_text
from oioioi.programs.models import ProgramSubmission

SCRIPT_PATH = os.path.join(os.path.dirname(__file__), "tools", "llm_references.py")

# Shown as "profile uncertain" below this confidence.
LOW_CONFIDENCE = 0.25


def _panel_url(contest):
    return reverse("llmdetect_panel", kwargs={"contest_id": contest.id})


def _user_url(contest, user_id, submission_id=None):
    url = reverse("llmdetect_user", kwargs={"contest_id": contest.id, "user_id": user_id})
    return f"{url}?submission={submission_id}" if submission_id else url


def _latest_decisions(contest):
    latest = {}
    for decision in ReviewDecision.objects.filter(contest=contest).order_by("date", "id"):
        latest[decision.user_id] = decision
    return latest


# Assessing runs outside the request's transaction: see ContestAssessor.
@transaction.non_atomic_requests
@contest_admin_menu_registry.register_decorator(
    _("LLM detection"),
    lambda request: _panel_url(request.contest),
    condition=is_contest_admin,
    order=120,
)
@enforce_condition(contest_exists & is_contest_admin)
def panel_view(request):
    contest = request.contest
    update_if_needed(contest)
    config = get_config(contest)
    rows = user_rankings(contest)
    users = User.objects.in_bulk([row["user_id"] for row in rows])
    decisions = _latest_decisions(contest)
    for row in rows:
        row["user"] = users[row["user_id"]]
        row["decision"] = decisions.get(row["user_id"])
        worst = row["top"][0] if row["top"] else None
        row["confidence"] = worst.context.get("profile", {}).get("confidence", 0) if worst else 0
        names = Counter()
        for assessment in row["top"]:
            for name, signal in assessment.signals.items():
                names[name] += signal["contribution"]
        row["signals"] = [SIGNAL_LABELS.get(name, name) for name, total in names.most_common(3) if total > 0.2]
        row["url"] = _user_url(contest, row["user_id"])
    references = Counter(LLMReferenceSolution.objects.filter(problem_instance__contest=contest).values_list("problem_instance_id", flat=True))
    problems = [
        {"pi": pi, "references": references.get(pi.id, 0)}
        for pi in ProblemInstance.objects.filter(contest=contest).select_related("round").order_by("round__start_date", "short_name")
    ]
    context = {
        "config": config,
        "rows": rows,
        "problems": problems,
        "settings_form": SettingsForm(instance=config, contest=contest, user=request.user),
        "threshold": FLAG_THRESHOLD,
        "low_confidence": LOW_CONFIDENCE,
        "middleware_enabled": "oioioi.llmdetect.middleware.ProblemViewMiddleware" in settings.MIDDLEWARE,
        "summary": _summary(contest),
    }
    return TemplateResponse(request, "llmdetect/panel.html", context)


def _summary(contest):
    """How many submissions there are, are analysed, and why the others are not."""
    program = ProgramSubmission.objects.filter(problem_instance__contest=contest, kind__in=SUBMISSION_KINDS, user__isnull=False)
    total = program.count()
    counted = contest_submissions(contest).count()
    users = contest_submissions(contest).order_by().values("user_id").distinct()
    return {
        "total": total,
        "counted": counted,
        "staff": total - counted,
        "analysed": RiskAssessment.objects.filter(contest=contest).count(),
        "history": history_submissions(contest, users).count(),
    }


# Assessing runs outside the request's transaction: see ContestAssessor.
@transaction.non_atomic_requests
@require_POST
@enforce_condition(contest_exists & is_contest_admin)
def recompute_view(request):
    assess_contest(request.contest)
    messages.success(request, _("Recomputed."))
    return redirect(_panel_url(request.contest))


@require_POST
@enforce_condition(contest_exists & is_contest_admin)
def settings_view(request):
    config = get_config(request.contest)
    form = SettingsForm(request.POST, instance=config, contest=request.contest, user=request.user)
    if form.is_valid():
        form.save()
        mark_dirty(request.contest)
        messages.success(request, _("Settings saved."))
    else:
        messages.error(request, _("Invalid settings."))
    return redirect(_panel_url(request.contest))


def _references_url(contest, problem_id=None, anchor=""):
    url = reverse("llmdetect_references", kwargs={"contest_id": contest.id})
    return (f"{url}?problem={problem_id}" if problem_id else url) + anchor


def _download(content, filename, content_type):
    response = HttpResponse(content, content_type=content_type)
    response["Content-Disposition"] = f'attachment; filename="{filename}"'
    return response


def _save_references(request, references):
    """Stores parsed references, skipping code already stored for the problem."""
    existing = set(LLMReferenceSolution.objects.filter(problem_instance__contest=request.contest).values_list("problem_instance_id", "source"))
    added = skipped = 0
    for reference in references:
        key = (reference["problem_instance"].id, reference["source"])
        if key in existing:
            skipped += 1
            continue
        existing.add(key)
        LLMReferenceSolution.objects.create(
            problem_instance=reference["problem_instance"],
            model_name=reference["model"],
            prompt_variant=reference["prompt"],
            filename=reference.get("filename") or f"bulk_line_{reference['line']}.cpp",
            source=reference["source"],
            added_by=request.user,
        )
        added += 1
    if added:
        mark_dirty(request.contest)
    return added, skipped


def _statement(pi, reread=False):
    """The stored statement text of a problem, read from its package or
    statement file the first time (reading a package takes a while).
    """
    statement = LLMStatement.objects.filter(problem_instance=pi).first()
    if statement is not None and not reread:
        return statement
    text, source, warnings = statement_text(pi)
    if statement is None:
        statement = LLMStatement(problem_instance=pi)
    statement.text, statement.source, statement.warnings, statement.checked = text, source or "", warnings, False
    statement.updated_at = timezone.now()
    statement.updated_by = None
    statement.save()
    return statement


@enforce_condition(contest_exists & is_contest_admin)
def references_view(request):
    contest = request.contest
    problems = list(problems_of(contest))
    if not problems:
        return TemplateResponse(request, "llmdetect/references.html", {"problems": []})
    wanted = request.GET.get("problem") or request.POST.get("problem")
    pi = next((p for p in problems if str(p.id) == wanted), problems[0])
    action = request.POST.get("action")

    if request.method == "POST" and action == "reread":
        _statement(pi, reread=True)
        messages.success(request, _("The statement was read again."))
        return redirect(_references_url(contest, pi.id, "#statement"))
    statement = _statement(pi)
    statement_form = StatementForm(initial={"text": statement.text})
    answers_form = AnswersForm(contest=contest, problem_instance=pi, auto_id="answer-%s")
    if request.method == "POST" and action == "statement":
        statement_form = StatementForm(request.POST)
        if statement_form.is_valid():
            data = statement_form.cleaned_data
            if data["text"] != statement.text:
                statement.source = LLMStatement.SOURCE_EDITED
            statement.text, statement.checked = data["text"], True
            statement.updated_at, statement.updated_by = timezone.now(), request.user
            statement.save()
            if "download" in request.POST:
                if not data["prompts"]:
                    messages.error(request, _("Choose the prompts to download."))
                    return redirect(_references_url(contest, pi.id, "#prompts"))
                content = prompts_file(contest, {pi.short_name: statement.text}, data["prompts"])
                return _download(content, f"prompts-{contest.id}-{pi.short_name}.json", "application/json")
            messages.success(request, _("The statement was saved."))
            return redirect(_references_url(contest, pi.id, "#statement"))
    elif request.method == "POST" and action == "answers":
        answers_form = AnswersForm(request.POST, request.FILES, contest=contest, problem_instance=pi, auto_id="answer-%s")
        if answers_form.is_valid():
            added, skipped = _save_references(request, answers_form.cleaned_data["references"])
            message = _("Added %(count)d references.") % {"count": added}
            if skipped:
                message += " " + _("Skipped %(count)d already stored.") % {"count": skipped}
            messages.success(request, message)
            return redirect(_references_url(contest, pi.id, "#references"))
    elif request.method == "POST":
        raise PermissionDenied

    counts = Counter(LLMReferenceSolution.objects.filter(problem_instance__contest=contest).values_list("problem_instance_id", flat=True))
    checked = set(LLMStatement.objects.filter(problem_instance__contest=contest, checked=True).values_list("problem_instance_id", flat=True))
    context = {
        "problems": [{"pi": p, "references": counts.get(p.id, 0), "checked": p.id in checked} for p in problems],
        "pi": pi,
        "statement": statement,
        "statement_form": statement_form,
        "answers_form": answers_form,
        "references": _with_prompt_labels(LLMReferenceSolution.objects.filter(problem_instance=pi).select_related("added_by").order_by("-id")),
        "prompts": [(key, label, instruction) for key, (label, instruction) in PROMPTS.items()],
        "unchecked": len(problems) - len(checked),
    }
    return TemplateResponse(request, "llmdetect/references.html", context)


def _with_prompt_labels(references):
    references = list(references)
    for reference in references:
        prompt = PROMPTS.get(reference.prompt_variant)
        reference.prompt_label = prompt[0] if prompt else reference.prompt_variant
    return references


@enforce_condition(contest_exists & is_contest_admin)
def all_prompts_view(request):
    """Prompts for every problem with a statement text, for the script."""
    contest = request.contest
    statements = {}
    for pi in problems_of(contest):
        text = _statement(pi).text
        if text:
            statements[pi.short_name] = text
    content = prompts_file(contest, statements, list(PROMPTS))
    return _download(content, f"prompts-{contest.id}.json", "application/json")


@enforce_condition(contest_exists & is_contest_admin)
def script_view(request):
    with open(SCRIPT_PATH, encoding="utf-8") as file:
        return _download(file.read(), "llm_references.py", "text/x-python")


@require_POST
@enforce_condition(contest_exists & is_contest_admin)
def delete_reference_view(request, reference_id):
    reference = get_object_or_404(LLMReferenceSolution, id=reference_id, problem_instance__contest=request.contest)
    problem_id = reference.problem_instance_id
    reference.delete()
    mark_dirty(request.contest)
    messages.success(request, _("Reference deleted."))
    return redirect(_references_url(request.contest, problem_id, "#references"))


def _source_or_none(submission):
    try:
        return read_source(submission)
    except Exception:
        return None


def _typical_submission(assessments, selected):
    """The participant's own code to compare with: a supervised one if
    there is any, otherwise the one closest to their usual style, on a
    different problem.
    """
    others = [a for a in assessments if a.submission.problem_instance_id != selected.submission.problem_instance_id]
    if not others:
        return None
    return min(others, key=lambda a: (not a.context.get("trusted", False), a.context.get("profile", {}).get("distance", 0)))


# Assessing runs outside the request's transaction: see ContestAssessor.
@transaction.non_atomic_requests
@enforce_condition(contest_exists & is_contest_admin)
def user_view(request, user_id):
    contest = request.contest
    user = get_object_or_404(User, id=user_id)
    update_if_needed(contest)
    assessments = RiskAssessment.objects.filter(contest=contest, user=user).select_related("submission__problem_instance__round")
    assessments = list(assessments.order_by("submission__date", "submission_id"))
    submissions = ProgramSubmission.objects.filter(id__in=[a.submission_id for a in assessments]).select_related("problem_instance").order_by("-date")
    form = DecisionForm(request.POST or None, submissions=submissions, initial={"submission": request.GET.get("submission")})
    if request.method == "POST":
        if not form.is_valid():
            raise PermissionDenied
        _save_decision(request, form, user, assessments)
        return redirect(_user_url(contest, user.id, request.GET.get("submission")))

    selected = None
    wanted = request.GET.get("submission")
    if wanted:
        selected = next((a for a in assessments if str(a.submission_id) == wanted), None)
    if selected is None and assessments:
        selected = max(assessments, key=lambda a: a.risk)

    peers = {a.context.get("peer", {}).get("user") for a in assessments if a.context.get("peer")} | {
        s.get("detail", {}).get("user") for a in assessments for s in a.signals.values()
    }
    users = User.objects.in_bulk([u for u in peers if u])
    rows = [
        {
            "assessment": a,
            "signals": signal_rows(a, users),
            "flagged": a.risk >= FLAG_THRESHOLD,
            "url": _user_url(contest, user.id, a.submission_id),
        }
        for a in assessments
    ]
    context = {
        "profile_user": user,
        "rows": rows,
        "threshold": FLAG_THRESHOLD,
        "decisions": ReviewDecision.objects.filter(contest=contest, user=user).select_related("reviewer", "submission__problem_instance"),
        "form": form,
        "low_confidence": LOW_CONFIDENCE,
        "disqualification_enabled": "oioioi.disqualification" in settings.INSTALLED_APPS,
    }
    if selected is not None:
        context.update(_comparison(contest, user, selected, assessments, users))
        if context["disqualification_enabled"]:
            context.update(_disqualification(selected))
    return TemplateResponse(request, "llmdetect/user.html", context)


def _disqualification(selected):
    """Whether the selected submission is disqualified, and a link to the
    disqualification form filled in for it otherwise.
    """
    from oioioi.disqualification.models import Disqualification

    submission = selected.submission
    if Disqualification.objects.filter(submission=submission, guilty=True).exists():
        return {"selected_disqualified": True}
    params = urlencode(
        {
            "user": submission.user_id,
            "submission": submission.id,
            "title": _("Solution not written by yourself"),
            "content": _("Submission #%(id)s (%(problem)s) was not written by you, so the problem counts 0.")
            % {"id": submission.id, "problem": submission.problem_instance.short_name},
        }
    )
    return {"disqualify_url": f"{reverse('oioioiadmin:disqualification_disqualification_add')}?{params}"}


def _comparison(contest, user, selected, assessments, users):
    submission = ProgramSubmission.objects.get(id=selected.submission_id)
    reference = None
    reference_id = selected.context.get("reference", {}).get("id")
    if reference_id:
        reference = LLMReferenceSolution.objects.filter(id=reference_id).first()
    typical = _typical_submission(assessments, selected)
    typical_source = _source_or_none(ProgramSubmission.objects.get(id=typical.submission_id)) if typical else None
    pi = selected.submission.problem_instance
    view = ProblemViewEvent.objects.filter(user=user, problem_instance=pi).first()
    timeline = []
    if pi.round is not None:
        timeline.append((pi.round.start_date, _("Round started"), None))
    if view is not None:
        timeline.append((view.date, _("Opened the statement"), None))
    for a in assessments:
        if a.submission.problem_instance_id == pi.id:
            timeline.append((a.submission.date, a.submission.get_score_display() or a.submission.get_status_display(), a))
    timeline.sort(key=lambda entry: entry[0])
    profile = selected.context.get("profile", {})
    return {
        "selected": selected,
        "selected_signals": signal_rows(selected, users),
        "selected_source": _source_or_none(submission),
        "reference": reference,
        "typical": typical,
        "typical_source": typical_source,
        "timeline": timeline,
        "profile": profile,
        "profile_features": profile_features(selected),
        "habits": habit_rows(selected),
        "peer": selected.context.get("peer"),
        "peer_user": users.get(selected.context.get("peer", {}).get("user")),
    }


@transaction.atomic
def _save_decision(request, form, user, assessments):
    decision = form.save(commit=False)
    decision.contest = request.contest
    decision.user = user
    decision.reviewer = request.user
    by_submission = {a.submission_id: a for a in assessments}
    if decision.submission_id in by_submission:
        assessment = by_submission[decision.submission_id]
        decision.risk = assessment.risk
        decision.signals = assessment.signals
    elif assessments:
        decision.risk = max(a.risk for a in assessments)
    decision.save()
    if decision.verdict == ReviewDecision.VERDICT_CONFIRMED and decision.submission_id:
        submission = ProgramSubmission.objects.get(id=decision.submission_id)
        source = _source_or_none(submission)
        if source is not None and not LLMReferenceSolution.objects.filter(submission=submission).exists():
            LLMReferenceSolution.objects.create(
                problem_instance=submission.problem_instance,
                kind=LLMReferenceSolution.KIND_CONFIRMED,
                filename=submission.source_file.name.rsplit("/", 1)[-1],
                source=source,
                submission=submission,
                added_by=request.user,
            )
    mark_dirty(request.contest)
    messages.success(request, _("Decision saved."))
