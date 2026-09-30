from django.db import migrations


def liberar_relatorio_paletes(apps, schema_editor):
    PerfilAcesso = apps.get_model("painel", "PerfilAcesso")
    for profile in PerfilAcesso.objects.all():
        permissoes = list(profile.permissoes or [])
        if "expedicao_planejamento" in permissoes and "relatorio_paletes_cd" not in permissoes:
            permissoes.append("relatorio_paletes_cd")
            profile.permissoes = permissoes
            profile.save(update_fields=["permissoes"])


def voltar_relatorio_paletes(apps, schema_editor):
    PerfilAcesso = apps.get_model("painel", "PerfilAcesso")
    for profile in PerfilAcesso.objects.all():
        permissoes = list(profile.permissoes or [])
        if "relatorio_paletes_cd" in permissoes:
            profile.permissoes = [perm for perm in permissoes if perm != "relatorio_paletes_cd"]
            profile.save(update_fields=["permissoes"])


class Migration(migrations.Migration):
    dependencies = [
        ("painel", "0029_seed_veiculos_frota"),
    ]

    operations = [
        migrations.RunPython(liberar_relatorio_paletes, voltar_relatorio_paletes),
    ]
