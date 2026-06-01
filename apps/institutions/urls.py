from django.urls import path

from apps.institutions import views

app_name = 'institutions'

urlpatterns = [
    path('', views.institution_list, name='list'),
]