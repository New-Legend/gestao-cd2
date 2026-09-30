from django.db import migrations


FROTA_ESCALA_PERMISSIONS = {
    "escala_veiculos_frota",
}

COLABORADORES_AUSENCIA_PERMISSIONS = {
    "ausencias_colaboradores",
}

COLABORADORES_GESTAO_PERMISSIONS = {
    "colaboradores_hub",
    "ausencias_colaboradores",
    "capacidade_operacao",
    "visualizar_gargalos_colaboradores",
}


def add_permissions(profile, permissions):
    current = set(profile.permissoes or [])
    updated = current | set(permissions)
    if updated != current:
        profile.permissoes = sorted(updated)
        profile.save(update_fields=["permissoes"])


def grant_permissions(apps, schema_editor):
    PerfilAcesso = apps.get_model("painel", "PerfilAcesso")

    for profile in PerfilAcesso.objects.select_related("user").all():
        current = set(profile.permissoes or [])
        cargo = (profile.cargo or "").strip().lower()
        username = (profile.user.username or "").strip().lower()

        if current & {"painel_frota", "veiculos_frota", "vincular_cargas_expedicao", "carregamento_veiculos"}:
            add_permissions(profile, FROTA_ESCALA_PERMISSIONS)

        if current & {
            "colaboradores_hub",
            "ferias_colaboradores",
            "pessoas_turno",
            "funcoes_turno",
            "capacidade_operacao",
            "visualizar_gargalos_colaboradores",
        }:
            add_permissions(profile, COLABORADORES_AUSENCIA_PERMISSIONS)

        if cargo in {
            "gestor_cd",
            "gerente",
            "analista",
            "supervisor",
            "supervisor_recebimento",
            "supervisor_separacao",
            "supervisor_conferencia_expedicao",
            "supervisor_frota",
            "lider",
            "lider_separacao",
            "lider_conferencia_expedicao",
            "lider_frota",
        }:
            add_permissions(profile, COLABORADORES_GESTAO_PERMISSIONS)

        if username in {"andressa", "andressa.santos", "andressa.silva"}:
            add_permissions(
                profile,
                COLABORADORES_GESTAO_PERMISSIONS
                | {
                    "ferias_colaboradores",
                    "gerenciar_ferias_colaboradores",
                    "configurar_alertas_ferias",
                    "notificacoes_ferias_colaboradores",
                    "mapa_calor_ferias",
                    "pessoas_turno",
                    "funcoes_turno",
                },
            )


class Migration(migrations.Migration):
    dependencies = [
        ("painel", "0046_colaboradorferias_setor_detalhado_and_more"),
    ]

    operations = [
        migrations.RunPython(grant_permissions, migrations.RunPython.noop),
    ]
