from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("painel", "0076_romaneio_xml_worker"),
    ]

    operations = [
        migrations.AddField(
            model_name="tmsviagem",
            name="destino_cd",
            field=models.CharField(blank=True, default="", max_length=7),
        ),
        migrations.AddField(
            model_name="tmsviagem",
            name="status_transferencia",
            field=models.CharField(blank=True, default="", max_length=40),
        ),
        migrations.AddField(
            model_name="tmsviagem",
            name="tipo_operacao",
            field=models.CharField(blank=True, default="", max_length=40),
        ),
        migrations.CreateModel(
            name="FrotaDemoSimulacao",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("cd_codigo", models.CharField(max_length=7)),
                ("numero_romaneio", models.CharField(default="TST-0000", max_length=40)),
                ("status", models.CharField(default="em_carregamento", max_length=30)),
                ("hard_lock", models.BooleanField(default=False)),
                ("notas_incluidas", models.PositiveIntegerField(default=0)),
                ("notas_futuras", models.PositiveIntegerField(default=0)),
                ("waypoint_atual", models.CharField(default="doca", max_length=20)),
                ("updated_at", models.DateTimeField(auto_now=True)),
            ],
            options={
                "db_table": "frota_demo_simulacoes",
                "constraints": [
                    models.UniqueConstraint(fields=("cd_codigo", "numero_romaneio"), name="frota_demo_cd_numero"),
                ],
            },
        ),
    ]
