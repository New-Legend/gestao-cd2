from django.db import migrations


FROTA_LINK_PERMISSIONS = {
    "painel",
    "consultar_registros",
    "criar_registros",
    "expedicao_planejamento",
    "expedicao",
    "relatorio_paletes_cd",
    "visualizar_cds_unificados",
    "veiculos_frota",
    "vincular_cargas_expedicao",
}

EXPEDITION_ONLY_PERMISSIONS = {
    "painel",
    "consultar_registros",
    "criar_registros",
    "expedicao",
    "visualizar_cds_unificados",
}

EXPEDITION_BLOCKED_PERMISSIONS = {
    "expedicao_planejamento",
    "relatorio_paletes_cd",
    "vincular_cargas_expedicao",
    "trocar_cd",
    "editar_registros",
    "excluir_registros",
}


def ordered_permissions(values):
    return sorted(set(values))


def grant(profile, permissions):
    current = set(profile.permissoes or [])
    profile.permissoes = ordered_permissions(current | set(permissions))
    profile.save(update_fields=["permissoes"])


def grant_and_block(profile, grant_permissions, blocked_permissions):
    current = set(profile.permissoes or [])
    current |= set(grant_permissions)
    current -= set(blocked_permissions)
    profile.permissoes = ordered_permissions(current)
    profile.save(update_fields=["permissoes"])


def liberar_permissoes_expedicao(apps, schema_editor):
    PerfilAcesso = apps.get_model("painel", "PerfilAcesso")

    for profile in PerfilAcesso.objects.select_related("user").all():
        username = (profile.user.username or "").strip().lower()
        cargo = (profile.cargo or "").strip().lower()

        if cargo == "master" or profile.user.is_superuser:
            grant(profile, {"vincular_cargas_expedicao"})
            continue

        if cargo == "supervisor_frota" or username in {"mauricio.junior", "mauricio"}:
            grant(profile, FROTA_LINK_PERMISSIONS)
            continue

        if username in {"expedicao.801", "expedicao801", "expedicao.806", "expedicao806"}:
            grant_and_block(profile, EXPEDITION_ONLY_PERMISSIONS, EXPEDITION_BLOCKED_PERMISSIONS)


def noop_reverse(apps, schema_editor):
    pass


class Migration(migrations.Migration):
    dependencies = [
        ("painel", "0036_expedicaovinculo"),
    ]

    operations = [
        migrations.RunPython(liberar_permissoes_expedicao, noop_reverse),
    ]
