from django.contrib import admin, messages

from apps.accounts.models import BrokerAccount
from apps.accounts.tasks import test_login_task


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
        # Dispatched, not run inline — Selenium OAuth can take up to ~120s, and
        # gunicorn only has a handful of workers, so blocking one for that long makes
        # the whole app appear to hang for every other user in the meantime.
        for account in queryset:
            test_login_task.delay(account.id)
        self.message_user(
            request,
            f'Login check started for {queryset.count()} account(s) — refresh in a moment to see the result.',
            level=messages.INFO,
        )
