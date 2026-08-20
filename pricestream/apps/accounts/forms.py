from django import forms

from apps.accounts.models import BrokerAccount


class BrokerAccountForm(forms.ModelForm):
    class Meta:
        model = BrokerAccount
        fields = [
            'nickname', 'broker', 'client_id', 'password', 'totp_secret',
            'api_key', 'vendor_code', 'source_ip', 'is_active',
        ]
        widgets = {
            'password': forms.PasswordInput(render_value=True),
            'totp_secret': forms.PasswordInput(render_value=True),
            'api_key': forms.PasswordInput(render_value=True),
        }
