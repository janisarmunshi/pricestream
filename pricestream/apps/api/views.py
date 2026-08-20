from django.utils.dateparse import parse_datetime
from rest_framework.exceptions import PermissionDenied
from rest_framework.generics import ListAPIView
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.api.serializers import ScriptSerializer, TickSerializer
from apps.instruments.models import Script
from apps.streaming.models import Subscription
from apps.ticks.queries import latest_tick_per_instrument, query_ticks


def _scope_for(request):
    """Every scoped endpoint funnels through here so the "which accounts/tokens can
    this key see" logic lives in exactly one place.
    """
    api_key = request.auth
    account_ids = api_key.scoped_account_ids()
    if not account_ids:
        raise PermissionDenied('This API key has no scoped broker accounts.')
    tokens = api_key.scoped_tokens()
    return account_ids, (tokens or None)


class InstrumentListView(ListAPIView):
    """GET /api/v1/instruments/ — instruments available to that key's scope, so an
    external system can discover what it can query before pulling data.
    """
    serializer_class = ScriptSerializer
    pagination_class = None

    def get_queryset(self):
        account_ids, tokens = _scope_for(self.request)
        qs = Script.objects.filter(
            subscriptions__account_id__in=account_ids, subscriptions__is_enabled=True,
        ).distinct()
        if tokens:
            qs = qs.filter(token__in=tokens)
        return qs


class TickListView(ListAPIView):
    """GET /api/v1/ticks/ — filter by instrument (token or symbol), account, and a
    time range; cursor-paginated since a single query could span millions of rows.
    """
    serializer_class = TickSerializer

    def get_queryset(self):
        account_ids, scoped_tokens = _scope_for(self.request)

        requested_account = self.request.query_params.get('account_id')
        if requested_account:
            requested_account = int(requested_account)
            if requested_account not in account_ids:
                raise PermissionDenied('This API key is not scoped to that account.')
            account_ids = [requested_account]

        token = self.request.query_params.get('token')
        symbol = self.request.query_params.get('symbol')
        tokens = None
        if token:
            tokens = [token]
        elif symbol:
            tokens = list(Script.objects.filter(symbol=symbol).values_list('token', flat=True))
        if scoped_tokens:
            tokens = [t for t in (tokens or scoped_tokens) if t in scoped_tokens]

        exch_seg = self.request.query_params.get('exch_seg')
        start = parse_datetime(self.request.query_params.get('start', '') or '')
        end = parse_datetime(self.request.query_params.get('end', '') or '')

        return query_ticks(account_ids=account_ids, tokens=tokens, exch_seg=exch_seg, start=start, end=end)


class LatestTickView(APIView):
    """GET /api/v1/ticks/latest/ — most recent tick per instrument, for a cheap
    "is this still updating" check without pulling a range.
    """

    def get(self, request):
        account_ids, scoped_tokens = _scope_for(request)
        token = request.query_params.get('token')
        tokens = [token] if token else scoped_tokens

        ticks = latest_tick_per_instrument(account_ids=account_ids, tokens=tokens)
        return Response(TickSerializer(ticks, many=True).data)
