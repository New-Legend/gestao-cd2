from django.db import migrations


def activate_billing_feature(apps, schema_editor):
    ConfiguracaoSistema = apps.get_model("painel", "ConfiguracaoSistema")
    ConfiguracaoSistema.objects.update_or_create(
        chave="feature_faturamento_expedicao",
        defaults={
            "valor": "sim",
            "descricao": "Recurso de faturamento ativado para o piloto simplificado.",
        },
    )


class Migration(migrations.Migration):

    dependencies = [
        ("painel", "0065_wagner_faturamento_permissions"),
    ]

    operations = [
        migrations.RunPython(activate_billing_feature, migrations.RunPython.noop),
    ]
