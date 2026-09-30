import re
import unicodedata

from django.db import migrations


UNIFIED_CD = "801+806"


FROTA_ASSISTENTE = {
    "painel",
    "painel_frota",
    "checklist_frota",
    "veiculos_frota",
    "escala_veiculos_frota",
    "solicitacao_caminhoes",
    "aprovacoes_carregamento",
    "acompanhar_lojas_prontas_carregamento",
    "vincular_cargas_expedicao",
    "aprovar_lancamento_manual_expedicao",
    "tratar_solicitacao_caminhoes",
    "relatorio_paletes_cd",
    "faturamento_expedicao",
    "consultar_registros",
    "criar_registros",
    "editar_registros",
    "ver_graficos_resumos",
    "visualizar_cds_unificados",
    "trocar_cd",
    "alertas_frota",
    "notificar_loja_pronta_carregamento",
    "notificar_carga_vinculada",
    "notificar_alteracao_carga",
    "notificar_solicitacao_caminhoes",
    "notificar_checklist_motorista",
    "notificar_motorista_vinculado",
    "receber_alertas_expedicao",
    "encerrar_retorno_frota",
    "exportar_frota",
}


FROTA_SUPERVISOR = FROTA_ASSISTENTE | {
    "checklist_frota_itens",
    "editar_checklist_frota",
    "materiais_frota",
    "lacres_frota",
    "lancar_manual_sem_aprovacao",
    "exportar_dados",
}


EXPEDICAO_FATURAMENTO = {
    "painel",
    "lojas_prontas_carregamento",
    "expedicao_planejamento",
    "relatorio_paletes_cd",
    "faturamento_expedicao",
    "expedicao",
    "consultar_registros",
    "criar_registros",
    "editar_registros",
    "ver_graficos_resumos",
    "receber_alertas_expedicao",
    "notificar_saldo_paletes",
    "notificar_loja_pronta_carregamento",
}


GERENCIAL_806 = {
    "painel",
    "painel_gestao",
    "relatorio_paletes_cd",
    "consultar_registros",
    "ver_graficos_resumos",
    "receber_alertas_expedicao",
    "notificar_carga_vinculada",
    "notificar_alteracao_carga",
    "notificar_loja_pronta_carregamento",
    "notificar_saldo_paletes",
}


GERENCIA_UNIFICADA = GERENCIAL_806 | {
    "visualizar_cds_unificados",
    "trocar_cd",
    "relatorios",
}


PALLETS_REDE = {
    "painel",
    "paletes_rede",
    "adicionar_loja_paletes_rede",
    "editar_saldo_paletes_rede",
    "movimentar_paletes_rede",
    "consultar_registros",
    "criar_registros",
    "editar_registros",
    "exportar_dados",
    "ver_graficos_resumos",
    "visualizar_cds_unificados",
    "trocar_cd",
}


MOTORISTA = {
    "checklist_frota",
    "notificar_motorista_vinculado",
}


PRESETS = [
    {
        "tokens": {"marcela"},
        "cargo": "assistente",
        "cd": UNIFIED_CD,
        "permissions": FROTA_ASSISTENTE,
    },
    {
        "tokens": {"mauricio", "mauricio"},
        "cargo": "supervisor_frota",
        "cd": UNIFIED_CD,
        "permissions": FROTA_SUPERVISOR,
    },
    {
        "tokens": {"wagner"},
        "cargo": "assistente",
        "cd": "801",
        "permissions": EXPEDICAO_FATURAMENTO,
    },
    {
        "tokens": {"leilane"},
        "cargo": "consulta",
        "cd": "806",
        "permissions": GERENCIAL_806,
    },
    {
        "tokens": {"naka", "naca"},
        "cargo": "gerente",
        "cd": UNIFIED_CD,
        "permissions": GERENCIA_UNIFICADA,
    },
    {
        "tokens": {"juliana"},
        "cargo": "assistente",
        "cd": UNIFIED_CD,
        "permissions": PALLETS_REDE,
    },
]


def normalize(value):
    text = unicodedata.normalize("NFKD", str(value or "")).encode("ascii", "ignore").decode("ascii")
    return re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()


def profile_text(user):
    return normalize(" ".join([user.username, user.first_name, user.last_name]))


def set_profile(profile, cargo, cd, permissions):
    profile.cargo = cargo
    profile.cd_padrao = cd
    profile.permissoes = sorted(permissions)
    profile.save(update_fields=["cargo", "cd_padrao", "permissoes", "atualizado_em"])


def apply_permission_presets(apps, schema_editor):
    User = apps.get_model("auth", "User")
    PerfilAcesso = apps.get_model("painel", "PerfilAcesso")

    for user in User.objects.filter(is_active=True):
        profile = PerfilAcesso.objects.filter(user=user).first()
        if not profile:
            continue
        current_permissions = set(profile.permissoes or [])
        if user.is_superuser or profile.cargo == "master" or "gestao_acesso_total" in current_permissions:
            continue
        if profile.cargo == "motorista":
            set_profile(profile, "motorista", profile.cd_padrao or "806", MOTORISTA)
            continue
        text = profile_text(user)
        for preset in PRESETS:
            if any(token in text for token in preset["tokens"]):
                set_profile(profile, preset["cargo"], preset["cd"], preset["permissions"])
                break


class Migration(migrations.Migration):

    dependencies = [
        ("painel", "0066_ativa_faturamento_piloto"),
    ]

    operations = [
        migrations.RunPython(apply_permission_presets, migrations.RunPython.noop),
    ]
