from django.urls import path

from apps.imports import views

app_name = 'imports'

urlpatterns = [
    path('upload/', views.import_upload, name='upload'),
    path('history/', views.import_history, name='history'),
]