from django.db import migrations
from django.utils import timezone


LOJAS_REDE_KRILL = [
    ("01", "KRILL P. GRANDE", "praia"),
    ("02", "MERI KRILL", "outros"),
    ("03", "UNIAO SAO VICENT", "praia"),
    ("04", "KRILL ARESTA", "outros"),
    ("05", "ALMEIDA ROCHA", "bairro"),
    ("07", "KRILL DE SANTOS", "praia"),
    ("08", "KRILL DE CUBATAO", "bairro"),
    ("10", "KRILL ITANHAEM", "praia"),
    ("12", "ALMEIDA ROCHA 2", "bairro"),
    ("13", "VICENTE CARVALHO", "bairro"),
    ("14", "KRILL BERTIOGA", "praia"),
    ("15", "KRILL PERUIBE", "praia"),
    ("16", "ROCHA PRAIA GRANDE", "praia"),
    ("17", "KRILL BORACEIA", "praia"),
    ("18", "KRILL CASQUEIRO", "bairro"),
    ("19", "KRILL CAICARA", "praia"),
    ("20", "KRILL SAMAMBAIA", "bairro"),
    ("21", "NOVA CINTRA", "bairro"),
    ("22", "KRILL BOICUCANGA", "praia"),
    ("24", "KRILL ENSEADA", "praia"),
    ("25", "KRILL FREI GASPAR", "bairro"),
    ("26", "KRILL PADRE ANCHIETA", "bairro"),
    ("27", "KRILL GONZAGA", "praia"),
    ("28", "KRILL MARACANA", "bairro"),
    ("801", "CD REDE KRILL", "plataforma"),
    ("805", "CD KRILL ALEMOA", "plataforma"),
    ("806", "CD JAPUI", "plataforma"),
]


def seed_lojas(apps, schema_editor):
    Loja = apps.get_model("painel", "Loja")
    hoje = timezone.localdate()
    for cd_unidade in ("806", "801"):
        for codigo, nome, grupo in LOJAS_REDE_KRILL:
            Loja.objects.update_or_create(
                cd_unidade=cd_unidade,
                codigo=codigo,
                defaults={
                    "data": hoje,
                    "nome": nome,
                    "grupo": grupo,
                    "ativa": True,
                    "observacao": "Cadastro base da Rede Krill.",
                },
            )


def unseed_lojas(apps, schema_editor):
    Loja = apps.get_model("painel", "Loja")
    codigos = [codigo for codigo, _nome, _grupo in LOJAS_REDE_KRILL]
    Loja.objects.filter(codigo__in=codigos, observacao="Cadastro base da Rede Krill.").delete()


class Migration(migrations.Migration):

    dependencies = [
        ("painel", "0006_controle_palete_vasilhame"),
    ]

    operations = [
        migrations.RunPython(seed_lojas, unseed_lojas),
    ]
