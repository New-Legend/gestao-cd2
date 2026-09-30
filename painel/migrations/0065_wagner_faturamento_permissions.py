from django.db import migrations


WAGNER_PERMISSIONS = {
    "painel",
    "expedicao",
    "faturamento_expedicao",
    "lojas_prontas_carregamento",
    "relatorio_paletes_cd",
    "consultar_registros",
    "criar_registros",
    "editar_registros",
    "receber_alertas_expedicao",
}


def grant_wagner_permissions(apps, schema_editor):
    User = apps.get_model("auth", "User")
    PerfilAcesso = apps.get_model("painel", "PerfilAcesso")
    users = User.objects.filter(username__icontains="wagner") | User.objects.filter(first_name__icontains="wagner") | User.objects.filter(last_name__icontains="wagner")
    for user in users.distinct():
        profile = PerfilAcesso.objects.filter(user=user).first()
        if not profile:
            continue
        current = set(profile.permissoes or [])
        updated = sorted(current | WAGNER_PERMISSIONS)
        if updated != profile.permissoes:
            profile.permissoes = updated
            if not profile.cargo:
                profile.cargo = "assistente"
            profile.save(update_fields=["permissoes", "cargo", "atualizado_em"])


class Migration(migrations.Migration):

    dependencies = [
        ("painel", "0064_loja_empilhadeira_indisponivel"),
    ]

    operations = [
        migrations.RunPython(grant_wagner_permissions, migrations.RunPython.noop),
    ]
