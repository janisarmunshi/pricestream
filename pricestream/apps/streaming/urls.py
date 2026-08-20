from django.urls import include, path

from apps.streaming import views

urlpatterns = [
    path('', views.dashboard, name='dashboard'),
    path('streaming/', views.streaming_control, name='streaming_control'),
    path('streaming/<int:account_id>/action/', views.streaming_action, name='streaming_action'),
    path('explorer/', views.data_explorer, name='data_explorer'),
    path('settings/', views.settings_view, name='settings'),
    path('logs/', views.logs_and_alerts, name='logs_and_alerts'),

    path('accounts/', include('apps.accounts.urls')),
    path('instruments/', include('apps.instruments.urls')),
]
