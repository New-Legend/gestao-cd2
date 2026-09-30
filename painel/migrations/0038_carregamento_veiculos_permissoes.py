from django.db import migrations


MASTER_PERMISSIONS = {
    "carregamento_veiculos",
    "vincular_cargas_expedicao",
    "corrigir_saldo_paletes",
}

FROTA_PERMISSIONS = {
    "carregamento_veiculos",
    "vincular_cargas_expedicao",
    "relatorio_paletes_cd",
    "visualizar_cds_unificados",
    "veiculos_frota",
}


def ordered_permissions(values):
    return sorted(set(values))


def add_permissions(profile, permissions):
    current = set(profile.permissoes or [])
    profile.permissoes = ordered_permissions(current | set(permissions))
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


def noop_reverse(apps, schema_editor):
    pass


class Migration(migrations.Migration):
    dependencies = [
        ("painel", "0037_expedicao_permissoes_vinculo"),
    ]

    operations = [
        migrations.RunPython(liberar_permissoes, noop_reverse),
    ]
