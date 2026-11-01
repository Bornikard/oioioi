from django.urls import reverse
from django.utils.translation import gettext_lazy as _

from oioioi.contests.admin import ProblemInstanceAdmin
from oioioi.czeciecamp.controllers import CzecieCampContestController
from oioioi.czeciecamp.models import get_camp_config


class CampProblemInstanceAdminMixin:
    """Links to the report of the latest rejudge of a problem, if there was one."""

    def inline_actions(self, instance):
        actions = super().inline_actions(instance)
        contest = instance.contest
        if contest is not None and isinstance(contest.controller, CzecieCampContestController) and str(instance.id) in get_camp_config(contest).last_rejudges:
            url = reverse("czeciecamp_rejudge_report", kwargs={"contest_id": contest.id, "problem_instance_id": instance.id})
            actions.append((url, _("Rejudge report")))
        return actions


ProblemInstanceAdmin.mix_in(CampProblemInstanceAdminMixin)
