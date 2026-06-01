from django.urls import path

from apps.accounts import views

app_name = 'accounts'

urlpatterns = [
    path('', views.account_list, name='list'),
]