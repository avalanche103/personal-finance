from django.urls import path

from apps.products import views

app_name = 'products'

urlpatterns = [
    path('new/', views.product_create, name='create'),
    path('', views.product_list, name='list'),
    path('<int:pk>/', views.product_detail, name='detail'),
]