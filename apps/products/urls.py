from django.urls import path

from apps.products import views

app_name = 'products'

urlpatterns = [
    path('', views.product_list, name='list'),
]