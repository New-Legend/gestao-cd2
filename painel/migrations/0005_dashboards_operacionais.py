from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion
import painel.models


class Migration(migrations.Migration):

    dependencies = [
        ("painel", "0004_pessoaturno"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="RecebimentoConferente",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("cd_unidade", models.CharField(choices=[("806", "CD 806 - Japui"), ("801", "CD 801")], default="806", max_length=3, verbose_name="CD")),
                ("data", models.DateField(default=painel.models.today, verbose_name="Data")),
                ("observacao", models.TextField(blank=True, verbose_name="Observacao")),
                ("criado_em", models.DateTimeField(auto_now_add=True)),
                ("atualizado_em", models.DateTimeField(auto_now=True)),
                ("conferente", models.CharField(max_length=120, verbose_name="Conferente")),
                ("paletes", models.PositiveIntegerField(default=0, verbose_name="Paletes")),
                ("cargas", models.PositiveIntegerField(default=0, verbose_name="Cargas")),
                ("minutos_trabalhados", models.PositiveIntegerField(default=0, verbose_name="Minutos trabalhados")),
                ("sem_agendamento", models.PositiveIntegerField(default=0, verbose_name="Recebido sem agendamento")),
                ("criado_por", models.ForeignKey(null=True, blank=True, on_delete=django.db.models.deletion.SET_NULL, to=settings.AUTH_USER_MODEL)),
            ],
            options={"ordering": ["-data", "-id"]},
        ),
        migrations.CreateModel(
            name="RessuprimentoPainel",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("cd_unidade", models.CharField(choices=[("806", "CD 806 - Japui"), ("801", "CD 801")], default="806", max_length=3, verbose_name="CD")),
                ("data", models.DateField(default=painel.models.today, verbose_name="Data")),
                ("observacao", models.TextField(blank=True, verbose_name="Observacao")),
                ("criado_em", models.DateTimeField(auto_now_add=True)),
                ("atualizado_em", models.DateTimeField(auto_now=True)),
                ("tipo_painel", models.CharField(choices=[("demanda", "Demanda"), ("ocupacao", "Ocupacao"), ("producao", "Producao")], default="demanda", max_length=20, verbose_name="Tipo de painel")),
                ("setor", models.CharField(max_length=80, verbose_name="Setor")),
                ("rua", models.CharField(blank=True, max_length=80, verbose_name="Rua")),
                ("qtd_itens", models.PositiveIntegerField(default=0, verbose_name="Qtd. itens")),
                ("qtd_volume", models.DecimalField(decimal_places=2, default=0, max_digits=14, verbose_name="Qtd. volume")),
                ("pendencia_itens", models.PositiveIntegerField(default=0, verbose_name="Pendencia itens")),
                ("pendencia_volume", models.DecimalField(decimal_places=2, default=0, max_digits=14, verbose_name="Pendencia volume")),
                ("produzido_itens", models.PositiveIntegerField(default=0, verbose_name="Produzido itens")),
                ("produzido_volume", models.DecimalField(decimal_places=2, default=0, max_digits=14, verbose_name="Produzido volume")),
                ("picking", models.DecimalField(decimal_places=2, default=0, max_digits=14, verbose_name="Picking")),
                ("pulmao", models.DecimalField(decimal_places=2, default=0, max_digits=14, verbose_name="Pulmao")),
                ("criado_por", models.ForeignKey(null=True, blank=True, on_delete=django.db.models.deletion.SET_NULL, to=settings.AUTH_USER_MODEL)),
            ],
            options={"ordering": ["-data", "-id"]},
        ),
        migrations.CreateModel(
            name="SeparacaoProdutividade",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("cd_unidade", models.CharField(choices=[("806", "CD 806 - Japui"), ("801", "CD 801")], default="806", max_length=3, verbose_name="CD")),
                ("data", models.DateField(default=painel.models.today, verbose_name="Data")),
                ("observacao", models.TextField(blank=True, verbose_name="Observacao")),
                ("criado_em", models.DateTimeField(auto_now_add=True)),
                ("atualizado_em", models.DateTimeField(auto_now=True)),
                ("setor", models.CharField(blank=True, max_length=80, verbose_name="Setor")),
                ("onda_gerada", models.PositiveIntegerField(default=0, verbose_name="Onda gerada")),
                ("total_a_produzir", models.DecimalField(decimal_places=2, default=0, max_digits=14, verbose_name="Total a produzir")),
                ("crossdocking_produzido", models.DecimalField(decimal_places=2, default=0, max_digits=14, verbose_name="Crossdocking produzido")),
                ("separacao_pulmao_produzido", models.DecimalField(decimal_places=2, default=0, max_digits=14, verbose_name="Separacao pulmao produzido")),
                ("separacao_picking_produzido", models.DecimalField(decimal_places=2, default=0, max_digits=14, verbose_name="Separacao picking produzido")),
                ("recursos_picking", models.PositiveIntegerField(default=0, verbose_name="Recursos picking")),
                ("horas_picking", models.DecimalField(decimal_places=2, default=0, max_digits=8, verbose_name="Horas picking")),
                ("recursos_pulmao", models.PositiveIntegerField(default=0, verbose_name="Recursos pulmao")),
                ("horas_pulmao", models.DecimalField(decimal_places=2, default=0, max_digits=8, verbose_name="Horas pulmao")),
                ("criado_por", models.ForeignKey(null=True, blank=True, on_delete=django.db.models.deletion.SET_NULL, to=settings.AUTH_USER_MODEL)),
            ],
            options={"ordering": ["-data", "-id"]},
        ),
    ]
