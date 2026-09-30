# Generated manually for Central CD Krill

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("painel", "0011_funcoes_turno"),
    ]

    operations = [
        migrations.AddField(
            model_name="checklistfrota",
            name="tipo_checklist",
            field=models.CharField(
                choices=[("saida", "Saída do CD"), ("retorno", "Retorno ao CD")],
                default="saida",
                max_length=12,
                verbose_name="Tipo de checklist",
            ),
        ),
        migrations.AddField(
            model_name="checklistfrota",
            name="ocorrencia_rota_loja",
            field=models.TextField(blank=True, verbose_name="Ocorrencia no caminho ou na loja"),
        ),
        migrations.AlterField(
            model_name="perfilacesso",
            name="cargo",
            field=models.CharField(
                choices=[
                    ("master", "Master"),
                    ("gerente", "Gerente"),
                    ("supervisor", "Supervisor"),
                    ("lider", "Líder"),
                    ("analista", "Analista"),
                    ("assistente", "Assistente"),
                    ("motorista", "Motorista"),
                    ("consulta", "Consulta"),
                ],
                default="assistente",
                max_length=20,
            ),
        ),
    ]