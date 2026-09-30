from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion
import painel.models


class Migration(migrations.Migration):

    dependencies = [
        ("painel", "0003_solicitacaosenha"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="PessoaTurno",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("cd_unidade", models.CharField(choices=[("806", "CD 806 - Japui"), ("801", "CD 801")], default="806", max_length=3, verbose_name="CD")),
                ("data", models.DateField(default=painel.models.today, verbose_name="Data")),
                ("observacao", models.TextField(blank=True, verbose_name="Observacao")),
                ("criado_em", models.DateTimeField(auto_now_add=True)),
                ("atualizado_em", models.DateTimeField(auto_now=True)),
                ("funcao", models.CharField(max_length=120, verbose_name="Funcao")),
                ("quadro_atual", models.PositiveIntegerField(default=0, verbose_name="Quadro atual")),
                ("planejado", models.PositiveIntegerField(default=0, verbose_name="Planejado")),
                ("ativos_dia", models.PositiveIntegerField(default=0, verbose_name="Ativos no dia")),
                ("atestados", models.PositiveIntegerField(default=0, verbose_name="Atestados")),
                ("afastados", models.PositiveIntegerField(default=0, verbose_name="Afastados")),
                ("ferias", models.PositiveIntegerField(default=0, verbose_name="Ferias")),
                ("criado_por", models.ForeignKey(null=True, blank=True, on_delete=django.db.models.deletion.SET_NULL, to=settings.AUTH_USER_MODEL)),
            ],
            options={
                "ordering": ["-data", "-id"],
            },
        ),
    ]
