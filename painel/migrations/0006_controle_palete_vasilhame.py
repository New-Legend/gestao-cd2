from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion
import painel.models


class Migration(migrations.Migration):

    dependencies = [
        ("painel", "0005_dashboards_operacionais"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="ControlePaleteVasilhame",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("cd_unidade", models.CharField(choices=[("806", "CD 806 - Japui"), ("801", "CD 801")], default="806", max_length=3, verbose_name="CD")),
                ("data", models.DateField(default=painel.models.today, verbose_name="Data")),
                ("observacao", models.TextField(blank=True, verbose_name="Observacao")),
                ("criado_em", models.DateTimeField(auto_now_add=True)),
                ("atualizado_em", models.DateTimeField(auto_now=True)),
                ("turno", models.CharField(choices=[("manha", "Manha"), ("tarde", "Tarde"), ("noite", "Noite"), ("dia", "Dia completo")], default="dia", max_length=20, verbose_name="Turno")),
                ("responsavel", models.CharField(blank=True, max_length=120, verbose_name="Responsavel")),
                ("palete_pbr", models.PositiveIntegerField(default=0, verbose_name="Paletes PBR")),
                ("palete_chep", models.PositiveIntegerField(default=0, verbose_name="Paletes CHEP")),
                ("palete_chapatex", models.PositiveIntegerField(default=0, verbose_name="Paletes Chapatex")),
                ("palete_descartavel", models.PositiveIntegerField(default=0, verbose_name="Paletes descartaveis")),
                ("vasilhame_coca", models.PositiveIntegerField(default=0, verbose_name="Vasilhames Coca-Cola retornavel")),
                ("vasilhame_cerveja", models.PositiveIntegerField(default=0, verbose_name="Vasilhames cerveja/engradado")),
                ("palete_cheio", models.PositiveIntegerField(default=0, verbose_name="Paletes cheios")),
                ("palete_vazio", models.PositiveIntegerField(default=0, verbose_name="Paletes vazios")),
                ("criado_por", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, to=settings.AUTH_USER_MODEL)),
            ],
            options={"ordering": ["-data", "-id"]},
        ),
    ]
