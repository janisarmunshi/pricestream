from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from apps.accounts.forms import BrokerAccountForm
from apps.accounts.models import BrokerAccount
from apps.accounts.services import test_login


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
    account = get_object_or_404(BrokerAccount, id=account_id, owner=request.user)
    ok, error = test_login(account, force=request.POST.get('force') == '1')
    if ok:
        messages.success(request, f'{account.nickname}: login OK.')
    else:
        messages.error(request, f'{account.nickname}: login failed — {error}')
    return redirect('account_list')
