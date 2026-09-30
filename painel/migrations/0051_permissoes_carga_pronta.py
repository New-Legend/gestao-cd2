from django.db import migrations


NEW_PERMISSIONS = {
    "lojas_prontas_carregamento",
    "acompanhar_lojas_prontas_carregamento",
    "notificar_loja_pronta_carregamento",
}


def add_permissions(profile, permissions):
    current = list(profile.permissoes or [])
    changed = False
    for permission in permissions:
        if permission not in current:
            current.append(permission)
            changed = True
    if changed:
        profile.permissoes = current
        profile.save(update_fields=["permissoes"])


def apply(apps, schema_editor):
    User = apps.get_model("auth", "User")
    PerfilAcesso = apps.get_model("painel", "PerfilAcesso")

    for profile in PerfilAcesso.objects.all():
        permissions = set(profile.permissoes or [])
        is_master = profile.cargo == "master" or "gestao_acesso_total" in permissions or "painel_master" in permissions
        if is_master:
            add_permissions(profile, NEW_PERMISSIONS)
            continue
        if permissions & {"expedicao", "expedicao_planejamento", "faturamento_expedicao"}:
            add_permissions(profile, {"lojas_prontas_carregamento"})
        if permissions & {"painel_frota", "carregamento_veiculos", "vincular_cargas_expedicao", "aprovacoes_carregamento"}:
            add_permissions(profile, {"acompanhar_lojas_prontas_carregamento", "notificar_loja_pronta_carregamento"})

    marlusa = User.objects.filter(username__iexact="marlusa").first()
    if marlusa:
        profile, _created = PerfilAcesso.objects.get_or_create(
            user=marlusa,
            defaults={
                "cargo": "motorista",
                "cd_padrao": "806",
                "permissoes": [],
            },
        )
        profile.cargo = "motorista"
        if not profile.cd_padrao:
            profile.cd_padrao = "806"
        profile.save(update_fields=["cargo", "cd_padrao"])
        add_permissions(profile, {"checklist_frota", "notificar_motorista_vinculado"})


class Migration(migrations.Migration):
    dependencies = [
        ("painel", "0050_carga_pronta_expedicao"),
    ]

    operations = [
        migrations.RunPython(apply, migrations.RunPython.noop),
    ]
