from django.urls import path

from apps.accounts import views

app_name = 'accounts'

urlpatterns = [
    path('new/', views.account_create, name='create'),
    path('transactions/new/', views.transaction_create, name='transaction_create'),
    path('', views.account_list, name='list'),
]