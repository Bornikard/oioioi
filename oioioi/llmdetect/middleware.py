import logging

from oioioi.contests.models import ProblemInstance

logger = logging.getLogger(__name__)

STATEMENT_URL_NAMES = ("problem_statement", "problem_statement_zip_index", "problem_statement_zip")


class ProblemViewMiddleware:
    """Remembers when a participant first opened the statement of a
    problem, for the time-based signals of :mod:`oioioi.llmdetect`.

    Must come after ``CurrentContestMiddleware``.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        match = getattr(request, "resolver_match", None)
        if match is not None and match.url_name in STATEMENT_URL_NAMES and response.status_code < 400 and request.user.is_authenticated:
            try:
                self._log(request, match.kwargs.get("problem_instance"))
            except Exception:
                logger.warning("Cannot log a problem statement view", exc_info=True)
        return response

    def _log(self, request, short_name):
        from oioioi.llmdetect.models import ProblemViewEvent

        contest = getattr(request, "contest", None)
        if contest is None or short_name is None:
            return
        pi = ProblemInstance.objects.filter(contest=contest, short_name=short_name).only("id").first()
        if pi is not None:
            ProblemViewEvent.objects.get_or_create(user=request.user, problem_instance=pi, defaults={"date": request.timestamp})
