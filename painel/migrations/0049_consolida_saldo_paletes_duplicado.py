from collections import defaultdict
import re

from django.db import migrations


def loja_codigo(valor):
    texto = str(valor or "").strip()
    match = re.match(r"^\s*0*(\d+)", texto)
    return match.group(1).zfill(2) if match else texto.upper()


def consolida_saldos(apps, schema_editor):
    ExpedicaoPlanejamento = apps.get_model("painel", "ExpedicaoPlanejamento")
    grupos = defaultdict(list)

    for item in ExpedicaoPlanejamento.objects.all().order_by("cd_unidade", "data", "id"):
        grupos[(item.cd_unidade, item.data, loja_codigo(item.loja))].append(item.pk)

    for _key, ids in grupos.items():
        if len(ids) <= 1:
            continue

        registros = list(ExpedicaoPlanejamento.objects.filter(pk__in=ids).order_by("atualizado_em", "id"))
        principal = registros[-1]
        duplicados = [item.pk for item in registros[:-1]]
        ExpedicaoPlanejamento.objects.filter(pk=principal.pk).update(loja=principal.loja)
        ExpedicaoPlanejamento.objects.filter(pk__in=duplicados).delete()


class Migration(migrations.Migration):

    dependencies = [
        ("painel", "0048_solicitacaocaminhaocd"),
    ]

    operations = [
        migrations.RunPython(consolida_saldos, migrations.RunPython.noop),
    ]
