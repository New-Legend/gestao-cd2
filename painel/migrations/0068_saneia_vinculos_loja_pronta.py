import re

from django.db import migrations


READY_NOTICE_RE = re.compile(r"#(\d+)")


def sync_ready_notice_links(apps, schema_editor):
    ExpedicaoVinculo = apps.get_model("painel", "ExpedicaoVinculo")
    SolicitacaoCargaPronta = apps.get_model("painel", "SolicitacaoCargaPronta")

    links = ExpedicaoVinculo.objects.filter(status="vinculado", observacao__icontains="loja pronta #")
    for link in links.iterator():
        match = READY_NOTICE_RE.search(link.observacao or "")
        if not match:
            continue
        notice = SolicitacaoCargaPronta.objects.filter(pk=int(match.group(1))).first()
        if not notice:
            continue
        if notice.status == "cancelada":
            link.status = "cancelado"
            link.save(update_fields=["status", "atualizado_em"])
        elif notice.status == "carregada":
            link.status = "carregado"
            link.carregado_em = notice.tratado_em or link.atualizado_em
            link.save(update_fields=["status", "carregado_em", "atualizado_em"])


class Migration(migrations.Migration):

    dependencies = [
        ("painel", "0067_presets_permissoes_piloto"),
    ]

    operations = [
        migrations.RunPython(sync_ready_notice_links, migrations.RunPython.noop),
    ]
