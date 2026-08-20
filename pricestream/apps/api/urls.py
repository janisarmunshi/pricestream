from django.urls import path

from apps.api.views import InstrumentListView, LatestTickView, TickListView

app_name = 'api'

urlpatterns = [
    path('v1/instruments/', InstrumentListView.as_view(), name='instruments'),
    path('v1/ticks/latest/', LatestTickView.as_view(), name='ticks-latest'),
    path('v1/ticks/', TickListView.as_view(), name='ticks'),
]
