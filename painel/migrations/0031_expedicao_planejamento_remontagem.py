from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("painel", "0030_relatorio_paletes_permissao"),
    ]

    operations = [
        migrations.AddField(
            model_name="expedicaoplanejamento",
            name="pode_remontar",
            field=models.BooleanField(default=False, verbose_name="Dá para remontar"),
        ),
        migrations.AddField(
            model_name="expedicaoplanejamento",
            name="qtd_remontavel",
            field=models.PositiveIntegerField(default=0, verbose_name="Qtd. que dá para remontar"),
        ),
        migrations.AddField(
            model_name="expedicaoplanejamento",
            name="carregado_em",
            field=models.DateTimeField(blank=True, null=True, verbose_name="Carregado em"),
        ),
    ]
