from django.urls import path

from apps.accounts import views

app_name = 'accounts'

urlpatterns = [
    path('new/', views.account_create, name='create'),
    path('transactions/', views.transaction_list, name='transaction_list'),
    path('transactions/new/', views.transaction_create, name='transaction_create'),
    path('transactions/<int:pk>/edit/', views.transaction_edit, name='transaction_edit'),
    path('transactions/<int:pk>/delete/', views.transaction_delete, name='transaction_delete'),
    path('transactions/<int:pk>/delete/confirm/', views.transaction_delete_confirm, name='transaction_delete_confirm'),
    path('', views.account_list, name='list'),
]