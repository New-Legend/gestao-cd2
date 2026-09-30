from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("painel", "0057_veiculofrota_plataforma_operacional"),
    ]

    operations = [
        migrations.AddField(
            model_name="checklistfrota",
            name="horario_chegada_loja",
            field=models.TimeField(blank=True, null=True, verbose_name="Chegada na loja"),
        ),
        migrations.AddField(
            model_name="checklistfrota",
            name="horario_saida_loja",
            field=models.TimeField(blank=True, null=True, verbose_name="Saida da loja"),
        ),
        migrations.AddField(
            model_name="solicitacaocaminhaocd",
            name="cd_destino",
            field=models.CharField(choices=[("806", "CD 806 - Japuí"), ("801", "CD 801")], default="806", max_length=3, verbose_name="CD que recebera o caminhao"),
        ),
    ]
