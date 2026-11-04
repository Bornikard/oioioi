import os
from collections import Counter

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.models import User
from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect
from django.template.response import TemplateResponse
from django.urls import reverse
from django.utils.translation import gettext_lazy as _
from django.views.decorators.http import require_POST

from oioioi.base.permissions import enforce_condition
from oioioi.contests.menu import contest_admin_menu_registry
from oioioi.contests.models import ProblemInstance
from oioioi.contests.utils import contest_exists, is_contest_admin
from oioioi.llmdetect.explain import SIGNAL_LABELS, habit_rows, profile_features, signal_rows
from oioioi.llmdetect.forms import BulkReferencesForm, DecisionForm, PromptsForm, ReferenceForm, SettingsForm
from oioioi.llmdetect.models import LLMReferenceSolution, ProblemViewEvent, ReviewDecision, RiskAssessment, get_config, mark_dirty
from oioioi.llmdetect.prompts import PROMPTS, prompts_file, statement_text
from oioioi.llmdetect.scoring import FLAG_THRESHOLD, assess_contest, read_source, update_if_needed, user_rankings
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
        "settings_form": SettingsForm(instance=config, contest=contest),
        "threshold": FLAG_THRESHOLD,
        "low_confidence": LOW_CONFIDENCE,
        "middleware_enabled": "oioioi.llmdetect.middleware.ProblemViewMiddleware" in settings.MIDDLEWARE,
    }
    return TemplateResponse(request, "llmdetect/panel.html", context)


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
    form = SettingsForm(request.POST, instance=config, contest=request.contest)
    if form.is_valid():
        form.save()
        mark_dirty(request.contest)
        messages.success(request, _("Settings saved."))
    else:
        messages.error(request, _("Invalid settings."))
    return redirect(_panel_url(request.contest))


def _references_url(contest, problem_id=None):
    url = reverse("llmdetect_references", kwargs={"contest_id": contest.id})
    return f"{url}?problem={problem_id}" if problem_id else url


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
            filename=f"bulk_line_{reference['line']}.cpp",
            source=reference["source"],
            added_by=request.user,
        )
        added += 1
    if added:
        mark_dirty(request.contest)
    return added, skipped


@enforce_condition(contest_exists & is_contest_admin)
def references_view(request):
    contest = request.contest
    problem = request.GET.get("problem")
    action = request.POST.get("action", "upload")
    # Two forms have a problem field: keep their HTML ids apart.
    form = ReferenceForm(contest=contest, initial={"problem_instance": problem}, auto_id="one-%s")
    prompts_form = PromptsForm(contest=contest, initial={"problem_instance": problem})
    bulk_form = BulkReferencesForm(contest=contest, initial={"default_problem": problem}, auto_id="bulk-%s")
    if request.method == "POST" and action == "upload":
        form = ReferenceForm(request.POST, request.FILES, contest=contest, auto_id="one-%s")
        if form.is_valid():
            data = form.cleaned_data
            for filename, source in data["sources"]:
                LLMReferenceSolution.objects.create(
                    problem_instance=data["problem_instance"],
                    model_name=data["model_name"],
                    prompt_variant=data["prompt_variant"],
                    filename=filename,
                    source=source,
                    added_by=request.user,
                )
            mark_dirty(contest)
            messages.success(request, _("Added %(count)d references.") % {"count": len(data["sources"])})
            return redirect(_references_url(contest, data["problem_instance"].id))
    elif request.method == "POST" and action == "bulk":
        bulk_form = BulkReferencesForm(request.POST, request.FILES, contest=contest, auto_id="bulk-%s")
        if bulk_form.is_valid():
            added, skipped = _save_references(request, bulk_form.cleaned_data["references"])
            message = _("Added %(count)d references.") % {"count": added}
            if skipped:
                message += " " + _("Skipped %(count)d already stored.") % {"count": skipped}
            messages.success(request, message)
            return redirect(_references_url(contest, problem) + "#bulk")
    elif request.method == "POST" and action == "prompts":
        prompts_form = PromptsForm(request.POST, contest=contest)
        if prompts_form.is_valid():
            data = prompts_form.cleaned_data
            pi = data["problem_instance"]
            content = prompts_file(contest, {pi.short_name: data["statement"]}, data["prompts"])
            return _download(content, f"prompts-{contest.id}-{pi.short_name}.json", "application/json")
    elif request.method == "POST":
        raise PermissionDenied
    elif problem and request.GET.get("statement"):
        pi = get_object_or_404(ProblemInstance, id=problem, contest=contest)
        text = _statement_or_empty(pi)
        if not text:
            messages.warning(request, _("Could not read the statement of this problem; paste its text."))
        prompts_form = PromptsForm(contest=contest, initial={"problem_instance": pi.id, "statement": text})

    references = LLMReferenceSolution.objects.filter(problem_instance__contest=contest).select_related("problem_instance")
    if problem:
        references = references.filter(problem_instance_id=problem)
    context = {
        "form": form,
        "prompts_form": prompts_form,
        "bulk_form": bulk_form,
        "references": references,
        "prompts": [(key, label, instruction) for key, (label, instruction) in PROMPTS.items()],
        "problem": problem,
    }
    return TemplateResponse(request, "llmdetect/references.html", context)


def _statement_or_empty(pi):
    try:
        return statement_text(pi)
    except Exception:
        return ""


@enforce_condition(contest_exists & is_contest_admin)
def all_prompts_view(request):
    """Prompts for every problem whose statement can be read, for the script."""
    contest = request.contest
    statements = {}
    for pi in ProblemInstance.objects.filter(contest=contest).order_by("round__start_date", "short_name"):
        text = _statement_or_empty(pi)
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
    return redirect(f"{reverse('llmdetect_references', kwargs={'contest_id': request.contest.id})}?problem={problem_id}")


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
    return TemplateResponse(request, "llmdetect/user.html", context)


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
