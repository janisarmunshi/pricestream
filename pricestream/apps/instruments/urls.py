from django.urls import path

from apps.instruments import views

urlpatterns = [
    path('', views.instrument_manager, name='instrument_manager'),
    path('<int:account_id>/toggle/<int:script_id>/', views.toggle_subscription, name='toggle_subscription'),
    path('<int:account_id>/import/', views.bulk_import, name='bulk_import'),
]
