from django.urls import path

from apps.imports import views

app_name = 'imports'

urlpatterns = [
    path('upload/', views.import_upload, name='upload'),
    path('history/', views.import_history, name='history'),
    path('jobs/<int:pk>/', views.import_job_detail, name='detail'),
    path('jobs/<int:pk>/progress/', views.import_job_progress, name='progress'),
    path('jobs/<int:pk>/records/<int:row_index>/', views.import_record_update, name='record_update'),
]