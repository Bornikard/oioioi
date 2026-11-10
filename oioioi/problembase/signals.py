import logging

from django.db import transaction
from django.db.models.signals import post_save
from django.dispatch import receiver

from oioioi.problems.models import ProblemPackage

logger = logging.getLogger(__name__)


def _fingerprint(problem_id):
    from oioioi.problembase.fingerprint import compute_fingerprint
    from oioioi.problembase.models import ensure_families
    from oioioi.problembase.rejection import auto_reject
    from oioioi.problems.models import Problem

    try:
        ensure_families()
        compute_fingerprint(Problem.objects.get(id=problem_id))
        auto_reject()
    except Exception:
        logger.exception("Could not fingerprint problem %s", problem_id)


@receiver(post_save, sender=ProblemPackage)
def _fingerprint_uploaded_problem(sender, instance, raw, **kwargs):
    if not raw and instance.status == "OK" and instance.problem_id:
        problem_id = instance.problem_id
        transaction.on_commit(lambda: _fingerprint(problem_id))
