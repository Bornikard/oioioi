import logging

from django.db import transaction

from oioioi.contests.controllers import ContestController
from oioioi.llmdetect.models import SubmissionFeatures, mark_dirty
from oioioi.programs.models import ProgramSubmission

logger = logging.getLogger(__name__)


def _compute_quietly(submission, **kwargs):
    from oioioi.llmdetect.scoring import compute_features

    try:
        with transaction.atomic():
            compute_features(submission, **kwargs)
    except Exception:
        logger.warning("Cannot compute LLM detection features of submission %s", submission.id, exc_info=True)


class LLMDetectContestControllerMixin:
    """Extracts the features of the code of submissions.

    Code pasted into the submission form is analyzed at once: only then is
    it known that it was pasted (the stored file is renamed), and the code
    is at hand. Uploaded files are analyzed in the evaluation worker once
    the submission is judged, so the submitting participant never waits.
    Failures are only logged; missing features are computed when the staff
    opens the panel.
    """

    def create_submission(self, request, problem_instance, form_data, **kwargs):
        submission = super().create_submission(request, problem_instance, form_data, **kwargs)
        code = form_data.get("code") if form_data.get("file") is None else None
        if code and isinstance(submission, ProgramSubmission):
            _compute_quietly(submission, source=code, pasted=True)
        return submission

    def submission_judged(self, submission, rejudged=False):
        super().submission_judged(submission, rejudged)
        if rejudged:
            # The code is the same, but scores-based signals may change.
            if submission.problem_instance.contest_id is not None:
                mark_dirty(submission.problem_instance.contest)
            return
        if SubmissionFeatures.objects.filter(submission_id=submission.id).exists():
            return
        program_submission = ProgramSubmission.objects.filter(id=submission.id).first()
        if program_submission is not None:
            _compute_quietly(program_submission)


ContestController.mix_in(LLMDetectContestControllerMixin)
