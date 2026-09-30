from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ("painel", "0002_auditlog_dados_antes_auditlog_dados_depois_backuplog_and_more"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="SolicitacaoSenha",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                (
                    "login_informado",
                    models.CharField(max_length=150),
                ),
                (
                    "status",
                    models.CharField(
                        choices=[("pendente", "Pendente"), ("atendida", "Atendida"), ("cancelada", "Cancelada")],
                        default="pendente",
                        max_length=20,
                    ),
                ),
                ("observacao", models.CharField(blank=True, max_length=240)),
                ("criado_em", models.DateTimeField(auto_now_add=True)),
                ("atendido_em", models.DateTimeField(blank=True, null=True)),
                (
                    "atendido_por",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="solicitacoes_senha_atendidas",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "usuario",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
            options={
                "ordering": ["-criado_em"],
            },
        ),
    ]
