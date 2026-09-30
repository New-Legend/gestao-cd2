# Generated manually to enable granular notification permissions.

from django.db import migrations


FROTA_NOTIFY_PERMISSIONS = {
    "notificar_carga_vinculada",
    "notificar_alteracao_carga",
    "receber_alertas_expedicao",
}

EXPEDICAO_NOTIFY_PERMISSIONS = {
    "notificar_carga_vinculada",
    "receber_alertas_expedicao",
}

MOTORISTA_NOTIFY_PERMISSIONS = {
    "notificar_motorista_vinculado",
}


def ordered_permissions(values):
    return sorted(set(values or []))


def add_permissions(profile, permissions):
    current = set(profile.permissoes or [])
    updated = current | set(permissions)
    if updated != current:
        profile.permissoes = ordered_permissions(updated)
        profile.save(update_fields=["permissoes"])


def grant_notification_permissions(apps, schema_editor):
    PerfilAcesso = apps.get_model("painel", "PerfilAcesso")
    for profile in PerfilAcesso.objects.all():
        current = set(profile.permissoes or [])
        cargo = (profile.cargo or "").strip().lower()
        if current & {"vincular_cargas_expedicao", "aprovacoes_carregamento", "painel_frota"}:
            add_permissions(profile, FROTA_NOTIFY_PERMISSIONS)
        if current & {"expedicao", "carregamento_veiculos", "relatorio_paletes_cd", "expedicao_planejamento"}:
            add_permissions(profile, EXPEDICAO_NOTIFY_PERMISSIONS)
        if cargo == "motorista" or "checklist_frota" in current:
            add_permissions(profile, MOTORISTA_NOTIFY_PERMISSIONS)


class Migration(migrations.Migration):

    dependencies = [
        ("painel", "0043_pushsubscription_vapid_public_key"),
    ]

    operations = [
        migrations.RunPython(grant_notification_permissions, migrations.RunPython.noop),
    ]
