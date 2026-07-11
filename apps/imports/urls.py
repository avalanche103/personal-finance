from django.urls import path

from apps.imports import views

app_name = 'imports'

urlpatterns = [
    path('upload/', views.import_upload, name='upload'),
    path('sync/nbrb/', views.import_sync_nbrb, name='sync_nbrb'),
    path('sync/binance/', views.import_sync_binance, name='sync_binance'),
    path('priorlife/update/', views.import_priorlife_update, name='priorlife_update'),
    path('cash/operation/', views.import_cash_operation, name='cash_operation'),
    path('recent-jobs/', views.import_recent_jobs, name='recent_jobs'),
    path('history/', views.import_history, name='history'),
    path('jobs/<int:pk>/', views.import_job_detail, name='detail'),
    path('jobs/<int:pk>/progress/', views.import_job_progress, name='progress'),
    path('jobs/<int:pk>/records/<int:row_index>/', views.import_record_update, name='record_update'),
]