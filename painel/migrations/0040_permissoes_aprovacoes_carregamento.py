from django.db import migrations


MASTER_PERMISSIONS = {
    "aprovacoes_carregamento",
    "solicitar_lancamento_manual_expedicao",
    "aprovar_lancamento_manual_expedicao",
    "lancar_manual_sem_aprovacao",
}

FROTA_PERMISSIONS = {
    "aprovacoes_carregamento",
    "aprovar_lancamento_manual_expedicao",
    "visualizar_cds_unificados",
    "alertas_frota",
}

EXPEDICAO_PERMISSIONS = {
    "expedicao",
    "solicitar_lancamento_manual_expedicao",
}


def add_permissions(profile, permissions):
    current = set(profile.permissoes or [])
    profile.permissoes = sorted(current | set(permissions))
    profile.save(update_fields=["permissoes"])


def liberar_permissoes(apps, schema_editor):
    PerfilAcesso = apps.get_model("painel", "PerfilAcesso")

    for profile in PerfilAcesso.objects.select_related("user").all():
        username = (profile.user.username or "").strip().lower()
        cargo = (profile.cargo or "").strip().lower()

        if profile.user.is_superuser or cargo == "master":
            add_permissions(profile, MASTER_PERMISSIONS)
            continue

        if cargo == "supervisor_frota" or username in {"mauricio.junior", "mauricio"}:
            add_permissions(profile, FROTA_PERMISSIONS)

        if username in {"expedicao.801", "expedicao801", "expedicao.806", "expedicao806"}:
            add_permissions(profile, EXPEDICAO_PERMISSIONS)


def noop_reverse(apps, schema_editor):
    pass


class Migration(migrations.Migration):
    dependencies = [
        ("painel", "0039_solicitacaocarregamentomanual"),
    ]

    operations = [
        migrations.RunPython(liberar_permissoes, noop_reverse),
    ]
