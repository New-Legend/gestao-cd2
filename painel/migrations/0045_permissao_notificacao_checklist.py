# Generated manually to notify supervisors when drivers save checklist records.

from django.db import migrations


CHECKLIST_NOTIFICATION_PERMISSION = "notificar_checklist_motorista"


def grant_checklist_notification_permission(apps, schema_editor):
    PerfilAcesso = apps.get_model("painel", "PerfilAcesso")
    trigger_permissions = {
        "painel_frota",
        "editar_checklist_frota",
        "alertas_frota",
        "vincular_cargas_expedicao",
        "aprovacoes_carregamento",
    }
    trigger_roles = {
        "master",
        "gestor_cd",
        "gerente",
        "supervisor",
        "supervisor_frota",
        "lider_frota",
        "analista",
    }
    for profile in PerfilAcesso.objects.all():
        current = set(profile.permissoes or [])
        cargo = (profile.cargo or "").strip().lower()
        if current & trigger_permissions or cargo in trigger_roles:
            updated = sorted(current | {CHECKLIST_NOTIFICATION_PERMISSION})
            if updated != profile.permissoes:
                profile.permissoes = updated
                profile.save(update_fields=["permissoes"])


class Migration(migrations.Migration):

    dependencies = [
        ("painel", "0044_permissoes_notificacoes_frota"),
    ]

    operations = [
        migrations.RunPython(grant_checklist_notification_permission, migrations.RunPython.noop),
    ]
