import csv
import io

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from apps.accounts.models import BrokerAccount
from apps.instruments.models import Script
from apps.streaming.models import Subscription


@login_required
def instrument_manager(request):
    """Search/select per account, bulk CSV import, activate/deactivate. CSV import
    is a bulk-create over the same Subscription model, not a separate path.
    """
    accounts = BrokerAccount.objects.filter(owner=request.user)
    account_id = request.GET.get('account_id') or (accounts.first().id if accounts else None)
    search = request.GET.get('q', '')

    scripts = Script.objects.all()
    if search:
        scripts = scripts.filter(symbol__icontains=search)
    scripts = scripts[:100]

    subscribed_tokens = set()
    if account_id:
        subscribed_tokens = set(
            Subscription.objects.filter(account_id=account_id, is_enabled=True)
            .values_list('script__token', flat=True)
        )

    return render(request, 'pricestream/instrument_manager.html', {
        'accounts': accounts,
        'account_id': int(account_id) if account_id else None,
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

    return redirect(f"/instruments/?account_id={account_id}")


@login_required
@require_POST
def bulk_import(request, account_id):
    """Bulk CSV import: one column `token` or `symbol` per row, all mapped onto the
    same Subscription model the single-toggle path uses.
    """
    account = get_object_or_404(BrokerAccount, id=account_id, owner=request.user)
    csv_file = request.FILES.get('csv_file')
    if not csv_file:
        messages.error(request, 'No file uploaded.')
        return redirect(f"/instruments/?account_id={account_id}")

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

    return redirect(f"/instruments/?account_id={account_id}")
