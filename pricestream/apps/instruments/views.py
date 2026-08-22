import csv
import io

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_POST

from apps.accounts.models import BrokerAccount
from apps.instruments.models import Script
from apps.streaming.models import Subscription


@login_required
def instrument_manager(request):
    """Exchange-first token picker, matching Yantra's strategy-setup screens
    (BrokerAccount.getScriptByExchange() there / getSymbols() endpoint): pick an
    exchange first, then the symbol list narrows to just that exchange — an
    unfiltered symbol list across every exchange is enormous and unusable. Search
    within the chosen exchange, bulk CSV import, activate/deactivate. CSV import is
    a bulk-create over the same Subscription model, not a separate path.
    """
    accounts = BrokerAccount.objects.filter(owner=request.user)
    account_id = request.GET.get('account_id') or (accounts.first().id if accounts else None)
    exch_seg = request.GET.get('exch_seg', '')
    search = request.GET.get('q', '')

    exchanges = Script.objects.order_by('exch_seg').values_list('exch_seg', flat=True).distinct()

    scripts = Script.objects.none()
    if exch_seg:
        scripts = Script.objects.filter(exch_seg=exch_seg)
        if search:
            scripts = scripts.filter(symbol__icontains=search)
        scripts = scripts.order_by('symbol')[:200]

    subscribed_tokens = set()
    if account_id:
        subscribed_tokens = set(
            Subscription.objects.filter(account_id=account_id, is_enabled=True)
            .values_list('script__token', flat=True)
        )

    return render(request, 'pricestream/instrument_manager.html', {
        'accounts': accounts,
        'account_id': int(account_id) if account_id else None,
        'exchanges': exchanges,
        'exch_seg': exch_seg,
        'scripts': scripts,
        'search': search,
        'subscribed_tokens': subscribed_tokens,
    })


@login_required
@require_POST
def toggle_subscription(request, account_id, script_id):
    account = get_object_or_404(BrokerAccount, id=account_id, owner=request.user)
    script = get_object_or_404(Script, id=script_id)

    sub, created = Subscription.objects.get_or_create(account=account, script=script)
    if not created:
        sub.is_enabled = not sub.is_enabled
        sub.save(update_fields=['is_enabled'])

    exch_seg = request.POST.get('exch_seg', '')
    return redirect(f"{reverse('instrument_manager')}?account_id={account_id}&exch_seg={exch_seg}")


@login_required
@require_POST
def bulk_import(request, account_id):
    """Bulk CSV import: one column `token` or `symbol` per row, all mapped onto the
    same Subscription model the single-toggle path uses.
    """
    account = get_object_or_404(BrokerAccount, id=account_id, owner=request.user)
    exch_seg = request.POST.get('exch_seg', '')
    csv_file = request.FILES.get('csv_file')
    if not csv_file:
        messages.error(request, 'No file uploaded.')
        return redirect(f"{reverse('instrument_manager')}?account_id={account_id}&exch_seg={exch_seg}")

    reader = csv.DictReader(io.TextIOWrapper(csv_file.file, encoding='utf-8'))
    created_count = 0
    missing = []
    for row in reader:
        token = row.get('token')
        symbol = row.get('symbol')
        script = None
        if token:
            script = Script.objects.filter(token=token).first()
        elif symbol:
            script = Script.objects.filter(symbol=symbol).first()
        if script:
            _, created = Subscription.objects.get_or_create(account=account, script=script, defaults={'is_enabled': True})
            if created:
                created_count += 1
        else:
            missing.append(token or symbol)

    messages.success(request, f'Imported {created_count} subscriptions.')
    if missing:
        messages.warning(request, f"Not found: {', '.join(missing[:20])}")

    return redirect(f"{reverse('instrument_manager')}?account_id={account_id}&exch_seg={exch_seg}")
