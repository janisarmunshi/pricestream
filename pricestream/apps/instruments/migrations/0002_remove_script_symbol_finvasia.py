from django.db import migrations
from django.db.models import F


def backfill_symbol_from_finvasia(apps, schema_editor):
    """symbol was the truncated base name (e.g. SILVERMIC) while symbol_finvasia
    held the full tradable contract code (SILVERMIC31AUG26) — PriceStream is
    Finvasia-only, so collapse to a single field holding the full code, which is
    what every display and lookup actually needs.
    """
    Script = apps.get_model('instruments', 'Script')
    Script.objects.exclude(symbol_finvasia='').update(symbol=F('symbol_finvasia'))


class Migration(migrations.Migration):

    dependencies = [
        ('instruments', '0001_initial'),
    ]

    operations = [
        migrations.RunPython(backfill_symbol_from_finvasia, migrations.RunPython.noop),
        migrations.RemoveField(
            model_name='script',
            name='symbol_finvasia',
        ),
    ]
