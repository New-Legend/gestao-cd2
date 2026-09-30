# Generated manually to make Web Push subscriptions key-aware.

from django.db import migrations, models


def invalidate_legacy_subscriptions(apps, schema_editor):
    PushSubscription = apps.get_model("painel", "PushSubscription")
    PushSubscription.objects.update(
        ativo=False,
        ultimo_erro="Assinatura Push antiga invalidada. Ative notificacoes novamente neste aparelho.",
    )


class Migration(migrations.Migration):

    dependencies = [
        ("painel", "0042_veiculo_usuario_motorista"),
    ]

    operations = [
        migrations.AddField(
            model_name="pushsubscription",
            name="vapid_public_key",
            field=models.TextField(blank=True),
        ),
        migrations.RunPython(invalidate_legacy_subscriptions, migrations.RunPython.noop),
    ]
