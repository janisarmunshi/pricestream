from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from apps.accounts.forms import BrokerAccountForm
from apps.accounts.models import BrokerAccount
from apps.accounts.tasks import test_login_task


@login_required
def account_list(request):
    accounts = BrokerAccount.objects.filter(owner=request.user)
    return render(request, 'pricestream/accounts_list.html', {'accounts': accounts})


@login_required
def account_edit(request, account_id=None):
    instance = get_object_or_404(BrokerAccount, id=account_id, owner=request.user) if account_id else None

    if request.method == 'POST':
        form = BrokerAccountForm(request.POST, instance=instance)
        if form.is_valid():
            account = form.save(commit=False)
            account.owner = request.user
            # owner isn't a form field, so form.is_valid() never checked the real
            # (owner, client_id, broker) uniqueness — validate it here instead of
            # letting a duplicate reach the DB as a raw IntegrityError/500.
            duplicate = BrokerAccount.objects.filter(
                owner=request.user, client_id=account.client_id, broker=account.broker,
            ).exclude(pk=account.pk)
            if duplicate.exists():
                form.add_error(
                    'client_id',
                    f'You already have a {account.get_broker_display()} account with client ID "{account.client_id}".',
                )
            else:
                account.save()
                messages.success(request, f'Saved {account.nickname}.')
                return redirect('account_list')
    else:
        form = BrokerAccountForm(instance=instance)

    return render(request, 'pricestream/account_form.html', {'form': form, 'instance': instance})


@login_required
@require_POST
def account_delete(request, account_id):
    account = get_object_or_404(BrokerAccount, id=account_id, owner=request.user)
    account.delete()
    messages.success(request, 'Broker account deleted.')
    return redirect('account_list')


@login_required
@require_POST
def account_test_login(request, account_id):
    """Dispatched as a Celery task, never run inline — the Selenium OAuth flow can
    take up to ~120s, and running that inside a gunicorn worker request occupies it
    for the whole duration, which with only a handful of workers can make the entire
    app appear to hang for every other user in the meantime. The result lands on
    BrokerAccount.last_login_status/last_login_error/last_login_at, visible on the
    next page load of the accounts list — no need to block this request on it.
    """
    account = get_object_or_404(BrokerAccount, id=account_id, owner=request.user)
    test_login_task.delay(account.id, force=request.POST.get('force') == '1')
    messages.info(request, f'{account.nickname}: login check started, refresh in a moment to see the result.')
    return redirect('account_list')
