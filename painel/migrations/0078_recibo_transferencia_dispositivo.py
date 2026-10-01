from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ("painel", "0077_mapa_bau_transferencia"),
    ]

    operations = [
        migrations.CreateModel(
            name="TmsTransferenciaRecebimento",
            fields=[
                ("id", models.CharField(max_length=40, primary_key=True, serialize=False)),
                ("cd_origem", models.CharField(blank=True, default="", max_length=7)),
                ("cd_destino", models.CharField(blank=True, default="", max_length=7)),
                ("placa", models.CharField(blank=True, default="", max_length=20)),
                ("motorista_nome", models.CharField(blank=True, default="", max_length=160)),
                ("total_paletes", models.FloatField(default=0)),
                ("peso_total_kg", models.FloatField(default=0)),
                ("romaneios_json", models.JSONField(default=list)),
                ("notas_fiscais_json", models.JSONField(default=list)),
                ("recebido_por", models.CharField(blank=True, default="", max_length=160)),
                ("recebido_em", models.DateTimeField(auto_now_add=True)),
                ("viagem", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="recebimentos_transferencia", to="painel.tmsviagem")),
            ],
            options={
                "db_table": "tms_transferencia_recebimentos",
                "ordering": ["-recebido_em"],
            },
        ),
        migrations.AddField(
            model_name="logisticadispositivo",
            name="motorista_usuario",
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="dispositivos_logistica", to=settings.AUTH_USER_MODEL),
        ),
        migrations.AddField(
            model_name="logisticadispositivo",
            name="revogado_em",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="logisticadispositivo",
            name="token_hash",
            field=models.CharField(blank=True, default="", max_length=64),
        ),
    ]
