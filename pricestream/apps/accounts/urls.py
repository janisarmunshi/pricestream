from django.urls import path

from apps.accounts import views

urlpatterns = [
    path('', views.account_list, name='account_list'),
    path('new/', views.account_edit, name='account_create'),
    path('<int:account_id>/edit/', views.account_edit, name='account_edit'),
    path('<int:account_id>/delete/', views.account_delete, name='account_delete'),
    path('<int:account_id>/test-login/', views.account_test_login, name='account_test_login'),
]
