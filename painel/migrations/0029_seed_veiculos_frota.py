from django.db import migrations


VEICULOS = [
    ("ALLAN", "STH 8D95", "Plataforma"),
    ("ANDERSON", "STR 6A35", ""),
    ("BRENDON", "EYA 1H33", ""),
    ("EDILSON", "ESP 1316", "Plataforma"),
    ("EDUARDO", "FXJ 7564", ""),
    ("GODOI", "GAQ 4571", "Plataforma"),
    ("JEFFERSON", "CQU 6J55", "Plataforma"),
    ("JOHN LENNON", "EKJ 1380", "Plataforma"),
    ("JOSE PAULO", "FJD 2J56", "Plataforma"),
    ("KENIDY", "FPN 1F09", ""),
    ("MARCIEL", "EGG 7260", ""),
    ("MULLER", "FWH 8J76", ""),
    ("PABLO KALID", "SUJ 6J21", "Plataforma"),
    ("PAULO NETO", "FGA 3738", ""),
    ("PAULO SERGIO", "GJR 4D46", ""),
    ("RENATO", "STY 0F57", "Plataforma"),
    ("ROBERVALDO", "GEZ 1B94", "Plataforma"),
]


def seed_veiculos(apps, schema_editor):
    VeiculoFrota = apps.get_model("painel", "VeiculoFrota")
    for cd in ("801", "806"):
        for motorista, placa, tipo in VEICULOS:
            VeiculoFrota.objects.update_or_create(
                cd_unidade=cd,
                motorista=motorista,
                placa=placa,
                defaults={
                    "tipo_caminhao": tipo,
                    "ativo": True,
                    "observacao": "Cadastro inicial baseado na lista operacional da frota.",
                },
            )


def unseed_veiculos(apps, schema_editor):
    VeiculoFrota = apps.get_model("painel", "VeiculoFrota")
    for cd in ("801", "806"):
        for motorista, placa, _tipo in VEICULOS:
            VeiculoFrota.objects.filter(cd_unidade=cd, motorista=motorista, placa=placa).delete()


class Migration(migrations.Migration):

    dependencies = [
        ("painel", "0028_veiculofrota"),
    ]

    operations = [
        migrations.RunPython(seed_veiculos, unseed_veiculos),
    ]
