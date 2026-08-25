from django.contrib import admin, messages

from apps.api.models import ApiKey


@admin.register(ApiKey)
class ApiKeyAdmin(admin.ModelAdmin):
    list_display = ('label', 'key_prefix', 'is_active', 'created_at', 'last_used_at', 'revoked_at')
    list_filter = ('is_active',)
    filter_horizontal = ('scoped_accounts',)
    # scoped_scripts previously used filter_horizontal too, which renders EVERY
    # Script row into the page's HTML/DOM for client-side JS filtering — with
    # 170k+ synced instruments, that made the Add/Edit ApiKey page (and therefore
    # saving it, which re-renders the same page) extremely slow to load. Scripts
    # need Django's own admin registered with search_fields for autocomplete to
    # work (see apps/instruments/admin.py's ScriptAdmin.search_fields).
    autocomplete_fields = ('scoped_scripts',)
    readonly_fields = ('key_hash', 'key_prefix', 'created_at', 'last_used_at')
    actions = ['revoke_keys']

    def save_model(self, request, obj, form, change):
        if not change and not obj.key_hash:
            new_obj, raw_key = ApiKey.generate()
            obj.key_hash = new_obj.key_hash
            obj.key_prefix = new_obj.key_prefix
            super().save_model(request, obj, form, change)
            self.message_user(
                request,
                f'API key created. Copy it now, it will not be shown again: {raw_key}',
                level=messages.WARNING,
            )
        else:
            super().save_model(request, obj, form, change)

    @admin.action(description='Revoke selected API keys')
    def revoke_keys(self, request, queryset):
        from django.utils import timezone
        queryset.update(is_active=False, revoked_at=timezone.now())
