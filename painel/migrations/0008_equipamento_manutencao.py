from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion
import painel.models


class Migration(migrations.Migration):

    dependencies = [
        ("painel", "0007_seed_lojas_rede_krill"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="EquipamentoManutencao",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("cd_unidade", models.CharField(choices=[("806", "CD 806 - Japui"), ("801", "CD 801")], default="806", max_length=3, verbose_name="CD")),
                ("data", models.DateField(default=painel.models.today, verbose_name="Data")),
                ("observacao", models.TextField(blank=True, verbose_name="Observacao")),
                ("criado_em", models.DateTimeField(auto_now_add=True)),
                ("atualizado_em", models.DateTimeField(auto_now=True)),
                ("patrimonio", models.CharField(max_length=120, verbose_name="Patrimonio")),
                ("equipamento", models.CharField(blank=True, max_length=120, verbose_name="Equipamento/serie")),
                ("tipo", models.CharField(choices=[("operacional", "Paleteira/coletor"), ("administrativo", "Computador administrativo"), ("empilhadeira", "Empilhadeira"), ("outro", "Outro equipamento")], default="operacional", max_length=30, verbose_name="Tipo")),
                ("problema", models.CharField(max_length=240, verbose_name="Problema relatado")),
                ("destino", models.CharField(choices=[("ti", "TI"), ("interno", "Interno"), ("fornecedor", "Fornecedor"), ("outro", "Outro")], default="ti", max_length=30, verbose_name="Destino")),
                ("responsavel", models.CharField(blank=True, max_length=120, verbose_name="Responsavel")),
                ("data_retorno", models.DateField(blank=True, null=True, verbose_name="Data retorno")),
                ("status", models.CharField(choices=[("aberto", "Aberto"), ("enviado", "Enviado"), ("retornado", "Retornado"), ("sem_conserto", "Sem conserto"), ("cancelado", "Cancelado")], default="aberto", max_length=30, verbose_name="Status")),
                ("criado_por", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, to=settings.AUTH_USER_MODEL)),
            ],
            options={"ordering": ["-data", "-id"]},
        ),
    ]
