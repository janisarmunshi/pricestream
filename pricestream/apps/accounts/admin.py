from django.contrib import admin, messages

from apps.accounts.models import BrokerAccount
from apps.accounts.services import test_login


@admin.register(BrokerAccount)
class BrokerAccountAdmin(admin.ModelAdmin):
    list_display = (
        'nickname', 'owner', 'broker', 'client_id', 'is_active',
        'last_login_status', 'last_login_at',
    )
    list_filter = ('broker', 'is_active', 'last_login_status')
    search_fields = ('nickname', 'client_id', 'owner__username')
    readonly_fields = ('access_token', 'last_login_at', 'last_login_status', 'last_login_error')
    actions = ['action_test_login']

    @admin.action(description='Test login / force relogin (re-authenticate via Selenium+TOTP)')
    def action_test_login(self, request, queryset):
        for account in queryset:
            ok, error = test_login(account)
            if ok:
                self.message_user(request, f'{account.nickname}: login OK', level=messages.SUCCESS)
            else:
                self.message_user(request, f'{account.nickname}: login FAILED — {error}', level=messages.ERROR)
