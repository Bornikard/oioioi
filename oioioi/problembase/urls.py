from django.urls import path

from oioioi.problembase import views

app_name = "problembase"

urlpatterns = [
    path("problembase/", views.family_list_view, name="problembase_list"),
    path("problembase/family/<int:family_id>/", views.family_view, name="problembase_family"),
    path("problembase/merge/", views.merge_view, name="problembase_merge"),
    path("problembase/unlink/<int:problem_id>/", views.unlink_view, name="problembase_unlink"),
    path("problembase/reject/<int:problem_id>/", views.reject_view, name="problembase_reject"),
    path("problembase/restore/<int:problem_id>/", views.restore_view, name="problembase_restore"),
    path("problembase/family/<int:family_id>/reject/", views.reject_family_view, name="problembase_reject_family"),
    path("problembase/contests/", views.contests_view, name="problembase_contests"),
    path("problembase/suggestions/", views.suggestions_view, name="problembase_suggestions"),
    path("problembase/suggestions/resolve/", views.resolve_view, name="problembase_resolve"),
    path("problembase/suggestions/merge_certain/", views.merge_certain_view, name="problembase_merge_certain"),
]
