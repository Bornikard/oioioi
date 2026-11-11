from collections import defaultdict
from dataclasses import dataclass, field

from django import forms
from django.contrib import messages
from django.core.paginator import Paginator
from django.db.models import Q
from django.shortcuts import get_object_or_404, redirect
from django.template.response import TemplateResponse
from django.urls import reverse
from django.utils.translation import gettext
from django.utils.translation import gettext_lazy as _
from django.views.decorators.http import require_POST

from oioioi.base.menu import navbar_links_registry
from oioioi.base.permissions import enforce_condition, is_superuser
from oioioi.contests.models import Contest, ProblemInstance
from oioioi.problembase.models import (
    ProblemDescription,
    ProblemFamily,
    ProblemFamilyMember,
    ProblemFingerprint,
    ensure_families,
    mark_not_same,
    merge_families,
    unlink_problem,
)
from oioioi.problembase.rejection import auto_reject, rejected_problem_ids
from oioioi.problembase.seen import SUBMITTED, build_ban, seen_by
from oioioi.problembase.similarity import find_clusters
from oioioi.problembase.stats import contest_rows
from oioioi.problems.models import Problem, ProblemPackage

FAMILIES_PER_PAGE = 100
SORTS = ("", "rating", "-rating")  # by name, by rating ascending, descending
BAN_PARAMS = ("ban", "ban_contest", "ban_logins")


@dataclass
class Use:
    contest: Contest
    round_name: str
    date: object


@dataclass
class FamilyRow:
    family: ProblemFamily
    problems: list = field(default_factory=list)
    rejected: list = field(default_factory=list)
    uses: list = field(default_factory=list)
    description: ProblemDescription | None = None
    seen: dict = field(default_factory=dict)  # user_id -> {contest_id: how}

    @property
    def all_rejected(self):
        return not self.problems

    @property
    def shown(self):
        return self.problems or self.rejected

    @property
    def name(self):
        return self.shown[-1].name if self.shown else ""

    @property
    def short_names(self):
        return sorted({p.short_name for p in self.shown})

    @property
    def contests(self):
        seen, result = set(), []
        for use in self.uses:
            if use.contest.id not in seen:
                seen.add(use.contest.id)
                result.append(use)
        return result


def problem_url(problem, instances=()):
    """The problem's page: its problemset site, or its statement in a contest."""
    site = getattr(problem, "problemsite", None)
    if site is not None:
        return reverse("problem_site", kwargs={"site_key": site.url_key})
    for pi in instances:
        return reverse("problem_statement", kwargs={"contest_id": pi.contest_id, "problem_instance": pi.short_name})
    return None


def _family_rows(family_ids, rejected=frozenset()):
    rows = {fid: FamilyRow(family=family) for fid, family in ProblemFamily.objects.in_bulk(family_ids).items()}
    members = ProblemFamilyMember.objects.filter(family_id__in=family_ids).select_related("problem__problemsite").prefetch_related("problem__names")
    members = members.order_by("problem_id")
    family_of = {}
    for member in members:
        row = rows[member.family_id]
        (row.rejected if member.problem_id in rejected else row.problems).append(member.problem)
        family_of[member.problem_id] = member.family_id
    instances = ProblemInstance.objects.filter(problem_id__in=family_of, contest__isnull=False).select_related("contest", "round")
    for pi in instances:
        date = pi.round.start_date if pi.round else pi.contest.creation_date
        rows[family_of[pi.problem_id]].uses.append(Use(pi.contest, pi.round.name if pi.round else "", date))
    for row in rows.values():
        row.uses.sort(key=lambda use: use.date, reverse=True)  # newest first
    for description in ProblemDescription.objects.filter(family_id__in=family_ids):
        rows[description.family_id].description = description
    return [rows[fid] for fid in family_ids if fid in rows]


def _ban_from_request(request):
    if "ban" in request.GET:
        contest_ids = request.GET.getlist("ban_contest")
        logins = request.GET.get("ban_logins", "")
    else:
        contest = getattr(request, "contest", None)
        contest_ids = [contest.id] if contest else []
        logins = ""
    contests = list(Contest.objects.filter(id__in=contest_ids))
    return build_ban(contests, logins), logins


def _ban_query(request):
    query = request.GET.copy()
    for key in list(query):
        if key not in BAN_PARAMS:
            del query[key]
    return query.urlencode()


def _attach_seen(rows, ban):
    seen = seen_by([u.id for u in ban.users], [row.family.id for row in rows])
    for row in rows:
        row.seen = seen.get(row.family.id, {})


def _ban_context(request, ban, logins):
    return {
        "ban": ban,
        "ban_logins": logins,
        "ban_query": _ban_query(request),
        "all_contests": Contest.objects.order_by("-creation_date"),
        "ban_contest_ids": {c.id for c in ban.contests},
    }


def _with(query, **params):
    """A copy of a QueryDict as a query string, with params set (None removes)."""
    query = query.copy()
    for key, value in params.items():
        query.pop(key, None)
        if value is not None:
            query[key] = value
    return query.urlencode()


def _int_param(request, name):
    value = request.GET.get(name, "").strip()
    return int(value) if value.isdigit() else None


@enforce_condition(is_superuser)
def family_list_view(request):
    ensure_families()
    ban, logins = _ban_from_request(request)
    query = request.GET.get("q", "").strip()
    hide_seen = bool(request.GET.get("hide_seen")) and bool(ban)
    show_rejected = bool(request.GET.get("show_rejected"))
    rating_min, rating_max = _int_param(request, "rating_min"), _int_param(request, "rating_max")
    full_view = request.GET.get("view") == "full"
    sort = request.GET.get("sort", "")
    sort = sort if sort in SORTS else ""
    rejected = rejected_problem_ids()

    problems = Problem.objects.all()
    if query:
        problems = problems.filter(
            Q(short_name__icontains=query)
            | Q(legacy_name__icontains=query)
            | Q(names__name__icontains=query)
            | Q(probleminstance__contest__name__icontains=query)
            | Q(probleminstance__contest__id__iexact=query)
        )
    matching = set(ProblemFamilyMember.objects.filter(problem__in=problems.values("id")).values_list("family_id", flat=True))
    names, rejected_names, kept_families = {}, {}, set()
    for member in (
        ProblemFamilyMember.objects.filter(family_id__in=matching).select_related("problem").prefetch_related("problem__names").order_by("problem_id")
    ):
        # the newest upload wins
        if member.problem_id in rejected:
            rejected_names[member.family_id] = member.problem.name
        else:
            names[member.family_id] = member.problem.name
            kept_families.add(member.family_id)
    rejected_families = matching - kept_families
    if not show_rejected:
        matching = kept_families
    ratings = dict(ProblemDescription.objects.filter(family_id__in=matching, rating__isnull=False).values_list("family_id", "rating"))
    if rating_min is not None or rating_max is not None:
        low, high = rating_min or 0, rating_max if rating_max is not None else 10**6
        matching = {fid for fid in matching if fid in ratings and low <= ratings[fid] <= high}
    family_ids = sorted(matching, key=lambda fid: (names.get(fid, rejected_names.get(fid, "")).lower(), fid))
    if sort:
        # stable: equal ratings stay by name; problems without a rating go last
        rated = sorted((fid for fid in family_ids if fid in ratings), key=lambda fid: ratings[fid], reverse=sort == "-rating")
        family_ids = rated + [fid for fid in family_ids if fid not in ratings]

    seen_family_ids = set()
    if ban:
        seen_family_ids = set(seen_by([u.id for u in ban.users], family_ids).keys())
        if hide_seen:
            family_ids = [fid for fid in family_ids if fid not in seen_family_ids]

    page = Paginator(family_ids, FAMILIES_PER_PAGE).get_page(request.GET.get("page"))
    rows = _family_rows(list(page.object_list), rejected)
    if ban:
        _attach_seen(rows, ban)

    page_query = request.GET.copy()
    page_query.pop("page", None)
    sort_query = page_query.copy()
    sort_query["sort"] = SORTS[(SORTS.index(sort) + 1) % len(SORTS)]
    if not sort_query["sort"]:
        del sort_query["sort"]
    context = {
        "rows": rows,
        "page": page,
        "page_query": page_query.urlencode(),
        "query": query,
        "hide_seen": hide_seen,
        "show_rejected": show_rejected,
        "rating_min": rating_min,
        "rating_max": rating_max,
        "sort": sort,
        "sort_query": sort_query.urlencode(),
        "full_view": full_view,
        "short_view_query": _with(page_query, view=None),
        "full_view_query": _with(page_query, view="full"),
        "rejected_count": len(rejected_families),
        "family_count": len(family_ids),
        "seen_count": len(seen_family_ids),
        "keep_for_ban": [(key, value) for key, values in page_query.lists() if key not in BAN_PARAMS for value in values if value],
    }
    context.update(_ban_context(request, ban, logins))
    return TemplateResponse(request, "problembase/list.html", context)


class DescriptionForm(forms.ModelForm):
    class Meta:
        model = ProblemDescription
        fields = ["statement", "solution", "rating", "rating_reason"]
        widgets = {
            "statement": forms.Textarea(attrs={"rows": 4}),
            "solution": forms.Textarea(attrs={"rows": 4}),
        }


@enforce_condition(is_superuser)
def family_view(request, family_id):
    ensure_families()
    family = get_object_or_404(ProblemFamily, id=family_id)
    description = ProblemDescription.objects.filter(family=family).first()

    if request.method == "POST":
        form = DescriptionForm(request.POST, instance=description or ProblemDescription(family=family))
        if form.is_valid():
            description = form.save(commit=False)
            description.source = "manual"
            description.save()
            messages.success(request, _("Description saved."))
            return redirect(_family_url(family, request))
    else:
        form = DescriptionForm(instance=description)

    ban, logins = _ban_from_request(request)
    rejected = rejected_problem_ids()
    row = _family_rows([family.id], rejected)[0]
    if ban:
        _attach_seen([row], ban)

    statuses = dict(ProblemFamilyMember.objects.filter(family=family).values_list("problem_id", "status"))
    problem_ids = [p.id for p in row.problems + row.rejected]
    fingerprints = ProblemFingerprint.objects.in_bulk(problem_ids)
    hash_count = defaultdict(int)
    for fp in fingerprints.values():
        for h in {entry["hash"] for entry in fp.solution_hashes}:
            hash_count[h] += 1
    uses = defaultdict(list)
    for pi in ProblemInstance.objects.filter(problem_id__in=problem_ids, contest__isnull=False).select_related("contest", "round"):
        uses[pi.problem_id].append(pi)
    uploads = {}
    for package in ProblemPackage.objects.filter(problem_id__in=problem_ids, status="OK").order_by("creation_date"):
        uploads[package.problem_id] = package.creation_date

    packages = []
    for problem in row.problems + row.rejected:
        fp = fingerprints.get(problem.id)
        if fp is None:
            solution = None
        else:
            hashes = {entry["hash"] for entry in fp.solution_hashes}
            solution = "same" if any(hash_count[h] > 1 for h in hashes) else ("different" if hashes else None)
        instances = sorted(uses[problem.id], key=lambda pi: pi.round.start_date if pi.round else pi.contest.creation_date, reverse=True)
        for pi in instances:
            pi.statement_url = reverse("problem_statement", kwargs={"contest_id": pi.contest_id, "problem_instance": pi.short_name})
        packages.append(
            {
                "problem": problem,
                "url": problem_url(problem, instances),
                "instances": instances,
                "uploaded": uploads.get(problem.id),
                "solution": solution if len(problem_ids) > 1 else None,
                "rejected": problem.id in rejected,
                "auto": statuses.get(problem.id) == "auto",
            }
        )

    seen_rows = []
    if ban:
        contests = Contest.objects.in_bulk({cid for per_user in row.seen.values() for cid in per_user})
        for user in ban.users:
            if user.id in row.seen:
                seen_rows.append(
                    {
                        "user": user,
                        "contests": [(contests[cid], how == SUBMITTED) for cid, how in sorted(row.seen[user.id].items())],
                    }
                )

    context = {
        "row": row,
        "description": description,
        "form": form,
        "packages": packages,
        "seen_rows": seen_rows,
        "edit": request.method == "POST" or request.GET.get("edit") == "1",
    }
    context.update(_ban_context(request, ban, logins))
    return TemplateResponse(request, "problembase/family.html", context)


def _family_url(family, request):
    url = reverse("problembase_family", kwargs={"family_id": family.id})
    query = _ban_query(request) if request.method == "GET" else request.POST.get("ban_query", "")
    return f"{url}?{query}" if query else url


def _next_url(request, default):
    url = request.POST.get("next", "")
    return url if url.startswith("/") and not url.startswith("//") else default


@enforce_condition(is_superuser)
def contests_view(request):
    return TemplateResponse(request, "problembase/contests.html", {"contests": contest_rows()})


@require_POST
@enforce_condition(is_superuser)
def merge_view(request):
    family_ids = [int(fid) for fid in request.POST.getlist("family") if fid.isdigit()]
    if len(family_ids) < 2:
        messages.error(request, _("Select at least two problems to merge."))
        return redirect(_next_url(request, reverse("problembase_list")))
    target = merge_families(family_ids)
    messages.success(request, _("Merged into one problem."))
    return redirect(_next_url(request, reverse("problembase_family", kwargs={"family_id": target.id})))


@require_POST
@enforce_condition(is_superuser)
def unlink_view(request, problem_id):
    member = get_object_or_404(ProblemFamilyMember, problem_id=problem_id)
    old_family = member.family
    unlink_problem(member.problem)
    messages.success(request, _("Package moved to a separate problem."))
    return redirect(_family_url(old_family, request))


@enforce_condition(is_superuser)
def suggestions_view(request):
    ensure_families()
    auto_reject()
    clusters = find_clusters()
    rejected = rejected_problem_ids()
    rows = {row.family.id: row for row in _family_rows([fid for c in clusters for fid in c.family_ids], rejected)}
    items = []
    for cluster in clusters:
        families = [rows[fid] for fid in cluster.family_ids if fid in rows]
        if len(families) < 2:
            continue
        index = {row.family.id: i + 1 for i, row in enumerate(families)}
        edges = [{"a": index[a], "b": index[b], "evidence": e} for (a, b), e in sorted(cluster.edges.items()) if a in index and b in index]
        items.append({"cluster": cluster, "families": families, "edges": edges})
    for item, following in zip(items, [*items[1:], None], strict=False):
        item["next"] = following["families"][0].family.id if following else None
    certain = sum(1 for item in items if item["cluster"].certain)

    solutions = defaultdict(set)
    family_of = dict(ProblemFamilyMember.objects.values_list("problem_id", "family_id"))
    fingerprinted = 0
    for problem_id, hashes in ProblemFingerprint.objects.values_list("problem_id", "solution_hashes"):
        fingerprinted += 1
        solutions[family_of.get(problem_id)].update(entry["name"] for entry in hashes)
    for item in items:
        for row in item["families"]:
            row.solution_names = sorted(solutions.get(row.family.id, ()))
    context = {
        "items": items,
        "certain_count": certain,
        "fingerprinted": fingerprinted,
        "problem_count": len(family_of),
        "rejected_count": len(rejected),
    }
    return TemplateResponse(request, "problembase/suggestions.html", context)


@require_POST
@enforce_condition(is_superuser)
def resolve_view(request):
    """Merge the checked families of a suggestion, or mark them as different from
    each other; the unchecked ones are left as they are."""
    checked = {int(fid) for fid in request.POST.getlist("family") if fid.isdigit()}
    after = request.POST.get("after", "")
    url = reverse("problembase_suggestions")
    if len(checked) < 2:
        messages.error(request, _("Select at least two problems."))
        return redirect(f"{url}#f{min(checked)}" if checked else url)

    if request.POST.get("action") == "different":
        problems = defaultdict(list)
        for problem_id, family_id in ProblemFamilyMember.objects.filter(family_id__in=checked).values_list("problem_id", "family_id"):
            problems[family_id].append(problem_id)
        ordered = sorted(checked)
        for i, family_id in enumerate(ordered):
            mark_not_same(problems[family_id], [p for other in ordered[i + 1 :] for p in problems[other]])
        messages.success(request, _("Marked as different problems."))
        return redirect(f"{url}#f{after}" if after.isdigit() else url)

    target = merge_families(checked)
    messages.success(request, _("Merged into one problem."))
    return redirect(f"{url}#f{target.id}")


@require_POST
@enforce_condition(is_superuser)
def merge_certain_view(request):
    merged = 0
    for cluster in find_clusters():
        if cluster.certain:
            merge_families(cluster.family_ids)
            merged += 1
    messages.success(request, gettext("Merged %d groups with identical model solutions.") % merged)
    return redirect("problembase_suggestions")


def _set_status(problem_ids, status):
    ProblemFamilyMember.objects.filter(problem_id__in=problem_ids).update(status=status)


@require_POST
@enforce_condition(is_superuser)
def reject_view(request, problem_id):
    member = get_object_or_404(ProblemFamilyMember, problem_id=problem_id)
    _set_status([problem_id], "rejected")
    messages.success(request, _("Package rejected."))
    return redirect(_family_url(member.family, request))


@require_POST
@enforce_condition(is_superuser)
def restore_view(request, problem_id):
    member = get_object_or_404(ProblemFamilyMember, problem_id=problem_id)
    _set_status([problem_id], "kept")
    messages.success(request, _("Package restored."))
    return redirect(_family_url(member.family, request))


@require_POST
@enforce_condition(is_superuser)
def reject_family_view(request, family_id):
    """Reject all packages of a family (from a merge suggestion)."""
    rejected = rejected_problem_ids()
    problem_ids = [p for p in ProblemFamilyMember.objects.filter(family_id=family_id).values_list("problem_id", flat=True) if p not in rejected]
    _set_status(problem_ids, "rejected")
    messages.success(request, _("Package rejected."))
    url = reverse("problembase_suggestions")
    others = [fid for fid in request.POST.getlist("cluster") if fid.isdigit() and int(fid) != family_id]
    return redirect(f"{url}#f{others[0]}" if others else url)


navbar_links_registry.register(
    name="problembase",
    text=_("Problem catalog"),
    url_generator=lambda request: reverse("problembase_list"),
    condition=is_superuser,
    order=250,
)
