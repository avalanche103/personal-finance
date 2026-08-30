from django.urls import path

from apps.market import views

app_name = 'market'

urlpatterns = [
	path('deposit-offers/', views.deposit_offers_list, name='deposit_offers'),
	path('deposit-offers/refresh/', views.deposit_offers_refresh, name='deposit_offers_refresh'),
]
