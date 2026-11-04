from django.urls import path

from oioioi.llmdetect import views

app_name = "llmdetect"

contest_patterns = [
    path("llmdetect/", views.panel_view, name="llmdetect_panel"),
    path("llmdetect/recompute/", views.recompute_view, name="llmdetect_recompute"),
    path("llmdetect/settings/", views.settings_view, name="llmdetect_settings"),
    path("llmdetect/references/", views.references_view, name="llmdetect_references"),
    path("llmdetect/references/<int:reference_id>/delete/", views.delete_reference_view, name="llmdetect_delete_reference"),
    path("llmdetect/user/<int:user_id>/", views.user_view, name="llmdetect_user"),
]
