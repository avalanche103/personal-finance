from django.urls import path

from apps.dashboard import views

app_name = 'dashboard'

urlpatterns = [
    path('', views.dashboard_home, name='home'),
    path('exchange-rates/', views.exchange_rate_history, name='exchange_rates'),
    path('portfolio-report/', views.portfolio_report, name='portfolio_report'),
    path('partials/latest-rates/', views.dashboard_latest_rates, name='latest_rates'),
    path('partials/summary/', views.dashboard_summary, name='summary'),
    path('partials/recent-imports/', views.dashboard_recent_imports, name='recent_imports'),
]