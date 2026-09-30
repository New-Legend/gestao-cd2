import csv
import io
import json
import os
import re
import secrets
import socket
import time
import unicodedata
import uuid
import zipfile
from collections import OrderedDict, defaultdict
from dataclasses import replace
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from urllib.parse import parse_qsl, urldefrag

import pandas as pd
from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill
from django import forms
from django.conf import settings
from django.contrib import messages
from django.contrib.auth import authenticate, login, logout
from django.contrib.auth.decorators import login_required
from django.contrib.auth.models import User
from django.core.management import call_command
from django.contrib.sessions.models import Session
from django.db import IntegrityError, connection, models as django_models, transaction
from django.db.models import Case, Count, F, IntegerField, Max, Prefetch, Q, Sum, Value, When
from django.http import FileResponse, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils.http import urlencode
from django.utils import timezone
from django.utils.http import url_has_allowed_host_and_scheme
from django.forms.models import model_to_dict
from django.views.decorators.cache import never_cache
from django.views.decorators.csrf import ensure_csrf_cookie
from django.views.decorators.http import require_POST

from .forms import PerfilUsuarioForm, PreferenciasForm, SolicitacaoAcessoForm, SolicitacaoSenhaForm, UsuarioForm, build_model_form
from .models import (
    AuditLog,
    Avaria,
    BackupLog,
    ChecklistFrotaGrupo,
    ChecklistFrotaItem,
    ChamadoSaldo,
    ColaboradorFerias,
    ColaboradorAusencia,
    ChecklistFrota,
    Conferencia,
    ConfiguracaoSistema,
    ControlePaleteVasilhame,
    Equipamento,
    EquipamentoManutencao,
    EscalaVeiculoFrota,
    Expedicao,
    ExpedicaoFaturamento,
    ExpedicaoPlanejamento,
    ExpedicaoVinculo,
    AprovacaoOperacional,
    FechamentoDia,
    FuncaoTurno,
    LacreFrota,
    Loja,
    MaterialConsumo,
    PaleteRedeMovimentacao,
    PaleteRedeSaldo,
    VeiculoFrota,
    MelhoriaSistema,
    OcorrenciaOperacional,
    Pendencia,
    PerfilAcesso,
    PessoaTurno,
    ParametroCD,
    Prestador,
    ProdutoGtin,
    Recebimento,
    RecebimentoAgenda,
    RecebimentoConferente,
    Ressuprimento,
    RessuprimentoPainel,
    SETOR_CHOICES,
    Separacao,
    SeparacaoProdutividade,
    SolicitacaoAcesso,
    SolicitacaoCarregamentoManual,
    SolicitacaoCargaPronta,
    SolicitacaoCaminhaoCD,
    SolicitacaoSenha,
    PushSubscription,
    SistemaNotificacao,
    Unitizador,
)
from .registry import (
    ALL_PERMISSION_KEYS,
    ALL_PERMISSIONS,
    FIELD_LABELS,
    LEGACY_PERMISSION_EXPANSION,
    LEGACY_PERMISSION_MIGRATION_MARKER,
    MODULES,
    MODULE_BY_KEY,
)
from .utils import (
    backup_if_due,
    create_backup,
    database_status,
    diagnostics_snapshot,
    disk_status,
    get_config,
    network_backup_dir,
    pilot_trial_state,
    publish_server_address,
    server_urls,
    set_config,
)

try:
    from pywebpush import WebPushException, webpush
except Exception:  # pragma: no cover - dependência opcional em ambiente local antigo
    WebPushException = Exception
    webpush = None


def normalize(value):
    text = unicodedata.normalize("NFD", str(value or "").strip().lower())
    text = "".join(ch for ch in text if unicodedata.category(ch) != "Mn")
    return "".join(ch if ch.isalnum() else "_" for ch in text).strip("_")


FEATURE_FATURAMENTO_CONFIG = "feature_faturamento_expedicao"
FEATURE_COLABORADORES_SETOR_DETALHADO_CONFIG = "feature_colaboradores_setor_detalhado"
REQUIRE_LOAD_VALUE_ON_CONFIRM_CONFIG = "rule_obrigar_valor_ao_confirmar_carregamento"
CHECKLIST_INTERCALACAO_MANUAL_CONFIG = "rule_checklist_intercalacao_manual"
OPTIONAL_FEATURES = {
    "faturamento_expedicao": {
        "config": FEATURE_FATURAMENTO_CONFIG,
        "title": "Faturamento da Expedição",
        "description": "Habilita acompanhamento de NF, erro fiscal, liberação Ronilo/lacre e alertas ligados ao faturamento.",
        "permissions": {
            "faturamento_expedicao",
            "registrar_erro_fiscal",
            "liberar_carga_faturada",
        },
        "modules": {"faturamento_expedicao"},
    },
    "colaboradores_setor_detalhado": {
        "config": FEATURE_COLABORADORES_SETOR_DETALHADO_CONFIG,
        "title": "Setores detalhados dos colaboradores",
        "description": "Habilita divisão por rua, setor interno ou área específica para cálculo de gargalo no 801/806.",
        "permissions": {"setores_detalhados_colaboradores"},
        "modules": set(),
    },
}

SYSTEM_RULES = {
    "suspender_uso_operacional": {
        "config": "sistema_bloqueado",
        "title": "Suspender uso operacional",
        "description": "Quando ligado, somente usuários master acessam o sistema. Use para pausar o piloto sem apagar dados nem alterar permissões.",
        "default": True,
    },
    "obrigar_valor_confirmar_carregamento": {
        "config": REQUIRE_LOAD_VALUE_ON_CONFIRM_CONFIG,
        "title": "Obrigar valor ao confirmar carregamento",
        "description": "Quando ligado, a Expedição só confirma uma carga carregada depois de informar o valor faturado para a Frota acompanhar.",
    },
    "checklist_intercalacao_manual": {
        "config": CHECKLIST_INTERCALACAO_MANUAL_CONFIG,
        "title": "Intercalacao manual no check-list",
        "description": "Quando ligado, o motorista informa passagem entre CDs no check-list. Desligado, a intercalacao fica implicita pela carga montada com CD 801 e CD 806.",
    },
}

_OPTIONAL_FEATURE_STATE_CACHE = {"expires_at": 0.0, "permissions": set(), "modules": set()}
_ACTIVE_PERMISSION_KEYS_CACHE = {"expires_at": 0.0, "value": None}
_FLOATING_ALERTS_CACHE = {}
_RUNTIME_VIEW_CACHE = {}
OPTIONAL_FEATURE_CACHE_SECONDS = 15


def cached_runtime_value(key, ttl_seconds, builder):
    now = time.monotonic()
    cached = _RUNTIME_VIEW_CACHE.get(key)
    if cached and now < cached.get("expires_at", 0):
        return cached["value"]
    value = builder()
    if len(_RUNTIME_VIEW_CACHE) > 300:
        for cache_key, item in list(_RUNTIME_VIEW_CACHE.items()):
            if now >= item.get("expires_at", 0):
                _RUNTIME_VIEW_CACHE.pop(cache_key, None)
    _RUNTIME_VIEW_CACHE[key] = {"value": value, "expires_at": now + ttl_seconds}
    return value


def clear_runtime_permission_cache():
    _OPTIONAL_FEATURE_STATE_CACHE.update({"expires_at": 0.0, "permissions": set(), "modules": set()})
    _ACTIVE_PERMISSION_KEYS_CACHE.update({"expires_at": 0.0, "value": None})


def feature_enabled(feature_key):
    feature = OPTIONAL_FEATURES.get(feature_key)
    if not feature:
        return False
    return str(get_config(feature["config"], "nao")).lower() in {"sim", "true", "1", "on"}


def system_rule_enabled(rule_key):
    rule = SYSTEM_RULES.get(rule_key)
    if not rule:
        return False
    default = "sim" if rule.get("default") else "nao"
    return str(get_config(rule["config"], default)).lower() in {"sim", "true", "1", "on"}


def optional_feature_state():
    now = time.monotonic()
    if now < _OPTIONAL_FEATURE_STATE_CACHE["expires_at"]:
        return _OPTIONAL_FEATURE_STATE_CACHE
    disabled_permissions = set()
    disabled_modules = set()
    for key, feature in OPTIONAL_FEATURES.items():
        if not feature_enabled(key):
            disabled_permissions.update(feature["permissions"])
            disabled_modules.update(feature["modules"])
    _OPTIONAL_FEATURE_STATE_CACHE.update(
        {
            "expires_at": now + OPTIONAL_FEATURE_CACHE_SECONDS,
            "permissions": disabled_permissions,
            "modules": disabled_modules,
        }
    )
    return _OPTIONAL_FEATURE_STATE_CACHE


def disabled_optional_permissions():
    return set(optional_feature_state()["permissions"])


def disabled_optional_modules():
    return set(optional_feature_state()["modules"])


def active_permission_keys():
    now = time.monotonic()
    cached = _ACTIVE_PERMISSION_KEYS_CACHE["value"]
    if cached is not None and now < _ACTIVE_PERMISSION_KEYS_CACHE["expires_at"]:
        return list(cached)
    disabled = disabled_optional_permissions()
    keys = [key for key in ALL_PERMISSION_KEYS if key in PILOT_PERMISSIONS and key not in disabled]
    _ACTIVE_PERMISSION_KEYS_CACHE.update({"expires_at": now + OPTIONAL_FEATURE_CACHE_SECONDS, "value": tuple(keys)})
    return keys


def active_permissions():
    active = set(active_permission_keys())
    return [(key, label) for key, label in ALL_PERMISSIONS if key in active]


def permission_item(key, label):
    title = label
    description = ""
    if ": " in label:
        title, description = label.split(": ", 1)
    return {
        "key": key,
        "label": title,
        "description": description,
    }


PERMISSION_PRESETS = [
    {
        "title": "Gerencial",
        "description": "Para acompanhar CD, Frota, Expedição e férias sem operar lançamentos.",
        "permissions": [
            "painel",
            "painel_gestao",
            "consultar_registros",
            "ver_graficos_resumos",
            "relatorio_paletes_cd",
            "expedicao",
            "expedicao_planejamento",
            "visualizar_cds_unificados",
            "receber_alertas_expedicao",
            "alertas_frota",
            "alertas_ferias",
        ],
    },
    {
        "title": "Frota",
        "description": "Para responder pedidos, enviar caminhões, acompanhar motoristas e manter placas.",
        "permissions": [
            "painel",
            "painel_frota",
            "checklist_frota",
            "veiculos_frota",
            "escala_veiculos_frota",
            "solicitacao_caminhoes",
            "tratar_solicitacao_caminhoes",
            "acompanhar_lojas_prontas_carregamento",
            "vincular_cargas_expedicao",
            "aprovacoes_carregamento",
            "aprovar_lancamento_manual_expedicao",
            "encerrar_retorno_frota",
            "consultar_registros",
            "criar_registros",
            "editar_registros",
            "notificar_solicitacao_caminhoes",
            "notificar_motorista_vinculado",
            "notificar_checklist_motorista",
            "alertas_frota",
        ],
    },
    {
        "title": "Expedição",
        "description": "Para informar loja pronta, saldo de pallets, baixar carga e consultar resumo.",
        "permissions": [
            "painel",
            "lojas_prontas_carregamento",
            "expedicao_planejamento",
            "relatorio_paletes_cd",
            "expedicao",
            "solicitar_lancamento_manual_expedicao",
            "consultar_registros",
            "criar_registros",
            "editar_registros",
            "corrigir_saldo_paletes",
            "notificar_loja_pronta_carregamento",
            "notificar_saldo_paletes",
            "receber_alertas_expedicao",
        ],
    },
    {
        "title": "Faturamento",
        "description": "Para registrar carga BlueSoft, valor faturado e liberação fiscal.",
        "permissions": [
            "painel",
            "faturamento_expedicao",
            "visualizar_valor_faturamento",
            "lojas_prontas_carregamento",
            "relatorio_paletes_cd",
            "consultar_registros",
            "criar_registros",
            "editar_registros",
            "registrar_erro_fiscal",
            "liberar_carga_faturada",
            "notificar_loja_pronta_carregamento",
            "receber_alertas_expedicao",
        ],
    },
    {
        "title": "Pallets da Rede",
        "description": "Para controlar PBR, CHEP, descartavel e PBR 2 por CD, loja e fornecedor.",
        "permissions": [
            "painel",
            "paletes_rede",
            "adicionar_loja_paletes_rede",
            "editar_saldo_paletes_rede",
            "movimentar_paletes_rede",
            "consultar_registros",
            "criar_registros",
            "editar_registros",
            "exportar_dados",
            "relatorios",
        ],
    },
    {
        "title": "Motorista",
        "description": "Para preencher check-list e receber orientação da Frota.",
        "permissions": [
            "painel",
            "checklist_frota",
            "consultar_registros",
            "criar_registros",
            "notificar_motorista_vinculado",
        ],
    },
]


def permission_presets():
    active = set(active_permission_keys())
    presets = []
    for preset in PERMISSION_PRESETS:
        permissions = [key for key in preset["permissions"] if key in active]
        if permissions:
            presets.append({**preset, "permissions": permissions})
    return presets


def can_manage_system_features(user):
    profile = ensure_profile(user)
    return bool(getattr(user, "is_superuser", False) or (profile and profile.cargo == "master"))


def can_manage_push(user):
    profile = ensure_profile(user)
    return bool(
        getattr(user, "is_superuser", False)
        or (profile and profile.cargo == "master")
        or user_has_perm(user, "gerenciar_push")
    )


def can_view_billing_value(user):
    if not getattr(user, "is_authenticated", False):
        return False
    profile = ensure_profile(user)
    if getattr(user, "is_superuser", False) or (profile and profile.cargo == "master"):
        return True
    if user_has_perm(user, "visualizar_valor_faturamento"):
        return True
    if profile and profile.cargo in {
        "supervisor",
        "supervisor_frota",
        "supervisor_conferencia_expedicao",
        "gerente",
    }:
        return True
    identity = normalize(" ".join(filter(None, [user.username, user.get_full_name()])))
    return any(name in identity for name in {"marcela", "wagner", "leilane"})


def ensure_profile(user):
    if not user.is_authenticated:
        return None
    cached_profile = getattr(user, "_krill_profile_cache", None)
    if cached_profile is not None:
        return cached_profile
    active_keys = active_permission_keys()
    profile, _created = PerfilAcesso.objects.get_or_create(
        user=user,
        defaults={
            "cargo": "master" if user.is_superuser else "assistente",
            "permissoes": active_keys if user.is_superuser else ["painel"],
        },
    )
    if user.is_superuser and set(profile.permissoes) != set(active_keys):
        profile.permissoes = active_keys
        profile.cargo = "master"
        profile.save(update_fields=["permissoes", "cargo", "atualizado_em"])
    elif LEGACY_PERMISSION_MIGRATION_MARKER not in profile.permissoes:
        migrated = set(profile.permissoes)
        for legacy_key, new_keys in LEGACY_PERMISSION_EXPANSION.items():
            if legacy_key in migrated:
                migrated.update(new_keys)
        migrated.add(LEGACY_PERMISSION_MIGRATION_MARKER)
        if migrated != set(profile.permissoes):
            profile.permissoes = list(migrated)
            profile.save(update_fields=["permissoes", "atualizado_em"])
    user._krill_profile_cache = profile
    return profile


def user_permission_set(user):
    if not getattr(user, "is_authenticated", False):
        return set()
    cached = getattr(user, "_krill_permission_set_cache", None)
    if cached is not None:
        return cached
    active = set(active_permission_keys())
    if getattr(user, "is_superuser", False):
        perms = active
    else:
        profile = ensure_profile(user)
        perms = set(profile.permissoes or []) if profile else set()
        if profile and profile.cargo == "master":
            perms = active
    user._krill_permission_set_cache = perms
    return perms


ROLE_SECTOR_SCOPES = {
    "supervisor_recebimento": {"recebimento", "operacao_compartilhada"},
    "supervisor_separacao": {"separacao", "ressuprimento", "operacao_compartilhada"},
    "supervisor_conferencia_expedicao": {"conferencia", "expedicao", "operacao_compartilhada"},
    "supervisor_frota": {"frota", "motorista", "operacao_compartilhada"},
    "lider_separacao": {"separacao", "ressuprimento", "operacao_compartilhada"},
    "lider_conferencia_expedicao": {"conferencia", "expedicao", "operacao_compartilhada"},
    "lider_frota": {"frota", "motorista", "operacao_compartilhada"},
}


def managed_sectors_for_user(user):
    """Return None for unrestricted roles or the sectors managed by a specific leader."""
    profile = ensure_profile(user)
    if not profile:
        return set()
    return ROLE_SECTOR_SCOPES.get(profile.cargo)


def can_manage_improvements(user):
    profile = ensure_profile(user)
    return bool(
        user.is_superuser
        or (profile and profile.cargo == "master")
        or user_has_perm(user, "gerenciar_melhorias_sistema")
    )


def user_has_perm(user, perm):
    if perm not in active_permission_keys():
        return False
    if user.is_superuser:
        return True
    profile = ensure_profile(user)
    if profile and profile.cargo == "master":
        return True
    return bool(profile and perm in user_permission_set(user))


def can_manage_access(user):
    return user_has_perm(user, "gestao_acesso") or user_has_perm(user, "gestao_acesso_total")


def generate_temp_password():
    return f"{secrets.randbelow(900000) + 100000}"


SECURITY_AUDIT_ACTIONS = [
    "usuario_criado",
    "usuario_atualizado",
    "login_pausado",
    "login_reativado",
    "senha_redefinida",
    "senha_redefinida_solicitacao",
    "senha_redefinida_pelo_master",
    "perfil_atualizado",
    "senha_alterada_pelo_usuario",
    "nome_perfil_alterado",
]


INTERFACE_LABELS_CONFIG = "rotulos_interface"

DEFAULT_PANEL_LABELS = OrderedDict(
    [
        ("dashboard", "Início"),
        ("painel_gestao", "Painel Gerencial"),
        ("sentinela_servidor", "Sentinela do servidor"),
        ("auditoria", "Painel Master"),
        ("relatorios", "Relatórios"),
        ("painel_frota", "Painel da Frota"),
        ("importar", "Importar planilha"),
        ("corrigir_cd", "Corrigir CD"),
        ("preferencias", "Minha tela"),
        ("usuarios", "Gestão de acesso"),
        ("personalizar_interface", "Personalizar nomes"),
        ("recursos_sistema", "Recursos do sistema"),
        ("apresentacao_sistema", "Apresentação do sistema"),
        ("treinamento_sistema", "Treinamento rápido"),
        ("notificacoes_push", "Notificações Push"),
    ]
)

DEFAULT_PANEL_ICONS = {
    "dashboard": "D",
    "painel_gestao": "PG",
    "sentinela_servidor": "SO",
    "auditoria": "M",
    "relatorios": "R",
    "painel_frota": "PF",
    "importar": "I",
    "corrigir_cd": "CD",
    "preferencias": "T",
    "usuarios": "A",
    "personalizar_interface": "N",
    "recursos_sistema": "RS",
    "apresentacao_sistema": "AP",
    "treinamento_sistema": "TR",
    "notificacoes_push": "NP",
}

SIMPLIFIED_MODULE_TEXT = {
    "expedicao_planejamento": {
        "title": "Saldo de pallets",
        "icon": "PD",
        "description": "Veja e corrija quantos pallets cada loja ainda tem no CD.",
    },
    "lojas_prontas_carregamento": {
        "title": "Lojas prontas para carregar",
        "icon": "LP",
        "description": "Avise a Frota quando a loja já estiver conferida e liberada.",
    },
    "relatorio_paletes_cd": {
        "title": "Resumo de pallets",
        "icon": "RP",
        "description": "Veja saldo, reservado e expedido sem abrir a lista completa.",
    },
    "expedicao": {
        "title": "Baixar carga expedida",
        "icon": "CE",
        "description": "Confirme o que saiu fisicamente do CD.",
    },
    "solicitar_lancamento_manual_expedicao": {
        "title": "Pedir exceção",
        "icon": "SM",
        "description": "Peça liberação para carregar fora do fluxo normal.",
    },
    "faturamento_expedicao": {
        "title": "Faturamento BlueSoft",
        "icon": "FT",
        "description": "Informe valor faturado, carga BlueSoft e liberação fiscal.",
    },
    "paletes_rede": {
        "title": "Pallets da rede",
        "icon": "PR",
        "description": "Controle PBR, CHEP, descartavel e PBR 2 por CD e loja.",
    },
    "paletes_vasilhames": {
        "title": "Paletes e vasilhames",
        "icon": "PV",
        "description": "Controle entradas, saidas e movimentacoes de apoio.",
    },
    "unitizadores": {
        "title": "Unitizadores",
        "icon": "UT",
        "description": "Acompanhe envio e retorno de unitizadores por loja.",
    },
    "equipamentos": {
        "title": "Equipamentos do CD",
        "icon": "EQ",
        "description": "Cadastre coletores, paleteiras, computadores e empilhadeiras.",
    },
    "manutencao_equipamentos": {
        "title": "Manutenção de equipamentos",
        "icon": "ME",
        "description": "Registre itens enviados para TI, manutenção ou reparo.",
    },
    "checklist_frota_itens": {
        "title": "Regras do check-list",
        "icon": "RC",
        "description": "Configure perguntas, grupos e frequencias do check-list.",
    },
    "veiculos_frota": {
        "title": "Motoristas, placas e status",
        "icon": "MP",
        "description": "Cadastre placas, motoristas, plataforma e disponibilidade.",
    },
    "escala_veiculos_frota": {
        "title": "Escala de motoristas",
        "icon": "EM",
        "description": "Informe troca de motorista por férias, manutenção ou escala.",
    },
    "solicitacao_caminhoes": {
        "title": "Pedido de caminhões",
        "icon": "PC",
        "description": "Solicite caminhões para outro CD com destino e período.",
    },
    "aprovacoes_carregamento": {
        "title": "Exceções da Frota",
        "icon": "AF",
        "description": "Aprove ou recuse exceções de carregamento manual.",
    },
}

EXPEDICAO_HUB_MODULES = {
    "lojas_prontas_carregamento",
    "expedicao_planejamento",
    "relatorio_paletes_cd",
    "expedicao",
    "solicitar_lancamento_manual_expedicao",
    "faturamento_expedicao",
}

OPERACAO_CD_HUB_MODULES = {
    "paletes_rede",
    "paletes_vasilhames",
    "unitizadores",
    "equipamentos",
    "manutencao_equipamentos",
}

BUILTIN_HUB_SPECS = [
    {
        "key": "hub_central_expedicao",
        "title": "Central Expedição",
        "icon": "CE",
        "menu": "Expedição",
        "description": "Lojas prontas, saldo, expedição, relatórios e faturamento em um fluxo só.",
        "cards": [
            "module:lojas_prontas_carregamento",
            "module:expedicao_planejamento",
            "module:relatorio_paletes_cd",
            "module:expedicao",
            "module:solicitar_lancamento_manual_expedicao",
            "module:faturamento_expedicao",
        ],
        "hide_menu_cards": True,
    },
    {
        "key": "hub_operacao_cd",
        "title": "Operação CD",
        "icon": "OC",
        "menu": "Operação CD",
        "description": "Pallets da rede, vasilhames, unitizadores, equipamentos e manutenção.",
        "cards": [
            "module:paletes_rede",
            "module:paletes_vasilhames",
            "module:unitizadores",
            "module:equipamentos",
            "module:manutencao_equipamentos",
        ],
        "hide_menu_cards": True,
    },
]

FROTA_HUB_MODULES = {
    "checklist_frota",
    "checklist_frota_itens",
    "carregamento_veiculos",
    "aprovacoes_carregamento",
    "solicitacao_caminhoes",
    "veiculos_frota",
    "escala_veiculos_frota",
    "materiais_frota",
    "lacres_frota",
}

FROTA_HUB_PERMISSIONS = FROTA_HUB_MODULES | {
    "painel_frota",
    "editar_checklist_frota",
    "encerrar_retorno_frota",
    "exportar_frota",
    "alertas_frota",
    "vincular_cargas_expedicao",
    "solicitar_lancamento_manual_expedicao",
    "aprovar_lancamento_manual_expedicao",
    "lancar_manual_sem_aprovacao",
    "tratar_solicitacao_caminhoes",
    "notificar_solicitacao_caminhoes",
    "acompanhar_lojas_prontas_carregamento",
    "notificar_loja_pronta_carregamento",
}

COLABORADORES_HUB_MODULES = {
    "ferias_colaboradores",
    "ausencias_colaboradores",
    "pessoas_turno",
    "funcoes_turno",
}

COLABORADORES_HUB_PERMISSIONS = COLABORADORES_HUB_MODULES | {
    "colaboradores_hub",
    "alertas_ferias",
    "mapa_calor_ferias",
    "capacidade_operacao",
    "configurar_alertas_ferias",
    "visualizar_gargalos_colaboradores",
    "gerenciar_ferias_colaboradores",
    "notificacoes_ferias_colaboradores",
    "setores_detalhados_colaboradores",
}

# Fusão definitiva: todos os módulos do registry entram no sistema único
# (Gestão CD + aliases Assistente Krill). Antes o piloto restringia o menu.
PILOT_MODULES = {module.key for module in MODULES}

PILOT_PERMISSIONS = PILOT_MODULES | {
    "painel",
    "painel_gestao",
    "painel_master",
    "relatorios",
    "auditoria_acessos",
    "preferencias",
    "gestao_acesso",
    "gestao_acesso_total",
    "personalizar_interface",
    "modo_demonstracao",
    "alertas_frota",
    "alertas_ferias",
    "notificacoes_ferias_colaboradores",
    "setores_detalhados_colaboradores",
    "mapa_calor_ferias",
    "capacidade_operacao",
    "configurar_alertas_ferias",
    "visualizar_gargalos_colaboradores",
    "gerenciar_ferias_colaboradores",
    "saude_equipamentos",
    "criar_registros",
    "consultar_registros",
    "ver_graficos_resumos",
    "painel_frota",
    "visualizar_cds_unificados",
    "vincular_cargas_expedicao",
    "aprovar_lancamento_manual_expedicao",
    "lancar_manual_sem_aprovacao",
    "tratar_solicitacao_caminhoes",
    "notificar_solicitacao_caminhoes",
    "acompanhar_lojas_prontas_carregamento",
    "notificar_loja_pronta_carregamento",
    "corrigir_saldo_paletes",
    "registrar_erro_fiscal",
    "liberar_carga_faturada",
    "visualizar_valor_faturamento",
    "adicionar_loja_paletes_rede",
    "editar_saldo_paletes_rede",
    "movimentar_paletes_rede",
    "notificar_carga_vinculada",
    "notificar_alteracao_carga",
    "notificar_saldo_paletes",
    "notificar_motorista_vinculado",
    "notificar_checklist_motorista",
    "receber_alertas_expedicao",
    "gerenciar_push",
    "editar_checklist_frota",
    "encerrar_retorno_frota",
    "exportar_frota",
    "trocar_cd",
    "editar_registros",
    "excluir_registros",
    "exportar_dados",
    "sentinela_servidor",
    "tms_expedicao",
    "tms_krill",
}


DEFAULT_NAV_GROUPS = OrderedDict(
    [
        ("Colaboradores", "Colaboradores"),
        ("Operação CD", "Operação CD"),
        ("Recursos e perdas", "Recursos e perdas"),
        ("Frota", "Frota"),
        ("Expedição", "Expedição"),
        ("Motorista", "Motorista"),
    ]
)

MODULE_NAV_GROUPS = {
    "pendencias": "Rotina do dia",
    "recebimento_agenda": "Rotina do dia",
    "recebimentos": "Rotina do dia",
    "prestadores": "Rotina do dia",
    "colaboradores_hub": "Colaboradores",
    "pessoas_turno": "Colaboradores",
    "funcoes_turno": "Colaboradores",
    "ferias_colaboradores": "Colaboradores",
    "ausencias_colaboradores": "Colaboradores",
    "recebimento_conferentes": "Operação CD",
    "conferencias": "Operação CD",
    "unitizadores": "Operação CD",
    "paletes_vasilhames": "Operação CD",
    "paletes_rede": "Operação CD",
    "separacao": "Operação CD",
    "ressuprimento": "Operação CD",
    "ressuprimento_painel": "Operação CD",
    "expedicao_planejamento": "Expedição",
    "expedicao": "Expedição",
    "solicitar_lancamento_manual_expedicao": "Expedição",
    "relatorio_paletes_cd": "Expedição",
    "equipamentos": "Recursos e perdas",
    "manutencao_equipamentos": "Recursos e perdas",
    "materiais": "Recursos e perdas",
    "avarias": "Recursos e perdas",
    "chamados_saldo": "Recursos e perdas",
    "checklist_frota": "Frota",
    "checklist_frota_itens": "Frota",
    "veiculos_frota": "Frota",
    "escala_veiculos_frota": "Frota",
    "aprovacoes_carregamento": "Frota",
    "solicitacao_caminhoes": "Frota",
    "materiais_frota": "Frota",
    "lacres_frota": "Frota",
    "ocorrencias": "Gestão e base",
    "aprovacoes": "Gestão e base",
    "fechamentos": "Gestão e base",
    "melhorias_sistema": "Gestão e base",
    "lojas": "Gestão e base",
    "gtins": "Gestão e base",
    "parametros_cd": "Gestão e base",
}


_INTERFACE_LABELS_CACHE = {"value": None, "expires_at": 0.0}
INTERFACE_LABELS_CACHE_SECONDS = 5


def clear_interface_labels_cache():
    _INTERFACE_LABELS_CACHE["value"] = None
    _INTERFACE_LABELS_CACHE["expires_at"] = 0.0


def interface_labels():
    now = time.monotonic()
    cached = _INTERFACE_LABELS_CACHE["value"]
    if cached is not None and now < _INTERFACE_LABELS_CACHE["expires_at"]:
        return cached
    try:
        payload = json.loads(get_config(INTERFACE_LABELS_CONFIG, "{}") or "{}")
    except (TypeError, ValueError):
        payload = {}
    labels = {
        "modules": payload.get("modules", {}) if isinstance(payload.get("modules", {}), dict) else {},
        "panels": payload.get("panels", {}) if isinstance(payload.get("panels", {}), dict) else {},
        "menus": payload.get("menus", {}) if isinstance(payload.get("menus", {}), dict) else {},
        "module_menus": payload.get("module_menus", {}) if isinstance(payload.get("module_menus", {}), dict) else {},
        "icons": payload.get("icons", {}) if isinstance(payload.get("icons", {}), dict) else {},
        "panel_icons": payload.get("panel_icons", {}) if isinstance(payload.get("panel_icons", {}), dict) else {},
        "hubs": payload.get("hubs", []) if isinstance(payload.get("hubs", []), list) else [],
    }
    _INTERFACE_LABELS_CACHE["value"] = labels
    _INTERFACE_LABELS_CACHE["expires_at"] = now + INTERFACE_LABELS_CACHE_SECONDS
    return labels


def panel_label(key, default=None):
    labels = interface_labels()["panels"]
    if key == "dashboard" and labels.get(key) == "Painel do dia":
        return "Início"
    return labels.get(key) or default or DEFAULT_PANEL_LABELS.get(key, key)


def clean_interface_icon(value, default):
    cleaned = "".join(char for char in (value or "").strip().upper() if char.isalnum() or char == "!")[:3]
    return cleaned or default


def panel_icon(key, default=None):
    fallback = default or DEFAULT_PANEL_ICONS.get(key, key[:2].upper())
    return clean_interface_icon(interface_labels()["panel_icons"].get(key), fallback)


def customize_module(module):
    labels = interface_labels()
    custom_title = labels["modules"].get(module.key)
    custom_icon = labels["icons"].get(module.key)
    if module.key == "melhorias_sistema" and custom_title == "Check-up do Sistema":
        custom_title = None
    if module.key == "melhorias_sistema" and custom_icon == "CS":
        custom_icon = None
    simplified = SIMPLIFIED_MODULE_TEXT.get(module.key, {})
    title = custom_title or simplified.get("title") or module.title
    icon = clean_interface_icon(custom_icon, simplified.get("icon") or module.icon)
    description = simplified.get("description") or module.description
    if title == module.title and icon == module.icon and description == module.description:
        return module
    return replace(module, title=title, icon=icon, description=description)


def nav_group_label(group):
    return interface_labels()["menus"].get(group) or DEFAULT_NAV_GROUPS.get(group, group)


def interface_menu_key(label):
    text = unicodedata.normalize("NFKD", str(label or "")).encode("ascii", "ignore").decode("ascii")
    text = re.sub(r"[^a-zA-Z0-9]+", "_", text).strip("_").lower()
    return f"custom_{text[:48]}" if text else ""


def interface_hub_key(label):
    text = unicodedata.normalize("NFKD", str(label or "")).encode("ascii", "ignore").decode("ascii")
    text = re.sub(r"[^a-zA-Z0-9]+", "_", text).strip("_").lower()
    return f"hub_{text[:48]}" if text else ""


def module_nav_group(module, profile=None):
    if profile and profile.cargo == "motorista" and module.key in {"checklist_frota", "lacres_frota"}:
        return "Motorista"
    custom_group = interface_labels()["module_menus"].get(module.key)
    if custom_group:
        return custom_group
    return MODULE_NAV_GROUPS.get(module.key, module.menu)


def fixed_panel_cards(user):
    panels = []
    candidates = [
        ("dashboard", "dashboard", "painel", "Visao inicial conforme permissoes do usuario."),
        ("painel_gestao", "painel_gestao", "painel_gestao", "Indicadores gerenciais e historico de pallets."),
        ("relatorios", "relatorios", "relatorios", "Relatorios e exportacoes disponiveis."),
        ("auditoria", "auditoria", "painel_master", "Auditoria, logs e acompanhamento sensivel."),
        ("usuarios", "usuarios", "gestao_acesso", "Usuarios, perfis e permissoes."),
        ("personalizar_interface", "personalizar_interface", "personalizar_interface", "Nomes, abas e centrais em cards."),
        ("recursos_sistema", "recursos_sistema", "configurar_recursos_sistema", "Recursos opcionais do piloto."),
        ("notificacoes_push", "notificacoes_push", "gerenciar_push", "Configuracao de avisos push."),
    ]
    for key, route, perm, description in candidates:
        if key == "auditoria":
            allowed = user_has_perm(user, "painel_master") or user_has_perm(user, "auditoria_acessos")
        elif key == "recursos_sistema":
            allowed = can_manage_system_features(user)
        elif key == "notificacoes_push":
            allowed = can_manage_push(user)
        else:
            allowed = user_has_perm(user, perm)
        if allowed:
            panels.append(
                {
                    "id": f"panel:{key}",
                    "kind": "panel",
                    "key": key,
                    "title": panel_label(key, DEFAULT_PANEL_LABELS.get(key, key)),
                    "icon": panel_icon(key, DEFAULT_PANEL_ICONS.get(key, key[:2].upper())),
                    "description": description,
                    "url": reverse(route),
                    "menu": "Principal" if key not in {"usuarios", "personalizar_interface", "recursos_sistema", "notificacoes_push"} else "Ferramentas",
                }
            )
    return panels


def hidden_modules_from_custom_hubs(labels=None):
    labels = labels or interface_labels()
    hidden = set()
    for hub in BUILTIN_HUB_SPECS:
        if not hub.get("hide_menu_cards"):
            continue
        for card_id in hub.get("cards", []):
            if isinstance(card_id, str) and card_id.startswith("module:"):
                hidden.add(card_id.split(":", 1)[1])
    for hub in labels["hubs"]:
        if not isinstance(hub, dict) or not hub.get("hide_menu_cards"):
            continue
        for card_id in hub.get("cards", []):
            if isinstance(card_id, str) and card_id.startswith("module:"):
                hidden.add(card_id.split(":", 1)[1])
    return hidden


def available_card_links(user):
    cards = fixed_panel_cards(user)
    for module in visible_modules(user, include_hub_hidden=True):
        cards.append(
            {
                "id": f"module:{module.key}",
                "kind": "module",
                "key": module.key,
                "title": module.title,
                "icon": module.icon,
                "description": module.description,
                "url": reverse("module_list", args=[module.key]),
                "menu": module_nav_group(module, ensure_profile(user)),
            }
        )
    for hub in builtin_hubs_for_user(user):
        cards.append(
            {
                "id": f"panel:{hub['key']}",
                "kind": "panel",
                "key": hub["key"],
                "title": hub["title"],
                "icon": hub["icon"],
                "description": hub["description"],
                "url": reverse("central_personalizada", args=[hub["key"]]),
                "menu": hub["menu"],
            }
        )
    if can_open_frota_hub(user):
        cards.append(
            {
                "id": "panel:frota_hub",
                "kind": "panel",
                "key": "frota_hub",
                "title": "Central da Frota",
                "icon": "CF",
                "description": "Cards de checklists, carregamentos e rotinas da frota.",
                "url": reverse("frota_hub"),
                "menu": "Frota",
            }
        )
    return cards


def hub_from_spec(spec, available):
    selected_cards = [available[item] for item in spec.get("cards", []) if item in available]
    if not selected_cards:
        return None
    return {
        "key": spec["key"],
        "title": spec["title"],
        "icon": spec["icon"],
        "menu": spec["menu"],
        "cards": selected_cards,
        "description": spec["description"],
        "builtin": True,
    }


def hub_card_variant(card, title=None, description=None, icon=None, url=None):
    updated = dict(card)
    if title:
        updated["title"] = title
    if description:
        updated["description"] = description
    if icon:
        updated["icon"] = icon
    if url:
        updated["url"] = url
    return updated


def dynamic_expedition_hub(request, hub):
    if hub.get("key") != "hub_central_expedicao":
        return hub
    cd = current_cd(request)
    cards_by_key = {card.get("key"): card for card in hub.get("cards", [])}

    def pick(key, title=None, description=None, icon=None, query=None):
        card = cards_by_key.get(key)
        if not card:
            return None
        url = card.get("url")
        if query:
            separator = "&" if "?" in url else "?"
            url = f"{url}{separator}{query}"
        return hub_card_variant(card, title=title, description=description, icon=icon, url=url)

    if str(cd) == UNIFIED_CD:
        variants = [
            pick("expedicao_planejamento", "Saldo por CD", "Acompanhe 801 e 806 separados, sem misturar os saldos.", "PD"),
            pick("lojas_prontas_carregamento", "Fila de carregamento", "Veja o que cada CD liberou para a Frota carregar.", "LP", "modo=fila"),
            pick("expedicao", "Confirmar cargas", "Confirme somente cargas já definidas pela Frota para cada CD.", "CE"),
            pick("relatorio_paletes_cd", "Resumo comparativo", "Compare saldo, reservado e expedido por CD e loja.", "RP"),
            pick("faturamento_expedicao", "Faturamento BlueSoft", "Acompanhe valores, carga BlueSoft e liberação fiscal.", "FT"),
            pick("solicitar_lancamento_manual_expedicao", "Pedir exceção", "Solicite aprovação para carregar fora do fluxo normal.", "SM"),
        ]
        description = "Visão 801-806 em blocos: saldo, loja pronta, carregamento, relatório e faturamento sem misturar os CDs."
    elif str(cd) == "801":
        variants = [
            pick("expedicao_planejamento", "Saldo em box", "Informe o saldo finalizado por loja antes de liberar para carregamento.", "PD"),
            pick("lojas_prontas_carregamento", "Informar loja pronta", "Informe a quantidade pronta agora; o sistema abate do saldo do 801.", "LP", "modo=form"),
            pick("expedicao", "Confirmar carregamento", "Baixe a carga quando o caminhão definido pela Frota for carregado.", "CE"),
            pick("relatorio_paletes_cd", "Resumo do 801", "Veja saldo em box, reservado e expedido por loja.", "RP"),
            pick("faturamento_expedicao", "Faturamento BlueSoft", "Use para carga BlueSoft, valor faturado e liberação fiscal.", "FT"),
            pick("solicitar_lancamento_manual_expedicao", "Pedir exceção", "Peça liberação para carregar fora do fluxo normal.", "SM"),
        ]
        description = "Fluxo do CD 801: informar saldo em box, informar loja pronta, deixar a Frota vincular e confirmar o carregamento."
    else:
        variants = [
            pick("lojas_prontas_carregamento", "Informar loja pronta", "Informe a loja já conferida no 806 para alimentar a Frota e o saldo operacional.", "LP", "modo=form"),
            pick("expedicao", "Confirmar carregamento", "Baixe a carga quando o caminhão definido pela Frota for carregado.", "CE"),
            pick("relatorio_paletes_cd", "Resumo do 806", "Veja pallets disponíveis, reservados e expedidos do CD 806.", "RP"),
            pick("faturamento_expedicao", "Faturamento BlueSoft", "Use para carga BlueSoft, valor faturado e liberação fiscal.", "FT"),
            pick("solicitar_lancamento_manual_expedicao", "Pedir exceção", "Peça liberação para carregar fora do fluxo normal.", "SM"),
            pick("expedicao_planejamento", "Ajustar saldo", "Use apenas para correção manual quando o saldo físico não bater.", "PD"),
        ]
        description = "Fluxo do CD 806: informar loja já conferida, enviar para a Frota e acompanhar o carregamento sem depender do saldo manual."

    cards = [card for card in variants if card]
    if not cards:
        return hub
    updated = dict(hub)
    updated["cards"] = cards
    updated["description"] = description
    updated["scope_label"] = cd_scope_label(cd)
    return updated


def builtin_hubs_for_user(user):
    cards = []
    for module in visible_modules(user, include_hub_hidden=True):
        cards.append(
            {
                "id": f"module:{module.key}",
                "kind": "module",
                "key": module.key,
                "title": module.title,
                "icon": module.icon,
                "description": module.description,
                "url": reverse("module_list", args=[module.key]),
                "menu": module_nav_group(module, ensure_profile(user)),
            }
        )
    available = {card["id"]: card for card in cards}
    hubs = []
    for spec in BUILTIN_HUB_SPECS:
        hub = hub_from_spec(spec, available)
        if hub:
            hubs.append(hub)
    return hubs


def custom_hubs_for_user(user):
    labels = interface_labels()
    available = {card["id"]: card for card in available_card_links(user)}
    builtin_keys = {hub["key"] for hub in builtin_hubs_for_user(user)}
    hubs = [hub for hub in builtin_hubs_for_user(user)]
    for hub in labels["hubs"]:
        if not isinstance(hub, dict):
            continue
        key = hub.get("key") or interface_hub_key(hub.get("title"))
        if key in builtin_keys:
            continue
        title = str(hub.get("title") or "").strip()
        if not key or not title:
            continue
        selected_cards = [available[item] for item in hub.get("cards", []) if item in available]
        if not selected_cards:
            continue
        description = str(hub.get("description") or "").strip() or default_hub_description(title, selected_cards)
        hubs.append(
            {
                "key": key,
                "title": title,
                "icon": clean_interface_icon(hub.get("icon"), "CT"),
                "menu": hub.get("menu") or "Principal",
                "cards": selected_cards,
                "description": description,
            }
        )
    return hubs


def custom_hub_groups(user):
    grouped = OrderedDict()
    for hub in custom_hubs_for_user(user):
        grouped.setdefault(nav_group_label(hub["menu"]), []).append(hub)
    return grouped


def default_hub_description(title, cards):
    titles = [card.get("title", "") for card in cards if card.get("title")]
    if titles:
        return f"Central para acessar rapidamente: {', '.join(titles[:3])}."
    return f"Central personalizada para reunir telas e paineis de {title}."


def navigation_groups(user):
    grouped = OrderedDict()
    for group, modules in grouped_modules(user).items():
        grouped.setdefault(group, {"modules": [], "hubs": []})
        grouped[group]["modules"].extend(modules)
    for group, hubs in custom_hub_groups(user).items():
        grouped.setdefault(group, {"modules": [], "hubs": []})
        grouped[group]["hubs"].extend(hubs)
    return grouped


UNIFIED_CD = "801+806"
PILOT_FOCUS_CD = "806"
SHOW_UNIFIED_CD_IN_SELECTOR = True


def normalized_profile_cd(profile):
    cd = str(getattr(profile, "cd_padrao", "") or "806")
    if cd == UNIFIED_CD:
        return UNIFIED_CD
    return "801" if cd == "801" else "806"


def can_view_unified_cd(user):
    if not getattr(user, "is_authenticated", False):
        return False
    profile = ensure_profile(user)
    return bool(profile and normalized_profile_cd(profile) == UNIFIED_CD) or user_has_perm(user, "visualizar_cds_unificados")


def current_cd(request):
    profile = ensure_profile(request.user)
    profile_cd = normalized_profile_cd(profile) if profile else "806"
    if profile_cd == UNIFIED_CD:
        cd = request.session.get("cd_unidade")
        if str(cd) in {"801", "806"} and request.session.get("cd_unidade_manual"):
            return str(cd)
        if str(cd) == UNIFIED_CD:
            return UNIFIED_CD
        request.session["cd_unidade"] = UNIFIED_CD
        request.session["cd_unidade_manual"] = False
        return UNIFIED_CD
    if profile and not user_has_perm(request.user, "trocar_cd"):
        request.session["cd_unidade"] = profile_cd
        return profile_cd
    cd = request.session.get("cd_unidade") or profile_cd
    if str(cd) == UNIFIED_CD and SHOW_UNIFIED_CD_IN_SELECTOR and can_view_unified_cd(request.user):
        return UNIFIED_CD
    return "801" if str(cd) == "801" else "806"


def cd_values(cd):
    if str(cd) == UNIFIED_CD:
        return ["801", "806"]
    return ["801" if str(cd) == "801" else "806"]


def operation_cd_from_post(request, cd, field_name="cd_destino"):
    scope = cd_values(cd)
    if len(scope) == 1:
        return scope[0], ""
    cd_postado = request.POST.get(field_name, "").strip()
    if cd_postado in scope:
        return cd_postado, ""
    return "", "Escolha o CD da operacao antes de salvar."


def cd_queryset(model, cd):
    return model.objects.filter(cd_unidade__in=cd_values(cd))


def cd_scope_label(cd):
    return "CD 801-806" if str(cd) == UNIFIED_CD else f"CD {cd_values(cd)[0]}"


def can_access_module(user, module):
    if module.key in disabled_optional_modules():
        return False
    if module.key == "colaboradores_hub":
        return can_open_colaboradores_hub(user)
    if module.key == "checklist_frota":
        return (
            user_has_perm(user, "checklist_frota")
            or user_has_perm(user, "painel_frota")
            or user_has_perm(user, "editar_checklist_frota")
        )
    if module.key == "checklist_frota_itens":
        return user_has_perm(user, module.perm) or user_has_perm(user, "editar_checklist_frota")
    if module.key == "lojas_prontas_carregamento":
        return (
            user_has_perm(user, module.perm)
            or user_has_perm(user, "acompanhar_lojas_prontas_carregamento")
            or user_has_perm(user, "vincular_cargas_expedicao")
        )
    if module.key == "faturamento_expedicao" and not can_view_billing_value(user):
        return False
    return user_has_perm(user, module.perm)


def can_create_records(user, module, profile=None):
    if module.key == "checklist_frota":
        return user_has_perm(user, "checklist_frota")
    if module.key == "checklist_frota_itens":
        return user_has_perm(user, "criar_registros") or user_has_perm(user, "editar_checklist_frota")
    return user_has_perm(user, "criar_registros")


def can_consult_records(user, module):
    if module.key == "checklist_frota":
        return (
            user_has_perm(user, "painel_frota")
            or user_has_perm(user, "consultar_registros")
            or user_has_perm(user, "editar_checklist_frota")
        )
    if module.key == "checklist_frota_itens":
        return user_has_perm(user, "consultar_registros") or user_has_perm(user, "editar_checklist_frota")
    return user_has_perm(user, "consultar_registros")


def can_view_module_charts(user, module):
    if module.key == "checklist_frota":
        return user_has_perm(user, "painel_frota") or user_has_perm(user, "ver_graficos_resumos")
    return user_has_perm(user, "ver_graficos_resumos")


def can_export_module(user, module):
    if module.key in {"checklist_frota", "materiais_frota", "lacres_frota"}:
        return user_has_perm(user, "exportar_frota") or user_has_perm(user, "exportar_dados")
    return user_has_perm(user, "exportar_dados")


def visible_modules(user, include_hub_hidden=False):
    profile = ensure_profile(user)
    hidden = set(profile.abas_ocultas if profile else [])
    # Produtividade Separação volta ao menu (alias Krill: produtividade_cd)
    hidden_from_menu = set()
    if not include_hub_hidden:
        hidden_from_menu.update(hidden_modules_from_custom_hubs())
    return [
        customize_module(module)
        for module in MODULES
        if module.key in PILOT_MODULES
        and can_access_module(user, module)
        and module.key not in hidden
        and module.key not in hidden_from_menu
    ]


def grouped_modules(user):
    grouped = OrderedDict((nav_group_label(group), []) for group in DEFAULT_NAV_GROUPS)
    profile = ensure_profile(user)
    for module in visible_modules(user):
        if profile and profile.cargo != "motorista" and module.key in FROTA_HUB_MODULES:
            continue
        if module.key in COLABORADORES_HUB_MODULES and can_open_colaboradores_hub(user):
            continue
        menu = nav_group_label(module_nav_group(module, profile))
        grouped.setdefault(menu, []).append(module)
    return OrderedDict((menu, modules) for menu, modules in grouped.items() if modules)


def can_open_frota_hub(user):
    if not getattr(user, "is_authenticated", False):
        return False
    profile = ensure_profile(user)
    if profile and profile.cargo == "motorista":
        return False
    return any(user_has_perm(user, perm) for perm in FROTA_HUB_PERMISSIONS)


def can_open_colaboradores_hub(user):
    if not getattr(user, "is_authenticated", False):
        return False
    profile = ensure_profile(user)
    if profile and profile.cargo == "motorista":
        return False
    return any(user_has_perm(user, perm) for perm in COLABORADORES_HUB_PERMISSIONS)


def url_with_query(url, params):
    query = urlencode({key: value for key, value in params.items() if value not in (None, "")})
    if not query:
        return url
    base_url, fragment = urldefrag(url)
    separator = "&" if "?" in base_url else "?"
    rebuilt = f"{base_url}{separator}{query}"
    return f"{rebuilt}#{fragment}" if fragment else rebuilt


def module_key_alias(key):
    aliases = {
        "relatorio_paletes": "relatorio_paletes_cd",
        "painel_frota": "checklist_frota",
        "paletes_cd": "expedicao_planejamento",
        "pallets_cd": "expedicao_planejamento",
        "pallets_por_cd": "expedicao_planejamento",
        "carregamento-veiculos": "carregamento_veiculos",
        "carregamento_veiculo": "carregamento_veiculos",
        "carregar_veiculo": "carregamento_veiculos",
        "carregar-veiculo": "carregamento_veiculos",
        "saldo_expedicao": "expedicao",
        # Aliases Assistente Krill → Gestão CD (mesmo banco, sem rotas duplicadas)
        "produtividade_cd": "separacao",
        "painel_estacao": "ressuprimento_painel",
        "separacao_controle": "separacao",
        "regras_separacao": "separacao",
        "avaria_triagem": "avarias",
        "expedicao_controle": "expedicao",
        "historico_saldos_paletes": "relatorio_paletes_cd",
        "veiculos_disponibilidade": "veiculos_frota",
        "disponibilidade_veiculos": "veiculos_frota",
        "informar_loja_pronta": "lojas_prontas_carregamento",
        "loja_pronta": "lojas_prontas_carregamento",
    }
    return aliases.get(key, key)


MODULE_SCREEN_TEMPLATES = {
    "lojas_prontas_carregamento": "painel/lojas_prontas.html",
    "expedicao_planejamento": "painel/expedicao_planejamento.html",
    "carregamento_veiculos": "painel/expedicao_planejamento.html",
    "relatorio_paletes_cd": "painel/relatorio_paletes_cd.html",
    "expedicao": "painel/expedicao.html",
    "solicitar_lancamento_manual_expedicao": "painel/expedicao_manual.html",
    "paletes_rede": "painel/paletes_rede.html",
}


@login_required
def module_legacy_query_redirect(request, key, legacy_query=""):
    key = module_key_alias(key)
    if key == "painel_gerencial":
        return redirect("painel_gestao")
    raw_query = legacy_query or ""
    if raw_query and not raw_query.startswith("data="):
        raw_query = f"data={raw_query}"
    params = dict(parse_qsl(raw_query, keep_blank_values=True))
    params.update(dict(parse_qsl(request.META.get("QUERY_STRING", ""), keep_blank_values=True)))
    return redirect(url_with_query(reverse("module_list", kwargs={"key": key}), params))


def frota_hub_link(key, title, description, icon, url, user, perms=(), highlight=None):
    allowed = not perms or any(user_has_perm(user, perm) for perm in perms)
    if not allowed:
        return None
    return {
        "key": key,
        "title": title,
        "description": description,
        "icon": icon,
        "url": url,
        "highlight": highlight or "",
    }


def module_anchor_url(key, anchor):
    return f"{reverse('module_list', args=[key])}#{anchor}"


def frota_hub_load_groups(cd, data_obj, can_unify=False):
    cds = ["801", "806"] if can_unify else [cd]
    vinculos = (
        ExpedicaoVinculo.objects.filter(cd_unidade__in=cds, data=data_obj, status="vinculado")
        .order_by("periodo", "motorista", "placa", "loja", "cd_unidade")
    )
    groups = OrderedDict()
    for item in vinculos:
        key = (item.periodo, normalize(item.motorista), normalize(item.placa))
        group = groups.setdefault(
            key,
            {
                "periodo": item.get_periodo_display(),
                "motorista": item.motorista or "Sem motorista",
                "placa": item.placa or "Sem placa",
                "total": 0,
                "lojas": [],
                "cds": {"801": 0, "806": 0},
            },
        )
        group["total"] += item.qtd_paletes
        if item.cd_unidade in group["cds"]:
            group["cds"][item.cd_unidade] += item.qtd_paletes
        group["lojas"].append(f"{item.loja} · CD {item.cd_unidade} · {item.qtd_paletes}")
    return list(groups.values())[:8]


def frota_open_departures_queryset(cd, unificado=False):
    queryset = ChecklistFrota.objects.all() if unificado else cd_queryset(ChecklistFrota, cd)
    return (
        queryset.filter(
            tipo_checklist="saida",
            acompanhar_retorno=True,
            retorno_registrado__isnull=True,
            retorno_dispensado=False,
        )
        .select_related("criado_por", "retorno_dispensado_por")
        .order_by("data", "horario_saida", "id")
    )


def matching_open_departure(cd, placa="", motorista="", unificado=False):
    queryset = frota_open_departures_queryset(cd, unificado=unificado)
    placa = (placa or "").strip()
    motorista = (motorista or "").strip()
    if placa:
        queryset = queryset.filter(placa__iexact=placa)
    if motorista:
        queryset = queryset.filter(motorista__iexact=motorista)
    return queryset.first()


def frota_open_trip_data(cd, unificado=False, limit=20, driver_user=None):
    queryset = frota_open_departures_queryset(cd, unificado=unificado)
    now = timezone.now()
    if driver_user is not None:
        scoped_rows = [row for row in queryset[:200] if driver_can_handle_checklist_record(driver_user, row, row.data)]
        total = len(scoped_rows)
        rows = scoped_rows[:limit]
    else:
        total = queryset.count()
        rows = list(queryset[:limit])
    for row in rows:
        started_at = row.criado_em
        if row.data and row.horario_saida:
            started_at = timezone.make_aware(
                datetime.combine(row.data, row.horario_saida),
                timezone.get_current_timezone(),
            )
        elapsed_hours = max(0, (now - started_at).total_seconds() / 3600)
        row.horas_em_aberto = round(elapsed_hours, 1)
        row.tempo_em_aberto = "menos de 1h" if elapsed_hours < 1 else f"{int(elapsed_hours)}h"
        row.nivel_retorno = "alta" if elapsed_hours >= 12 or row.data < timezone.localdate() else "media" if elapsed_hours >= 8 else "normal"
    return {"total": total, "rows": rows}


def frota_closed_return_history(cd, unificado=False, limit=20):
    queryset = ChecklistFrota.objects.all() if unificado else cd_queryset(ChecklistFrota, cd)
    rows = list(
        queryset.filter(
            tipo_checklist="saida",
            acompanhar_retorno=True,
            retorno_registrado__isnull=True,
            retorno_dispensado=True,
        )
        .select_related("retorno_dispensado_por")
        .order_by("-retorno_dispensado_em", "-atualizado_em", "-id")[:limit]
    )
    driver_counts = {}
    for row in (
        queryset.filter(
            tipo_checklist="saida",
            acompanhar_retorno=True,
            retorno_registrado__isnull=True,
            retorno_dispensado=True,
        )
        .values("motorista")
        .annotate(total=Count("id"))
    ):
        driver_counts[normalize(row["motorista"])] = row["total"]
    for row in rows:
        row.reincidencia_motorista = driver_counts.get(normalize(row.motorista), 0)
    return {"total": len(rows), "rows": rows}


def floating_frota_alerts(cd):
    hoje = timezone.localdate()
    checklists = cd_queryset(ChecklistFrota, cd).filter(data=hoje)
    summary = checklists.aggregate(
        total=Count("id"),
        saidas=Count("id", filter=Q(tipo_checklist="saida")),
        retornos=Count("id", filter=Q(tipo_checklist="retorno")),
        manutencoes=Count("id", filter=Q(necessita_manutencao=True)),
    )
    total_checklists = summary["total"] or 0
    saidas = summary["saidas"] or 0
    retornos = summary["retornos"] or 0
    manutencoes = summary["manutencoes"] or 0
    intercalacoes = (
        cd_queryset(ChecklistFrota, cd).filter(intercala_cd=True, intercalacao_concluida=False).count()
        if system_rule_enabled("checklist_intercalacao_manual")
        else 0
    )
    retornos_pendentes = frota_open_trip_data(cd, limit=5)
    carregamentos_pendentes = cd_queryset(SolicitacaoCarregamentoManual, cd).filter(status="pendente").count()
    items = []
    if total_checklists:
        items.append({"nivel": "normal", "texto": f"{total_checklists} check-list(s) lançado(s) hoje: {saidas} saída(s) e {retornos} retorno(s)."})
    for row in retornos_pendentes["rows"]:
        destino = f" para {row.loja_destino}" if row.loja_destino else ""
        items.append(
            {
                "nivel": row.nivel_retorno,
                "texto": f"Retorno pendente: {row.motorista}, placa {row.placa}{destino}, em aberto há {row.tempo_em_aberto}.",
            }
        )
    if retornos_pendentes["total"] > len(retornos_pendentes["rows"]):
        items.append({"nivel": "alta", "texto": f"Mais {retornos_pendentes['total'] - len(retornos_pendentes['rows'])} retorno(s) pendente(s) no Painel da Frota."})
    if manutencoes:
        items.append({"nivel": "alta", "texto": f"{manutencoes} check-list(s) indicando necessidade de manutenção."})
    if intercalacoes:
        items.append({"nivel": "alta", "texto": f"{intercalacoes} rota(s) com intercalação pendente."})
    if carregamentos_pendentes:
        items.append({"nivel": "alta", "texto": f"{carregamentos_pendentes} solicitação(ões) de carregamento aguardando aprovação da Frota."})
    return {
        "titulo": "Frota",
        "subtitulo": "Check-list e carregamentos",
        "url": reverse("frota_hub"),
        "count": retornos_pendentes["total"] + manutencoes + intercalacoes + carregamentos_pendentes,
        "items": items,
    }


def floating_ferias_alerts(cd):
    rows = ferias_alertas(cd)
    items = [
        {
            "nivel": "alta" if row.status in {"em_ferias", "retornando"} else "media",
            "texto": f"{row.colaborador}: {row.get_status_display()} de {row.inicio_ferias:%d/%m/%Y} até {row.fim_ferias:%d/%m/%Y}. Retorno {row.retorno_previsto:%d/%m/%Y}.",
        }
        for row in rows
    ]
    if not items:
        items.append({"nivel": "normal", "texto": "Nenhum alerta de férias para os próximos dias."})
    return {
        "titulo": "Férias",
        "subtitulo": "Colaboradores e retorno",
        "url": reverse("module_list", args=["ferias_colaboradores"]),
        "count": len(rows),
        "items": items,
    }


def floating_alerts_context(user, cd):
    if not getattr(user, "is_authenticated", False):
        return {"enabled": False, "count": 0, "groups": []}
    cache_key = (
        getattr(user, "pk", None),
        str(cd),
        timezone.localdate().isoformat(),
        bool(user_has_perm(user, "alertas_frota")),
        bool(user_has_perm(user, "alertas_ferias")),
    )
    now = time.monotonic()
    cached = _FLOATING_ALERTS_CACHE.get(cache_key)
    if cached and now < cached.get("expires_at", 0):
        return cached["value"]
    groups = []
    if cache_key[3]:
        groups.append(floating_frota_alerts(cd))
    if cache_key[4]:
        groups.append(floating_ferias_alerts(cd))
    groups = [group for group in groups if group["count"] > 0]
    value = {
        "enabled": bool(groups),
        "count": sum(group["count"] for group in groups),
        "groups": groups,
    }
    if len(_FLOATING_ALERTS_CACHE) > 200:
        for key, item in list(_FLOATING_ALERTS_CACHE.items()):
            if now >= item.get("expires_at", 0):
                _FLOATING_ALERTS_CACHE.pop(key, None)
    _FLOATING_ALERTS_CACHE[cache_key] = {"value": value, "expires_at": now + 20}
    return value


WEBPUSH_KEY_VERSION = "2026-07-12-v4-pem"
_WEBPUSH_KEY_CACHE = {"version": "", "public": "", "private": ""}


def clear_webpush_key_cache():
    _WEBPUSH_KEY_CACHE.update({"version": "", "public": "", "private": ""})


def webpush_key_pair():
    if (
        _WEBPUSH_KEY_CACHE["version"] == WEBPUSH_KEY_VERSION
        and _WEBPUSH_KEY_CACHE["public"]
        and _WEBPUSH_KEY_CACHE["private"]
    ):
        return _WEBPUSH_KEY_CACHE["public"], _WEBPUSH_KEY_CACHE["private"]

    config_version = get_config("webpush_vapid_version", "").strip()
    public_key = get_config("webpush_vapid_public_key", "").strip()
    private_key = get_config("webpush_vapid_private_key", "").strip()
    if public_key and private_key and config_version == WEBPUSH_KEY_VERSION and webpush_pair_matches(public_key, private_key):
        _WEBPUSH_KEY_CACHE.update({"version": WEBPUSH_KEY_VERSION, "public": public_key, "private": private_key})
        return public_key, private_key

    public_key, private_key = generate_vapid_pair()
    if not public_key or not private_key:
        env_public = os.environ.get("WEBPUSH_VAPID_PUBLIC_KEY", "").strip()
        env_private = os.environ.get("WEBPUSH_VAPID_PRIVATE_KEY", "").strip()
        if env_public and env_private and webpush_pair_matches(env_public, env_private):
            _WEBPUSH_KEY_CACHE.update({"version": WEBPUSH_KEY_VERSION, "public": env_public, "private": env_private})
            return env_public, env_private
    if public_key and private_key:
        set_config("webpush_vapid_public_key", public_key, descricao="Chave publica Web Push gerada automaticamente")
        set_config("webpush_vapid_private_key", private_key, descricao="Chave privada Web Push gerada automaticamente")
        set_config("webpush_vapid_version", WEBPUSH_KEY_VERSION, descricao="Versao das chaves Web Push")
        PushSubscription.objects.update(ativo=False, ultimo_erro="Assinatura invalidada por rotacao de chave Push. Ative novamente neste aparelho.")
        _WEBPUSH_KEY_CACHE.update({"version": WEBPUSH_KEY_VERSION, "public": public_key, "private": private_key})
    return public_key, private_key


def webpush_public_key():
    public_key, _private_key = webpush_key_pair()
    return public_key


def webpush_private_key():
    _public_key, private_key = webpush_key_pair()
    return private_key


def webpush_vapid_private_key():
    private_key = webpush_private_key()
    if not private_key:
        return ""
    if private_key.lstrip().startswith("-----BEGIN"):
        try:
            from py_vapid import Vapid

            return Vapid.from_pem(private_key.encode("utf-8"))
        except Exception:
            return ""
    return private_key


def webpush_email():
    return (
        os.environ.get("WEBPUSH_VAPID_SUBJECT")
        or os.environ.get("WEBPUSH_VAPID_EMAIL")
        or os.environ.get("PUBLIC_APP_URL")
        or "https://modelo-teste-operacional.onrender.com"
    ).strip()


def webpush_b64url(data):
    import base64

    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def webpush_b64url_decode(value):
    import base64

    value = (value or "").strip()
    padding = "=" * ((4 - len(value) % 4) % 4)
    return base64.urlsafe_b64decode((value + padding).encode("ascii"))


def webpush_public_key_from_private(private_key):
    try:
        from cryptography.hazmat.primitives.asymmetric import ec
        from cryptography.hazmat.primitives import serialization
    except Exception:
        return ""
    try:
        private_key = (private_key or "").strip()
        if private_key.startswith("-----BEGIN"):
            private_object = serialization.load_pem_private_key(private_key.encode("utf-8"), password=None)
            public_numbers = private_object.public_key().public_numbers()
            public_bytes = b"\x04" + public_numbers.x.to_bytes(32, "big") + public_numbers.y.to_bytes(32, "big")
            return webpush_b64url(public_bytes)
        private_bytes = webpush_b64url_decode(private_key)
        if len(private_bytes) != 32:
            return ""
        private_number = int.from_bytes(private_bytes, "big")
        private_object = ec.derive_private_key(private_number, ec.SECP256R1())
        public_numbers = private_object.public_key().public_numbers()
        public_bytes = b"\x04" + public_numbers.x.to_bytes(32, "big") + public_numbers.y.to_bytes(32, "big")
        return webpush_b64url(public_bytes)
    except Exception:
        return ""


def webpush_pair_matches(public_key, private_key):
    derived_public_key = webpush_public_key_from_private(private_key)
    return bool(derived_public_key and derived_public_key == (public_key or "").strip())


def generate_vapid_pair():
    try:
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import ec
    except Exception:
        return "", ""
    private_key = ec.generate_private_key(ec.SECP256R1())
    public_numbers = private_key.public_key().public_numbers()
    public_bytes = b"\x04" + public_numbers.x.to_bytes(32, "big") + public_numbers.y.to_bytes(32, "big")
    private_pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode("ascii")
    return webpush_b64url(public_bytes), private_pem


def webpush_is_enabled():
    return bool(webpush and webpush_public_key() and webpush_private_key())


def webpush_error_message(exc):
    raw_message = str(exc)
    response_status = getattr(getattr(exc, "response", None), "status_code", None)
    if "BadJwtToken" in raw_message:
        return "Assinatura Push antiga ou incompatível. Este aparelho foi desativado; ative as notificações novamente."
    if response_status in {404, 410}:
        return "Assinatura Push expirada neste aparelho. Ative as notificações novamente."
    if response_status in {400, 403}:
        return "O provedor recusou esta assinatura Push. Ative as notificações novamente neste aparelho."
    return raw_message[:1000]


def send_web_push(subscription, payload):
    if not webpush_is_enabled() or not subscription.ativo:
        return False
    current_public_key = webpush_public_key()
    if not subscription.vapid_public_key or subscription.vapid_public_key != current_public_key:
        subscription.ativo = False
        subscription.ultimo_erro = "Assinatura Push usa chave antiga. Ative notificações novamente neste aparelho."
        subscription.save(update_fields=["ativo", "ultimo_erro", "atualizado_em"])
        return False
    try:
        webpush(
            subscription_info={
                "endpoint": subscription.endpoint,
                "keys": {"p256dh": subscription.p256dh, "auth": subscription.auth},
            },
            data=json.dumps(payload, ensure_ascii=False),
            vapid_private_key=webpush_vapid_private_key(),
            vapid_claims={"sub": webpush_email()},
        )
        subscription.ultimo_erro = ""
        subscription.save(update_fields=["ultimo_erro", "atualizado_em"])
        return True
    except WebPushException as exc:
        subscription.ultimo_erro = webpush_error_message(exc)
        response_status = getattr(getattr(exc, "response", None), "status_code", None)
        if response_status in {400, 403, 404, 410} or "BadJwtToken" in str(exc):
            subscription.ativo = False
            subscription.save(update_fields=["ultimo_erro", "ativo", "atualizado_em"])
        else:
            subscription.save(update_fields=["ultimo_erro", "atualizado_em"])
    except Exception as exc:
        subscription.ultimo_erro = str(exc)[:1000]
        subscription.save(update_fields=["ultimo_erro", "atualizado_em"])
    return False


def create_system_notification(user, titulo, mensagem, url="", categoria="geral", cd_unidade="", payload=None):
    if not user or not getattr(user, "is_active", False):
        return None
    notification = SistemaNotificacao.objects.create(
        usuario=user,
        titulo=titulo,
        mensagem=mensagem,
        url=url,
        categoria=categoria,
        cd_unidade=cd_unidade or "",
        payload=payload or {},
    )
    push_payload = {
        "title": titulo,
        "body": mensagem,
        "url": url or "/",
        "tag": f"modelo-teste-{categoria}-{notification.pk}",
        "notification_id": notification.pk,
    }
    sent = False
    for subscription in user.push_subscriptions.filter(ativo=True):
        sent = send_web_push(subscription, push_payload) or sent
    if sent:
        notification.enviada_push_em = timezone.now()
        notification.save(update_fields=["enviada_push_em"])
    return notification


def notify_users_by_permission(permission, titulo, mensagem, url="", categoria="geral", cd_unidade="", exclude_user=None):
    users = User.objects.filter(is_active=True).select_related("perfil_krill")
    for user in users:
        if exclude_user and user.pk == exclude_user.pk:
            continue
        if user_has_perm(user, permission):
            profile = ensure_profile(user)
            if cd_unidade and profile and not can_view_unified_cd(user) and profile.cd_padrao != cd_unidade:
                continue
            create_system_notification(user, titulo, mensagem, url, categoria, cd_unidade)


def notify_truck_request_created(solicitacao, actor=None):
    url = reverse("module_list", args=["solicitacao_caminhoes"])
    mensagem = (
        f"CD {solicitacao.cd_unidade} solicitou {solicitacao.qtd_caminhoes} caminhão(ões) "
        f"para o CD {solicitacao.cd_destino} em {solicitacao.data_necessidade:%d/%m/%Y} ({solicitacao.get_periodo_display()})."
    )
    if solicitacao.motivo:
        mensagem += f" Motivo: {solicitacao.motivo}."
    if solicitacao.precisa_plataforma:
        mensagem += f" Precisa de plataforma: {solicitacao.qtd_plataforma or solicitacao.qtd_caminhoes} caminhão(ões)."
    notify_users_by_permission(
        "notificar_solicitacao_caminhoes",
        "Solicitação de caminhões",
        mensagem,
        url,
        "frota",
        "",
        exclude_user=actor,
    )


def normalize_vehicle_key(value):
    return "".join(ch for ch in str(value or "").upper() if ch.isalnum())


def active_vehicle_scale(placa="", data_obj=None):
    placa_key = normalize_vehicle_key(placa)
    if not placa_key:
        return None
    data_obj = data_obj or timezone.localdate()
    for escala in (
        EscalaVeiculoFrota.objects.select_related("usuario_responsavel")
        .filter(ativo=True, inicio__lte=data_obj, fim__gte=data_obj)
        .order_by("-inicio", "-id")
    ):
        if normalize_vehicle_key(escala.placa) == placa_key:
            return escala
    return None


def active_scale_for_user(user, data_obj=None):
    if not getattr(user, "is_authenticated", False):
        return None
    data_obj = data_obj or timezone.localdate()
    return (
        EscalaVeiculoFrota.objects.select_related("usuario_responsavel")
        .filter(ativo=True, usuario_responsavel=user, inicio__lte=data_obj, fim__gte=data_obj)
        .order_by("-inicio", "-id")
        .first()
    )


def driver_identity_keys_for_user(user, data_obj=None):
    plate_keys = set()
    driver_keys = set()
    if not getattr(user, "is_authenticated", False):
        return plate_keys, driver_keys
    data_obj = data_obj or timezone.localdate()
    full_name = (user.get_full_name() or "").strip()
    for value in [user.username, user.username.replace(".", " "), full_name]:
        if value:
            driver_keys.add(normalize(value))
    for veiculo in VeiculoFrota.objects.filter(ativo=True, usuario_motorista=user):
        if veiculo.placa:
            plate_keys.add(normalize_vehicle_key(veiculo.placa))
        if veiculo.motorista:
            driver_keys.add(normalize(veiculo.motorista))
    escala = active_scale_for_user(user, data_obj)
    if escala:
        if escala.placa:
            plate_keys.add(normalize_vehicle_key(escala.placa))
        if escala.motorista_responsavel:
            driver_keys.add(normalize(escala.motorista_responsavel))
    return plate_keys, driver_keys


def driver_can_handle_checklist_record(user, record, data_obj=None):
    plate_keys, driver_keys = driver_identity_keys_for_user(user, data_obj)
    record_plate = normalize_vehicle_key(getattr(record, "placa", ""))
    record_driver = normalize(getattr(record, "motorista", ""))
    return bool((record_plate and record_plate in plate_keys) or (record_driver and record_driver in driver_keys))


def checklist_vehicle_initial_for_user(user, cd):
    today = timezone.localdate()
    initial = {}
    plate_keys = set()
    driver_keys = {normalize(user.username), normalize(user.username.replace(".", " "))}
    full_name = (user.get_full_name() or "").strip()
    if full_name:
        driver_keys.add(normalize(full_name))

    escala = active_scale_for_user(user, today)
    if escala:
        veiculo = (
            cd_queryset(VeiculoFrota, cd)
            .filter(ativo=True, placa__iexact=escala.placa)
            .order_by("-data", "-id")
            .first()
        )
        initial.update({
            "motorista": escala.motorista_responsavel,
            "placa": escala.placa,
            "veiculo": getattr(veiculo, "tipo_caminhao", "") or "",
        })
        plate_keys.add(normalize_vehicle_key(escala.placa))
        driver_keys.add(normalize(escala.motorista_responsavel))
    else:
        veiculo = (
            cd_queryset(VeiculoFrota, cd)
            .filter(ativo=True, usuario_motorista=user)
            .order_by("-data", "-id")
            .first()
        )
        if veiculo:
            initial.update({
                "motorista": veiculo.motorista,
                "placa": veiculo.placa,
                "veiculo": veiculo.tipo_caminhao,
            })
            plate_keys.add(normalize_vehicle_key(veiculo.placa))
            driver_keys.add(normalize(veiculo.motorista))

    if initial.get("placa"):
        plate_keys.add(normalize_vehicle_key(initial["placa"]))
    if initial.get("motorista"):
        driver_keys.add(normalize(initial["motorista"]))

    if plate_keys or driver_keys:
        recent_loads = (
            ExpedicaoVinculo.objects.filter(data__gte=today - timedelta(days=1), status__in=["vinculado", "carregado"])
            .order_by("-data", "-criado_em", "-id")
        )
        for vinculo in recent_loads:
            if normalize_vehicle_key(vinculo.placa) not in plate_keys and normalize(vinculo.motorista) not in driver_keys:
                continue
            initial["loja_destino"] = vinculo.loja
            initial["motorista"] = initial.get("motorista") or vinculo.motorista
            initial["placa"] = initial.get("placa") or vinculo.placa
            if not initial.get("veiculo"):
                veiculo = (
                    cd_queryset(VeiculoFrota, vinculo.cd_unidade)
                    .filter(ativo=True, placa__iexact=vinculo.placa)
                    .order_by("-data", "-id")
                    .first()
                )
                initial["veiculo"] = getattr(veiculo, "tipo_caminhao", "") or ""
            break
    return initial


def driver_route_from_fleet_links(user, cd, data_obj=None):
    if not getattr(user, "is_authenticated", False):
        return None
    data_obj = data_obj or timezone.localdate()
    route_cd_scope = UNIFIED_CD
    initial = checklist_vehicle_initial_for_user(user, route_cd_scope)
    plate_keys = {normalize_vehicle_key(initial.get("placa"))} if initial.get("placa") else set()
    driver_keys = {normalize(initial.get("motorista"))} if initial.get("motorista") else set()
    full_name = (user.get_full_name() or "").strip()
    for value in [user.username, user.username.replace(".", " "), full_name]:
        if value:
            driver_keys.add(normalize(value))
    veiculos = VeiculoFrota.objects.filter(ativo=True, usuario_motorista=user)
    for veiculo in veiculos:
        if veiculo.placa:
            plate_keys.add(normalize_vehicle_key(veiculo.placa))
        if veiculo.motorista:
            driver_keys.add(normalize(veiculo.motorista))
    escala = active_scale_for_user(user, data_obj)
    if escala:
        if escala.placa:
            plate_keys.add(normalize_vehicle_key(escala.placa))
        if escala.motorista_responsavel:
            driver_keys.add(normalize(escala.motorista_responsavel))
    if not plate_keys and not driver_keys:
        return None
    vinculos = []
    for vinculo in ExpedicaoVinculo.objects.filter(
        cd_unidade__in=["801", "806"],
        data=data_obj,
        status__in=["vinculado", "carregado"],
    ).order_by("ordem_entrega", "cd_unidade", "loja", "id"):
        if normalize_vehicle_key(vinculo.placa) in plate_keys or normalize(vinculo.motorista) in driver_keys:
            vinculos.append(vinculo)
    if not vinculos:
        return None
    placa = initial.get("placa") or next((v.placa for v in vinculos if v.placa), "")
    motorista = initial.get("motorista") or next((v.motorista for v in vinculos if v.motorista), "")
    veiculo_nome = initial.get("veiculo") or ""
    if not veiculo_nome and placa:
        veiculo = VeiculoFrota.objects.filter(ativo=True, placa__iexact=placa).order_by("-data", "-id").first()
        veiculo_nome = getattr(veiculo, "tipo_caminhao", "") or ""
    saida_aberta = matching_open_departure(route_cd_scope, placa=placa, motorista=motorista, unificado=True)
    route_date = saida_aberta.data if saida_aberta else data_obj
    registros = list(
        ChecklistFrota.objects.filter(data=route_date, tipo_checklist__in=["chegada_loja", "saida_loja"])
        .order_by("criado_em", "id")
    )
    registros = [
        row for row in registros
        if normalize_vehicle_key(row.placa) == normalize_vehicle_key(placa) or normalize(row.motorista) == normalize(motorista)
    ]
    stops_map = OrderedDict()
    for vinculo_index, vinculo in enumerate(vinculos):
        key = loja_codigo(vinculo.loja)
        stop = stops_map.setdefault(
            key,
            {
                "loja": vinculo.loja,
                "cds": OrderedDict(),
                "qtd_paletes": 0,
                "chegou": False,
                "saiu": False,
                "ordem": vinculo.ordem_entrega or 0,
                "sequencia": vinculo_index,
                "orientacoes": [],
            },
        )
        if vinculo.ordem_entrega and (not stop["ordem"] or vinculo.ordem_entrega < stop["ordem"]):
            stop["ordem"] = vinculo.ordem_entrega
        if vinculo.orientacao_frota and vinculo.orientacao_frota not in stop["orientacoes"]:
            stop["orientacoes"].append(vinculo.orientacao_frota)
        stop["cds"][vinculo.cd_unidade] = stop["cds"].get(vinculo.cd_unidade, 0) + vinculo.qtd_paletes
        stop["qtd_paletes"] += vinculo.qtd_paletes
    for stop_key, stop in stops_map.items():
        stop["chegou"] = any(row.tipo_checklist == "chegada_loja" and loja_codigo(row.loja_destino) == stop_key for row in registros)
        stop["saiu"] = any(row.tipo_checklist == "saida_loja" and loja_codigo(row.loja_destino) == stop_key for row in registros)
        stop["cd_label"] = " + ".join(f"CD {cd_item}" for cd_item in stop["cds"].keys())
        if stop["saiu"]:
            stop["status"] = "concluida"
        elif stop["chegou"]:
            stop["status"] = "em loja"
        else:
            stop["status"] = "pendente"
    stops = sorted(stops_map.values(), key=lambda item: (0 if item["ordem"] else 1, item["ordem"] or item["sequencia"], item["sequencia"]))
    candidate_stops = []
    if not saida_aberta:
        next_type = "saida"
        candidate_stops = stops
    else:
        candidate_stops = [stop for stop in stops if stop["chegou"] and not stop["saiu"]]
        if candidate_stops:
            next_type = "saida_loja"
        else:
            candidate_stops = [stop for stop in stops if not stop["chegou"]]
            next_type = "chegada_loja" if candidate_stops else "retorno"
    current_stop = candidate_stops[0] if len(candidate_stops) == 1 else None
    route_label = " + ".join(stop["loja"] for stop in stops[:3])
    if len(stops) > 3:
        route_label += f" + {len(stops) - 3} loja(s)"
    type_labels = dict(ChecklistFrota.TIPO_CHECKLIST)
    return {
        "placa": placa,
        "motorista": motorista,
        "veiculo": veiculo_nome,
        "data": route_date,
        "saida_aberta": saida_aberta,
        "stops": stops,
        "candidate_stops": candidate_stops,
        "current_stop": current_stop,
        "next_type": next_type,
        "next_label": type_labels.get(next_type, next_type),
        "route_label": route_label[:120],
        "total_paletes": sum(stop["qtd_paletes"] for stop in stops),
        "cds": sorted({cd_item for stop in stops for cd_item in stop["cds"].keys()}),
        "intercalacao_por_carga": len({cd_item for stop in stops for cd_item in stop["cds"].keys()}) > 1,
        "completed": sum(1 for stop in stops if stop["saiu"]),
    }


def apply_driver_route_to_checklist_form(form, route):
    if not route:
        return
    if "tipo_checklist" in form.fields:
        labels = dict(ChecklistFrota.TIPO_CHECKLIST)
        next_type = route.get("next_type")
        form.fields["tipo_checklist"].choices = [(next_type, labels.get(next_type, next_type))]
        form.fields["tipo_checklist"].initial = next_type
    if "motorista" in form.fields:
        form.fields["motorista"].initial = route.get("motorista", "")
    if "placa" in form.fields:
        form.fields["placa"].initial = route.get("placa", "")
    if "veiculo" in form.fields:
        form.fields["veiculo"].initial = route.get("veiculo", "")
    if "loja_destino" in form.fields:
        current_stop = route.get("current_stop")
        candidate_stops = route.get("candidate_stops") or []
        if route.get("next_type") in {"chegada_loja", "saida_loja"} and candidate_stops:
            choices = [(stop["loja"], stop["loja"]) for stop in candidate_stops]
            form.fields["loja_destino"].initial = (current_stop or candidate_stops[0])["loja"]
        else:
            route_label = route.get("route_label", "")
            choices = [(route_label, route_label)] if route_label else []
            form.fields["loja_destino"].initial = route_label
        if choices:
            form.fields["loja_destino"].widget = forms.Select(choices=choices)
            form.fields["loja_destino"].required = False


def validate_driver_route_checklist(request, obj, route):
    if not route:
        return None
    expected = route.get("next_type")
    if obj.tipo_checklist != expected:
        return f"A proxima etapa da rota e {route.get('next_label')}. Registre essa etapa antes de continuar."
    current_stop = route.get("current_stop")
    if obj.tipo_checklist in {"chegada_loja", "saida_loja"}:
        candidate_stops = route.get("candidate_stops") or ([current_stop] if current_stop else [])
        selected_stop = next((stop for stop in candidate_stops if loja_codigo(stop["loja"]) == loja_codigo(obj.loja_destino)), None)
        if not selected_stop:
            return "Escolha uma loja pendente desta rota antes de continuar."
        obj.loja_destino = selected_stop["loja"]
    elif obj.tipo_checklist == "saida":
        obj.loja_destino = route.get("route_label", obj.loja_destino)
    elif obj.tipo_checklist == "retorno":
        pendentes = [stop["loja"] for stop in route.get("stops", []) if not stop["saiu"]]
        if pendentes:
            return "Ainda existe loja pendente nesta rota: " + ", ".join(pendentes[:3])
        obj.loja_destino = route.get("route_label", obj.loja_destino)
    if route.get("motorista"):
        obj.motorista = route["motorista"]
    if route.get("placa"):
        obj.placa = route["placa"]
    if route.get("veiculo"):
        obj.veiculo = route["veiculo"]
    if route.get("cds"):
        current_cd_value = str(getattr(obj, "cd_unidade", "") or "")
        obj.cd_unidade = current_cd_value if current_cd_value in route["cds"] else route["cds"][0]
    return None


def active_links_for_ready_notice(solicitacao):
    marker = f"#{solicitacao.pk}"
    return ExpedicaoVinculo.objects.filter(
        cd_unidade=solicitacao.cd_unidade,
        data=solicitacao.data,
        loja=solicitacao.loja,
        periodo=solicitacao.periodo,
        status="vinculado",
        observacao__icontains=marker,
    )


def ready_notice_id_from_vinculo(vinculo):
    match = re.search(r"#(\d+)", vinculo.observacao or "")
    return int(match.group(1)) if match else None


def sync_ready_notice_after_vinculo_closed(vinculo, user=None):
    notice_id = ready_notice_id_from_vinculo(vinculo)
    solicitacao = SolicitacaoCargaPronta.objects.filter(pk=notice_id).first() if notice_id else None
    if not solicitacao:
        solicitacao = (
            SolicitacaoCargaPronta.objects.filter(
                cd_unidade=vinculo.cd_unidade,
                data=vinculo.data,
                loja=vinculo.loja,
                periodo=vinculo.periodo,
                status="em_carregamento",
            )
            .order_by("-atualizado_em", "-id")
            .first()
        )
    if not solicitacao:
        return
    if active_links_for_ready_notice(solicitacao).exists():
        return
    if vinculo.status == "carregado":
        solicitacao.status = "carregada"
        solicitacao.resposta = f"Carregamento confirmado pela Expedição: {vinculo.placa or vinculo.motorista}."
    elif vinculo.status == "cancelado":
        solicitacao.status = "pronta"
        solicitacao.resposta = "Carga liberada para nova escolha de caminhão."
    else:
        return
    solicitacao.tratado_por = user or solicitacao.tratado_por
    solicitacao.tratado_em = timezone.now()
    solicitacao.save(update_fields=["status", "resposta", "tratado_por", "tratado_em", "atualizado_em"])


def apply_ready_notice_quantity_adjustment(solicitacao, old_quantity, new_quantity, user, saldo_restante_801=None):
    if solicitacao.cd_unidade not in {"801", "806"}:
        return None
    labels_cd = loja_label_map([solicitacao.cd_unidade])
    loja_normalizada = loja_label_from_value(solicitacao.loja, labels_cd)
    codigo_loja = loja_codigo(loja_normalizada)
    registros = [
        item
        for item in ExpedicaoPlanejamento.objects.select_for_update()
        .filter(cd_unidade=solicitacao.cd_unidade)
        .order_by("atualizado_em", "id")
        if loja_codigo(item.loja) == codigo_loja
    ]
    planejamento = next((item for item in registros if item.data == solicitacao.data), None)
    if planejamento is None:
        planejamento = registros[-1] if registros else ExpedicaoPlanejamento(cd_unidade=solicitacao.cd_unidade)
    saldo_atual = planejamento.qtd_paletes if planejamento.pk else 0
    if solicitacao.cd_unidade == "806":
        novo_saldo = new_quantity
        observacao = f"Saldo pronto corrigido pela fila. Antes: {old_quantity}; novo: {new_quantity}."
    elif saldo_restante_801 is not None:
        novo_saldo = max(0, saldo_restante_801)
        observacao = f"Saldo restante informado apos conferência/remontagem. Antes: {saldo_atual}; novo: {novo_saldo}."
    else:
        novo_saldo = max(0, saldo_atual + old_quantity - new_quantity)
        observacao = f"Saldo ajustado pela correção da loja pronta. Aviso antes: {old_quantity}; novo aviso: {new_quantity}."
    planejamento.cd_unidade = solicitacao.cd_unidade
    planejamento.data = solicitacao.data
    planejamento.loja = loja_normalizada or solicitacao.loja
    planejamento.qtd_paletes = novo_saldo
    planejamento.status = "planejado" if novo_saldo else "cancelado"
    planejamento.pode_remontar = False
    planejamento.qtd_remontavel = 0
    planejamento.carregado_em = None
    planejamento.observacao = observacao
    planejamento.criado_por = user
    planejamento.save()
    duplicados = [item.pk for item in registros if item.pk != planejamento.pk]
    if duplicados:
        ExpedicaoPlanejamento.objects.filter(pk__in=duplicados).delete()
    return planejamento


def collaborator_unavailability(colaborador="", data_obj=None):
    colaborador_key = normalize(colaborador)
    if not colaborador_key:
        return None
    data_obj = data_obj or timezone.localdate()
    ferias_qs = ColaboradorFerias.objects.filter(
        inicio_ferias__lte=data_obj,
        fim_ferias__gte=data_obj,
    ).exclude(status__in=["cancelada", "retornado"])
    for ferias in ferias_qs:
        if normalize(ferias.colaborador) == colaborador_key:
            return {"tipo": "ferias", "label": "Férias", "registro": ferias}

    ausencia_qs = ColaboradorAusencia.objects.filter(
        inicio__lte=data_obj,
        fim__gte=data_obj,
    ).exclude(status__in=["cancelada", "encerrada"])
    for ausencia in ausencia_qs:
        if normalize(ausencia.colaborador) == colaborador_key:
            return {"tipo": ausencia.tipo, "label": ausencia.get_tipo_display(), "registro": ausencia}
    return None


def collaborator_is_unavailable(colaborador="", data_obj=None):
    return collaborator_unavailability(colaborador, data_obj) is not None


def driver_is_on_vacation(motorista="", data_obj=None):
    motorista_key = normalize(motorista)
    if not motorista_key:
        return False
    unavailable = collaborator_unavailability(motorista, data_obj)
    return bool(unavailable and unavailable["tipo"] == "ferias")


def user_for_driver_name(motorista=""):
    motorista_key = normalize(motorista)
    if not motorista_key:
        return None
    aliases = {motorista_key, motorista_key.replace(" ", "."), motorista_key.split(" ")[0]}
    for user in User.objects.filter(is_active=True):
        if normalize(user.username) in aliases:
            return user
    return None


def find_driver_notification_user(motorista="", placa="", data_obj=None):
    escala = active_vehicle_scale(placa, data_obj)
    if escala:
        if collaborator_is_unavailable(escala.motorista_responsavel, data_obj):
            return None
        if escala.usuario_responsavel and escala.usuario_responsavel.is_active:
            return escala.usuario_responsavel
        substitute_user = user_for_driver_name(escala.motorista_responsavel)
        if substitute_user:
            return substitute_user

    placa_key = normalize_vehicle_key(placa)
    motorista_key = normalize(motorista)
    for veiculo in VeiculoFrota.objects.select_related("usuario_motorista").filter(ativo=True, usuario_motorista__isnull=False):
        if placa_key and normalize_vehicle_key(veiculo.placa) == placa_key:
            if collaborator_is_unavailable(veiculo.motorista, data_obj):
                return None
            return veiculo.usuario_motorista
        if motorista_key and normalize(veiculo.motorista) == motorista_key:
            if collaborator_is_unavailable(veiculo.motorista, data_obj):
                return None
            return veiculo.usuario_motorista
    if motorista_key:
        if collaborator_is_unavailable(motorista, data_obj):
            return None
        return user_for_driver_name(motorista)
    return None


def users_for_notification_permission(permission, cd_unidade="", exclude_user=None):
    selected = OrderedDict()
    for user in User.objects.filter(is_active=True).select_related("perfil_krill"):
        if exclude_user and user.pk == exclude_user.pk:
            continue
        if not user_has_perm(user, permission):
            continue
        profile = ensure_profile(user)
        if cd_unidade and profile and not can_view_unified_cd(user) and profile.cd_padrao != cd_unidade:
            continue
        selected[user.pk] = user
    return selected


def notification_user_requires_cd_scope(user):
    management_permissions = {
        "painel_master",
        "painel_gestao",
        "gestao_acesso_total",
        "painel_frota",
        "vincular_cargas_expedicao",
        "aprovacoes_carregamento",
        "aprovar_lancamento_manual_expedicao",
    }
    if any(user_has_perm(user, permission) for permission in management_permissions):
        return False
    return user_has_perm(user, "expedicao") or user_has_perm(user, "carregamento_veiculos")


def add_notification_users(recipients, permission, actor=None, cd_unidade="", expedition_scope_cds=None):
    for user in User.objects.filter(is_active=True).select_related("perfil_krill"):
        if actor and user.pk == actor.pk:
            continue
        if not user_has_perm(user, permission):
            continue
        profile = ensure_profile(user)
        if expedition_scope_cds and notification_user_requires_cd_scope(user):
            if not profile or profile.cd_padrao not in expedition_scope_cds:
                continue
        if cd_unidade:
            if profile and notification_user_requires_cd_scope(user) and profile.cd_padrao != cd_unidade:
                continue
            if profile and not can_view_unified_cd(user) and profile.cd_padrao != cd_unidade:
                continue
        recipients[user.pk] = user


def expedition_user_receives_operational_message(user):
    profile = ensure_profile(user)
    management_cargos = {
        "master",
        "gestor_cd",
        "gerente",
        "supervisor",
        "supervisor_recebimento",
        "supervisor_separacao",
        "supervisor_conferencia_expedicao",
        "supervisor_frota",
        "lider",
        "lider_separacao",
        "lider_conferencia_expedicao",
        "lider_frota",
        "analista",
    }
    if profile and profile.cargo in management_cargos:
        return False
    return user_has_perm(user, "receber_alertas_expedicao") and user_has_perm(user, "expedicao")


def expedition_action_text(evento):
    normalized = normalize(evento)
    if "cancelado" in normalized:
        return "Ação: não carregar sem nova orientação da Frota."
    if "confirmad" in normalized:
        return "Ação: carregamento confirmado na expedição."
    return "Ação: inspeção/expedição deve lançar a carga na BlueSoft usando este motorista e esta placa."


def notify_expedition_load(vinculos, titulo, evento, actor=None, permissions=None, include_driver=True):
    vinculos = [v for v in vinculos if v]
    if not vinculos:
        return 0
    permissions = permissions or ["notificar_carga_vinculada", "receber_alertas_expedicao"]
    loja = vinculos[0].loja
    placa = vinculos[0].placa
    motorista = vinculos[0].motorista
    cd_totals = OrderedDict()
    for vinculo in vinculos:
        cd_totals[vinculo.cd_unidade] = cd_totals.get(vinculo.cd_unidade, 0) + vinculo.qtd_paletes
    planejamentos = {
        planejamento.cd_unidade: planejamento
        for planejamento in current_pallet_rows(cd_totals.keys())
        if loja_codigo(planejamento.loja) == loja_codigo(loja)
    }
    detalhes_cd = ", ".join(f"CD {cd}: {qtd} pallet(s)" for cd, qtd in cd_totals.items())
    destino = motorista or placa or "motorista/placa não informado"
    mensagem_gerencial = f"{loja}: {detalhes_cd}. {evento}: {destino}."
    if placa and motorista:
        mensagem_gerencial = f"{loja}: {detalhes_cd}. {evento}: {motorista} / {placa}."
    destino_operacional = " / ".join(part for part in [motorista, placa] if part) or destino
    linhas_operacionais = [f"{loja} - {destino_operacional}"]
    for cd_unidade, qtd in cd_totals.items():
        linhas_operacionais.append(f"CD {cd_unidade}: {qtd} pallet(s)")
    linhas_operacionais.append(expedition_action_text(evento))
    mensagem_operacional = "\n".join(linhas_operacionais)
    url = reverse("module_list", args=["carregamento_veiculos"])
    recipients = OrderedDict()
    driver_user = find_driver_notification_user(motorista, placa, vinculos[0].data) if include_driver else None
    if driver_user and user_has_perm(driver_user, "notificar_motorista_vinculado") and (not actor or driver_user.pk != actor.pk):
        recipients[driver_user.pk] = driver_user
    for permission in permissions:
        if permission == "receber_alertas_expedicao":
            for cd_unidade in cd_totals.keys():
                add_notification_users(recipients, permission, actor=actor, cd_unidade=cd_unidade)
        else:
            add_notification_users(recipients, permission, actor=actor, expedition_scope_cds=set(cd_totals.keys()))
    for user in User.objects.filter(is_active=True).select_related("perfil_krill"):
        if actor and user.pk == actor.pk:
            continue
        if not user_has_perm(user, "receber_alertas_expedicao"):
            continue
        profile = ensure_profile(user)
        if profile and normalized_profile_cd(profile) in cd_totals:
            recipients[user.pk] = user
    notification_cd = next(iter(cd_totals.keys())) if len(cd_totals) == 1 else ""
    for user in recipients.values():
        mensagem = mensagem_operacional if expedition_user_receives_operational_message(user) else mensagem_gerencial
        create_system_notification(
            user,
            titulo,
            mensagem,
            url,
            "frota",
            notification_cd,
            {
                "cds": list(cd_totals.keys()),
                "loja": loja,
                "placa": placa,
                "motorista": motorista,
            },
        )
    return len(recipients)


def notify_pallet_balance_saved(planejamento, actor=None, zerado=False):
    recipients = OrderedDict()
    add_notification_users(recipients, "notificar_saldo_paletes", actor=actor, cd_unidade=planejamento.cd_unidade)
    if not recipients:
        return 0
    loja = planejamento.loja
    qtd = planejamento.qtd_paletes
    titulo = "Saldo de pallets atualizado"
    mensagem = f"CD {planejamento.cd_unidade} - {loja}: saldo atualizado para {qtd} pallet(s)."
    url = url_with_query(reverse('module_list', args=['expedicao_planejamento']), {'data': planejamento.data.isoformat(), 'loja': loja})
    for user in recipients.values():
        create_system_notification(
            user,
            titulo,
            mensagem,
            url,
            "expedicao",
            planejamento.cd_unidade,
            {
                "cd": planejamento.cd_unidade,
                "loja": loja,
                "qtd_paletes": qtd,
                "zerado": zerado,
            },
        )
    return len(recipients)


def driver_linked_loads(user):
    veiculos = list(VeiculoFrota.objects.filter(usuario_motorista=user, ativo=True))
    plate_keys = {normalize_vehicle_key(veiculo.placa) for veiculo in veiculos if veiculo.placa}
    driver_keys = {normalize(veiculo.motorista) for veiculo in veiculos if veiculo.motorista}
    full_name = (user.get_full_name() or "").strip()
    if full_name:
        driver_keys.add(normalize(full_name))
    driver_keys.add(normalize(user.username))
    driver_keys.add(normalize(user.username.replace(".", " ")))
    today = timezone.localdate()
    escalas = []
    for escala in EscalaVeiculoFrota.objects.filter(ativo=True, inicio__lte=today, fim__gte=today):
        if escala.usuario_responsavel_id == user.pk or normalize(escala.motorista_responsavel) in driver_keys:
            escalas.append(escala)
    for escala in escalas:
        if escala.placa:
            plate_keys.add(normalize_vehicle_key(escala.placa))
        if escala.motorista_responsavel:
            driver_keys.add(normalize(escala.motorista_responsavel))
    if not plate_keys and not driver_keys:
        return []
    start_date = timezone.localdate() - timedelta(days=1)
    rows = []
    vinculos = (
        ExpedicaoVinculo.objects.filter(data__gte=start_date, status__in=["vinculado", "carregado"])
        .order_by("data", "ordem_entrega", "cd_unidade", "loja", "criado_em")
    )
    for vinculo in vinculos:
        if normalize_vehicle_key(vinculo.placa) not in plate_keys and normalize(vinculo.motorista) not in driver_keys:
            continue
        rows.append(
            {
                "data": vinculo.data,
                "cd": vinculo.cd_unidade,
                "loja": vinculo.loja,
                "placa": vinculo.placa,
                "motorista": vinculo.motorista,
                "qtd_paletes": vinculo.qtd_paletes,
                "periodo": vinculo.get_periodo_display(),
                "status": vinculo.get_status_display(),
                "confirmada": vinculo.status == "carregado",
                "ordem": vinculo.ordem_entrega,
                "orientacao": vinculo.orientacao_frota,
            }
        )
    return rows[:20]


def notify_driver_checklist(checklist, actor=None):
    recipients = OrderedDict()
    for permission in ["notificar_checklist_motorista", "alertas_frota"]:
        add_notification_users(recipients, permission, actor=actor)
    if not recipients:
        return 0
    destino = f" para {checklist.loja_destino}" if checklist.loja_destino else ""
    placa = f" / {checklist.placa}" if checklist.placa else ""
    if checklist.tipo_checklist == "retorno":
        titulo = "Check-list de retorno"
        mensagem = f"{checklist.motorista}{placa} registrou retorno no CD {checklist.cd_unidade}{destino}."
    else:
        titulo = "Check-list de saída"
        mensagem = f"{checklist.motorista}{placa} registrou check-list de saída no CD {checklist.cd_unidade}{destino}."
    url = url_with_query(reverse("module_list", args=["checklist_frota"]), {"painel": "frota"})
    for user in recipients.values():
        create_system_notification(
            user,
            titulo,
            mensagem,
            url,
            "frota",
            checklist.cd_unidade,
            {"checklist_id": checklist.pk, "placa": checklist.placa, "motorista": checklist.motorista},
        )
    return len(recipients)


def notify_manual_loading_approved(solicitacao, actor=None):
    recipients = OrderedDict()
    add_notification_users(recipients, "notificar_alteracao_carga", actor=actor, expedition_scope_cds={solicitacao.cd_unidade})
    add_notification_users(recipients, "receber_alertas_expedicao", actor=actor, cd_unidade=solicitacao.cd_unidade)
    if not recipients:
        return 0
    destino = solicitacao.motorista or solicitacao.placa or "motorista/placa não informado"
    if solicitacao.motorista and solicitacao.placa:
        destino = f"{solicitacao.motorista} / {solicitacao.placa}"
    mensagem = f"{solicitacao.loja}: {solicitacao.qtd_paletes} pallet(s) do CD {solicitacao.cd_unidade} aprovados para {destino}."
    url = reverse("module_list", args=["expedicao"])
    for user in recipients.values():
        create_system_notification(
            user,
            "Carregamento aprovado",
            mensagem,
            url,
            "expedicao",
            solicitacao.cd_unidade,
            {"solicitacao_id": solicitacao.pk, "loja": solicitacao.loja, "placa": solicitacao.placa, "motorista": solicitacao.motorista},
        )
    return len(recipients)


def user_request_notifications(user):
    if not getattr(user, "is_authenticated", False):
        return {"count": 0, "items": []}

    def build():
        max_items = 50
        sistema_qs = SistemaNotificacao.objects.filter(usuario=user, lida_em__isnull=True).order_by("-criado_em")
        senha_qs = (
            SolicitacaoSenha.objects.filter(usuario=user, lido_pelo_usuario_em__isnull=True)
            .exclude(status="pendente")
            .order_by("-atendido_em", "-criado_em")
        )
        acesso_qs = (
            SolicitacaoAcesso.objects.filter(usuario=user, lido_pelo_usuario_em__isnull=True)
            .exclude(status="pendente")
            .order_by("-atendido_em", "-criado_em")
        )
        melhoria_qs = (
            MelhoriaSistema.objects.filter(criado_por=user, lido_pelo_solicitante_em__isnull=True)
            .filter(Q(decisao__gt="") | ~Q(status="sugestao"))
            .order_by("-atualizado_em", "-criado_em")
        )
        carregamento_qs = (
            SolicitacaoCarregamentoManual.objects.filter(
                criado_por=user,
                lido_pelo_solicitante_em__isnull=True,
                status__in=["aprovado", "recusado", "cancelado", "concluido"],
            )
            .order_by("-aprovado_em", "-atualizado_em", "-criado_em")
        )
        sistema_rows = list(
            sistema_qs[:max_items]
        )
        senha_rows = list(
            senha_qs[:max_items]
        )
        acesso_rows = list(
            acesso_qs[:max_items]
        )
        melhoria_rows = list(
            melhoria_qs[:max_items]
        )
        carregamento_rows = list(
            carregamento_qs[:max_items]
        )
        count = sistema_qs.count() + senha_qs.count() + acesso_qs.count() + melhoria_qs.count() + carregamento_qs.count()
        items = [
            {"tipo": item.get_categoria_display(), "status": "Novo", "texto": item.mensagem, "quando": item.criado_em}
            for item in sistema_rows
        ]
        items.extend(
            {
                "tipo": "Senha/login",
                "status": item.get_status_display(),
                "texto": item.observacao or "Sua solicitação foi atualizada.",
                "quando": item.atendido_em or item.criado_em,
            }
            for item in senha_rows
        )
        items.extend(
            {
                "tipo": "Acesso",
                "status": item.get_status_display(),
                "texto": item.resposta or "Seu pedido de acesso foi atualizado.",
                "quando": item.atendido_em or item.criado_em,
            }
            for item in acesso_rows
        )
        items.extend(
            {
                "tipo": "Melhoria",
                "status": item.get_status_display(),
                "texto": item.decisao or item.titulo,
                "quando": item.atualizado_em or item.criado_em,
            }
            for item in melhoria_rows
        )
        items.extend(
            {
                "tipo": "Carregamento",
                "status": item.get_status_display(),
                "texto": item.resposta or f"{item.loja}: {item.qtd_paletes} pallet(s).",
                "quando": item.aprovado_em or item.atualizado_em or item.criado_em,
            }
            for item in carregamento_rows
        )
        items.sort(key=lambda item: item.get("quando") or timezone.datetime.min.replace(tzinfo=timezone.utc), reverse=True)
        return {"count": count, "items": items[:max_items]}

    return cached_runtime_value(("user_request_notifications", user.pk), 2, build)


def context_base(request):
    from .gestao_cd_nav import grupos_menu

    cd_atual = current_cd(request)
    profile = ensure_profile(request.user) if getattr(request.user, "is_authenticated", False) else None
    trial_state = getattr(request, "pilot_trial_state", None) or pilot_trial_state()
    profile_version = profile.atualizado_em.isoformat() if profile else ""
    can_access = can_manage_access(request.user)
    can_system_features = can_manage_system_features(request.user)
    can_push_admin = can_manage_push(request.user)
    profile_cd = normalized_profile_cd(profile) if profile else "806"
    if request.user.is_authenticated:
        session_cd = request.session.get("cd_unidade") or cd_atual
        if request.session.get("ultima_rota") != request.path:
            request.session["ultima_rota"] = request.path
        if request.session.get("cd_unidade_atual") != session_cd:
            request.session["cd_unidade_atual"] = session_cd
        ultimo_acesso = request.session.get("ultimo_acesso")
        try:
            ultimo_acesso_dt = timezone.datetime.fromisoformat(ultimo_acesso) if ultimo_acesso else None
        except (TypeError, ValueError):
            ultimo_acesso_dt = None
        if not ultimo_acesso_dt or timezone.now() - ultimo_acesso_dt > timezone.timedelta(minutes=1):
            request.session["ultimo_acesso"] = timezone.now().isoformat()
    return {
        "cd_unidade": cd_atual,
        "cd_unidade_label": cd_scope_label(cd_atual),
        "app_version": settings.APP_VERSION,
        "current_full_path": request.get_full_path(),
        "show_topbar_back": False,
        "topbar_back_url": reverse("dashboard"),
        "module_groups": cached_runtime_value(("module_groups", request.user.pk, profile_version), 15, lambda: grouped_modules(request.user)),
        "navigation_groups": cached_runtime_value(("navigation_groups", request.user.pk, profile_version), 15, lambda: navigation_groups(request.user)),
        "custom_hub_groups": cached_runtime_value(("custom_hub_groups", request.user.pk, profile_version), 15, lambda: custom_hub_groups(request.user)),
        "floating_alerts": floating_alerts_context(request.user, cd_atual),
        "user_request_notifications": user_request_notifications(request.user),
        "ui_labels": {key: panel_label(key, value) for key, value in DEFAULT_PANEL_LABELS.items()},
        "ui_icons": {key: panel_icon(key, value) for key, value in DEFAULT_PANEL_ICONS.items()},
        "project_author": "João Pedro Magalhães Martins",
        "project_credit": "Projeto idealizado e desenvolvido por João Pedro Magalhães Martins para apoiar as rotinas administrativas e operacionais do CD.",
        "all_permissions": active_permissions() if can_access else [],
        "is_motorista": bool(request.user.is_authenticated and profile and profile.cargo == "motorista"),
        "can_dashboard": user_has_perm(request.user, "painel"),
        "can_management": user_has_perm(request.user, "painel_gestao"),
        "can_server": False,
        "can_diagnostic": False,
        "can_sentinel": user_has_perm(request.user, "sentinela_servidor") and not user_has_perm(request.user, "painel"),
        "can_master": user_has_perm(request.user, "painel_master"),
        "can_audit": user_has_perm(request.user, "painel_master") or user_has_perm(request.user, "auditoria_acessos"),
        "can_access": can_manage_access(request.user),
        "can_report": user_has_perm(request.user, "relatorios"),
        "can_preferences": user_has_perm(request.user, "preferencias"),
        "can_frota_panel": user_has_perm(request.user, "painel_frota") or user_has_perm(request.user, "editar_checklist_frota"),
        "can_frota_hub": can_open_frota_hub(request.user),
        "can_driver_checklist": user_has_perm(request.user, "checklist_frota"),
        "can_import": user_has_perm(request.user, "importar_planilhas"),
        "can_fix_cd": user_has_perm(request.user, "corrigir_cd"),
        "can_customize_ui": user_has_perm(request.user, "personalizar_interface"),
        "can_demo_mode": user_has_perm(request.user, "modo_demonstracao"),
        "demo_mode_active": bool(getattr(request, "demo_mode_active", False)),
        "demo_feedback": request.GET.get("demo_salvo") == "1",
        "pilot_trial": trial_state,
        "require_load_value_on_confirm": system_rule_enabled("obrigar_valor_confirmar_carregamento") and can_view_billing_value(request.user),
        "can_system_features": can_system_features,
        "can_manage_push": can_push_admin,
        "can_tms_expedicao": user_has_perm(request.user, "tms_expedicao"),
        "can_tms_krill": (
            user_has_perm(request.user, "tms_krill")
            or user_has_perm(request.user, "tms_expedicao")
            or user_has_perm(request.user, "painel")
        ),
        "gestao_cd_grupos": grupos_menu(request.path),
        "krill_nav_open": request.path.startswith(("/tms/", "/wms/", "/yms/", "/patio-docas", "/dashboard/")),
        "show_tools_menu": any(
            [
                user_has_perm(request.user, "importar_planilhas"),
                user_has_perm(request.user, "corrigir_cd"),
                user_has_perm(request.user, "preferencias"),
                can_access,
                user_has_perm(request.user, "personalizar_interface"),
                can_system_features,
                can_push_admin,
            ]
        ),
        "can_switch_cd": user_has_perm(request.user, "trocar_cd"),
        "can_unified_cd": can_view_unified_cd(request.user),
        "show_unified_cd_option": SHOW_UNIFIED_CD_IN_SELECTOR,
        "can_edit": user_has_perm(request.user, "editar_registros"),
        "can_delete": user_has_perm(request.user, "excluir_registros"),
    }

def grouped_permissions():
    groups = [
        (
            "Acesso e sistema",
            {
                "painel",
                "painel_gestao",
                "painel_master",
                "gestao_acesso",
                "gestao_acesso_total",
                "auditoria_acessos",
                "relatorios",
                "preferencias",
                "personalizar_interface",
                "modo_demonstracao",
                "trocar_cd",
                "sentinela_servidor",
            },
        ),
        (
            "Alertas e notificacoes",
            {
                "gerenciar_push",
                "notificar_carga_vinculada",
                "notificar_alteracao_carga",
                "notificar_saldo_paletes",
                "notificar_loja_pronta_carregamento",
                "notificar_solicitacao_caminhoes",
                "notificar_motorista_vinculado",
                "notificar_checklist_motorista",
                "receber_alertas_expedicao",
                "alertas_frota",
                "alertas_ferias",
                "notificacoes_ferias_colaboradores",
            },
        ),
        (
            "Ferias e equipe",
            {
                "colaboradores_hub",
                "pessoas_turno",
                "funcoes_turno",
                "ferias_colaboradores",
                "ausencias_colaboradores",
                "mapa_calor_ferias",
                "capacidade_operacao",
                "configurar_alertas_ferias",
                "visualizar_gargalos_colaboradores",
                "gerenciar_ferias_colaboradores",
                "setores_detalhados_colaboradores",
            },
        ),
        (
            "Expedição e faturamento",
            {
                "expedicao",
                "expedicao_planejamento",
                "faturamento_expedicao",
                "registrar_erro_fiscal",
                "liberar_carga_faturada",
                "visualizar_valor_faturamento",
                "lojas_prontas_carregamento",
                "solicitar_lancamento_manual_expedicao",
                "relatorio_paletes_cd",
                "corrigir_saldo_paletes",
                "visualizar_cds_unificados",
            },
        ),
        (
            "Operacao CD e pallets da rede",
            {
                "paletes_rede",
                "adicionar_loja_paletes_rede",
                "editar_saldo_paletes_rede",
                "movimentar_paletes_rede",
                "paletes_vasilhames",
                "unitizadores",
            },
        ),
        (
            "Equipamentos do CD",
            {
                "equipamentos",
                "manutencao_equipamentos",
                "saude_equipamentos",
            },
        ),
        (
            "Frota e motoristas",
            {
                "checklist_frota",
                "checklist_frota_itens",
                "veiculos_frota",
                "escala_veiculos_frota",
                "carregamento_veiculos",
                "aprovacoes_carregamento",
                "acompanhar_lojas_prontas_carregamento",
                "solicitacao_caminhoes",
                "painel_frota",
                "materiais_frota",
                "lacres_frota",
                "editar_checklist_frota",
                "encerrar_retorno_frota",
                "exportar_frota",
                "vincular_cargas_expedicao",
                "aprovar_lancamento_manual_expedicao",
                "lancar_manual_sem_aprovacao",
                "tratar_solicitacao_caminhoes",
            },
        ),
        (
            "Acoes dentro das telas",
            {
                "criar_registros",
                "consultar_registros",
                "ver_graficos_resumos",
                "editar_registros",
                "excluir_registros",
                "exportar_dados",
            },
        ),
    ]
    permissions = active_permissions()
    labels = dict(permissions)
    used = set()
    result = []
    for title, keys in groups:
        items = [permission_item(key, labels[key]) for key, _label in permissions if key in keys]
        used.update(item["key"] for item in items)
        if items:
            result.append({"title": title, "items": items})
    remaining = [permission_item(key, label) for key, label in permissions if key not in used]
    if remaining:
        result.append({"title": "Outras permissões", "items": remaining})
    return result

def user_access_summary(user):
    profile = ensure_profile(user)
    perms = set(profile.permissoes or [])
    cd_label = "CD 801-806" if profile.cd_padrao == UNIFIED_CD else f"CD {profile.cd_padrao}"
    if profile.cargo == "master" or user.is_superuser:
        area = "Master"
        area_key = "master"
    elif "checklist_frota" in perms and "criar_registros" in perms and "painel_frota" not in perms:
        area = "Motorista"
        area_key = "motorista"
    elif {"painel_frota", "solicitacao_caminhoes", "checklist_frota"} & perms:
        area = "Frota"
        area_key = "frota"
    elif {"lojas_prontas_carregamento", "faturamento_expedicao", "relatorio_paletes_cd"} & perms:
        area = "Expedição"
        area_key = "expedicao"
    elif {"paletes_rede", "paletes_vasilhames", "unitizadores", "equipamentos"} & perms:
        area = "Operacao CD"
        area_key = "operacao"
    elif {"ferias_colaboradores", "ausencias_colaboradores", "colaboradores_hub"} & perms:
        area = "Colaboradores"
        area_key = "colaboradores"
    elif "painel_gestao" in perms:
        area = "Gerencial"
        area_key = "gerencial"
    else:
        area = "Sem area definida"
        area_key = "sem_area"

    visible = []
    visibility_rules = [
        ("Início", "painel"),
        ("Gerencial", "painel_gestao"),
        ("Central da Frota", "painel_frota"),
        ("Check-list", "checklist_frota"),
        ("Pedido de caminhões", "solicitacao_caminhoes"),
        ("Fila de carregamento", "lojas_prontas_carregamento"),
        ("Faturamento", "faturamento_expedicao"),
        ("Pallets por CD", "relatorio_paletes_cd"),
        ("Pallets da rede", "paletes_rede"),
        ("Férias", "ferias_colaboradores"),
        ("Gestão de acesso", "gestao_acesso"),
    ]
    for label, perm in visibility_rules:
        if perm in perms:
            visible.append(label)
    visible = visible[:6]

    warnings = []
    if not user.is_active:
        warnings.append("Login pausado")
    if "painel" not in perms:
        warnings.append("Sem acesso ao início")
    if "faturamento_expedicao" in perms and "lojas_prontas_carregamento" not in perms:
        warnings.append("Faturamento sem lojas prontas")
    if "solicitacao_caminhoes" in perms and "tratar_solicitacao_caminhoes" not in perms and area_key == "frota":
        warnings.append("Frota sem permissão para responder pedidos")
    if profile.cd_padrao not in {"801", "806", UNIFIED_CD}:
        warnings.append("CD padrão indefinido")

    return {
        "user": user,
        "profile": profile,
        "area": area,
        "area_key": area_key,
        "cd_label": cd_label,
        "visible": visible,
        "warnings": warnings,
        "permission_count": len(perms),
        "search": " ".join(
            [
                user.username,
                user.get_full_name(),
                profile.get_cargo_display(),
                area,
                cd_label,
                "ativo" if user.is_active else "pausado",
            ]
        ).lower(),
    }

def terminate_user_sessions(user):
    for session in Session.objects.filter(expire_date__gte=timezone.now()):
        data = session.get_decoded()
        if str(data.get("_auth_user_id")) == str(user.pk):
            session.delete()


def active_user_sessions():
    sessions = []
    users_by_id = User.objects.in_bulk()
    for session in Session.objects.filter(expire_date__gte=timezone.now()).order_by("-expire_date"):
        data = session.get_decoded()
        user_id = data.get("_auth_user_id")
        if not user_id:
            continue
        user = users_by_id.get(int(user_id)) if str(user_id).isdigit() else None
        if not user:
            continue
        sessions.append(
            {
                "usuario": user.get_full_name() or user.username,
                "login": user.username,
                "rota": data.get("ultima_rota", ""),
                "cd": data.get("cd_unidade_atual", ""),
                "ultimo_acesso": data.get("ultimo_acesso", ""),
                "expira_em": session.expire_date,
            }
        )
    return sessions


def recent_access_logs(limit=30):
    return AuditLog.objects.select_related("user").filter(acao__in=["login", "logout"]).order_by("-criado_em")[:limit]


def would_block_last_active_master(user, new_active):
    profile = ensure_profile(user)
    if new_active or profile.cargo != "master":
        return False
    active_masters = User.objects.filter(is_active=True, perfil_krill__cargo="master").count()
    return active_masters <= 1


def parse_iso_date(value, fallback=None):
    if not value:
        return fallback or timezone.localdate()
    try:
        return timezone.datetime.strptime(str(value), "%Y-%m-%d").date()
    except ValueError:
        return fallback or timezone.localdate()


def dashboard_counts(cd, data_obj):
    cache_key = ("dashboard_counts", str(cd), data_obj.isoformat())

    def build():
        tomorrow = data_obj + timezone.timedelta(days=1)
        agenda = cd_queryset(RecebimentoAgenda, cd).filter(data__in=[data_obj, tomorrow]).aggregate(
            hoje_total=Count("id", filter=Q(data=data_obj)),
            hoje_agendados=Count("id", filter=Q(data=data_obj, agendado=True)),
            hoje_nao_agendados=Count("id", filter=Q(data=data_obj, agendado=False)),
            amanha_total=Count("id", filter=Q(data=tomorrow)),
        )
        recebimentos = cd_queryset(Recebimento, cd).filter(data=data_obj).aggregate(
            total=Count("id"),
            paletes=Sum("paletes"),
        )
        checklists = cd_queryset(ChecklistFrota, cd).filter(data=data_obj).aggregate(
            total=Count("id"),
            saidas=Count("id", filter=Q(tipo_checklist="saida")),
            retornos=Count("id", filter=Q(tipo_checklist="retorno")),
            manutencoes=Count("id", filter=Q(necessita_manutencao=True)),
        )
        return {
            "tomorrow": tomorrow,
            "agenda_hoje_total": agenda["hoje_total"] or 0,
            "agenda_hoje_agendados": agenda["hoje_agendados"] or 0,
            "agenda_hoje_nao_agendados": agenda["hoje_nao_agendados"] or 0,
            "agenda_amanha_total": agenda["amanha_total"] or 0,
            "recebimentos_total": recebimentos["total"] or 0,
            "recebimentos_paletes": recebimentos["paletes"] or 0,
            "prestadores_total": cd_queryset(Prestador, cd).filter(data=data_obj).count(),
            "equipamentos_em_uso": cd_queryset(Equipamento, cd).filter(status="em_uso").count(),
            "materiais_pedido": cd_queryset(MaterialConsumo, cd).filter(precisa_pedir=True).count(),
            "divergencias": cd_queryset(Conferencia, cd).filter(data=data_obj).exclude(diferenca=0).count(),
            "avarias_abertas": cd_queryset(Avaria, cd).exclude(status="concluido").count(),
            "chamados_abertos": cd_queryset(ChamadoSaldo, cd).exclude(status="concluido").count(),
            "unidades_separadas": cd_queryset(Separacao, cd).filter(data=data_obj).aggregate(total=Sum("unidades"))["total"] or 0,
            "paletes_expedidos": cd_queryset(Expedicao, cd).filter(data=data_obj).aggregate(total=Sum("qtd_paletes"))["total"] or 0,
            "pendencias_abertas": cd_queryset(Pendencia, cd).exclude(status="concluido").count(),
            "checklists_total": checklists["total"] or 0,
            "checklists_saidas": checklists["saidas"] or 0,
            "checklists_retornos": checklists["retornos"] or 0,
            "checklists_manutencoes": checklists["manutencoes"] or 0,
            "frota_intercalacoes_pendentes": (
                cd_queryset(ChecklistFrota, cd).filter(intercala_cd=True, intercalacao_concluida=False).count()
                if system_rule_enabled("checklist_intercalacao_manual")
                else 0
            ),
            "backup_ok": BackupLog.objects.filter(status="ok", criado_em__date=data_obj).exists(),
        }

    return cached_runtime_value(cache_key, 30, build)


def operational_overview(cd, data):
    data_obj = parse_iso_date(data)
    cache_key = ("operational_overview", str(cd), data_obj.isoformat())

    def build():
        counts = dashboard_counts(cd, data_obj)
        tomorrow = counts["tomorrow"]
        recebimento_count = counts["recebimentos_total"]
        agenda_hoje_count = counts["agenda_hoje_total"]
        agenda_hoje_agendada = bool(counts["agenda_hoje_agendados"])
        agenda_amanha_count = counts["agenda_amanha_total"]
        unidades_separadas = counts["unidades_separadas"]
        paletes_expedidos = counts["paletes_expedidos"]
        prestadores_count = counts["prestadores_total"]
        pendencias_count = counts["pendencias_abertas"]
        materiais_count = counts["materiais_pedido"]
        avarias_count = counts["avarias_abertas"]
        chamados_count = counts["chamados_abertos"]
        divergencias_count = counts["divergencias"]
        backup_dia = counts["backup_ok"]

        checklist = [
            {
                "item": "Recebimentos lançados",
                "ok": bool(recebimento_count) or not agenda_hoje_count,
                "detalhe": f"{recebimento_count} NFs recebidas no dia",
            },
            {
                "item": "Separação lançada",
                "ok": bool(unidades_separadas),
                "detalhe": f"{unidades_separadas} unidades separadas",
            },
            {
                "item": "Expedição lançada",
                "ok": bool(paletes_expedidos),
                "detalhe": f"{paletes_expedidos} paletes expedidos",
            },
            {
                "item": "Agenda de recebimento do dia seguinte",
                "ok": bool(agenda_amanha_count),
                "detalhe": f"{agenda_amanha_count} veículos informados para {tomorrow:%d/%m/%Y}",
            },
            {
                "item": "Prestadores conferidos",
                "ok": True,
                "detalhe": f"{prestadores_count} prestadores registrados",
            },
            {
                "item": "Pendências revisadas",
                "ok": not pendencias_count,
                "detalhe": f"{pendencias_count} pendências abertas",
            },
            {
                "item": "Backup do dia",
                "ok": backup_dia,
                "detalhe": "Backup OK encontrado" if backup_dia else "Nenhum backup OK registrado hoje",
            },
        ]

        alertas = []
        if materiais_count:
            alertas.append({"tipo": "Materiais", "detalhe": f"{materiais_count} itens abaixo do mínimo"})
        if avarias_count:
            alertas.append({"tipo": "Avarias", "detalhe": f"{avarias_count} avarias ainda abertas"})
        if chamados_count:
            alertas.append({"tipo": "GLPI", "detalhe": f"{chamados_count} chamados de saldo em acompanhamento"})
        if divergencias_count:
            alertas.append({"tipo": "Conferência", "detalhe": f"{divergencias_count} divergências no dia"})
        if pendencias_count:
            alertas.append({"tipo": "Pendências", "detalhe": f"{pendencias_count} itens pendentes"})
        if agenda_hoje_agendada and not recebimento_count:
            alertas.append({"tipo": "Recebimento", "detalhe": "Existe agenda para hoje sem recebimento lançado"})
        for row in ferias_alertas(cd)[:3]:
            alertas.append({"tipo": "Férias", "detalhe": f"{row.colaborador}: {row.get_status_display()} - retorno {row.retorno_previsto:%d/%m/%Y}"})
        return {"checklist": checklist, "alertas": alertas}

    return cached_runtime_value(cache_key, 30, build)


def alert_permissions(tipo):
    tipo_base = str(tipo or "").lower()
    if "material" in tipo_base:
        return {"materiais"}
    if "avaria" in tipo_base:
        return {"avarias"}
    if "glpi" in tipo_base or "saldo" in tipo_base:
        return {"chamados_saldo"}
    if "confer" in tipo_base:
        return {"conferencias"}
    if "pend" in tipo_base:
        return {"pendencias"}
    if "receb" in tipo_base:
        return {"recebimento_agenda", "recebimentos"}
    if "frota" in tipo_base:
        return {"checklist_frota", "painel_frota"}
    if "férias" in tipo_base or "ferias" in tipo_base:
        return {"ferias_colaboradores", "alertas_ferias", "mapa_calor_ferias"}
    if "equip" in tipo_base:
        return {"equipamentos", "manutencao_equipamentos"}
    if "pessoa" in tipo_base:
        return {"pessoas_turno", "funcoes_turno", "ferias_colaboradores"}
    if "ocorr" in tipo_base:
        return {"ocorrencias"}
    return set()


def alert_target_url(tipo):
    tipo_base = str(tipo or "").lower()
    if "material" in tipo_base:
        return reverse("module_list", args=["materiais"])
    if "avaria" in tipo_base:
        return reverse("module_list", args=["avarias"])
    if "glpi" in tipo_base or "saldo" in tipo_base:
        return reverse("module_list", args=["chamados_saldo"])
    if "confer" in tipo_base:
        return reverse("module_list", args=["conferencias"])
    if "pend" in tipo_base:
        return reverse("module_list", args=["pendencias"])
    if "receb" in tipo_base:
        return reverse("module_list", args=["recebimentos"])
    if "frota" in tipo_base:
        return url_with_query(reverse("module_list", args=["checklist_frota"]), {"painel": "frota"})
    if "férias" in tipo_base or "ferias" in tipo_base:
        return reverse("module_list", args=["ferias_colaboradores"])
    if "equip" in tipo_base:
        return reverse("module_list", args=["equipamentos"])
    if "pessoa" in tipo_base:
        return reverse("module_list", args=["pessoas_turno"])
    if "ocorr" in tipo_base:
        return reverse("module_list", args=["ocorrencias"])
    return ""


def can_view_dashboard_area(user, permissions):
    return any(user_has_perm(user, perm) for perm in permissions)


def dashboard_value(user, permissions, resolver):
    if not can_view_dashboard_area(user, permissions):
        return None
    return resolver()


def build_alert_center(user, cd, data_obj, alertas_operacionais):
    items = []
    for alerta in alertas_operacionais:
        tipo = alerta.get("tipo") or "Operação"
        if not can_view_dashboard_area(user, alert_permissions(tipo)):
            continue
        items.append(
            {
                "nivel": "media",
                "area": tipo,
                "titulo": tipo,
                "texto": alerta.get("detalhe") or "",
                "data": data_obj,
                "url": alert_target_url(tipo),
            }
        )
    for alerta in alertas_inteligentes(cd, data_obj):
        area = alerta.get("area") or "Operação"
        if not can_view_dashboard_area(user, alert_permissions(area)):
            continue
        nivel = alerta.get("nivel") or "media"
        items.append(
            {
                "nivel": nivel,
                "area": area,
                "titulo": area,
                "texto": alerta.get("texto") or "",
                "data": data_obj,
                "url": alert_target_url(area),
            }
        )

    unique = []
    seen = set()
    for item in items:
        key = (item["area"], item["texto"])
        if key in seen:
            continue
        seen.add(key)
        unique.append(item)

    priority = {"critica": 0, "alta": 1, "media": 2, "normal": 3}
    unique.sort(key=lambda item: (priority.get(item["nivel"], 2), item["area"], item["texto"]))
    return unique[:12]


def lock_key(module_key, pk):
    return f"edit_lock:{module_key}:{pk}"


def get_edit_lock(module_key, pk):
    cfg = ConfiguracaoSistema.objects.filter(chave=lock_key(module_key, pk)).first()
    if not cfg or not cfg.valor:
        return None
    try:
        payload = json.loads(cfg.valor)
        updated = timezone.datetime.fromisoformat(payload.get("em", ""))
        if timezone.is_naive(updated):
            updated = timezone.make_aware(updated)
    except Exception:
        cfg.delete()
        return None
    if timezone.now() - updated > timezone.timedelta(minutes=15):
        cfg.delete()
        return None
    return payload


def acquire_edit_lock(request, module_key, pk):
    current = get_edit_lock(module_key, pk)
    if current and str(current.get("user_id")) != str(request.user.pk):
        return current
    payload = {
        "user_id": request.user.pk,
        "usuario": request.user.get_full_name() or request.user.username,
        "em": timezone.now().isoformat(),
    }
    ConfiguracaoSistema.objects.update_or_create(
        chave=lock_key(module_key, pk),
        defaults={
            "valor": json.dumps(payload, ensure_ascii=False),
            "descricao": "Trava temporária de edição de registro",
            "atualizado_por": request.user,
        },
    )
    return None


def release_edit_lock(request, module_key, pk):
    current = get_edit_lock(module_key, pk)
    if current and str(current.get("user_id")) == str(request.user.pk):
        ConfiguracaoSistema.objects.filter(chave=lock_key(module_key, pk)).delete()


def serializable_dict(obj):
    data = model_to_dict(obj)
    clean = {}
    for key, value in data.items():
        if hasattr(value, "isoformat"):
            clean[key] = value.isoformat()
        else:
            clean[key] = str(value) if value is not None else ""
    return clean


def log_action(request, acao, modulo="", objeto=None, detalhe="", cd_unidade="", antes=None, depois=None):
    user = request.user if request.user.is_authenticated else None
    AuditLog.objects.create(
        user=user,
        usuario_nome=user.get_full_name() or user.username if user else "",
        acao=acao,
        modulo=modulo,
        cd_unidade=cd_unidade or (current_cd(request) if user else ""),
        objeto_id=str(objeto.pk) if objeto else "",
        detalhe=detalhe[:2000],
        dados_antes=antes or {},
        dados_depois=depois or (serializable_dict(objeto) if objeto else {}),
        computador=socket.gethostname(),
        ip=request.META.get("REMOTE_ADDR", ""),
    )


def default_login_redirect(user):
    profile = ensure_profile(user)
    if user_has_perm(user, "sentinela_servidor") and not user_has_perm(user, "painel"):
        return redirect("sentinela_servidor")
    if profile and profile.cargo == "motorista" and user_has_perm(user, "checklist_frota"):
        return redirect("module_list", key="checklist_frota")
    return redirect("dashboard")


def login_view(request):
    if request.user.is_authenticated:
        return default_login_redirect(request.user)
    next_url = request.GET.get("next") or request.POST.get("next") or ""
    if request.method == "POST":
        username = request.POST.get("username", "").strip()
        password = request.POST.get("password", "")
        user = authenticate(request, username=username, password=password)
        if user and user.is_active:
            login(request, user)
            ensure_profile(user)
            log_action(request, "login", "acesso", detalhe="Entrada no sistema")
            profile = ensure_profile(user)
            if profile and profile.cargo == "motorista":
                request.session.set_expiry(settings.MOTORISTA_SESSION_AGE_SECONDS)
                return default_login_redirect(user)
            if next_url and url_has_allowed_host_and_scheme(next_url, allowed_hosts={request.get_host()}):
                return redirect(next_url)
            return default_login_redirect(user)
        messages.error(request, "Login ou senha inválido.")
    return render(request, "painel/login.html", {"next_url": next_url})


def solicitar_senha(request):
    if request.user.is_authenticated:
        return redirect("dashboard")
    form = SolicitacaoSenhaForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        login_informado = form.cleaned_data["login"].strip()
        usuario = User.objects.filter(username__iexact=login_informado).first()
        if not usuario:
            messages.error(request, "Login nao encontrado. Para novo cadastro ou reativacao, use Solicitar acesso.")
            return redirect("login")
        filtro = Q(status="pendente", login_informado__iexact=login_informado)
        filtro |= Q(status="pendente", usuario=usuario)
        solicitacao = SolicitacaoSenha.objects.filter(filtro).first()
        if not solicitacao:
            solicitacao = SolicitacaoSenha.objects.create(
                usuario=usuario,
                login_informado=login_informado,
                observacao="" if usuario else "Login não encontrado automaticamente.",
            )
            log_action(
                request,
                "solicitacao_senha",
                "acesso",
                detalhe=f"Pedido de redefinição para {login_informado}",
            )
        messages.success(request, "Pedido enviado. Aguarde o master redefinir sua senha.")
        return redirect("login")
    return render(request, "painel/solicitar_senha.html", {"form": form})


def solicitar_acesso(request):
    form = SolicitacaoAcessoForm(request.POST or None, user=request.user if request.user.is_authenticated else None)
    if request.method == "POST" and form.is_valid():
        cd = form.cleaned_data["cd_unidade"] or (current_cd(request) if request.user.is_authenticated else "")
        pedido = SolicitacaoAcesso.objects.create(
            usuario=request.user if request.user.is_authenticated else None,
            tipo=form.cleaned_data["tipo"],
            nome=form.cleaned_data["nome"].strip(),
            login_desejado=form.cleaned_data["login_desejado"].strip(),
            cd_unidade=cd,
            cargo=form.cleaned_data["cargo"].strip(),
            setor=form.cleaned_data["setor"].strip(),
            contato=form.cleaned_data["contato"].strip(),
            justificativa=form.cleaned_data["justificativa"].strip(),
        )
        log_action(
            request,
            "solicitacao_acesso",
            "gestao_acesso",
            detalhe=f"{pedido.get_tipo_display()} - {pedido.nome} - {pedido.login_desejado}",
        )
        messages.success(request, "Pedido de acesso enviado. Aguarde o retorno da gestão de acesso.")
        if request.user.is_authenticated:
            return redirect("minhas_solicitacoes")
        return redirect("login")
    template = "painel/solicitar_acesso_logado.html" if request.user.is_authenticated else "painel/solicitar_acesso.html"
    ctx = context_base(request) if request.user.is_authenticated else {}
    ctx.update({"form": form})
    return render(request, template, ctx)


def logout_view(request):
    if request.user.is_authenticated:
        log_action(request, "logout", "acesso", detalhe="Saída do sistema")
    logout(request)
    return redirect("login")


@login_required
def modo_demonstracao(request):
    if not user_has_perm(request.user, "modo_demonstracao"):
        messages.error(request, "Seu usuario nao tem permissao para usar o modo demonstracao.")
        return redirect("dashboard")

    action = request.GET.get("acao")
    next_url = request.GET.get("next") or request.META.get("HTTP_REFERER") or reverse("dashboard")
    if not url_has_allowed_host_and_scheme(
        next_url,
        allowed_hosts={request.get_host()},
        require_https=request.is_secure(),
    ):
        next_url = reverse("dashboard")

    response = redirect(next_url)
    if action == "desativar":
        response.delete_cookie("modo_demo", path="/", samesite="Lax")
        messages.success(request, "Modo demonstracao desativado.")
    else:
        response.set_cookie(
            "modo_demo",
            "1",
            max_age=60 * 60 * 8,
            path="/",
            secure=request.is_secure(),
            samesite="Lax",
        )
        messages.success(request, "Modo demonstracao ativado. As acoes serao simuladas e nao gravarao no banco real.")
    return response


def csrf_failure(request, reason=""):
    if request.headers.get("X-Requested-With") == "XMLHttpRequest":
        return JsonResponse(
            {
                "ok": False,
                "error": "csrf_expirado",
                "message": "A sessão foi atualizada. Recarregue a tela e tente novamente.",
            },
            status=403,
        )
    return redirect("login")


@login_required
def sentinela_servidor(request):
    if not user_has_perm(request.user, "sentinela_servidor"):
        messages.error(request, "Seu usuário não tem permissão para abrir a Sentinela do servidor.")
        return redirect("dashboard")
    ctx = context_base(request)
    ctx.update(
        {
            "ping_interval_seconds": 180,
            "server_time": timezone.localtime(),
        }
    )
    return render(request, "painel/sentinela_servidor.html", ctx)


@ensure_csrf_cookie
def app_status(request):
    return JsonResponse({"version": settings.APP_VERSION, "authenticated": request.user.is_authenticated})


@ensure_csrf_cookie
def keepalive(request):
    return JsonResponse({"ok": True, "version": settings.APP_VERSION, "server_time": timezone.localtime().isoformat()})


@never_cache
def service_worker(request):
    response = render(request, "painel/service_worker.js", {"app_version": settings.APP_VERSION}, content_type="application/javascript")
    response["Service-Worker-Allowed"] = "/"
    response["Cache-Control"] = "no-cache, no-store, must-revalidate"
    return response


@never_cache
def pwa_manifest(request):
    return render(request, "painel/manifest.webmanifest", content_type="application/manifest+json")


@never_cache
def pwa_icon(request, filename):
    allowed = {
        "apple-touch-icon.png": "gestao-cd-192.png",
        "apple-touch-icon-precomposed.png": "gestao-cd-192.png",
        "favicon.ico": "gestao-cd-192.png",
    }
    icon_name = allowed.get(filename)
    if not icon_name:
        return HttpResponse(status=404)
    candidates = [
        settings.BASE_DIR / "static" / "painel" / "img" / icon_name,
        settings.BASE_DIR / "painel" / "static" / "painel" / "img" / icon_name,
    ]
    icon_path = next((path for path in candidates if path.exists()), None)
    if not icon_path:
        return HttpResponse(status=404)
    response = FileResponse(icon_path.open("rb"), content_type="image/png")
    response["Cache-Control"] = "no-cache, no-store, must-revalidate"
    return response


@login_required
def push_public_key(request):
    return JsonResponse({"enabled": webpush_is_enabled(), "public_key": webpush_public_key()})


@login_required
@require_POST
def push_status(request):
    try:
        payload = json.loads(request.body.decode("utf-8") or "{}")
    except (TypeError, ValueError):
        payload = {}
    endpoint = payload.get("endpoint", "")
    current_public_key = webpush_public_key()
    subscription = None
    if endpoint:
        subscription = PushSubscription.objects.filter(endpoint=endpoint, usuario=request.user).first()
        if subscription and subscription.ativo and subscription.vapid_public_key != current_public_key:
            subscription.ativo = False
            subscription.ultimo_erro = "Assinatura Push usa chave antiga. Ative notificações novamente neste aparelho."
            subscription.save(update_fields=["ativo", "ultimo_erro", "atualizado_em"])
    active = bool(
        subscription
        and subscription.ativo
        and subscription.vapid_public_key == current_public_key
        and webpush_is_enabled()
    )
    return JsonResponse(
        {
            "ok": True,
            "active": active,
            "enabled": webpush_is_enabled(),
            "has_subscription": bool(subscription),
            "last_error": subscription.ultimo_erro if subscription else "",
        }
    )


@login_required
@require_POST
def push_subscribe(request):
    try:
        payload = json.loads(request.body.decode("utf-8") or "{}")
    except (TypeError, ValueError):
        payload = {}
    endpoint = payload.get("endpoint", "")
    keys = payload.get("keys") or {}
    p256dh = keys.get("p256dh", "")
    auth = keys.get("auth", "")
    if not endpoint or not p256dh or not auth:
        return JsonResponse({"ok": False, "error": "subscription_invalida"}, status=400)
    PushSubscription.objects.update_or_create(
        endpoint=endpoint,
        defaults={
            "usuario": request.user,
            "p256dh": p256dh,
            "auth": auth,
            "vapid_public_key": webpush_public_key(),
            "user_agent": request.META.get("HTTP_USER_AGENT", "")[:1000],
            "ativo": True,
            "ultimo_erro": "",
        },
    )
    return JsonResponse({"ok": True})


@login_required
@require_POST
def push_unsubscribe(request):
    try:
        payload = json.loads(request.body.decode("utf-8") or "{}")
    except (TypeError, ValueError):
        payload = {}
    endpoint = payload.get("endpoint", "")
    if endpoint:
        PushSubscription.objects.filter(endpoint=endpoint, usuario=request.user).update(ativo=False)
    return JsonResponse({"ok": True})


@login_required
@require_POST
def push_test(request):
    if not can_manage_push(request.user):
        return JsonResponse({"ok": False, "message": "Seu usuário não tem permissão para enviar teste Push."}, status=403)
    test_started_at = timezone.now() - timedelta(seconds=2)
    before_count = PushSubscription.objects.filter(usuario=request.user, ativo=True).count()
    notification = create_system_notification(
        request.user,
        "Teste de notificação",
        "Se esta mensagem apareceu fora do sistema, o Push está funcionando neste aparelho.",
        url=reverse("dashboard"),
        categoria="teste_push",
        cd_unidade=current_cd(request),
    )
    sent = bool(notification.enviada_push_em)
    active_subscriptions = PushSubscription.objects.filter(usuario=request.user, ativo=True)
    last_error = (
        PushSubscription.objects.filter(usuario=request.user)
        .filter(atualizado_em__gte=test_started_at)
        .exclude(ultimo_erro="")
        .order_by("-atualizado_em")
        .values_list("ultimo_erro", flat=True)
        .first()
        or ""
    )
    if sent:
        message = "Push enviado para este usuário."
    elif not webpush_is_enabled():
        message = "Web Push não está habilitado no servidor."
    elif before_count == 0 and not active_subscriptions.exists():
        message = "Nenhum aparelho registrado. Toque em ativar notificações neste aparelho."
    elif last_error:
        message = f"Aparelho registrado, mas o envio foi recusado: {last_error[:180]}"
    else:
        message = "Notificação interna criada, mas nenhum aparelho confirmou o recebimento Push."
    return JsonResponse(
        {
            "ok": True,
            "push_sent": sent,
            "active_subscriptions": active_subscriptions.count(),
            "webpush_enabled": webpush_is_enabled(),
            "message": message,
        }
    )


@login_required
def notificacoes_push(request):
    if not can_manage_push(request.user):
        messages.error(request, "Seu usuário não tem permissão para gerenciar notificações Push.")
        return redirect("dashboard")

    if request.method == "POST":
        action = request.POST.get("action", "")
        if action == "reset_keys":
            public_key, private_key = generate_vapid_pair()
            if public_key and private_key:
                set_config("webpush_vapid_public_key", public_key, request.user, "Chave publica Web Push redefinida")
                set_config("webpush_vapid_private_key", private_key, request.user, "Chave privada Web Push redefinida")
                set_config("webpush_vapid_version", WEBPUSH_KEY_VERSION, request.user, "Versao das chaves Web Push")
                _WEBPUSH_KEY_CACHE.update({"version": WEBPUSH_KEY_VERSION, "public": public_key, "private": private_key})
                PushSubscription.objects.update(ativo=False, ultimo_erro="Assinatura invalidada por redefinição manual de chave Push.")
                messages.success(request, "Chaves Push redefinidas. Os usuários precisarão ativar notificações novamente no aparelho.")
            else:
                messages.error(request, "Não foi possível gerar novas chaves Push.")
            return redirect("notificacoes_push")

        if action == "disable_subscription":
            subscription = PushSubscription.objects.select_related("usuario").filter(pk=request.POST.get("subscription_id")).first()
            if subscription:
                subscription.ativo = False
                subscription.ultimo_erro = f"Desvinculado por {request.user.get_full_name() or request.user.username}."
                subscription.save(update_fields=["ativo", "ultimo_erro", "atualizado_em"])
                messages.success(request, f"Aparelho de {subscription.usuario.get_full_name() or subscription.usuario.username} desvinculado.")
            else:
                messages.error(request, "Aparelho não encontrado.")
            return redirect("notificacoes_push")

        targets = []
        if action == "test_self":
            targets = [request.user]
        elif action == "test_user":
            user_id = request.POST.get("user_id")
            targets = list(User.objects.filter(pk=user_id, is_active=True))
        elif action == "test_all":
            targets = list(User.objects.filter(is_active=True, push_subscriptions__ativo=True).distinct())

        if targets:
            sent = 0
            for user in targets:
                notification = create_system_notification(
                    user,
                    "Modelo de Teste",
                    "Teste enviado pelo painel master do Modelo de Teste.",
                    url=reverse("dashboard"),
                    categoria="teste_push",
                    cd_unidade=current_cd(request),
                )
                if notification and notification.enviada_push_em:
                    sent += 1
            messages.success(request, f"Teste processado para {len(targets)} usuário(s). Push enviado para {sent}.")
        else:
            messages.error(request, "Nenhum usuário ativo com assinatura Push foi encontrado para este teste.")
        return redirect("notificacoes_push")

    ctx = context_base(request)
    subscriptions = PushSubscription.objects.select_related("usuario").order_by("-atualizado_em")[:200]
    ctx.update(
        {
            "title": "Notificações Push",
            "webpush_enabled": webpush_is_enabled(),
            "push_public_key": webpush_public_key(),
            "subscriptions": subscriptions,
            "active_push_users": PushSubscription.objects.filter(ativo=True).values("usuario_id").distinct().count(),
            "users": User.objects.filter(is_active=True).order_by("first_name", "username"),
        }
    )
    return render(request, "painel/notificacoes_push.html", ctx)


@login_required
@require_POST
def notification_read(request):
    try:
        payload = json.loads(request.body.decode("utf-8") or "{}")
    except (TypeError, ValueError):
        payload = {}
    notification_id = payload.get("notification_id")
    qs = SistemaNotificacao.objects.filter(usuario=request.user, lida_em__isnull=True)
    if notification_id:
        qs = qs.filter(pk=notification_id)
    qs.update(lida_em=timezone.now())
    if not notification_id:
        now = timezone.now()
        SolicitacaoSenha.objects.filter(usuario=request.user, lido_pelo_usuario_em__isnull=True).exclude(status="pendente").update(lido_pelo_usuario_em=now)
        SolicitacaoAcesso.objects.filter(usuario=request.user, lido_pelo_usuario_em__isnull=True).exclude(status="pendente").update(lido_pelo_usuario_em=now)
        MelhoriaSistema.objects.filter(criado_por=request.user, lido_pelo_solicitante_em__isnull=True).filter(Q(decisao__gt="") | ~Q(status="sugestao")).update(lido_pelo_solicitante_em=now)
        SolicitacaoCarregamentoManual.objects.filter(
            criado_por=request.user,
            lido_pelo_solicitante_em__isnull=True,
            status__in=["aprovado", "recusado", "cancelado", "concluido"],
        ).update(lido_pelo_solicitante_em=now)
    return JsonResponse({"ok": True})


def offline(request):
    return render(request, "painel/offline.html")


def health_status(request):
    db_status = database_status()
    return JsonResponse(
        {
            "ok": db_status["ok"],
            "database": db_status,
            "version": settings.APP_VERSION,
            "server_time": timezone.localtime().isoformat(),
            "computer": socket.gethostname(),
        }
    )


def diagnostics_report_text(snapshot, backups, usuarios_ativos):
    lines = [
        "Modelo de Teste - DIAGNOSTICO DO SISTEMA",
        f"Gerado em: {timezone.localtime(snapshot['gerado_em']):%d/%m/%Y %H:%M:%S}",
        f"Computador: {snapshot['computador']}",
        f"Sistema: {snapshot['sistema']}",
        f"Python: {snapshot['python']}",
        f"Versao do app: {snapshot['versao']}",
        f"Pasta do sistema: {snapshot['base_dir']}",
        "",
        "ENDERECOS DE ACESSO",
        *snapshot["urls"],
        "",
        "BANCO DE DADOS",
        f"Status: {'OK' if snapshot['banco']['ok'] else 'ERRO'}",
        f"Mensagem: {snapshot['banco']['mensagem']}",
        f"Banco: {snapshot['banco'].get('banco', '')}",
        f"Usuario: {snapshot['banco'].get('usuario', '')}",
        f"Porta Django: {snapshot['banco'].get('porta', '')}",
        f"PostgreSQL bin existe: {'sim' if snapshot['postgres']['bin_ok'] else 'não'}",
        f"PostgreSQL data existe: {'sim' if snapshot['postgres']['data_ok'] else 'não'}",
        f"PostgreSQL data: {snapshot['postgres']['data']}",
        "",
        "DISCO",
        f"Total: {snapshot['disco']['total_gb']} GB",
        f"Usado: {snapshot['disco']['usado_gb']} GB ({snapshot['disco']['percentual_usado']}%)",
        f"Livre: {snapshot['disco']['livre_gb']} GB",
        "",
        "PASTA .50",
        f"Configurada: {snapshot['pasta_50']['configurada'] or 'não configurada'}",
        f"Acessível: {'sim' if snapshot['pasta_50']['acessivel'] else 'não'}",
        f"Backup automatico: {snapshot['pasta_50']['backup_automatico']}",
        "",
        "USUARIOS ATIVOS",
    ]
    if usuarios_ativos:
        for item in usuarios_ativos:
            lines.append(f"- {item['usuario']} ({item['login']}) CD {item['cd']} rota {item['rota']}")
    else:
        lines.append("- Nenhum usuario ativo encontrado.")

    lines.extend(["", "ULTIMOS BACKUPS"])
    if backups:
        for backup in backups:
            lines.append(
                f"- {timezone.localtime(backup.criado_em):%d/%m/%Y %H:%M} | "
                f"{backup.get_destino_display()} | {backup.get_status_display()} | {backup.arquivo or backup.mensagem}"
            )
    else:
        lines.append("- Nenhum backup registrado.")

    for title, key in [("LOG DO ABRIDOR", "launcher"), ("LOG DO POSTGRESQL", "postgres"), ("LOG DO SERVIDOR", "servidor")]:
        lines.extend(["", title])
        values = snapshot["logs"].get(key) or []
        if values:
            lines.extend(values)
        else:
            lines.append("- Sem linhas recentes.")
    return "\n".join(str(line) for line in lines)


@login_required
def diagnostico(request):
    if not user_has_perm(request.user, "diagnostico"):
        messages.error(request, "Você não tem acesso ao diagnóstico do servidor.")
        return redirect("dashboard")

    snapshot = diagnostics_snapshot()
    backups = list(BackupLog.objects.all()[:10])
    usuarios_ativos = active_user_sessions()
    if request.GET.get("exportar") == "txt":
        response = HttpResponse(
            diagnostics_report_text(snapshot, backups, usuarios_ativos),
            content_type="text/plain; charset=utf-8",
        )
        filename = f"diagnostico_modelo_teste_{timezone.localtime():%Y%m%d_%H%M%S}.txt"
        response["Content-Disposition"] = f'attachment; filename="{filename}"'
        log_action(request, "diagnostico_txt", "central_servidor", detalhe="Relatorio de diagnostico exportado")
        return response

    ctx = context_base(request)
    ctx.update({"snapshot": snapshot, "backups": backups, "usuarios_ativos": usuarios_ativos})
    return render(request, "painel/diagnostico.html", ctx)


@login_required
def set_cd(request):
    profile = ensure_profile(request.user)
    if not user_has_perm(request.user, "trocar_cd"):
        if profile:
            profile_cd = normalized_profile_cd(profile)
            request.session["cd_unidade"] = PILOT_FOCUS_CD if profile_cd == UNIFIED_CD else profile_cd
        messages.error(request, "Seu usuário não tem permissão para trocar de CD.")
        return redirect(request.META.get("HTTP_REFERER") or "dashboard")
    cd = request.POST.get("cd_unidade") or request.GET.get("cd_unidade")
    if cd == UNIFIED_CD and can_view_unified_cd(request.user) and SHOW_UNIFIED_CD_IN_SELECTOR:
        request.session["cd_unidade"] = cd
        request.session["cd_unidade_manual"] = False
        log_action(request, "troca_cd", "sistema", detalhe="CD selecionado: 801-806", cd_unidade="")
    elif cd == UNIFIED_CD:
        request.session["cd_unidade"] = PILOT_FOCUS_CD
        request.session["cd_unidade_manual"] = False
        log_action(request, "troca_cd", "sistema", detalhe="Visao 801-806 redirecionada para CD 806", cd_unidade=PILOT_FOCUS_CD)
    elif cd in {"801", "806"}:
        request.session["cd_unidade"] = cd
        request.session["cd_unidade_manual"] = True
        log_action(request, "troca_cd", "sistema", detalhe=f"CD selecionado: {cd}", cd_unidade=cd)
    return redirect(request.META.get("HTTP_REFERER") or "dashboard")


@login_required
def dashboard(request):
    profile = ensure_profile(request.user)
    if user_has_perm(request.user, "sentinela_servidor") and not user_has_perm(request.user, "painel"):
        return redirect("sentinela_servidor")
    if profile and profile.cargo == "motorista" and user_has_perm(request.user, "checklist_frota"):
        return redirect("module_list", key="checklist_frota")
    cd = current_cd(request)
    backup_if_due(user=request.user)
    if network_backup_dir():
        try:
            publish_server_address(user=request.user)
        except Exception:
            pass
    data = request.GET.get("data") or timezone.localdate().isoformat()
    data_obj = parse_iso_date(data)
    metric_cache = {}

    def cached_metric(key, resolver):
        if key not in metric_cache:
            metric_cache[key] = resolver()
        return metric_cache[key]

    common_counts = cached_metric("dashboard_counts", lambda: dashboard_counts(cd, data_obj))
    card_specs = [
        ({"recebimento_agenda"}, "Veículos agendados", lambda: common_counts["agenda_hoje_agendados"]),
        ({"recebimento_agenda"}, "Veículos não agendados", lambda: common_counts["agenda_hoje_nao_agendados"]),
        ({"recebimentos"}, "NF recebidas", lambda: common_counts["recebimentos_total"]),
        ({"recebimentos"}, "Paletes recebidos", lambda: common_counts["recebimentos_paletes"]),
        ({"prestadores"}, "Prestadores no CD", lambda: common_counts["prestadores_total"]),
        ({"equipamentos"}, "Equipamentos em uso", lambda: common_counts["equipamentos_em_uso"]),
        ({"materiais"}, "Materiais para pedido", lambda: common_counts["materiais_pedido"]),
        ({"conferencias"}, "Divergências", lambda: common_counts["divergencias"]),
        ({"avarias"}, "Avarias abertas", lambda: common_counts["avarias_abertas"]),
        ({"chamados_saldo"}, "Chamados saldo", lambda: common_counts["chamados_abertos"]),
        ({"separacao"}, "Unidades separadas", lambda: common_counts["unidades_separadas"]),
        ({"expedicao"}, "Paletes expedidos", lambda: common_counts["paletes_expedidos"]),
        ({"pessoas_turno", "funcoes_turno"}, "Faltas no quadro", lambda: sum(item["falta"] for item in cached_metric("faltas", lambda: pessoas_faltando(cd, data_obj)))),
        ({"ferias_colaboradores", "mapa_calor_ferias"}, "Férias em alerta", lambda: len(cached_metric("ferias_alertas", lambda: ferias_alertas(cd)))),
        ({"checklist_frota", "painel_frota"}, "Check-lists frota", lambda: common_counts["checklists_total"]),
        ({"checklist_frota", "painel_frota"}, "Passagens pendentes", lambda: common_counts["frota_intercalacoes_pendentes"]),
    ]
    cards = []
    for permissions, label, resolver in card_specs:
        value = dashboard_value(request.user, permissions, resolver)
        if value is not None:
            cards.append((label, value))
    show_closure_panel = user_has_perm(request.user, "fechamentos") or user_has_perm(request.user, "relatorios")
    show_pendencias_panel = user_has_perm(request.user, "pendencias")
    show_alert_center = can_view_dashboard_area(
        request.user,
        {
            "materiais",
            "avarias",
            "chamados_saldo",
            "conferencias",
            "pendencias",
            "recebimento_agenda",
            "recebimentos",
            "checklist_frota",
            "painel_frota",
            "ferias_colaboradores",
            "alertas_ferias",
            "mapa_calor_ferias",
            "equipamentos",
            "manutencao_equipamentos",
            "ocorrencias",
            "pessoas_turno",
            "funcoes_turno",
        },
    )
    pendencias = MODULE_BY_KEY["pendencias"].model.objects.filter(cd_unidade=cd).exclude(status="concluido")[:8] if show_pendencias_panel else []
    overview = operational_overview(cd, data) if (show_closure_panel or show_alert_center) else {"checklist": [], "alertas": []}
    central_alertas = build_alert_center(request.user, cd, data_obj, overview["alertas"]) if show_alert_center else []
    show_colaboradores_panel = can_view_dashboard_area(request.user, {"pessoas_turno", "funcoes_turno", "ferias_colaboradores", "mapa_calor_ferias", "alertas_ferias"})
    show_frota_panel = can_view_dashboard_area(request.user, {"checklist_frota", "painel_frota", "materiais_frota", "lacres_frota"})
    colaboradores_panel = {
        "ferias": cached_metric("ferias_alertas", lambda: ferias_alertas(cd)) if can_view_dashboard_area(request.user, {"ferias_colaboradores", "mapa_calor_ferias", "alertas_ferias"}) else [],
        "faltas": cached_metric("faltas", lambda: pessoas_faltando(cd, data_obj)) if user_has_perm(request.user, "pessoas_turno") else [],
        "funcoes": cd_queryset(FuncaoTurno, cd).filter(data=data_obj).order_by("setor", "turno", "nome")[:8] if user_has_perm(request.user, "funcoes_turno") else [],
        "heatmap": cached_metric("heatmap_ferias", lambda: build_ferias_heatmap(cd)) if user_has_perm(request.user, "mapa_calor_ferias") else None,
    }
    frota_panel = {
        "checklists": common_counts["checklists_total"] if show_frota_panel else 0,
        "saidas": common_counts["checklists_saidas"] if show_frota_panel else 0,
        "retornos": common_counts["checklists_retornos"] if show_frota_panel else 0,
        "retornos_pendentes": cached_metric("frota_retorno_pendente", lambda: frota_open_departures_queryset(cd).count()) if show_frota_panel else 0,
        "manutencoes": common_counts["checklists_manutencoes"] if show_frota_panel else 0,
        "intercalacoes": common_counts["frota_intercalacoes_pendentes"] if show_frota_panel else 0,
        "show_intercalacoes": system_rule_enabled("checklist_intercalacao_manual"),
    }
    home_actions = []
    for hub in custom_hubs_for_user(request.user):
        home_actions.append(
            {
                "title": hub["title"],
                "description": hub["description"],
                "icon": hub["icon"],
                "url": reverse("central_personalizada", args=[hub["key"]]),
                "tone": "central",
            }
        )
    if can_open_frota_hub(request.user):
        home_actions.append(
            {
                "title": "Central da Frota",
                "description": "Check-list, disponibilidade, pedidos de caminhão e fila de carregamento.",
                "icon": "CF",
                "url": reverse("frota_hub"),
                "tone": "frota",
            }
        )
    if user_has_perm(request.user, "painel_gestao"):
        home_actions.append(
            {
                "title": "Painel Gerencial",
                "description": "Resumo de operação, frota, expedição e colaboradores para acompanhamento.",
                "icon": "PG",
                "url": reverse("painel_gestao"),
                "tone": "gerencial",
            }
        )
    if user_has_perm(request.user, "relatorios"):
        home_actions.append(
            {
                "title": "Relatórios",
                "description": "Consulta, impressão e análise histórica dos registros do sistema.",
                "icon": "RL",
                "url": reverse("relatorios"),
                "tone": "relatorio",
            }
        )
    seen_urls = set()
    home_actions = [
        action for action in home_actions
        if not (action["url"] in seen_urls or seen_urls.add(action["url"]))
    ]
    ctx = context_base(request)
    ctx.update(
        {
            "data": data,
            "cards": cards,
            "home_actions": home_actions,
            "pendencias": pendencias,
            "central_alertas": central_alertas,
            "show_report_panel": user_has_perm(request.user, "relatorios"),
            "show_closure_panel": show_closure_panel,
            "show_pendencias_panel": show_pendencias_panel,
            "show_colaboradores_panel": show_colaboradores_panel,
            "show_frota_panel": show_frota_panel,
            "colaboradores_panel": colaboradores_panel,
            "frota_panel": frota_panel,
            **overview,
        }
    )
    return render(request, "painel/dashboard.html", ctx)


@login_required
def frota_hub(request):
    if not can_open_frota_hub(request.user):
        messages.error(request, "Seu usuário não tem acesso à Central da Frota.")
        return redirect("dashboard")

    cd = current_cd(request)
    data_obj = parse_iso_date(request.GET.get("data"), timezone.localdate())
    can_unify = str(cd) == UNIFIED_CD and can_view_unified_cd(request.user)
    cds = ["801", "806"] if can_unify else [cd]
    link_query = urlencode({"data": data_obj.isoformat()})
    selected_store_query = link_query

    pendentes_retorno = frota_open_trip_data(cd, unificado=can_unify, limit=5)
    aprovacoes_pendentes = SolicitacaoCarregamentoManual.objects.filter(
        cd_unidade__in=cds,
        status="pendente",
    ).count()
    solicitacoes_caminhoes_pendentes = SolicitacaoCaminhaoCD.objects.filter(
        cd_unidade__in=(["801", "806"] if user_has_perm(request.user, "tratar_solicitacao_caminhoes") else cds),
        status="pendente",
    ).count()
    lojas_prontas_pendentes = SolicitacaoCargaPronta.objects.filter(
        cd_unidade__in=cds,
        data=data_obj,
        status="pronta",
    ).count()
    pallets_por_cd = []
    saldos = 0
    vinculados = 0
    for cd_item in cds:
        saldo_cd = sum(item.qtd_paletes for item in current_pallet_rows([cd_item]))
        vinculado_cd = ExpedicaoVinculo.objects.filter(
            cd_unidade=cd_item,
            status="vinculado",
        ).aggregate(total=Sum("qtd_paletes"))["total"] or 0
        livre_cd = max(saldo_cd - vinculado_cd, 0)
        saldos += saldo_cd
        vinculados += vinculado_cd
        pallets_por_cd.append(
            {
                "cd": cd_item,
                "saldo": saldo_cd,
                "vinculado": vinculado_cd,
                "livre": livre_cd,
            }
        )
    livres = max(saldos - vinculados, 0)
    checklists = ChecklistFrota.objects.filter(cd_unidade__in=cds, data=data_obj).count()
    manutencoes_checklist = ChecklistFrota.objects.filter(
        cd_unidade__in=cds,
        data=data_obj,
        necessita_manutencao=True,
    ).count()
    escalas_ativas = EscalaVeiculoFrota.objects.filter(
        cd_unidade__in=cds,
        ativo=True,
        inicio__lte=data_obj,
        fim__gte=data_obj,
    ).count()
    disponibilidade_frota = frota_availability_rows(cds)
    veiculos_disponiveis = sum(1 for row in disponibilidade_frota if row["available"])
    veiculos_indisponiveis = max(len(disponibilidade_frota) - veiculos_disponiveis, 0)

    sections = [
        {
            "title": "Carregamentos",
            "description": "Use aqui para decidir qual caminhão vai carregar e para onde ele deve ir.",
            "cards": [
                frota_hub_link(
                    "lojas_prontas_carregamento",
                    "Enviar caminhão para loja pronta",
                    "Ver lojas liberadas pela Expedição e escolher caminhão.",
                    "FC",
                    url_with_query(reverse("module_list", args=["lojas_prontas_carregamento"]), {"data": data_obj.isoformat(), "modo": "fila"}),
                    request.user,
                    {"acompanhar_lojas_prontas_carregamento", "vincular_cargas_expedicao"},
                    f"{lojas_prontas_pendentes} pronta(s)",
                ),
                frota_hub_link(
                    "solicitacao_caminhoes",
                    "Responder pedido de caminhões",
                    "Enviar um ou mais motoristas quando o CD solicitar caminhão.",
                    "PC",
                    reverse("module_list", args=["solicitacao_caminhoes"]),
                    request.user,
                    {"solicitacao_caminhoes", "tratar_solicitacao_caminhoes"},
                    f"{solicitacoes_caminhoes_pendentes} pendente(s)",
                ),
                frota_hub_link(
                    "disponibilidade_frota",
                    "Ver disponibilidade da Frota",
                    "Saber quem está disponível, ocupado, em entrega, limpeza ou manutenção.",
                    "DV",
                    "#disponibilidade-frota",
                    request.user,
                    {"painel_frota", "veiculos_frota", "vincular_cargas_expedicao"},
                    f"{veiculos_disponiveis} disponível(is)",
                ),
            ],
        },
        {
            "title": "Rotina da Frota",
            "description": "Acompanhe viagem, retorno e exceções que precisam de decisão.",
            "cards": [
                frota_hub_link(
                    "checklist_frota",
                    "Check-list do Motorista",
                    "Registrar saida do CD, chegada na loja, saida da loja e retorno.",
                    "CL",
                    reverse("module_list", args=["checklist_frota"]),
                    request.user,
                    {"checklist_frota"},
                ),
                frota_hub_link(
                    "painel_frota",
                    "Acompanhar viagens",
                    "Ver viagens iniciadas, retornos pendentes e ocorrencias.",
                    "AF",
                    url_with_query(reverse("module_list", args=["checklist_frota"]), {"painel": "frota"}),
                    request.user,
                    {"painel_frota", "editar_checklist_frota"},
                    f"{pendentes_retorno['total']} retorno(s)",
                ),
                frota_hub_link(
                    "aprovacoes_carregamento",
                    "Resolver exceções",
                    "Aprovar ou recusar pedidos manuais feitos pela Expedição.",
                    "AE",
                    reverse("module_list", args=["aprovacoes_carregamento"]),
                    request.user,
                    {"aprovacoes_carregamento", "aprovar_lancamento_manual_expedicao"},
                    f"{aprovacoes_pendentes} pendente(s)",
                ),
                frota_hub_link(
                    "checklist_frota_itens",
                    "Editar Check-list",
                    "Editar grupos, perguntas e regras do check-list dos motoristas.",
                    "EC",
                    reverse("module_list", args=["checklist_frota_itens"]),
                    request.user,
                    {"checklist_frota_itens", "editar_checklist_frota"},
                ),
            ],
        },
        {
            "title": "Consultas e Cadastros",
            "description": "Cadastros e consultas usadas para manter a Frota funcionando.",
            "cards": [
                frota_hub_link(
                    "expedicao_planejamento",
                    "Consultar saldo de pallets",
                    "Consultar ou corrigir pallets por loja e CD.",
                    "SP",
                    f"{reverse('module_list', args=['expedicao_planejamento'])}{selected_store_query}",
                    request.user,
                    {"expedicao_planejamento"},
                    f"{livres} pallet(s) livre(s)",
                ),
                frota_hub_link(
                    "relatorio_paletes_cd",
                    "Resumo de pallets da expedição",
                    "Consultar visão consolidada por loja e CD.",
                    "RP",
                    f"{reverse('module_list', args=['relatorio_paletes_cd'])}{link_query}",
                    request.user,
                    {"relatorio_paletes_cd"},
                ),
                frota_hub_link(
                    "veiculos_frota",
                    "Motoristas, placas e status",
                    "Cadastrar motoristas, placas, plataforma e disponibilidade.",
                    "MP",
                    reverse("module_list", args=["veiculos_frota"]),
                    request.user,
                    {"veiculos_frota"},
                ),
                frota_hub_link(
                    "escala_veiculos_frota",
                    "Escala de Motoristas",
                    "Informar motorista responsável por uma placa em troca, férias ou manutenção.",
                    "EM",
                    reverse("module_list", args=["escala_veiculos_frota"]),
                    request.user,
                    {"escala_veiculos_frota", "veiculos_frota"},
                    f"{escalas_ativas} troca(s) ativa(s)",
                ),
                frota_hub_link(
                    "materiais_frota",
                    "Materiais da Frota",
                    "Controlar materiais usados pela frota.",
                    "MF",
                    reverse("module_list", args=["materiais_frota"]),
                    request.user,
                    {"materiais_frota"},
                ),
                frota_hub_link(
                    "lacres_frota",
                    "Ocorrencias de Lacres",
                    "Registrar ocorrencias operacionais relacionadas a lacres.",
                    "LC",
                    reverse("module_list", args=["lacres_frota"]),
                    request.user,
                    {"lacres_frota"},
                ),
            ],
        },
    ]
    sections = [
        {**section, "cards": [card for card in section["cards"] if card]}
        for section in sections
    ]
    sections = [section for section in sections if section["cards"]]

    ctx = context_base(request)
    ctx.update(
        {
            "data": data_obj,
            "can_unify": can_unify,
            "frota_sections": sections,
            "frota_metrics": [
                ("Check-lists hoje", checklists),
                ("Retornos pendentes", pendentes_retorno["total"]),
                ("Pendências/manutenção", manutencoes_checklist),
                ("Veiculos disponiveis", veiculos_disponiveis),
                ("Veiculos indisponiveis", veiculos_indisponiveis),
                ("Pallets disponiveis", livres),
                ("Pallets reservados", vinculados),
                ("Lojas prontas", lojas_prontas_pendentes),
                ("Aprovações pendentes", aprovacoes_pendentes),
                ("Escalas ativas", escalas_ativas),
            ],
            "frota_pallets_por_cd": pallets_por_cd,
            "frota_disponibilidade": disponibilidade_frota,
            "veiculos_disponiveis": veiculos_disponiveis,
            "veiculos_indisponiveis": veiculos_indisponiveis,
            "frota_load_groups": frota_hub_load_groups(cd, data_obj, can_unify),
        }
    )
    return render(request, "painel/frota_hub.html", ctx)


@login_required
def colaboradores_hub(request):
    if not can_open_colaboradores_hub(request.user):
        messages.error(request, "Seu usuário não tem acesso à Gestão de Colaboradores.")
        return redirect("dashboard")

    cd = current_cd(request)
    hoje = timezone.localdate()
    config = ferias_alert_config()

    if request.method == "POST":
        action = request.POST.get("action")
        if action == "salvar_alertas_ferias":
            if not user_has_perm(request.user, "configurar_alertas_ferias"):
                messages.error(request, "Você não tem permissão para configurar alertas de férias.")
                return redirect("colaboradores_hub")
            config = save_ferias_alert_config(request)
            log_action(request, "ferias_alertas_config", "colaboradores_hub", detalhe="Configuração de alertas de férias atualizada")
            messages.success(request, "Configuração dos alertas salva.")
            return redirect("colaboradores_hub")
        if action == "processar_alertas_ferias":
            if not user_has_perm(request.user, "configurar_alertas_ferias"):
                messages.error(request, "Você não tem permissão para processar alertas de férias.")
                return redirect("colaboradores_hub")
            enviados = process_ferias_notifications(cd, config)
            log_action(request, "ferias_alertas_processados", "colaboradores_hub", detalhe=f"{enviados} notificação(ões) criada(s)")
            messages.success(request, f"Alertas processados. {enviados} notificação(ões) criada(s).")
            return redirect("colaboradores_hub")

    capacidade = build_capacidade_operacao(cd, config) if (
        user_has_perm(request.user, "capacidade_operacao")
        or user_has_perm(request.user, "visualizar_gargalos_colaboradores")
        or user_has_perm(request.user, "mapa_calor_ferias")
    ) else None
    ferias_rows = ferias_alertas(cd) if user_has_perm(request.user, "ferias_colaboradores") or user_has_perm(request.user, "alertas_ferias") else []
    heatmap = build_ferias_heatmap(cd) if user_has_perm(request.user, "mapa_calor_ferias") else None
    pessoas_hoje = cd_queryset(PessoaTurno, cd).filter(data=hoje) if user_has_perm(request.user, "pessoas_turno") else PessoaTurno.objects.none()
    funcoes_ativas = cd_queryset(FuncaoTurno, cd).filter(ativa=True).count() if user_has_perm(request.user, "funcoes_turno") else 0
    ferias_hoje = (
        cd_queryset(ColaboradorFerias, cd)
        .exclude(status__in=["cancelada", "retornado"])
        .filter(inicio_ferias__lte=hoje, fim_ferias__gte=hoje)
        .count()
    ) if user_has_perm(request.user, "ferias_colaboradores") else 0
    ausencias_hoje = (
        cd_queryset(ColaboradorAusencia, cd)
        .exclude(status__in=["cancelada", "encerrada"])
        .filter(inicio__lte=hoje, fim__gte=hoje)
        .count()
    ) if user_has_perm(request.user, "ausencias_colaboradores") else 0
    gargalos = [row for row in (capacidade or {}).get("rows", []) if row["nivel"] in {"atencao", "critico"}]

    def card(key, title, description, icon, url, perms=(), highlight=""):
        return frota_hub_link(key, title, description, icon, url, request.user, perms, highlight)

    sections = [
        {
            "title": "Pessoas e capacidade",
            "description": "Quadro do dia, funções cadastradas e leitura de gargalo por setor.",
            "cards": [
                card(
                    "pessoas_turno",
                    "Pessoas no Turno",
                    "Lançar planejado, ativos, férias, atestados, folgas e faltas por setor.",
                    "PT",
                    reverse("module_list", args=["pessoas_turno"]),
                    {"pessoas_turno"},
                    f"{pessoas_hoje.count()} registro(s) hoje" if user_has_perm(request.user, "pessoas_turno") else "",
                ),
                card(
                    "funcoes_turno",
                    "Funções do Turno",
                    "Cadastrar funções e quadro padrão usados no lançamento diário.",
                    "FT",
                    reverse("module_list", args=["funcoes_turno"]),
                    {"funcoes_turno"},
                    f"{funcoes_ativas} função(ões) ativa(s)" if user_has_perm(request.user, "funcoes_turno") else "",
                ),
                card(
                    "ausencias_colaboradores",
                    "Ausências dos Colaboradores",
                    "Cadastrar atestados, afastamentos, folgas e faltas por colaborador.",
                    "AU",
                    reverse("module_list", args=["ausencias_colaboradores"]),
                    {"ausencias_colaboradores", "gerenciar_ferias_colaboradores"},
                    f"{ausencias_hoje} ausência(s) hoje",
                ),
                card(
                    "gargalos_colaboradores",
                    "Gargalos da Operação",
                    "Ver setores em atenção ou crítico por férias, atestados, faltas e ausências.",
                    "GO",
                    "#gargalos-colaboradores",
                    {"visualizar_gargalos_colaboradores", "capacidade_operacao"},
                    f"{len(gargalos)} em alerta",
                ),
            ],
        },
        {
            "title": "Férias e alertas",
            "description": "Agenda de férias, mapa de calor e regras de notificação para supervisão.",
            "cards": [
                card(
                    "ferias_colaboradores",
                    "Férias dos Colaboradores",
                    "Cadastrar férias, retorno previsto e status por colaborador.",
                    "FE",
                    reverse("module_list", args=["ferias_colaboradores"]),
                    {"ferias_colaboradores", "gerenciar_ferias_colaboradores"},
                    f"{ferias_hoje} em férias hoje",
                ),
                card(
                    "mapa_calor_ferias",
                    "Mapa de Calor",
                    "Enxergar pressão por setor, função e dia para antecipar gargalos.",
                    "MC",
                    reverse("module_list", args=["ferias_colaboradores"]),
                    {"mapa_calor_ferias"},
                    f"{heatmap['total_colaboradores']} no período" if heatmap else "",
                ),
                card(
                    "alertas_ferias",
                    "Alertas de Férias",
                    "Configurar dias, limites e disparar alertas para os responsáveis.",
                    "AF",
                    "#alertas-ferias",
                    {"configurar_alertas_ferias", "notificacoes_ferias_colaboradores", "alertas_ferias"},
                    "Ativo" if config.get("ativo") else "Pausado",
                ),
            ],
        },
        {
            "title": "Apoio e EPI",
            "description": "Espaço preparado para controles de apoio aos colaboradores.",
            "cards": [
                card(
                    "epi_colaboradores",
                    "EPI e Colaboradores",
                    "Área preparada para controlar entrega, validade e pendências de EPI.",
                    "EP",
                    "#epi-colaboradores",
                    {"colaboradores_hub"},
                    "Em preparação",
                ),
            ],
        },
    ]
    sections = [{**section, "cards": [item for item in section["cards"] if item]} for section in sections]
    sections = [section for section in sections if section["cards"]]

    ctx = context_base(request)
    ctx.update(
        {
            "data": hoje,
            "colaboradores_sections": sections,
            "colaboradores_metrics": [
                ("Pessoas planejadas", sum_field(pessoas_hoje, "planejado") if user_has_perm(request.user, "pessoas_turno") else 0),
                ("Pessoas ativas", sum_field(pessoas_hoje, "ativos_dia") if user_has_perm(request.user, "pessoas_turno") else 0),
                ("Férias hoje", ferias_hoje),
                ("Ausências hoje", ausencias_hoje),
                ("Gargalos", len(gargalos)),
                ("Alertas próximos", len(ferias_rows)),
            ],
            "capacidade_operacao": capacidade,
            "gargalos_colaboradores": gargalos,
            "ferias_alertas": ferias_rows,
            "ferias_alert_config": config,
            "can_configure_ferias_alerts": user_has_perm(request.user, "configurar_alertas_ferias"),
        }
    )
    return render(request, "painel/colaboradores_hub.html", ctx)


def pessoa_nome(user):
    return user.get_full_name() or user.username


def sum_field(queryset, field):
    return queryset.aggregate(total=Sum(field))["total"] or 0


def pessoa_turno_ausencias(row):
    return (
        (row.atestados or 0)
        + (row.afastados or 0)
        + (row.ferias or 0)
        + (row.folgas or 0)
        + (row.faltas_sem_justificativa or 0)
    )


def pessoas_faltando(cd, data_obj):
    def build():
        rows = []
        for row in cd_queryset(PessoaTurno, cd).filter(data=data_obj).order_by("cd_unidade", "setor", "turno", "funcao"):
            falta = max((row.planejado or 0) - (row.ativos_dia or 0), 0)
            if falta:
                rows.append({"row": row, "falta": falta})
        return rows

    return cached_runtime_value(("pessoas_faltando", str(cd), data_obj.isoformat()), 30, build)


def alertas_inteligentes(cd, data_obj):
    def build():
        inicio_7 = data_obj - timezone.timedelta(days=6)
        alertas = []
        pendencias_count = cd_queryset(Pendencia, cd).exclude(status="concluido").count()
        ocorrencias_criticas = cd_queryset(OcorrenciaOperacional, cd).exclude(status__in=["resolvido", "cancelado"]).filter(severidade__in=["alta", "critica"]).exists()
        pessoas = pessoas_faltando(cd, data_obj)
        equipamentos_count = cd_queryset(Equipamento, cd).filter(status__in=["manutencao", "perdido"]).count()
        manutencoes_count = cd_queryset(EquipamentoManutencao, cd).exclude(status__in=["retornado", "cancelado"]).count()
        materiais_count = cd_queryset(MaterialConsumo, cd).filter(precisa_pedir=True).count()
        frota_pendente_count = (
            cd_queryset(ChecklistFrota, cd).filter(intercala_cd=True, intercalacao_concluida=False).count()
            if system_rule_enabled("checklist_intercalacao_manual")
            else 0
        )

        if pendencias_count >= 5:
            alertas.append({"nivel": "alta", "area": "Pendências", "texto": f"{pendencias_count} pendências abertas no CD {cd}."})
        if ocorrencias_criticas:
            alertas.append({"nivel": "critica", "area": "Ocorrências", "texto": "Existem ocorrências de severidade alta ou crítica."})
        if pessoas:
            total_faltas = sum(item["falta"] for item in pessoas)
            alertas.append({"nivel": "alta", "area": "Pessoas", "texto": f"Faltam {total_faltas} pessoa(s) no quadro do dia."})
        if equipamentos_count or manutencoes_count:
            alertas.append({"nivel": "media", "area": "Equipamentos", "texto": f"{equipamentos_count + manutencoes_count} item(ns) parados ou em manutenção."})
        if materiais_count:
            alertas.append({"nivel": "media", "area": "Materiais", "texto": f"{materiais_count} material(is) abaixo do minimo."})
        if frota_pendente_count:
            alertas.append({"nivel": "alta", "area": "Frota", "texto": f"{frota_pendente_count} rota(s) com passagem por outro CD pendente."})

        issue_filter = (
            Q(pane_mecanica=True)
            | Q(furo_pneu=True)
            | Q(acidente=True)
            | Q(multa=True)
            | Q(avaria_carga=True)
            | Q(vazamento_viagem=True)
            | Q(outra_ocorrencia=True)
            | ~Q(ocorrencia_rota_loja="")
        )
        placas = (
            cd_queryset(ChecklistFrota, cd)
            .filter(data__range=[inicio_7, data_obj])
            .filter(issue_filter)
            .exclude(placa="")
            .values("placa")
            .annotate(total=Count("id"))
            .filter(total__gte=3)
            .order_by("-total", "placa")[:5]
        )
        for item in placas:
            alertas.append({"nivel": "alta", "area": "Frota", "texto": f"Placa {item['placa']} teve {item['total']} ocorrências em 7 dias."})

        coletores = (
            cd_queryset(EquipamentoManutencao, cd).filter(tipo="operacional", data__range=[inicio_7, data_obj])
            .values("patrimonio")
            .annotate(total=Count("id"))
            .order_by("-total")
        )
        for item in coletores:
            if item["patrimonio"] and item["total"] >= 3:
                alertas.append({"nivel": "media", "area": "Equipamentos", "texto": f"Patrimônio {item['patrimonio']} foi {item['total']} vezes para manutenção em 7 dias."})
        return alertas[:12]

    return cached_runtime_value(("alertas_inteligentes", str(cd), data_obj.isoformat()), 30, build)


def resumo_fechamento(cd, data_obj):
    recebimentos = cd_queryset(Recebimento, cd).filter(data=data_obj)
    separacoes = cd_queryset(Separacao, cd).filter(data=data_obj)
    expedicoes = cd_queryset(Expedicao, cd).filter(data=data_obj)
    checklists = cd_queryset(ChecklistFrota, cd).filter(data=data_obj)
    pessoas = cd_queryset(PessoaTurno, cd).filter(data=data_obj)
    pendencias = cd_queryset(Pendencia, cd).exclude(status="concluido")
    ocorrencias = cd_queryset(OcorrenciaOperacional, cd).exclude(status__in=["resolvido", "cancelado"])
    return (
        f"Fechamento {cd_scope_label(cd)} em {data_obj:%d/%m/%Y}: "
        f"{recebimentos.count()} recebimento(s), "
        f"{sum_field(recebimentos, 'paletes')} paletes recebidos, "
        f"{sum_field(separacoes, 'unidades')} unidades separadas, "
        f"{sum_field(expedicoes, 'qtd_paletes')} paletes expedidos, "
        f"{checklists.count()} checklist(s) de frota, "
        f"{pessoas.count()} registro(s) de pessoas, "
        f"{pendencias.count()} pendência(s) aberta(s) e "
        f"{ocorrencias.count()} ocorrência(s) em acompanhamento."
    )


def gerar_fechamento_automatico(cd, data_obj, user):
    recebimentos = Recebimento.objects.filter(cd_unidade=cd, data=data_obj)
    expedicoes = Expedicao.objects.filter(cd_unidade=cd, data=data_obj)
    checklists = ChecklistFrota.objects.filter(cd_unidade=cd, data=data_obj)
    pessoas = PessoaTurno.objects.filter(cd_unidade=cd, data=data_obj)
    avarias_abertas = Avaria.objects.filter(cd_unidade=cd).exclude(status="concluido")
    pendencias_amanha = Pendencia.objects.filter(cd_unidade=cd, data__gt=data_obj).exclude(status="concluido")
    values = {
        "responsavel": pessoa_nome(user),
        "recebimento_fechado": recebimentos.exists(),
        "expedicao_fechada": expedicoes.exists(),
        "frota_conferida": checklists.exists() and not checklists.filter(necessita_manutencao=True).exists(),
        "pessoas_registradas": pessoas.exists(),
        "avarias_revisadas": not avarias_abertas.exists(),
        "pendencias_amanha": pendencias_amanha.exists(),
        "resumo_gerencial": resumo_fechamento(cd, data_obj),
        "finalizado_por": user,
        "finalizado_em": timezone.now(),
    }
    completo = all(
        [
            values["recebimento_fechado"],
            values["expedicao_fechada"],
            values["frota_conferida"],
            values["pessoas_registradas"],
            values["avarias_revisadas"],
        ]
    )
    values["status"] = "fechado" if completo else "em_revisao"
    fechamento, _created = FechamentoDia.objects.update_or_create(
        cd_unidade=cd,
        data=data_obj,
        turno="dia",
        defaults={**values, "criado_por": user},
    )
    return fechamento


def cd_kpis(cd, data_obj, inicio_semana, inicio_mes):
    receb_dia = Recebimento.objects.filter(cd_unidade=cd, data=data_obj)
    sep_dia = Separacao.objects.filter(cd_unidade=cd, data=data_obj)
    exp_dia = Expedicao.objects.filter(cd_unidade=cd, data=data_obj)
    return {
        "cd": cd,
        "recebimentos": receb_dia.count(),
        "paletes_recebidos": sum_field(receb_dia, "paletes"),
        "unidades_separadas": sum_field(sep_dia, "unidades"),
        "paletes_expedidos": sum_field(exp_dia, "qtd_paletes"),
        "ocorrencias_abertas": OcorrenciaOperacional.objects.filter(cd_unidade=cd).exclude(status__in=["resolvido", "cancelado"]).count(),
        "avarias_abertas": Avaria.objects.filter(cd_unidade=cd).exclude(status="concluido").count(),
        "produtividade_mes": (
            sum_field(SeparacaoProdutividade.objects.filter(cd_unidade=cd, data__range=[inicio_mes, data_obj]), "separacao_picking_produzido")
            + sum_field(Separacao.objects.filter(cd_unidade=cd, data__range=[inicio_mes, data_obj]), "separacao_picking_produzido")
        ),
        "expedicao_semana": sum_field(Expedicao.objects.filter(cd_unidade=cd, data__range=[inicio_semana, data_obj]), "qtd_paletes"),
    }


def pct(part, total):
    if not total:
        return 0
    return round((float(part or 0) / float(total or 0)) * 100, 1)


def query_count(qs):
    return qs.count() if hasattr(qs, "count") else len(qs)


def choice_rows(model, field_name, rows, total_field="total"):
    for row in rows:
        row["label"] = choice_label(model, field_name, row.get(field_name))
        row["total"] = row.get(total_field) or row.get("total") or 0
    return rows


def build_operational_score(indicadores):
    score = 100
    score -= min(indicadores["pendencias_abertas"] * 3, 18)
    score -= min(indicadores["ocorrencias_criticas"] * 10, 25)
    score -= min(indicadores["ocorrencias_altas"] * 6, 18)
    score -= min(indicadores["divergencias"] * 3, 15)
    score -= min(indicadores["avarias_abertas"] * 2, 12)
    score -= min(indicadores["chamados_abertos"] * 2, 12)
    score -= min(indicadores["materiais_criticos"] * 3, 12)
    score -= min(indicadores["equipamentos_parados"] * 3, 15)
    score -= min(indicadores["faltas_turno"] * 4, 16)
    score = max(score, 0)
    if score >= 85:
        nivel = "Estavel"
        classe = "ok"
    elif score >= 65:
        nivel = "Atenção"
        classe = "warning"
    else:
        nivel = "Crítico"
        classe = "paused"
    return {"score": score, "nivel": nivel, "classe": classe}


def build_data_quality(cd, inicio, fim):
    lojas_validas = set()
    for loja in Loja.objects.filter(cd_unidade__in=cd_values(cd), ativa=True):
        lojas_validas.update({loja.codigo, loja.nome, str(loja)})
    gtins_validos = set(ProdutoGtin.objects.filter(cd_unidade__in=cd_values(cd), ativo=True).values_list("gtin", flat=True))
    separacao_sem_loja = cd_queryset(Separacao, cd).filter(data__range=[inicio, fim])
    expedicao_sem_loja = cd_queryset(Expedicao, cd).filter(data__range=[inicio, fim])
    if lojas_validas:
        separacao_sem_loja = separacao_sem_loja.exclude(loja__in=lojas_validas)
        expedicao_sem_loja = expedicao_sem_loja.exclude(loja__in=lojas_validas)
    conferencia_sem_gtin = cd_queryset(Conferencia, cd).filter(data__range=[inicio, fim]).exclude(gtin="")
    ressuprimento_sem_gtin = cd_queryset(Ressuprimento, cd).filter(data__range=[inicio, fim]).exclude(gtin="")
    chamado_sem_gtin = cd_queryset(ChamadoSaldo, cd).filter(data__range=[inicio, fim]).exclude(gtin="")
    avaria_sem_gtin = cd_queryset(Avaria, cd).filter(data__range=[inicio, fim]).exclude(gtin="")
    if gtins_validos:
        conferencia_sem_gtin = conferencia_sem_gtin.exclude(gtin__in=gtins_validos)
        ressuprimento_sem_gtin = ressuprimento_sem_gtin.exclude(gtin__in=gtins_validos)
        chamado_sem_gtin = chamado_sem_gtin.exclude(gtin__in=gtins_validos)
        avaria_sem_gtin = avaria_sem_gtin.exclude(gtin__in=gtins_validos)
    checks = [
        {
            "area": "Cadastro de lojas",
            "indicador": "Lojas usadas e não cadastradas",
            "valor": separacao_sem_loja.count() + expedicao_sem_loja.count(),
            "acao": "Conferir códigos de loja importados da Bluesoft.",
        },
        {
            "area": "GTIN/produtos",
            "indicador": "GTINs usados e não cadastrados",
            "valor": conferencia_sem_gtin.count() + ressuprimento_sem_gtin.count() + chamado_sem_gtin.count() + avaria_sem_gtin.count(),
            "acao": "Importar ou corrigir base de produtos do CD.",
        },
        {
            "area": "Recebimento",
            "indicador": "Recebimentos sem NF",
            "valor": cd_queryset(Recebimento, cd).filter(data__range=[inicio, fim]).filter(Q(nota_fiscal="") | Q(fornecedor="")).count(),
            "acao": "Preencher fornecedor e nota fiscal para rastreio.",
        },
        {
            "area": "Equipamentos",
            "indicador": "Equipamentos sem patrimônio/série",
            "valor": cd_queryset(Equipamento, cd).filter(Q(equipamento="") | Q(patrimonio="")).count(),
            "acao": "Completar dados para controle por colaborador.",
        },
        {
            "area": "GLPI",
            "indicador": "Chamados sem número GLPI",
            "valor": cd_queryset(ChamadoSaldo, cd).exclude(status="concluido").filter(numero_glpi="").count(),
            "acao": "Vincular número do chamado para acompanhamento.",
        },
    ]
    total_pontos = len(checks)
    problemas = sum(1 for item in checks if item["valor"])
    score = pct(total_pontos - problemas, total_pontos)
    return {"score": score, "problemas": problemas, "checks": checks}


def build_gestao_360(cd, data_obj):
    inicio_semana = data_obj - timezone.timedelta(days=data_obj.weekday())
    inicio_mes = data_obj.replace(day=1)
    pendencias = cd_queryset(Pendencia, cd).exclude(status="concluido")
    ocorrencias = cd_queryset(OcorrenciaOperacional, cd).exclude(status__in=["resolvido", "cancelado"])
    avarias = cd_queryset(Avaria, cd).exclude(status="concluido")
    chamados = cd_queryset(ChamadoSaldo, cd).exclude(status="concluido")
    materiais = cd_queryset(MaterialConsumo, cd).filter(precisa_pedir=True)
    equipamentos = cd_queryset(Equipamento, cd).filter(status__in=["manutencao", "perdido"])
    divergencias = cd_queryset(Conferencia, cd).filter(data__range=[inicio_semana, data_obj]).exclude(diferenca=0)
    faltas = sum(item["falta"] for item in pessoas_faltando(cd, data_obj))
    indicadores = {
        "pendencias_abertas": pendencias.count(),
        "ocorrencias_criticas": ocorrencias.filter(severidade="critica").count(),
        "ocorrencias_altas": ocorrencias.filter(severidade="alta").count(),
        "divergencias": divergencias.count(),
        "avarias_abertas": avarias.count(),
        "chamados_abertos": chamados.count(),
        "materiais_criticos": materiais.count(),
        "equipamentos_parados": equipamentos.count(),
        "faltas_turno": faltas,
    }
    score = build_operational_score(indicadores)
    recebimentos_mes = cd_queryset(Recebimento, cd).filter(data__range=[inicio_mes, data_obj])
    separacao_mes = cd_queryset(Separacao, cd).filter(data__range=[inicio_mes, data_obj])
    expedicao_mes = cd_queryset(Expedicao, cd).filter(data__range=[inicio_mes, data_obj])
    pessoas_dia = cd_queryset(PessoaTurno, cd).filter(data=data_obj)
    planejado = sum_field(pessoas_dia, "planejado")
    ativos = sum_field(pessoas_dia, "ativos_dia")
    cards = [
        {"label": "Risco operacional", "value": f"{score['score']}%", "detail": score["nivel"], "class": score["classe"]},
        {"label": "Qualidade dos dados", "value": f"{build_data_quality(cd, inicio_semana, data_obj)['score']}%", "detail": "Base para BI", "class": "ok"},
        {"label": "Cobertura de pessoas", "value": f"{pct(ativos, planejado)}%", "detail": f"{ativos}/{planejado} ativos", "class": "ok" if ativos >= planejado else "warning"},
        {"label": "Acuracidade conferência", "value": f"{pct(query_count(cd_queryset(Conferencia, cd).filter(data__range=[inicio_semana, data_obj]).filter(diferenca=0)), query_count(cd_queryset(Conferencia, cd).filter(data__range=[inicio_semana, data_obj])))}%", "detail": "Semana atual", "class": "ok" if not divergencias.exists() else "warning"},
        {"label": "Paletes recebidos mês", "value": sum_field(recebimentos_mes, "paletes"), "detail": f"{recebimentos_mes.count()} NFs", "class": "ok"},
        {"label": "Unidades separadas mês", "value": sum_field(separacao_mes, "unidades"), "detail": f"{separacao_mes.count()} lançamentos", "class": "ok"},
        {"label": "Paletes expedidos mês", "value": sum_field(expedicao_mes, "qtd_paletes"), "detail": f"{expedicao_mes.count()} lançamentos", "class": "ok"},
        {"label": "Pendências abertas", "value": pendencias.count(), "detail": "Todos os setores", "class": "warning" if pendencias.exists() else "ok"},
    ]
    setores = []
    for setor_key, setor_label in SETOR_CHOICES:
        setores.append(
            {
                "setor": setor_label,
                "pendencias": pendencias.filter(setor__icontains=setor_label).count(),
                "ocorrencias": ocorrencias.filter(setor=setor_key).count(),
                "pessoas_faltando": sum(item["falta"] for item in pessoas_faltando(cd, data_obj) if item["row"].setor == setor_key),
                "divergencias": divergencias.filter(setor__icontains=setor_label).count(),
            }
        )
    setores = sorted(setores, key=lambda item: item["pendencias"] + item["ocorrencias"] + item["pessoas_faltando"] + item["divergencias"], reverse=True)[:8]
    trilha_bi = [
        {"camada": "Dimensões", "tabelas": "dim_data, dim_cd, dim_loja, dim_produto, dim_colaborador, dim_setor", "uso": "Filtros estáveis para todos os dashboards."},
        {"camada": "Fatos", "tabelas": "f_recebimento, f_separacao, f_expedicao, f_avaria, f_glpi, f_pessoas, f_equipamentos", "uso": "Métricas por dia, CD, loja, produto e setor."},
        {"camada": "Gestão", "tabelas": "f_ocorrencias, f_aprovacoes, f_fechamento, f_pendencias", "uso": "Rastreio de decisão, gargalos e SLA."},
        {"camada": "Qualidade", "tabelas": "dq_validacoes, dq_importacoes, auditoria", "uso": "Confiabilidade da base antes de mandar para a diretoria."},
    ]
    inovacoes = [
        {"prioridade": "Alta", "tema": "Modo TV do CD", "ganho": "Painel em tela cheia com operação do dia, alertas e fechamento."},
        {"prioridade": "Alta", "tema": "SLA por pendência", "ganho": "Prazo automático e atraso visível para líder/supervisor."},
        {"prioridade": "Alta", "tema": "Importação Bluesoft guiada", "ganho": "Validação antes de salvar e aviso de loja/GTIN desconhecido."},
        {"prioridade": "Média", "tema": "QR Code de equipamentos", "ganho": "Abrir histórico de coletor, paleteira, empilhadeira ou computador pelo celular."},
        {"prioridade": "Média", "tema": "Previsão de materiais", "ganho": "Pedido sugerido por consumo médio e estoque mínimo."},
        {"prioridade": "Média", "tema": "Resumo executivo automático", "ganho": "Texto pronto para gerente com riscos, causas e ações."},
        {"prioridade": "Pesquisa", "tema": "IA assistente operacional", "ganho": "Perguntar 'o que está atrasado hoje' e receber resposta com base nos dados."},
    ]
    return {
        "score": score,
        "indicadores": indicadores,
        "cards": cards,
        "setores": setores,
        "qualidade": build_data_quality(cd, inicio_semana, data_obj),
        "ocorrencias_tipo": choice_rows(
            OcorrenciaOperacional,
            "tipo",
            list(ocorrencias.values("tipo").annotate(total=Count("id")).order_by("-total")),
        ),
        "status_operacao": [
            {"area": "Recebimento", "valor": cd_queryset(Recebimento, cd).filter(data=data_obj).count(), "meta": "NFs lançadas"},
            {"area": "Separação", "valor": sum_field(cd_queryset(Separacao, cd).filter(data=data_obj), "unidades"), "meta": "Unidades do dia"},
            {"area": "Expedição", "valor": sum_field(cd_queryset(Expedicao, cd).filter(data=data_obj), "qtd_paletes"), "meta": "Paletes do dia"},
            {"area": "Conferência", "valor": divergencias.count(), "meta": "Divergências na semana"},
            {"area": "Avarias", "valor": avarias.count(), "meta": "Itens abertos"},
            {"area": "GLPI", "valor": chamados.count(), "meta": "Chamados abertos"},
        ],
        "trilha_bi": trilha_bi,
        "inovacoes": inovacoes,
    }


def build_decision_center(cd, data_obj, modo, gestao_360):
    pendencias = cd_queryset(Pendencia, cd).exclude(status="concluido")
    ocorrencias = cd_queryset(OcorrenciaOperacional, cd).exclude(status__in=["resolvido", "cancelado"])
    aprovacoes = cd_queryset(AprovacaoOperacional, cd).filter(status="pendente")
    faltas = pessoas_faltando(cd, data_obj)
    materiais = cd_queryset(MaterialConsumo, cd).filter(precisa_pedir=True)
    equipamentos = cd_queryset(Equipamento, cd).filter(status__in=["manutencao", "perdido"])
    frota_manutencao = cd_queryset(ChecklistFrota, cd).filter(data=data_obj, necessita_manutencao=True)
    recebimentos_dia = cd_queryset(Recebimento, cd).filter(data=data_obj)
    separacao_dia = cd_queryset(Separacao, cd).filter(data=data_obj)
    expedicao_dia = cd_queryset(Expedicao, cd).filter(data=data_obj)

    acoes = []
    if pendencias.exists():
        acoes.append({
            "prioridade": "Alta",
            "area": "Pendencias",
            "responsavel": "Lider",
            "motivo": f"{pendencias.count()} pendencia(s) aberta(s)",
            "acao": "Assumir as pendencias do dia e fechar as que ja foram resolvidas.",
        })
    if ocorrencias.filter(severidade__in=["alta", "critica"]).exists():
        acoes.append({
            "prioridade": "Crítica",
            "area": "Ocorrências",
            "responsavel": "Supervisor",
            "motivo": "Existe ocorrência alta ou crítica em aberto",
            "acao": "Validar causa, responsavel e prazo antes do fechamento.",
        })
    if faltas:
        acoes.append({
            "prioridade": "Alta",
            "area": "Pessoas",
            "responsavel": "Supervisor",
            "motivo": f"{sum(item['falta'] for item in faltas)} pessoa(s) faltando no turno",
            "acao": "Rebalancear equipe entre setores ou registrar impacto operacional.",
        })
    if frota_manutencao.exists() or equipamentos.exists():
        acoes.append({
            "prioridade": "Alta",
            "area": "Frota/equipamentos",
            "responsavel": "Frota / Administrativo",
            "motivo": f"{frota_manutencao.count() + equipamentos.count()} item(ns) exigem ação",
            "acao": "Conferir checklist, retirar equipamento parado da operação e abrir acompanhamento.",
        })
    if materiais.exists():
        acoes.append({
            "prioridade": "Media",
            "area": "Materiais",
            "responsavel": "Administrativo",
            "motivo": f"{materiais.count()} material(is) abaixo do minimo",
            "acao": "Gerar lista de pedido e validar consumo previsto da semana.",
        })
    if not recebimentos_dia.exists() and not separacao_dia.exists() and not expedicao_dia.exists():
        acoes.append({
            "prioridade": "Media",
            "area": "Dados",
            "responsavel": "Assistente",
            "motivo": "Sem lançamentos operacionais para a data",
            "acao": "Confirmar se a data está correta ou importar/lançar dados antes do relatório.",
        })
    if aprovacoes.exists():
        acoes.append({
            "prioridade": "Alta",
            "area": "Aprovações",
            "responsavel": "Supervisor/Gerente",
            "motivo": f"{aprovacoes.count()} aprovação(ões) pendente(s)",
            "acao": "Validar, aprovar ou reprovar antes de encerrar a rotina.",
        })

    if not acoes:
        acoes.append({
            "prioridade": "Normal",
            "area": "Operação",
            "responsavel": "Todos",
            "motivo": "Sem risco crítico pelos dados lançados",
            "acao": "Manter acompanhamento e gerar fechamento no fim do turno.",
        })

    rotinas = {
        "lider": [
            "Assumir pendencias do setor e registrar responsavel.",
            "Checar pessoas faltando e avisar supervisor quando impactar separação/expedição.",
            "Resolver ocorrências simples e deixar observação objetiva.",
            "Confirmar se recebimento, separação e expedição foram lançados.",
        ],
        "supervisor": [
            "Olhar gargalos por setor e priorizar alta/crítica.",
            "Validar produtividade da semana e divergências de conferência.",
            "Cobrar pendências recorrentes e materiais críticos.",
            "Validar fechamento automático e aprovações de supervisor.",
        ],
        "gerente": [
            "Comparar CD 801 x CD 806 e observar tendencia de semana/mes.",
            "Ver risco operacional, qualidade dos dados e cobertura de pessoas.",
            "Aprovar decisões críticas e remover bloqueios entre áreas.",
            "Gerar relatório executivo ou base Power BI para acompanhamento.",
        ],
    }
    atalhos = [
        {"label": "Ocorrencias", "url": reverse("module_list", args=["ocorrencias"])},
        {"label": "Fechamento", "url": reverse("module_list", args=["fechamentos"])},
        {"label": "Relatorio Gestao 360", "url": f"{reverse('relatorios')}tipo=gestao_360&inicio={data_obj.isoformat()}&fim={data_obj.isoformat()}"},
    ]
    if modo in {"supervisor", "gerente"}:
        atalhos.append({"label": "Power BI", "url": f"{reverse('relatorios')}tipo=geral&inicio={data_obj.isoformat()}&fim={data_obj.isoformat()}&exportar=powerbi"})
    return {
        "acoes": acoes[:8],
        "rotina": rotinas.get(modo, rotinas["lider"]),
        "atalhos": atalhos,
        "resumo": (
            f"{gestao_360['score']['nivel']} ({gestao_360['score']['score']}%) - "
            f"{len([acao for acao in acoes if acao['prioridade'] in {'Alta', 'Crítica'}])} prioridade(s) alta(s)"
        ),
    }


def build_painel_gestao(cd, data_obj, modo):
    inicio_semana = data_obj - timezone.timedelta(days=data_obj.weekday())
    inicio_mes = data_obj.replace(day=1)
    pendencias_base = cd_queryset(Pendencia, cd).exclude(status="concluido")
    ocorrencias_base = cd_queryset(OcorrenciaOperacional, cd).exclude(status__in=["resolvido", "cancelado"])
    aprovacoes_base = cd_queryset(AprovacaoOperacional, cd).filter(status="pendente")
    pendencias = pendencias_base.order_by("data", "setor")[:12]
    ocorrencias = ocorrencias_base.order_by("-severidade", "data")[:12]
    aprovacoes = aprovacoes_base.order_by("data")[:12]
    fechamento = None if str(cd) == UNIFIED_CD else FechamentoDia.objects.filter(cd_unidade=cd, data=data_obj, turno="dia").first()
    cards = [
        ("Pendências abertas", pendencias_base.count()),
        ("Ocorrências abertas", ocorrencias_base.count()),
        ("Aprovacoes pendentes", aprovacoes_base.count()),
        ("Faltas no turno", sum(item["falta"] for item in pessoas_faltando(cd, data_obj))),
        ("Equipamentos parados", cd_queryset(Equipamento, cd).filter(status__in=["manutencao", "perdido"]).count()),
        ("Frota manutenção", cd_queryset(ChecklistFrota, cd).filter(data=data_obj, necessita_manutencao=True).count()),
    ]
    setores = (
        ocorrencias_base
        .values("setor")
        .annotate(total=Count("id"))
        .order_by("-total")
    )
    problemas = (
        cd_queryset(OcorrenciaOperacional, cd).filter(data__gte=data_obj - timezone.timedelta(days=30))
        .values("tipo")
        .annotate(total=Count("id"))
        .order_by("-total")[:8]
    )
    produtividade = sorted(
        list(cd_queryset(SeparacaoProdutividade, cd).filter(data__range=[inicio_semana, data_obj]))
        + list(cd_queryset(Separacao, cd).filter(data__range=[inicio_semana, data_obj]).exclude(total_a_produzir=0, separacao_picking_produzido=0, separacao_pulmao_produzido=0)),
        key=lambda row: (row.data, row.pk or 0),
        reverse=True,
    )[:8]
    gestao_360 = build_gestao_360(cd, data_obj)
    return {
        "cards_gestao": cards,
        "modo": modo,
        "alertas_inteligentes": alertas_inteligentes(cd, data_obj),
        "pendencias_gestao": pendencias,
        "ocorrencias_gestao": ocorrencias,
        "aprovacoes_gestao": aprovacoes,
        "fechamento_gestao": fechamento,
        "pessoas_faltando": pessoas_faltando(cd, data_obj),
        "setores_gargalo": setores,
        "problemas_recorrentes": problemas,
        "produtividade_gestao": produtividade,
        "comparativo_cds": [cd_kpis(item, data_obj, inicio_semana, inicio_mes) for item in ["801", "806"]],
        "resumo_fechamento_auto": resumo_fechamento(cd, data_obj),
        "gestao_360": gestao_360,
        "central_decisao": build_decision_center(cd, data_obj, modo, gestao_360),
    }


@login_required
def painel_gestao(request):
    if not user_has_perm(request.user, "painel_gestao"):
        profile = ensure_profile(request.user)
        if profile and profile.cargo == "motorista":
            return redirect("module_list", key="checklist_frota")
        messages.error(request, "Você não tem acesso ao painel de gestão.")
        return redirect("dashboard")
    cd_atual = current_cd(request)
    cd = request.POST.get("cd_escopo") or request.GET.get("cd_escopo") or cd_atual
    if cd not in {"801", "806", UNIFIED_CD}:
        cd = cd_atual
    if cd == UNIFIED_CD and not can_view_unified_cd(request.user):
        cd = cd_atual
    profile = ensure_profile(request.user)
    data = request.POST.get("data") or request.GET.get("data") or timezone.localdate().isoformat()
    data_obj = parse_iso_date(data)
    modo = request.POST.get("modo") or request.GET.get("modo") or (profile.cargo if profile and profile.cargo in {"lider", "supervisor", "gerente"} else "lider")
    if modo not in {"lider", "supervisor", "gerente"}:
        modo = "lider"

    if request.method == "POST":
        action = request.POST.get("action")
        if action in {"assumir_pendencia", "resolver_pendencia"}:
            pendencia = get_object_or_404(cd_queryset(Pendencia, cd), pk=request.POST.get("pendencia_id"))
            antes = serializable_dict(pendencia)
            pendencia.responsavel = pessoa_nome(request.user)
            pendencia.status = "concluido" if action == "resolver_pendencia" else "andamento"
            pendencia.save(update_fields=["responsavel", "status", "atualizado_em"])
            log_action(request, action, "painel_gestao", objeto=pendencia, antes=antes)
            messages.success(request, "Pendência atualizada.")
        elif action in {"assumir_ocorrencia", "resolver_ocorrencia"}:
            ocorrencia = get_object_or_404(cd_queryset(OcorrenciaOperacional, cd), pk=request.POST.get("ocorrencia_id"))
            antes = serializable_dict(ocorrencia)
            if action == "assumir_ocorrencia":
                ocorrencia.assumido_por = request.user
                ocorrencia.assumido_em = timezone.now()
                ocorrencia.status = "analise"
                ocorrencia.responsavel = pessoa_nome(request.user)
            else:
                ocorrencia.resolvido_por = request.user
                ocorrencia.resolvido_em = timezone.now()
                ocorrencia.status = "resolvido"
                ocorrencia.solucao = request.POST.get("solucao", "").strip() or ocorrencia.solucao or "Resolvido pelo painel de gestão."
            ocorrencia.save()
            log_action(request, action, "painel_gestao", objeto=ocorrencia, antes=antes)
            messages.success(request, "Ocorrencia atualizada.")
        elif action in {"validar_aprovacao", "aprovar_aprovacao", "reprovar_aprovacao"}:
            aprovacao = get_object_or_404(cd_queryset(AprovacaoOperacional, cd), pk=request.POST.get("aprovacao_id"))
            antes = serializable_dict(aprovacao)
            aprovacao.status = {"validar_aprovacao": "validado", "aprovar_aprovacao": "aprovado", "reprovar_aprovacao": "reprovado"}[action]
            aprovacao.aprovado_por = request.user
            aprovacao.aprovado_em = timezone.now()
            aprovacao.justificativa = request.POST.get("justificativa", "").strip() or aprovacao.justificativa
            aprovacao.save()
            log_action(request, action, "painel_gestao", objeto=aprovacao, antes=antes)
            messages.success(request, "Aprovacao atualizada.")
        elif action == "gerar_fechamento":
            if cd == UNIFIED_CD:
                messages.error(request, "Para gerar fechamento, escolha um CD específico.")
                return redirect(f"{reverse('painel_gestao')}data={data_obj.isoformat()}&modo={modo}&cd_escopo={cd}")
            fechamento = gerar_fechamento_automatico(cd, data_obj, request.user)
            log_action(request, "fechamento_automatico", "painel_gestao", objeto=fechamento)
            messages.success(request, "Fechamento do dia gerado.")
        return redirect(f"{reverse('painel_gestao')}data={data_obj.isoformat()}&modo={modo}&cd_escopo={cd}")

    ctx = context_base(request)
    ctx.update(
        {
            "data": data_obj.isoformat(),
            "cd_escopo": cd,
            "cd_escopo_label": cd_scope_label(cd),
            "can_unified_view": can_view_unified_cd(request.user),
            **build_painel_gestao(cd, data_obj, modo),
        }
    )
    return render(request, "painel/painel_gestao.html", ctx)


def build_painel_gerencial_cards(cd, inicio, fim):
    cd_list = cd_values(cd)

    def total(qs, field="qtd_paletes"):
        return qs.aggregate(total=Sum(field)).get("total") or 0

    cds = []
    saldo_total = 0
    vinculado_total = 0
    expedido_total = 0
    for cd_item in cd_list:
        vinculos = ExpedicaoVinculo.objects.filter(cd_unidade=cd_item, status="vinculado")
        expedicoes = Expedicao.objects.filter(cd_unidade=cd_item, data__range=[inicio, fim]).exclude(status="cancelado")
        saldo = int(sum(item.qtd_paletes for item in current_pallet_rows([cd_item])))
        vinculado = int(total(vinculos))
        expedido = int(total(expedicoes))
        saldo_livre = max(saldo - vinculado, 0)
        saldo_total += saldo
        vinculado_total += vinculado
        expedido_total += expedido
        cds.append(
            {
                "cd": cd_item,
                "saldo": saldo,
                "vinculado": vinculado,
                "expedido": expedido,
                "saldo_livre": saldo_livre,
            }
        )

    checklists = cd_queryset(ChecklistFrota, cd).filter(data__range=[inicio, fim])
    saidas = checklists.filter(tipo_checklist="saida")
    retornos = checklists.filter(tipo_checklist="retorno")
    retornos_pendentes = saidas.filter(retorno_registrado__isnull=True, retorno_dispensado=False).count()
    solicitacoes_pendentes = cd_queryset(SolicitacaoCarregamentoManual, cd).filter(data__range=[inicio, fim], status="pendente").count()

    ferias_ativas = cd_queryset(ColaboradorFerias, cd).filter(
        inicio_ferias__lte=fim,
        fim_ferias__gte=inicio,
    ).exclude(status__in=["cancelada", "retornado"])
    ausencias_ativas = cd_queryset(ColaboradorAusencia, cd).filter(
        inicio__lte=fim,
        fim__gte=inicio,
    ).exclude(status__in=["cancelada", "encerrada"])
    ferias_proximas = cd_queryset(ColaboradorFerias, cd).filter(
        inicio_ferias__gt=fim,
        inicio_ferias__lte=fim + timezone.timedelta(days=7),
    ).exclude(status__in=["cancelada", "retornado"])

    manutencoes_abertas = cd_queryset(EquipamentoManutencao, cd).exclude(status__in=["retornado", "cancelado"])
    equipamentos_parados = cd_queryset(Equipamento, cd).filter(status__in=["manutencao", "perdido"])
    saldo_livre_total = max(saldo_total - vinculado_total, 0)

    resumo_cards = [
        {
            "label": "Pallets disponiveis",
            "value": saldo_livre_total,
            "detail": f"Atual {saldo_total} | reservado {vinculado_total} | expedido no periodo {expedido_total}",
        },
        {
            "label": "Caminhoes a caminho",
            "value": cd_queryset(ExpedicaoVinculo, cd).filter(data__range=[inicio, fim], status="vinculado").count(),
            "detail": "Veiculos definidos pela Frota e ainda nao baixados",
        },
        {
            "label": "Retornos pendentes",
            "value": retornos_pendentes,
            "detail": f"{saidas.count()} saida(s) e {retornos.count()} retorno(s)",
        },
        {
            "label": "Pessoas ausentes",
            "value": ferias_ativas.count() + ausencias_ativas.count(),
            "detail": f"{ferias_ativas.count()} em ferias e {ausencias_ativas.count()} ausencia(s)",
        },
    ]

    frota_cards = [
        {"label": "Saidas registradas", "value": saidas.count(), "detail": "Motoristas que iniciaram viagem", "alert": False},
        {"label": "Check-lists de retorno", "value": retornos.count(), "detail": "Retornos registrados no sistema", "alert": False},
        {"label": "Retornos pendentes", "value": retornos_pendentes, "detail": "Viagens ainda sem retorno", "alert": retornos_pendentes > 0},
        {"label": "Excecoes pendentes", "value": solicitacoes_pendentes, "detail": "Pedidos aguardando Frota/supervisao", "alert": solicitacoes_pendentes > 0},
    ]
    agrupados = OrderedDict()
    for vinculo in (
        ExpedicaoVinculo.objects.filter(cd_unidade__in=cd_list, data__range=[inicio, fim], status="vinculado")
        .order_by("-data", "cd_unidade", "motorista", "placa", "loja")
    ):
        chave = (vinculo.data, vinculo.cd_unidade, vinculo.motorista or "", vinculo.placa or "", vinculo.status)
        item = agrupados.setdefault(
            chave,
            {
                "data": vinculo.data,
                "cd": vinculo.cd_unidade,
                "motorista": vinculo.motorista or "Sem motorista",
                "placa": vinculo.placa or "Sem placa",
                "status": "A caminho",
                "total": 0,
                "lojas": [],
            },
        )
        item["total"] += vinculo.qtd_paletes
        item["lojas"].append(f"{vinculo.loja}: {vinculo.qtd_paletes} pallet(s)")
    caminhoes_gerenciais = list(agrupados.values())[:18]

    colaboradores_cards = [
        {"label": "Férias em andamento", "value": ferias_ativas.count(), "detail": "Colaboradores fora no periodo", "alert": ferias_ativas.exists()},
        {"label": "Férias nos próximos 7 dias", "value": ferias_proximas.count(), "detail": "Saídas que precisam de cobertura", "alert": ferias_proximas.exists()},
        {"label": "Ausências ativas", "value": ausencias_ativas.count(), "detail": "Atestados, faltas, folgas ou afastamentos", "alert": ausencias_ativas.exists()},
    ]
    equipamentos_cards = [
        {"label": "Equipamentos em manutenção", "value": manutencoes_abertas.count(), "detail": "Chamados ou envios ainda abertos", "alert": manutencoes_abertas.exists()},
        {"label": "Equipamentos parados", "value": equipamentos_parados.count(), "detail": "Itens em manutenção ou perdidos", "alert": equipamentos_parados.exists()},
        {"label": "Equipamentos em uso", "value": cd_queryset(Equipamento, cd).filter(status="em_uso").count(), "detail": "Recursos atualmente atribuídos", "alert": False},
    ]

    alertas = []
    if retornos_pendentes:
        alertas.append({"nivel": "alerta", "titulo": "Retorno de frota pendente", "texto": f"{retornos_pendentes} viagem(ns) ainda precisam de retorno ou dispensa."})
    if solicitacoes_pendentes:
        alertas.append({"nivel": "alerta", "titulo": "Aprovação manual pendente", "texto": f"{solicitacoes_pendentes} pedido(s) aguardam decisão da frota."})
    if ferias_ativas.count() + ausencias_ativas.count() >= 3:
        alertas.append({"nivel": "critico", "titulo": "Pressão de pessoas", "texto": "Há três ou mais ausências/férias no período selecionado."})
    if manutencoes_abertas.count() >= 3:
        alertas.append({"nivel": "alerta", "titulo": "Manutenção acumulada", "texto": "Há três ou mais equipamentos aguardando resolução."})

    return {
        "resumo_cards": resumo_cards,
        "frota_cards": frota_cards,
        "colaboradores_cards": colaboradores_cards,
        "equipamentos_cards": equipamentos_cards,
        "caminhoes_gerenciais": caminhoes_gerenciais,
        "cds": cds,
        "alertas_gerenciais": alertas,
    }


def build_pallets_historico_gerencial(cd, inicio, fim, loja_filtro=""):
    cds = cd_values(cd)
    lojas = loja_label_map(cds)
    loja_filtro_codigo = loja_codigo(loja_filtro)

    planejamentos = (
        ExpedicaoPlanejamento.objects.filter(cd_unidade__in=cds, data__range=[inicio, fim])
        .exclude(status="cancelado")
        .values("data", "cd_unidade", "loja")
        .annotate(total=Sum("qtd_paletes"))
    )
    expedicoes = (
        Expedicao.objects.filter(cd_unidade__in=cds, data__range=[inicio, fim])
        .exclude(status="cancelado")
        .values("data", "cd_unidade", "loja")
        .annotate(total=Sum("qtd_paletes"))
    )

    linhas = OrderedDict()
    total_saldo = 0
    total_expedido = 0

    def ensure_row(data, cd_unidade, loja):
        codigo = loja_codigo(loja)
        if loja_filtro_codigo and codigo != loja_filtro_codigo:
            return None
        label = loja_label_from_value(loja, lojas)
        lojas.setdefault(codigo, label)
        key = (data, cd_unidade, codigo)
        if key not in linhas:
            linhas[key] = {
                "data": data,
                "cd": cd_unidade,
                "loja": label,
                "saldo": 0,
                "expedido": 0,
                "restante": 0,
            }
        return linhas[key]

    for row in planejamentos:
        item = ensure_row(row["data"], row["cd_unidade"], row["loja"])
        if item is None:
            continue
        qtd = int(row["total"] or 0)
        item["saldo"] += qtd
        total_saldo += qtd

    for row in expedicoes:
        item = ensure_row(row["data"], row["cd_unidade"], row["loja"])
        if item is None:
            continue
        qtd = int(row["total"] or 0)
        item["expedido"] += qtd
        total_expedido += qtd

    for item in linhas.values():
        item["restante"] = max(item["saldo"] - item["expedido"], 0)

    reservas_atuais = defaultdict(int)
    for row in (
        ExpedicaoVinculo.objects.filter(cd_unidade__in=cds, status="vinculado")
        .values("cd_unidade", "loja")
        .annotate(total=Sum("qtd_paletes"))
    ):
        reservas_atuais[(row["cd_unidade"], loja_codigo(row["loja"]))] += int(row["total"] or 0)

    expedido_periodo = defaultdict(int)
    for row in expedicoes:
        expedido_periodo[(row["cd_unidade"], loja_codigo(row["loja"]))] += int(row["total"] or 0)

    saldo_atual_linhas = []
    for row in current_pallet_rows(cds):
        codigo = loja_codigo(row.loja)
        if loja_filtro_codigo and codigo != loja_filtro_codigo:
            continue
        label = loja_label_from_value(row.loja, lojas)
        lojas.setdefault(codigo, label)
        saldo = int(row.qtd_paletes or 0)
        reservado = int(reservas_atuais.get((row.cd_unidade, codigo), 0))
        expedido = int(expedido_periodo.get((row.cd_unidade, codigo), 0))
        livre = max(saldo - reservado, 0)
        saldo_atual_linhas.append(
            {
                "cd": row.cd_unidade,
                "loja": label,
                "saldo": saldo,
                "reservado": reservado,
                "expedido": expedido,
                "livre": livre,
            }
        )

    saldo_atual_linhas = sorted(
        saldo_atual_linhas,
        key=lambda item: (loja_codigo(item["loja"]), item["cd"]),
    )
    saldo_atual_por_cd = {
        cd_item: [item for item in saldo_atual_linhas if item["cd"] == cd_item and (item["saldo"] or item["reservado"] or item["expedido"])]
        for cd_item in cds
    }
    saldo_atual_resumo_por_cd = []
    for cd_item in cds:
        itens_cd = saldo_atual_por_cd.get(cd_item, [])
        saldo_atual_resumo_por_cd.append(
            {
                "cd": cd_item,
                "saldo": sum(item["saldo"] for item in itens_cd),
                "reservado": sum(item["reservado"] for item in itens_cd),
                "expedido": sum(item["expedido"] for item in itens_cd),
                "livre": sum(item["livre"] for item in itens_cd),
                "lojas": len(itens_cd),
            }
        )

    linhas_ordenadas = sorted(linhas.values(), key=lambda item: (item["data"], item["loja"], item["cd"]), reverse=True)
    por_cd = []
    for cd_item in cds:
        itens_cd = [item for item in linhas.values() if item["cd"] == cd_item]
        saldo_cd = sum(item["saldo"] for item in itens_cd)
        expedido_cd = sum(item["expedido"] for item in itens_cd)
        restante_cd = sum(item["restante"] for item in itens_cd)
        por_cd.append(
            {
                "cd": cd_item,
                "saldo": saldo_cd,
                "expedido": expedido_cd,
                "restante": restante_cd,
                "lojas": sum(1 for item in itens_cd if item["restante"] > 0),
            }
        )
    lojas_com_saldo = sorted(
        [item for item in linhas.values() if item["restante"] > 0],
        key=lambda item: (item["restante"], item["saldo"], item["loja"]),
        reverse=True,
    )[:12]
    lojas_por_cd = {
        cd_item: sorted(
            [item for item in linhas.values() if item["cd"] == cd_item and item["restante"] > 0],
            key=lambda item: (item["restante"], item["saldo"], item["loja"]),
            reverse=True,
        )[:8]
        for cd_item in cds
    }

    return {
        "inicio": inicio,
        "fim": fim,
        "loja_filtro": loja_filtro,
        "linhas": linhas_ordenadas[:300],
        "por_cd": por_cd,
        "lojas_com_saldo": lojas_com_saldo,
        "lojas_por_cd": lojas_por_cd,
        "saldo_atual_linhas": saldo_atual_linhas,
        "saldo_atual_por_cd": saldo_atual_por_cd,
        "saldo_atual_resumo_por_cd": saldo_atual_resumo_por_cd,
        "modo_unificado": len(cds) > 1,
        "total_saldo": total_saldo,
        "total_expedido": total_expedido,
        "total_restante": max(total_saldo - total_expedido, 0),
        "lojas": sorted(set(lojas.values()), key=loja_codigo),
    }


@login_required
def painel_gestao(request):
    if not user_has_perm(request.user, "painel_gestao"):
        profile = ensure_profile(request.user)
        if profile and profile.cargo == "motorista":
            return redirect("module_list", key="checklist_frota")
        messages.error(request, "Você não tem acesso ao Painel Gerencial.")
        return redirect("dashboard")

    cd = current_cd(request)
    fim_historico = parse_iso_date(request.GET.get("fim"), timezone.localdate())
    inicio_historico = parse_iso_date(request.GET.get("inicio"), fim_historico)
    if inicio_historico > fim_historico:
        inicio_historico, fim_historico = fim_historico, inicio_historico
    periodo_limite_aplicado = False
    limite_inicio = fim_historico - timezone.timedelta(days=179)
    if inicio_historico < limite_inicio:
        inicio_historico = limite_inicio
        periodo_limite_aplicado = True
    loja_historico = request.GET.get("loja_hist", "").strip()
    dados = {
        **build_painel_gerencial_cards(cd, inicio_historico, fim_historico),
        "historico_pallets": build_pallets_historico_gerencial(cd, inicio_historico, fim_historico, loja_historico),
    }

    ctx = context_base(request)
    ctx.update(
        {
            "inicio_historico": inicio_historico.isoformat(),
            "fim_historico": fim_historico.isoformat(),
            "periodo_limite_aplicado": periodo_limite_aplicado,
            "cd_escopo": cd,
            "cd_escopo_label": cd_scope_label(cd),
            "can_unified_view": can_view_unified_cd(request.user),
            **dados,
        }
    )
    return render(request, "painel/painel_gestao.html", ctx)


@login_required
def central_personalizada(request, key):
    hub = next((item for item in custom_hubs_for_user(request.user) if item["key"] == key), None)
    if not hub:
        messages.warning(request, "Central não encontrada ou sem painéis liberados para este usuário.")
        return redirect("dashboard")
    hub = dynamic_expedition_hub(request, hub)
    ctx = context_base(request)
    ctx.update({"hub": hub})
    return render(request, "painel/central_personalizada.html", ctx)


def render_storage_warning():
    return bool(os.environ.get("RENDER") or os.environ.get("RENDER_EXTERNAL_HOSTNAME"))


def database_size_bytes():
    try:
        with connection.cursor() as cursor:
            if connection.vendor == "postgresql":
                cursor.execute("select pg_database_size(current_database())")
                return int(cursor.fetchone()[0] or 0)
            if connection.vendor == "sqlite":
                path = connection.settings_dict.get("NAME")
                return Path(path).stat().st_size if path and Path(path).exists() else 0
    except Exception:
        return 0
    return 0


def database_health_snapshot():
    size_bytes = database_size_bytes()
    registros = []
    total = 0
    for module in MODULES:
        try:
            count = module.model.objects.count()
        except Exception:
            count = 0
        total += count
        if count:
            registros.append({"titulo": module.title, "quantidade": count})
    registros = sorted(registros, key=lambda item: item["quantidade"], reverse=True)[:8]
    last_backup = BackupLog.objects.filter(status="ok").first()
    warnings = []
    if render_storage_warning():
        warnings.append("Render Free é bom para teste, mas não deve ser o único lugar dos dados reais.")
    if not last_backup:
        warnings.append("Ainda não existe backup OK registrado.")
    elif timezone.now() - last_backup.criado_em > timedelta(hours=24):
        warnings.append("Último backup OK tem mais de 24 horas.")
    return {
        "engine": connection.vendor,
        "size_bytes": size_bytes,
        "size_mb": round(size_bytes / (1024 * 1024), 1) if size_bytes else 0,
        "total_registros": total,
        "maiores_tabelas": registros,
        "render": render_storage_warning(),
        "avisos": warnings,
    }


def full_backup_zip_response(user):
    data_buffer = io.StringIO()
    call_command(
        "dumpdata",
        "--natural-foreign",
        "--natural-primary",
        "--exclude",
        "contenttypes",
        "--exclude",
        "auth.permission",
        stdout=data_buffer,
    )
    stamp = timezone.localtime().strftime("%Y%m%d_%H%M%S")
    diagnostics = {
        "sistema": "Modelo de Teste",
        "gerado_em": timezone.localtime().isoformat(),
        "usuario": user.username if user and user.is_authenticated else "",
        "banco": database_status(),
        "saude": database_health_snapshot(),
    }
    readme = "\n".join(
        [
            "BACKUP COMPLETO - Modelo de Teste",
            "",
            "Este ZIP contém os dados exportados pelo próprio sistema.",
            "Guarde uma cópia fora do Render e fora do computador do trabalho.",
            "Para restaurar, peça ajuda no Codex antes de importar.",
        ]
    )
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("modelo_teste_dados.json", data_buffer.getvalue())
        archive.writestr("diagnostico_backup.json", json.dumps(diagnostics, ensure_ascii=False, indent=2, default=str))
        archive.writestr("LEIA_ME_BACKUP.txt", readme)
    payload = output.getvalue()
    BackupLog.objects.create(
        destino="local",
        status="ok",
        arquivo=f"download_backup_completo_{stamp}.zip",
        tamanho_bytes=len(payload),
        mensagem="Backup completo ZIP baixado pelo navegador.",
        criado_por=user,
    )
    response = HttpResponse(payload, content_type="application/zip")
    response["Content-Disposition"] = f'attachment; filename="modelo_teste_backup_completo_{stamp}.zip"'
    return response


@login_required
def central_servidor(request):
    if not user_has_perm(request.user, "central_servidor"):
        messages.error(request, "Você não tem acesso à central do servidor.")
        return redirect("dashboard")

    if request.method == "POST":
        action = request.POST.get("action")
        if action == "salvar_pasta_50":
            pasta = request.POST.get("pasta_50", "").strip()
            set_config("pasta_50_backup", pasta, user=request.user, descricao="Pasta compartilhada .50 para backups")
            set_config(
                "backup_automatico_50",
            "sim" if request.POST.get("backup_automatico_50") == "on" else "não",
                user=request.user,
                descricao="Backup automático diário na pasta .50",
            )
            log_action(request, "configuracao_servidor", "central_servidor", detalhe=f"Pasta .50: {pasta}")
            messages.success(request, "Configuração salva.")
            return redirect("central_servidor")
        if action == "backup_local":
            try:
                log = create_backup("local", user=request.user)
                log_action(request, "backup_local", "central_servidor", detalhe=log.arquivo)
                messages.success(request, "Backup local criado.")
            except Exception as exc:
                messages.error(request, f"Falha no backup local: {exc}")
            return redirect("central_servidor")
        if action == "backup_50":
            try:
                log = create_backup("rede_50", user=request.user)
                log_action(request, "backup_50", "central_servidor", detalhe=log.arquivo or log.mensagem)
                if log.status == "ok":
                    messages.success(request, "Backup criado na pasta .50.")
                else:
                    messages.error(request, log.mensagem)
            except Exception as exc:
                messages.error(request, f"Falha no backup .50: {exc}")
            return redirect("central_servidor")
        if action == "backup_zip":
            log_action(request, "backup_zip", "central_servidor", detalhe="Backup completo baixado pelo navegador")
            return full_backup_zip_response(request.user)
        if action == "publicar_endereco":
            payload = publish_server_address(user=request.user)
            if payload:
                log_action(request, "endereco_publicado", "central_servidor", detalhe=payload["principal"])
                messages.success(request, "Endereço publicado na pasta .50.")
            else:
                messages.error(request, "Configure uma pasta .50 válida antes de publicar.")
            return redirect("central_servidor")

    usuarios_ativos = active_user_sessions()
    active_sessions = len(usuarios_ativos)
    pasta_50 = get_config("pasta_50_backup", "")
    pasta_50_ok = bool(network_backup_dir())
    backups = BackupLog.objects.all()[:12]
    last_backup = BackupLog.objects.filter(status="ok").first()
    ctx = context_base(request)
    ctx.update(
        {
            "urls_servidor": server_urls(),
            "db_status": database_status(),
            "db_health": database_health_snapshot(),
            "disk": disk_status(),
            "pasta_50": pasta_50,
            "pasta_50_ok": pasta_50_ok,
            "backup_automatico_50": get_config("backup_automatico_50", "sim").lower() in {"sim", "true", "1"},
            "backups": backups,
            "last_backup": last_backup,
            "active_sessions": active_sessions,
            "usuarios_ativos": usuarios_ativos,
            "ultimo_endereco_publicado": get_config("ultimo_endereco_publicado", ""),
        }
    )
    return render(request, "painel/central_servidor.html", ctx)


def object_matches_search(obj, q):
    if not q:
        return True
    qn = normalize(q)
    for field in obj._meta.fields:
        if field.name in {"observacao", "criado_em", "atualizado_em"} or isinstance(field, django_models.CharField):
            if qn in normalize(getattr(obj, field.name, "")):
                return True
    return False


def module_export_value(obj, field_name):
    display = getattr(obj, f"get_{field_name}_display", None)
    if callable(display):
        value = display()
    else:
        value = getattr(obj, field_name, "")
        if callable(value):
            value = value()
    if isinstance(value, bool):
        return "Sim" if value else "Não"
    if hasattr(value, "strftime"):
        class_name = value.__class__.__name__.lower()
        if class_name == "time":
            return value.strftime("%H:%M")
        if hasattr(value, "hour"):
            return value.strftime("%d/%m/%Y %H:%M")
        return value.strftime("%d/%m/%Y")
    return xlsx_value(value)


def export_module_csv(module, rows, cd, q):
    response = HttpResponse(content_type="text/csv; charset=utf-8-sig")
    response["Content-Disposition"] = f'attachment; filename="{module.key}_cd_{cd}.csv"'
    response.write("\ufeff")
    writer = csv.writer(response, delimiter=";")
    writer.writerow([module.title, f"CD {cd}", f"Filtro: {q or 'todos'}"])
    writer.writerow([])
    writer.writerow([module.model._meta.get_field(field).verbose_name for field in module.fields])
    for obj in rows:
        writer.writerow([module_export_value(obj, field) for field in module.fields])
    return response


def export_module_xlsx(module, rows, cd, q):
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Registros"
    title_fill = PatternFill("solid", fgColor="D9EAF7")
    header_fill = PatternFill("solid", fgColor="EDF2F7")
    sheet.append([module.title, f"CD {cd}", f"Filtro: {q or 'todos'}"])
    for cell in sheet[1]:
        cell.font = Font(bold=True)
        cell.fill = title_fill
    sheet.append([])
    sheet.append([module.model._meta.get_field(field).verbose_name for field in module.fields])
    for cell in sheet[sheet.max_row]:
        cell.font = Font(bold=True)
        cell.fill = header_fill
    for obj in rows:
        sheet.append([module_export_value(obj, field) for field in module.fields])
    for column in sheet.columns:
        max_length = max(len(str(cell.value or "")) for cell in column)
        sheet.column_dimensions[column[0].column_letter].width = min(max(max_length + 2, 12), 45)
    output = io.BytesIO()
    workbook.save(output)
    output.seek(0)
    response = HttpResponse(
        output.read(),
        content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )
    response["Content-Disposition"] = f'attachment; filename="{module.key}_cd_{cd}.xlsx"'
    return response


def export_module_docx(module, rows, cd, q):
    from docx import Document

    rows = list(rows)
    document = Document()
    document.add_heading(module.title, level=1)
    document.add_paragraph(f"CD: {cd}")
    document.add_paragraph(f"Filtro aplicado: {q or 'todos'}")
    document.add_paragraph(f"Registros: {len(rows)}")

    fields = list(module.list_display)
    table = document.add_table(rows=1, cols=len(fields))
    table.style = "Table Grid"
    header = table.rows[0].cells
    for index, field in enumerate(fields):
        header[index].text = FIELD_LABELS.get(field, str(field).replace("_", " ").title())
    for obj in rows:
        cells = table.add_row().cells
        for index, field in enumerate(fields):
            cells[index].text = str(module_export_value(obj, field))

    output = io.BytesIO()
    document.save(output)
    output.seek(0)
    response = HttpResponse(
        output.read(),
        content_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    )
    response["Content-Disposition"] = f'attachment; filename="{module.key}_cd_{cd}.docx"'
    return response


def frota_issue_labels(row):
    checks = (
        ("pane_mecanica", "Pane mecanica"),
        ("furo_pneu", "Furo de pneu"),
        ("acidente", "Acidente"),
        ("multa", "Multa"),
        ("avaria_carga", "Avaria na carga"),
        ("vazamento_viagem", "Vazamento durante viagem"),
        ("outra_ocorrencia", "Outra ocorrencia"),
    )
    labels = [label for field, label in checks if getattr(row, field, False)]
    if getattr(row, "ocorrencia_rota_loja", ""):
        labels.append("Ocorrencia caminho/loja")
    return labels


def build_frota_powerbi_tables(cd, rows):
    rows = list(rows)
    lacres = LacreFrota.objects.all() if str(cd) == "801+806" else LacreFrota.objects.filter(cd_unidade=cd)
    total = len(rows)
    manutencao = sum(1 for row in rows if row.necessita_manutencao)
    ocorrencias = sum(1 for row in rows if frota_issue_labels(row))
    km_total = sum(row.km_rodado for row in rows)
    veiculos = len({row.placa for row in rows if row.placa})
    responsaveis = len({row.responsavel_frota for row in rows if row.responsavel_frota})

    tables = [
        (
            "frota_resumo.csv",
            ["cd", "indicador", "valor"],
            [
                [cd, "checklists_registrados", total],
                [cd, "veiculos_distintos", veiculos],
                [cd, "necessitam_manutencao", manutencao],
                [cd, "com_ocorrencia", ocorrencias],
                [cd, "lacres_abertos", lacres.exclude(status__in=["resolvido", "cancelado"]).count()],
                [cd, "km_rodado", km_total],
                [cd, "responsaveis_distintos", responsaveis],
            ],
        )
    ]

    tables.append(
        (
            "frota_checklists.csv",
            [
                "cd",
                "data",
                "tipo_checklist",
                "motorista",
                "veiculo",
                "placa",
                "intercala_cd",
                "cd_intercalacao",
                "intercalacao_concluida",
                "status_intercalacao",
                "km_inicial",
                "km_final",
                "km_rodado",
                "horario_saida",
                "horario_retorno",
                "necessita_manutencao",
                "descricao_manutencao",
                "responsavel_frota",
                "ocorrencias",
                "descricao_ocorrencia",
                "ocorrencia_rota_loja",
            ],
            [
                [
                    row.cd_unidade,
                    row.data,
                    row.get_tipo_checklist_display(),
                    row.motorista,
                    row.veiculo,
                    row.placa,
                    "Sim" if row.intercala_cd else "Nao",
                    row.cd_intercalacao,
                    "Sim" if row.intercalacao_concluida else "Nao",
                    row.status_intercalacao,
                    row.km_inicial,
                    row.km_final,
                    row.km_rodado,
                    row.horario_saida,
                    row.horario_retorno,
                    "Sim" if row.necessita_manutencao else "Nao",
                    row.descricao_manutencao,
                    row.responsavel_frota,
                    ", ".join(frota_issue_labels(row)),
                    row.descricao_ocorrencia,
                    row.ocorrencia_rota_loja,
                ]
                for row in rows
            ],
        )
    )

    by_plate = OrderedDict()
    by_responsible = OrderedDict()
    issue_rows = []
    maintenance_rows = []
    for row in rows:
        plate = row.placa or "Não informado"
        by_plate.setdefault(plate, {"checklists": 0, "manutencao": 0, "ocorrencias": 0, "km": 0})
        by_plate[plate]["checklists"] += 1
        by_plate[plate]["km"] += row.km_rodado
        if row.necessita_manutencao:
            by_plate[plate]["manutencao"] += 1
            maintenance_rows.append([row.cd_unidade, row.data, plate, row.veiculo, row.motorista, row.descricao_manutencao, row.responsavel_frota])
        labels = frota_issue_labels(row)
        if labels:
            by_plate[plate]["ocorrencias"] += len(labels)
            for label in labels:
                issue_rows.append([row.cd_unidade, row.data, plate, row.veiculo, row.motorista, label, row.descricao_ocorrencia])

        responsible = row.responsavel_frota or "Não informado"
        by_responsible.setdefault(responsible, {"checklists": 0, "manutencao": 0, "ocorrencias": 0})
        by_responsible[responsible]["checklists"] += 1
        by_responsible[responsible]["manutencao"] += 1 if row.necessita_manutencao else 0
        by_responsible[responsible]["ocorrencias"] += len(labels)

    tables.append(
        (
            "frota_por_placa.csv",
            ["cd", "placa", "checklists", "necessitam_manutencao", "ocorrencias", "km_rodado"],
            [[cd, plate, data["checklists"], data["manutencao"], data["ocorrencias"], data["km"]] for plate, data in by_plate.items()],
        )
    )
    tables.append(
        (
            "frota_ocorrencias.csv",
            ["cd", "data", "placa", "veiculo", "motorista", "ocorrencia", "descricao"],
            issue_rows,
        )
    )
    tables.append(
        (
            "frota_manutencao.csv",
            ["cd", "data", "placa", "veiculo", "motorista", "descricao_manutencao", "responsavel_frota"],
            maintenance_rows,
        )
    )
    tables.append(
        (
            "frota_por_responsavel.csv",
            ["cd", "responsavel_frota", "checklists", "necessitam_manutencao", "ocorrencias"],
            [[cd, name, data["checklists"], data["manutencao"], data["ocorrencias"]] for name, data in by_responsible.items()],
        )
    )
    tables.append(
        (
            "frota_lacres.csv",
            ["cd_registro", "data", "motorista", "placa", "loja_destino", "lacre", "cd_lacre", "motivo", "status", "tratado_por"],
            [
                [
                    row.cd_unidade,
                    row.data,
                    row.motorista,
                    row.placa,
                    row.loja_destino,
                    row.lacre,
                    row.get_cd_lacre_display(),
                    row.get_motivo_display(),
                    row.get_status_display(),
                    row.tratado_por,
                ]
                for row in lacres.order_by("data", "placa")
            ],
        )
    )
    return tables


def export_frota_powerbi_package(cd, rows, q):
    folder = powerbi_output_dir() / "frota"
    folder.mkdir(parents=True, exist_ok=True)
    files = []
    for filename, headers, table_rows in build_frota_powerbi_tables(cd, rows):
        files.append(write_powerbi_file(folder, filename, headers, table_rows))

    readme = (
        "Modelo de Teste - BASE POWER BI DA FROTA\n\n"
        f"CD: {cd}\n"
        f"Filtro aplicado: {q or 'todos'}\n"
        f"Pasta atualizada: {folder}\n\n"
        "Arquivos principais:\n"
        "- frota_resumo.csv: cards gerais.\n"
        "- frota_checklists.csv: base completa dos checklists.\n"
        "- frota_por_placa.csv: graficos por veiculo/placa.\n"
        "- frota_ocorrencias.csv: tipos de ocorrência.\n"
        "- frota_manutencao.csv: itens enviados para manutenção.\n"
        "- frota_por_responsavel.csv: acompanhamento por responsável.\n"
        "- frota_lacres.csv: ocorrências de lacres por CD, placa, motorista e loja.\n"
    )
    readme_path = folder / "LEIA_ME_POWER_BI_FROTA.txt"
    readme_path.write_text(readme, encoding="utf-8")
    files.append((readme_path, readme.encode("utf-8")))

    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as package:
        for path, content in files:
            package.writestr(path.name, content)

    response = HttpResponse(output.getvalue(), content_type="application/zip")
    response["Content-Disposition"] = f'attachment; filename="powerbi_frota_cd_{cd}.zip"'
    return response


def choice_text(model, field_name, value):
    field = model._meta.get_field(field_name)
    return dict(field.choices).get(value, value or "Não informado")


def separacao_categoria_label(value):
    if not value:
        return "Não informado"
    parametro = ParametroCD.objects.filter(tipo="categoria_separacao", nome=value).first()
    return parametro.nome if parametro else choice_text(Separacao, "categoria", value)


def equipment_bar_rows(queryset, model, field_name, total):
    rows = []
    for item in queryset.values(field_name).annotate(total=Count("id")).order_by("-total", field_name):
        value = item[field_name]
        count = item["total"] or 0
        rows.append(
            {
                "label": choice_text(model, field_name, value),
                "count": count,
                "percent": int((count / total) * 100) if total else 0,
            }
        )
    return rows


def build_equipamentos_dashboard(cd):
    def build():
        equipamentos = Equipamento.objects.filter(cd_unidade=cd)
        manutencoes = EquipamentoManutencao.objects.filter(cd_unidade=cd)
        total = equipamentos.count()
        status_counts = {row["status"]: row["total"] for row in equipamentos.values("status").annotate(total=Count("id"))}
        tipo_rows = equipment_bar_rows(equipamentos, Equipamento, "tipo", total)
        status_rows = equipment_bar_rows(equipamentos, Equipamento, "status", total)
        manutencoes_abertas = manutencoes.filter(status__in=["aberto", "enviado"]).count()
        manutencoes_ti = manutencoes.filter(destino="ti")
        ranking_raw = list(
            manutencoes_ti.exclude(patrimonio="")
            .values("patrimonio")
            .annotate(total=Count("id"))
            .order_by("-total", "patrimonio")[:10]
        )
        equipamentos_por_patrimonio = {
            row["patrimonio"]: row["equipamento"] or "Sem descrição"
            for row in equipamentos.exclude(patrimonio="").values("patrimonio", "equipamento")
        }
        max_reparos = max([row["total"] for row in ranking_raw] or [1])
        ranking = []
        for row in ranking_raw:
            equipamento_nome = equipamentos_por_patrimonio.get(row["patrimonio"], "Sem descrição")
            nivel = "critico" if row["total"] >= 4 else "atencao" if row["total"] >= 3 else "normal"
            ranking.append(
                {
                    "patrimonio": row["patrimonio"],
                    "equipamento": equipamento_nome,
                    "total": row["total"],
                    "percent": int((row["total"] / max_reparos) * 100) if max_reparos else 0,
                    "nivel": nivel,
                    "acao": "Troca recomendada" if row["total"] >= 3 else "Monitorar",
                }
            )
        criticos_ti = sum(1 for row in ranking if row["total"] >= 3)
        sem_patrimonio = equipamentos.filter(patrimonio="").count()
        disponiveis = status_counts.get("devolvido", 0)
        em_uso = status_counts.get("em_uso", 0)
        manutencao_status = status_counts.get("manutencao", 0)

        return {
            "cards": [
                {"label": "Equipamentos cadastrados", "value": total},
                {"label": "Em uso", "value": em_uso},
                {"label": "Em manutenção", "value": manutencao_status},
                {"label": "Manutenções abertas", "value": manutencoes_abertas},
                {"label": "Idas ao TI registradas", "value": manutencoes_ti.count()},
            ],
            "health_cards": [
                {"label": "Disponíveis/devolvidos", "value": disponiveis, "tone": "ok"},
                {"label": "Em manutenção", "value": manutencao_status, "tone": "attention"},
                {"label": "Críticos 3+ TI", "value": criticos_ti, "tone": "critical" if criticos_ti else "ok"},
                {"label": "Sem patrimônio", "value": sem_patrimonio, "tone": "attention" if sem_patrimonio else "ok"},
            ],
            "status_rows": status_rows,
            "tipo_rows": tipo_rows,
            "ranking_ti": ranking,
            "sem_patrimonio": sem_patrimonio,
            "setor_rows": equipment_bar_rows(equipamentos, Equipamento, "setor", total),
            "manutencoes_recentes": list(manutencoes.order_by("-data", "-id")[:8]),
        }

    return cached_runtime_value(("equipamentos_dashboard", str(cd)), 30, build)


CHECKLIST_BASE_GROUP_CODES = [
    "identificacao_rota",
    "intercalacao_cds",
    "saida_documentos_seguranca",
    "saida_veiculo_bau",
    "chegada_loja",
    "ocorrencia_rota_loja",
    "retorno_cd",
    "fechamento_frota",
]

CHECKLIST_INTERCALACAO_FIELDS = {"intercala_cd", "cd_intercalacao", "intercalacao_concluida", "observacao_intercalacao"}


def checklist_frota_base_groups():
    groups = [
        {
            "title": "Identificação da rota",
            "hint": "Escolha a etapa do registro e confira motorista, placa e destino.",
            "kind": "ambos",
            "open": True,
            "fields": ["tipo_checklist", "data", "motorista", "placa", "loja_destino"],
        },
        {
            "title": "Intercalação entre CDs",
            "hint": "Use quando sair de um CD e precisar completar carga ou conferência no outro CD.",
            "kind": "ambos",
            "fields": ["intercala_cd", "cd_intercalacao", "intercalacao_concluida", "observacao_intercalacao"],
        },
        {
            "title": "Saída do CD - documentos e segurança",
            "hint": "Conferência obrigatória antes de sair do CD.",
            "kind": "saida",
            "fields": [
                "km_inicial",
                "horario_saida",
                "documentacao_veiculo_ok",
                "documentacao_veiculo_obs",
                "cnh_motorista_ok",
                "cnh_motorista_obs",
                "documento_motorista_ok",
                "documento_motorista_obs",
                "tacografo_ok",
                "tacografo_obs",
                "rastreador_ok",
                "rastreador_obs",
                "pneus_ok",
                "pneus_obs",
                "calibragem_pneus_ok",
                "calibragem_pneus_obs",
                "estepe_ok",
                "estepe_obs",
                "macaco_chave_ok",
                "macaco_chave_obs",
                "triangulo_ok",
                "triangulo_obs",
                "extintor_ok",
                "extintor_obs",
                "cintos_ok",
                "cintos_obs",
            ],
        },
        {
            "title": "Saída do CD - veículo e baú",
            "hint": "Luzes, mecânica, cabine e baú antes da viagem.",
            "kind": "saida",
            "fields": [
                "farois_ok",
                "farois_obs",
                "lanternas_ok",
                "lanternas_obs",
                "luz_freio_ok",
                "luz_freio_obs",
                "setas_ok",
                "setas_obs",
                "luz_re_ok",
                "luz_re_obs",
                "limpador_para_brisa_ok",
                "limpador_para_brisa_obs",
                "agua_limpador_ok",
                "agua_limpador_obs",
                "buzina_ok",
                "buzina_obs",
                "retrovisores_ok",
                "retrovisores_obs",
                "lataria_ok",
                "lataria_obs",
                "freios_ok",
                "freios_obs",
                "embreagem_ok",
                "embreagem_obs",
                "direcao_ok",
                "direcao_obs",
                "suspensao_ok",
                "suspensao_obs",
                "bateria_ok",
                "bateria_obs",
                "vazamentos_ok",
                "vazamentos_obs",
                "combustivel_ok",
                "combustivel_obs",
                "arla_ok",
                "arla_obs",
                "oleo_motor_ok",
                "oleo_motor_obs",
                "agua_radiador_ok",
                "agua_radiador_obs",
                "cabine_limpa_saida_ok",
                "cabine_limpa_saida_obs",
                "bau_limpo_saida_ok",
                "bau_limpo_saida_obs",
                "vedacao_bau_ok",
                "vedacao_bau_obs",
                "portas_travas_ok",
                "portas_travas_obs",
            ],
        },
        {
            "title": "Chegada na loja",
            "hint": "Registre o horário de chegada e qualquer ponto observado no trajeto ou na loja.",
            "kind": "chegada_loja",
            "fields": [
                "horario_chegada_loja",
                "ocorrencia_rota_loja",
            ],
        },
        {
            "title": "Saída da loja",
            "hint": "Registre a saída da loja e ocorrências antes de voltar ao CD.",
            "kind": "saida_loja",
            "fields": [
                "horario_saida_loja",
                "carga_retorno",
                "carga_retorno_descricao",
                "pane_mecanica",
                "furo_pneu",
                "acidente",
                "multa",
                "avaria_carga",
                "vazamento_viagem",
                "outra_ocorrencia",
                "descricao_ocorrencia",
                "ocorrencia_rota_loja",
            ],
        },
        {
            "title": "Retorno ao CD",
            "hint": "Fechamento ao voltar para o CD depois da entrega.",
            "kind": "retorno",
            "fields": [
                "km_final",
                "horario_retorno",
                "veiculo_limpo_retorno_ok",
                "veiculo_limpo_retorno_obs",
                "bau_limpo_retorno_ok",
                "bau_limpo_retorno_obs",
                "combustivel_informado_ok",
                "combustivel_informado_obs",
                "novas_avarias_ok",
                "novas_avarias_obs",
                "pneus_sem_danos_ok",
                "pneus_sem_danos_obs",
                "documentacao_entregue_ok",
                "documentacao_entregue_obs",
                "chaves_devolvidas_ok",
                "chaves_devolvidas_obs",
            ],
        },
        {
            "title": "Pendência / Manutenção da Frota",
            "hint": "Uso da frota ou supervisão para registrar manutenção, pendência ou observação final.",
            "kind": "retorno",
            "fields": ["necessita_manutencao", "descricao_manutencao", "responsavel_frota", "observacao"],
        },
    ]
    labels = [
        ("Identificação da rota", "Escolha a etapa do registro e confira motorista, placa e destino."),
        ("Intercalação entre CDs", "Use quando sair de um CD e precisar completar carga ou conferência no outro CD."),
        ("Saída do CD - documentos e segurança", "Conferência obrigatória antes de sair do CD."),
        ("Saída do CD - veículo e baú", "Luzes, mecânica, cabine e baú antes da viagem."),
        ("Chegada na loja", "Registre o horário de chegada e qualquer ponto observado no trajeto ou na loja."),
        ("Saída da loja", "Registre a saída da loja e ocorrências antes de voltar ao CD."),
        ("Retorno ao CD", "Fechamento ao voltar para o CD depois da entrega."),
        ("Pendência / Manutenção da Frota", "Uso da frota ou supervisão para registrar manutenção, pendência ou observação final."),
    ]
    for group, (title, hint) in zip(groups, labels):
        group["title"] = title
        group["hint"] = hint
    return groups


def checklist_field_label(field_name):
    try:
        return str(ChecklistFrota._meta.get_field(field_name).verbose_name)
    except Exception:
        return FIELD_LABELS.get(field_name, field_name.replace("_", " ").title())


_CHECKLIST_STRUCTURE_READY = set()


def ensure_checklist_structure(cd):
    cd = "801" if str(cd) == "801" else "806"
    if cd in _CHECKLIST_STRUCTURE_READY:
        if ChecklistFrotaGrupo.objects.filter(cd_unidade=cd).exists():
            return
        _CHECKLIST_STRUCTURE_READY.discard(cd)
    base_groups = checklist_frota_base_groups()
    expected_codes = set(CHECKLIST_BASE_GROUP_CODES[: len(base_groups)])
    expected_fields = {
        field_name
        for group in base_groups
        for field_name in group.get("fields", [])
    }
    existing_codes = set(
        ChecklistFrotaGrupo.objects.filter(cd_unidade=cd, codigo__in=expected_codes).values_list("codigo", flat=True)
    )
    existing_fields = set(
        ChecklistFrotaItem.objects.filter(cd_unidade=cd, campo_sistema__in=expected_fields).values_list("campo_sistema", flat=True)
    )
    if existing_codes == expected_codes and existing_fields == expected_fields:
        _CHECKLIST_STRUCTURE_READY.add(cd)
        return
    for index, group in enumerate(base_groups):
        code = CHECKLIST_BASE_GROUP_CODES[index]
        group_obj, created = ChecklistFrotaGrupo.objects.get_or_create(
            cd_unidade=cd,
            codigo=code,
            defaults={
                "titulo": group["title"],
                "dica": group.get("hint", ""),
                "tipo_checklist": group.get("kind", "ambos"),
                "ordem": (index + 1) * 10,
                "ativo": True,
                "mostrar_motorista": code != "fechamento_frota",
            },
        )
        group_updates = []
        desired_group_values = {
            "titulo": group["title"],
            "dica": group.get("hint", ""),
            "tipo_checklist": group.get("kind", "ambos"),
            "ordem": (index + 1) * 10,
            "mostrar_motorista": code != "fechamento_frota",
        }
        for field, value in desired_group_values.items():
            if getattr(group_obj, field) != value:
                setattr(group_obj, field, value)
                group_updates.append(field)
        if group_updates:
            group_obj.save(update_fields=[*group_updates, "atualizado_em"])
        for item_index, field_name in enumerate(group.get("fields", [])):
            item, item_created = ChecklistFrotaItem.objects.get_or_create(
                cd_unidade=cd,
                campo_sistema=field_name,
                defaults={
                    "grupo_config": group_obj,
                    "grupo": group_obj.titulo,
                    "titulo": checklist_field_label(field_name),
                    "tipo_checklist": group_obj.tipo_checklist,
                    "ordem": (item_index + 1) * 10,
                    "ativo": True,
                    "obrigatorio": False,
                },
            )
            updates = []
            if item.grupo_config_id != group_obj.pk:
                item.grupo_config = group_obj
                updates.append("grupo_config")
            if item.grupo != group_obj.titulo:
                item.grupo = group_obj.titulo
                updates.append("grupo")
            if item.tipo_checklist != group_obj.tipo_checklist:
                item.tipo_checklist = group_obj.tipo_checklist
                updates.append("tipo_checklist")
            if updates:
                item.save(update_fields=updates)
    _CHECKLIST_STRUCTURE_READY.add(cd)


def checklist_vehicle_for_initial(initial, cd):
    if not initial:
        return None
    placa = initial.get("placa", "")
    motorista = initial.get("motorista", "")
    qs = cd_queryset(VeiculoFrota, cd).filter(ativo=True)
    if placa:
        veiculo = qs.filter(placa__iexact=placa).order_by("-data", "-id").first()
        if veiculo:
            return veiculo
    if motorista:
        return qs.filter(motorista__iexact=motorista).order_by("-data", "-id").first()
    return None


def checklist_frequency_period(frequency, today=None):
    today = today or timezone.localdate()
    if frequency == "diario":
        return today, today
    if frequency == "semanal":
        start = today - timedelta(days=today.weekday())
        return start, start + timedelta(days=6)
    if frequency == "mensal":
        start = today.replace(day=1)
        next_month = (start.replace(day=28) + timedelta(days=4)).replace(day=1)
        return start, next_month - timedelta(days=1)
    return None, None


def checklist_frequency_for_item(item, vehicle=None):
    field = item.campo_sistema or ""
    if field in {"tacografo_ok", "tacografo_obs"} and vehicle:
        return vehicle.tacografo_frequencia or "diario"
    return item.frequencia or "sempre"


def checklist_frequency_lookup_field(field):
    if field == "tacografo_obs":
        return "tacografo_ok"
    if field.endswith("_obs"):
        candidate = f"{field[:-4]}_ok"
        if hasattr(ChecklistFrota, candidate):
            return candidate
    return field


def checklist_item_due(item, cd=None, initial=None, vehicle=None):
    frequency = checklist_frequency_for_item(item, vehicle)
    if frequency == "sempre" or not item.campo_sistema:
        return True
    start, end = checklist_frequency_period(frequency)
    if not start or not end:
        return True
    placa = (initial or {}).get("placa", "")
    motorista = (initial or {}).get("motorista", "")
    if not placa and not motorista:
        return True
    lookup_field = checklist_frequency_lookup_field(item.campo_sistema)
    if not hasattr(ChecklistFrota, lookup_field):
        return True
    qs = ChecklistFrota.objects.filter(data__range=(start, end))
    if cd:
        qs = cd_queryset(ChecklistFrota, cd).filter(data__range=(start, end))
    if placa:
        qs = qs.filter(placa__iexact=placa)
    elif motorista:
        qs = qs.filter(motorista__iexact=motorista)
    for row in qs.order_by("-data", "-id")[:50]:
        if getattr(row, lookup_field, None):
            return False
    return True


def checklist_frota_groups(motorista=False, cd=None, initial=None):
    show_manual_intercalacao = system_rule_enabled("checklist_intercalacao_manual")
    if not cd:
        groups = checklist_frota_base_groups()
        if not show_manual_intercalacao:
            groups = [group for group in groups if not (CHECKLIST_INTERCALACAO_FIELDS & set(group.get("fields", [])))]
        return groups[:-1] if motorista else groups
    ensure_checklist_structure(cd)
    vehicle = checklist_vehicle_for_initial(initial, cd)
    active_items = ChecklistFrotaItem.objects.filter(ativo=True).exclude(campo_sistema="").order_by("ordem", "titulo")
    group_rows = (
        cd_queryset(ChecklistFrotaGrupo, cd)
        .filter(ativo=True)
        .prefetch_related(Prefetch("itens", queryset=active_items, to_attr="active_system_items"))
        .order_by("ordem", "titulo")
    )
    if motorista:
        group_rows = group_rows.filter(mostrar_motorista=True)
    groups = []
    seen_groups = set()
    for group in group_rows:
        fields = [
            item.campo_sistema
            for item in group.active_system_items
            if item.campo_sistema and item.campo_sistema != "veiculo" and checklist_item_due(item, cd=cd, initial=initial, vehicle=vehicle)
        ]
        if not fields:
            continue
        deduped_fields = []
        for field in fields:
            if not show_manual_intercalacao and field in CHECKLIST_INTERCALACAO_FIELDS:
                continue
            if field not in deduped_fields:
                deduped_fields.append(field)
        if not deduped_fields:
            continue
        group_key = (group.codigo, group.titulo, group.tipo_checklist, tuple(deduped_fields))
        if group_key in seen_groups:
            continue
        seen_groups.add(group_key)
        groups.append(
            {
                "code": group.codigo,
                "title": group.titulo,
                "hint": group.dica,
                "kind": group.tipo_checklist,
                "open": group.codigo == "identificacao_rota",
                "fields": deduped_fields,
            }
        )
    return groups


def checklist_frota_custom_groups(cd):
    ensure_checklist_structure(cd)
    rows = (
        cd_queryset(ChecklistFrotaItem, cd)
        .filter(ativo=True, campo_sistema="")
        .filter(Q(grupo_config__isnull=True) | Q(grupo_config__ativo=True))
        .select_related("grupo_config")
        .order_by("grupo_config__ordem", "ordem", "grupo", "titulo")
    )
    grouped = OrderedDict()
    seen_items = set()
    for row in rows:
        title = row.grupo_config.titulo if row.grupo_config_id else (row.grupo or "Itens adicionais")
        kind = row.tipo_checklist
        item_key = (title, kind, row.titulo, bool(row.obrigatorio))
        if item_key in seen_items:
            continue
        seen_items.add(item_key)
        key = (title, kind)
        grouped.setdefault(key, {"title": title, "kind": kind, "items": []})
        grouped[key]["items"].append(row)
    return list(grouped.values())


def checklist_item_editor_context(rows, cd):
    ensure_checklist_structure(cd)
    rows = sorted(rows, key=lambda item: (item.grupo or "", item.ordem, item.titulo or ""))
    groups = OrderedDict()
    group_configs = list(cd_queryset(ChecklistFrotaGrupo, cd).order_by("ordem", "titulo"))
    for group_config in group_configs:
        groups[group_config.pk] = {"title": group_config.titulo, "items": [], "config": group_config}
    for row in rows:
        group_key = row.grupo_config_id or f"texto:{row.grupo or 'Itens adicionais'}"
        group_name = row.grupo_config.titulo if row.grupo_config_id else (row.grupo or "Itens adicionais")
        group = groups.setdefault(group_key, {"title": group_name, "items": [], "config": row.grupo_config})
        group["items"].append(row)
    return {"groups": list(groups.values()), "group_configs": group_configs}


def checklist_custom_payload(request, cd):
    payload = {}
    ensure_checklist_structure(cd)
    items = (
        cd_queryset(ChecklistFrotaItem, cd)
        .filter(ativo=True, campo_sistema="")
        .filter(Q(grupo_config__isnull=True) | Q(grupo_config__ativo=True))
    )
    for item in items:
        key = f"custom_item_{item.pk}"
        payload[str(item.pk)] = {
            "titulo": item.titulo,
            "grupo": item.grupo,
            "ok": request.POST.get(key) == "on",
            "observacao": request.POST.get(f"{key}_obs", "").strip(),
        }
    return payload


def apply_checklist_field_labels(form, cd):
    if not form or getattr(form._meta, "model", None) is not ChecklistFrota:
        return
    ensure_checklist_structure(cd)
    if not system_rule_enabled("checklist_intercalacao_manual"):
        for field_name in CHECKLIST_INTERCALACAO_FIELDS:
            form.fields.pop(field_name, None)
    items = cd_queryset(ChecklistFrotaItem, cd).filter(ativo=True).exclude(campo_sistema="")
    for item in items:
        if item.campo_sistema in form.fields:
            form.fields[item.campo_sistema].label = item.titulo


FERIAS_COLORS_CONFIG = "ferias_cores_status"
DEFAULT_FERIAS_COLORS = {
    "programada": "#dbeafe",
    "pre_ferias": "#fff3bf",
    "em_ferias": "#d1fae5",
    "retornando": "#fde68a",
    "retornado": "#e5e7eb",
    "cancelada": "#fee2e2",
}


def ferias_colors():
    try:
        payload = json.loads(get_config(FERIAS_COLORS_CONFIG, "{}") or "{}")
    except (TypeError, ValueError):
        payload = {}
    return {**DEFAULT_FERIAS_COLORS, **{key: value for key, value in payload.items() if isinstance(value, str) and value.startswith("#")}}


def ferias_alertas(cd):
    hoje = timezone.localdate()
    limite = hoje + timezone.timedelta(days=7)

    def build():
        return list(
            cd_queryset(ColaboradorFerias, cd)
            .filter(notificar_supervisor=True)
            .exclude(status__in=["retornado", "cancelada"])
            .filter(Q(inicio_ferias__lte=limite) | Q(fim_ferias__lte=limite) | Q(retorno_previsto__lte=limite))
            .order_by("inicio_ferias", "colaborador")[:8]
        )

    return cached_runtime_value(("ferias_alertas", str(cd), hoje.isoformat()), 30, build)


def ferias_heat_level(count):
    if count >= 4:
        return "critico"
    if count == 3:
        return "risco"
    if count in {1, 2}:
        return "atencao"
    return "baixo"


def build_ferias_heatmap(cd):
    hoje = timezone.localdate()
    cache_key = ("ferias_heatmap", str(cd), hoje.isoformat())

    def build():
        inicio = hoje - timezone.timedelta(days=hoje.weekday())
        dias = [inicio + timezone.timedelta(days=idx) for idx in range(35)]
        fim = dias[-1]
        ferias = list(
            cd_queryset(ColaboradorFerias, cd)
            .exclude(status__in=["cancelada", "retornado"])
            .filter(inicio_ferias__lte=fim, retorno_previsto__gte=inicio)
            .order_by("setor", "funcao", "colaborador")
        )

        setores = OrderedDict()
        funcoes = OrderedDict()
        colaboradores = []
        totais_por_dia = [0] * len(dias)

        for item in ferias:
            setor_label = item.get_setor_display()
            funcao_label = item.funcao or "Sem função informada"
            funcao_key = f"{setor_label} / {funcao_label}"
            setores.setdefault(setor_label, [0] * len(dias))
            funcoes.setdefault(funcao_key, {"label": funcao_key, "counts": [0] * len(dias)})

            dia_cells = []
            for idx, dia in enumerate(dias):
                ativo = item.inicio_ferias <= dia <= item.fim_ferias
                retorno = item.fim_ferias < dia <= item.retorno_previsto
                if ativo:
                    setores[setor_label][idx] += 1
                    funcoes[funcao_key]["counts"][idx] += 1
                    totais_por_dia[idx] += 1
                dia_cells.append({"date": dia, "active": ativo, "returning": retorno})

            colaboradores.append(
                {
                    "colaborador": item.colaborador,
                    "setor": setor_label,
                    "funcao": funcao_label,
                    "inicio": item.inicio_ferias,
                    "fim": item.fim_ferias,
                    "retorno": item.retorno_previsto,
                    "status": item.get_status_display(),
                    "days": dia_cells,
                }
            )

        def build_cells(counts):
            return [
                {"date": dias[idx], "count": count, "level": ferias_heat_level(count)}
                for idx, count in enumerate(counts)
            ]

        setor_rows = [
            {"label": label, "cells": build_cells(counts), "total": sum(counts)}
            for label, counts in setores.items()
        ]
        funcao_rows = [
            {"label": row["label"], "cells": build_cells(row["counts"]), "total": sum(row["counts"])}
            for row in funcoes.values()
        ]
        hot_days = [
            {"date": dias[idx], "count": count, "level": ferias_heat_level(count)}
            for idx, count in enumerate(totais_por_dia)
            if count
        ]

        return {
            "days": dias,
            "setor_rows": setor_rows,
            "funcao_rows": sorted(funcao_rows, key=lambda row: (-row["total"], row["label"]))[:10],
            "colaborador_rows": colaboradores[:40],
            "hot_days": sorted(hot_days, key=lambda row: (-row["count"], row["date"]))[:8],
            "total_colaboradores": len(ferias),
            "periodo_inicio": inicio,
            "periodo_fim": fim,
            "legend": [
                {"label": "0", "class": "baixo", "text": "Sem pressão"},
                {"label": "1-2", "class": "atencao", "text": "Atenção"},
                {"label": "3", "class": "risco", "text": "Risco"},
                {"label": "4+", "class": "critico", "text": "Crítico"},
            ],
        }

    return cached_runtime_value(cache_key, 30, build)


def build_capacidade_operacao(cd, config=None):
    config = config or ferias_alert_config()
    hoje = timezone.localdate()
    config_key = json.dumps(config, sort_keys=True, ensure_ascii=False)

    def build():
        limite = hoje + timezone.timedelta(days=7)
        detailed_enabled = feature_enabled("colaboradores_setor_detalhado")
        pessoas = list(cd_queryset(PessoaTurno, cd).filter(data=hoje).order_by("setor", "turno", "funcao"))
        ferias_periodo = list(
            cd_queryset(ColaboradorFerias, cd)
            .exclude(status__in=["cancelada", "retornado"])
            .filter(inicio_ferias__lte=limite, retorno_previsto__gte=hoje)
            .order_by("setor", "funcao", "colaborador")
        )
        ausencias_periodo = list(
            cd_queryset(ColaboradorAusencia, cd)
            .exclude(status__in=["cancelada", "encerrada"])
            .filter(inicio__lte=limite, fim__gte=hoje)
            .order_by("setor", "funcao", "colaborador")
        )
        setor_labels = dict(SETOR_CHOICES)
        setores = OrderedDict()

        def group_key(obj):
            setor = getattr(obj, "setor", "") or "outros"
            detail = (getattr(obj, "setor_detalhado", "") or "").strip()
            if detailed_enabled and detail:
                return f"{setor}|{detail}"
            return setor

        def group_label(key):
            if "|" in key:
                setor, detail = key.split("|", 1)
                return f"{setor_labels.get(setor, setor.title())} - {detail}"
            return setor_labels.get(key, key.title())

        def ensure_setor(key):
            key = key or "outros"
            if key not in setores:
                setores[key] = {
                    "key": key,
                    "label": group_label(key),
                    "planejado": 0,
                    "disponiveis": 0,
                    "atestados": 0,
                    "afastados": 0,
                    "ferias_lancadas": 0,
                    "folgas": 0,
                    "faltas_sem_justificativa": 0,
                    "ausencias_individuais": 0,
                    "ferias_hoje": 0,
                    "saidas_7": 0,
                    "retornos_7": 0,
                    "ausencias_hoje": 0,
                    "funcoes": OrderedDict(),
                }
            return setores[key]

        for row in pessoas:
            setor = ensure_setor(group_key(row))
            planejado = row.planejado or row.quadro_atual or 0
            ausencias_row = pessoa_turno_ausencias(row)
            disponiveis = row.ativos_dia or max(planejado - ausencias_row, 0)
            setor["planejado"] += planejado
            setor["disponiveis"] += disponiveis
            setor["atestados"] += row.atestados or 0
            setor["afastados"] += row.afastados or 0
            setor["ferias_lancadas"] += row.ferias or 0
            setor["folgas"] += row.folgas or 0
            setor["faltas_sem_justificativa"] += row.faltas_sem_justificativa or 0
            funcao_key = f"{row.turno}|{row.funcao}"
            setor["funcoes"][funcao_key] = {
                "funcao": row.funcao,
                "turno": row.get_turno_display(),
                "planejado": planejado,
                "disponiveis": disponiveis,
                "ausencias": ausencias_row,
            }

        for item in ferias_periodo:
            setor = ensure_setor(group_key(item))
            if item.inicio_ferias <= hoje <= item.fim_ferias:
                setor["ferias_hoje"] += 1
            if hoje <= item.inicio_ferias <= limite:
                setor["saidas_7"] += 1
            if hoje <= item.retorno_previsto <= limite:
                setor["retornos_7"] += 1

        for item in ausencias_periodo:
            setor = ensure_setor(group_key(item))
            if item.inicio <= hoje <= item.fim:
                setor["ausencias_hoje"] += 1
                setor["ausencias_individuais"] += 1

        rows = []
        totals = {"planejado": 0, "disponiveis": 0, "ferias_hoje": 0, "ausencias_hoje": 0, "saidas_7": 0, "retornos_7": 0, "criticos": 0}
        for setor in setores.values():
            planejado = setor["planejado"]
            ausencias = (
                setor["atestados"]
                + setor["afastados"]
                + setor["ferias_lancadas"]
                + setor["folgas"]
                + setor["faltas_sem_justificativa"]
            )
            ausencias = max(ausencias, setor["ferias_hoje"] + setor["ausencias_individuais"])
            perda_pct = int((ausencias / planejado) * 100) if planejado else 0
            if perda_pct >= config["limite_critico_pct"] or setor["ferias_hoje"] >= 4 or setor["ausencias_hoje"] >= 4:
                nivel = "critico"
                leitura = "Risco alto de gargalo. Vale revisar remanejamento ou apoio de outro setor."
                totals["criticos"] += 1
            elif perda_pct >= config["limite_atencao_pct"] or setor["ferias_hoje"] >= 2 or setor["ausencias_hoje"] >= 2 or setor["saidas_7"] >= 2:
                nivel = "atencao"
                leitura = "Atenção no quadro. Acompanhar antes do fechamento da escala."
            else:
                nivel = "ok"
                leitura = "Quadro sem pressão relevante para hoje."
            rows.append(
                {
                    **setor,
                    "ausencias": ausencias,
                    "perda_pct": perda_pct,
                    "bar": min(perda_pct, 100),
                    "nivel": nivel,
                    "leitura": leitura,
                    "funcoes_lista": list(setor["funcoes"].values()),
                }
            )
            totals["planejado"] += planejado
            totals["disponiveis"] += setor["disponiveis"]
            totals["ferias_hoje"] += setor["ferias_hoje"]
            totals["ausencias_hoje"] += setor["ausencias_hoje"]
            totals["saidas_7"] += setor["saidas_7"]
            totals["retornos_7"] += setor["retornos_7"]

        rows = sorted(rows, key=lambda row: ({"critico": 0, "atencao": 1, "ok": 2}[row["nivel"]], -row["perda_pct"], row["label"]))
        return {
            "data": hoje,
            "limite": limite,
            "cards": [
                {"label": "Disponíveis hoje", "value": totals["disponiveis"]},
                {"label": "Planejados hoje", "value": totals["planejado"]},
                {"label": "Férias hoje", "value": totals["ferias_hoje"]},
                {"label": "Ausências hoje", "value": totals["ausencias_hoje"]},
                {"label": "Saídas em 7 dias", "value": totals["saidas_7"]},
                {"label": "Retornos em 7 dias", "value": totals["retornos_7"]},
                {"label": "Setores críticos", "value": totals["criticos"]},
            ],
            "rows": rows,
            "has_data": bool(rows),
        }

    return cached_runtime_value(("capacidade_operacao", str(cd), hoje.isoformat(), config_key), 30, build)


FERIAS_ALERT_CONFIG_KEY = "ferias_alertas_config"
DEFAULT_FERIAS_ALERT_CONFIG = {
    "ativo": True,
    "dias_saida": 7,
    "dias_retorno": 3,
    "limite_atencao_pct": 12,
    "limite_critico_pct": 25,
    "notificar_master": True,
}


def bounded_int(value, default, minimum=0, maximum=365):
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return max(minimum, min(number, maximum))


def ferias_alert_config():
    try:
        payload = json.loads(get_config(FERIAS_ALERT_CONFIG_KEY, "{}") or "{}")
    except (TypeError, ValueError):
        payload = {}
    config = {**DEFAULT_FERIAS_ALERT_CONFIG, **{key: payload.get(key) for key in DEFAULT_FERIAS_ALERT_CONFIG if key in payload}}
    config["ativo"] = bool(config.get("ativo"))
    config["notificar_master"] = bool(config.get("notificar_master"))
    config["dias_saida"] = bounded_int(config.get("dias_saida"), DEFAULT_FERIAS_ALERT_CONFIG["dias_saida"], 0, 60)
    config["dias_retorno"] = bounded_int(config.get("dias_retorno"), DEFAULT_FERIAS_ALERT_CONFIG["dias_retorno"], 0, 60)
    config["limite_atencao_pct"] = bounded_int(config.get("limite_atencao_pct"), DEFAULT_FERIAS_ALERT_CONFIG["limite_atencao_pct"], 1, 100)
    config["limite_critico_pct"] = bounded_int(config.get("limite_critico_pct"), DEFAULT_FERIAS_ALERT_CONFIG["limite_critico_pct"], 1, 100)
    return config


def save_ferias_alert_config(request):
    config = {
        "ativo": request.POST.get("ativo") == "on",
        "dias_saida": bounded_int(request.POST.get("dias_saida"), DEFAULT_FERIAS_ALERT_CONFIG["dias_saida"], 0, 60),
        "dias_retorno": bounded_int(request.POST.get("dias_retorno"), DEFAULT_FERIAS_ALERT_CONFIG["dias_retorno"], 0, 60),
        "limite_atencao_pct": bounded_int(request.POST.get("limite_atencao_pct"), DEFAULT_FERIAS_ALERT_CONFIG["limite_atencao_pct"], 1, 100),
        "limite_critico_pct": bounded_int(request.POST.get("limite_critico_pct"), DEFAULT_FERIAS_ALERT_CONFIG["limite_critico_pct"], 1, 100),
        "notificar_master": request.POST.get("notificar_master") == "on",
    }
    if config["limite_critico_pct"] < config["limite_atencao_pct"]:
        config["limite_critico_pct"] = config["limite_atencao_pct"]
    set_config(FERIAS_ALERT_CONFIG_KEY, json.dumps(config, ensure_ascii=False), request.user, "Configuração dos alertas de férias")
    return config


def ferias_notification_recipients(cd, setor, config=None):
    config = config or ferias_alert_config()
    recipients = OrderedDict()
    for user in User.objects.filter(is_active=True).select_related("perfil_krill"):
        profile = ensure_profile(user)
        is_master = bool(getattr(user, "is_superuser", False) or (profile and profile.cargo == "master"))
        if is_master and not config.get("notificar_master", True):
            continue
        if not is_master and not user_has_perm(user, "notificacoes_ferias_colaboradores"):
            continue
        if cd and profile and not can_view_unified_cd(user) and profile.cd_padrao != cd:
            continue
        managed = managed_sectors_for_user(user)
        if managed is not None and setor not in managed:
            continue
        recipients[user.pk] = user
    return list(recipients.values())


def create_ferias_notification_once(user, alert_key, titulo, mensagem, url, cd, payload):
    if SistemaNotificacao.objects.filter(usuario=user, categoria="ferias", payload__alert_key=alert_key).exists():
        return False
    payload = {**payload, "alert_key": alert_key}
    create_system_notification(user, titulo, mensagem, url, "ferias", cd, payload)
    return True


def process_ferias_notifications(cd, config=None):
    config = config or ferias_alert_config()
    if not config.get("ativo"):
        return 0
    hoje = timezone.localdate()
    url = reverse("module_list", args=["ferias_colaboradores"])
    enviados = 0
    ferias_qs = (
        cd_queryset(ColaboradorFerias, cd)
        .filter(notificar_supervisor=True)
        .exclude(status__in=["retornado", "cancelada"])
    )
    eventos = [
        (
            "saida",
            "Férias próximas",
            ferias_qs.filter(inicio_ferias__range=[hoje, hoje + timezone.timedelta(days=config["dias_saida"])]),
            "início",
        ),
        (
            "retorno",
            "Retorno de férias próximo",
            ferias_qs.filter(retorno_previsto__range=[hoje, hoje + timezone.timedelta(days=config["dias_retorno"])]),
            "retorno",
        ),
    ]
    for tipo, titulo, queryset, label_data in eventos:
        for item in queryset[:80]:
            data_evento = item.inicio_ferias if tipo == "saida" else item.retorno_previsto
            setor = item.setor
            alert_key = f"ferias:{cd}:{tipo}:{item.pk}:{data_evento.isoformat()}"
            mensagem = f"{item.colaborador} tem {label_data} previsto em {data_evento:%d/%m/%Y} no setor {item.get_setor_display()}."
            for user in ferias_notification_recipients(cd, setor, config):
                enviados += int(create_ferias_notification_once(user, alert_key, titulo, mensagem, url, cd, {"tipo": tipo, "setor": setor, "colaborador_id": item.pk}))

    capacidade = build_capacidade_operacao(cd, config)
    for row in capacidade["rows"]:
        if row["nivel"] not in {"atencao", "critico"}:
            continue
        alert_key = f"gargalo_ferias:{cd}:{hoje.isoformat()}:{row['key']}:{row['nivel']}"
        titulo = "Gargalo de colaboradores"
        mensagem = f"{row['label']}: {row['disponiveis']} disponível(is) de {row['planejado']} planejado(s). {row['leitura']}"
        for user in ferias_notification_recipients(cd, row["key"], config):
            enviados += int(create_ferias_notification_once(user, alert_key, titulo, mensagem, url, cd, {"tipo": "gargalo", "setor": row["key"], "nivel": row["nivel"]}))
    return enviados


def bool_problem_count(queryset, fields):
    total = 0
    for field in fields:
        total += queryset.filter(**{field: False}).count()
    return total


def apply_frota_filters(queryset, placa="", motorista=""):
    if placa:
        queryset = queryset.filter(placa__icontains=placa)
    if motorista:
        queryset = queryset.filter(motorista__icontains=motorista)
    return queryset


def group_frota_checklists(rows):
    groups = OrderedDict()
    for row in rows:
        plate = (row.placa or "Sem placa").strip().upper()
        key = normalize(plate) or f"registro_{row.pk}"
        group = groups.setdefault(
            key,
            {
                "placa": plate,
                "veiculo": row.veiculo or "Veículo não informado",
                "motoristas": [],
                "rows": [],
                "ultima_data": row.data,
                "saidas": 0,
                "retornos": 0,
            },
        )
        if row.motorista and row.motorista not in group["motoristas"]:
            group["motoristas"].append(row.motorista)
        group["rows"].append(row)
        if row.data and (not group["ultima_data"] or row.data > group["ultima_data"]):
            group["ultima_data"] = row.data
        if row.tipo_checklist == "saida":
            group["saidas"] += 1
        else:
            group["retornos"] += 1
    return list(groups.values())


def build_frota_dashboard(cd, placa="", motorista="", unificado=False):
    cache_key = ("frota_dashboard", str(cd), normalize(placa), normalize(motorista), bool(unificado))

    def build():
        checklists = ChecklistFrota.objects.all() if unificado else ChecklistFrota.objects.filter(cd_unidade=cd)
        lacres = LacreFrota.objects.all() if unificado else LacreFrota.objects.filter(cd_unidade=cd)
        checklists = apply_frota_filters(checklists, placa, motorista).annotate(
            km_calculado=Case(
                When(km_final__gte=F("km_inicial"), then=F("km_final") - F("km_inicial")),
                default=Value(0),
                output_field=IntegerField(),
            )
        )
        lacres = apply_frota_filters(lacres, placa, motorista)
        issue_filter = (
            Q(pane_mecanica=True)
            | Q(furo_pneu=True)
            | Q(acidente=True)
            | Q(multa=True)
            | Q(avaria_carga=True)
            | Q(vazamento_viagem=True)
            | Q(outra_ocorrencia=True)
        )
        summary = checklists.aggregate(
            total=Count("id"),
            saidas=Count("id", filter=Q(tipo_checklist="saida")),
            retornos=Count("id", filter=Q(tipo_checklist="retorno")),
            manutencao=Count("id", filter=Q(necessita_manutencao=True)),
            intercalacao_pendente=(
                Count("id", filter=Q(intercala_cd=True, intercalacao_concluida=False))
                if system_rule_enabled("checklist_intercalacao_manual")
                else Count("id", filter=Q(pk__isnull=True))
            ),
            ocorrencias=Count("id", filter=issue_filter),
            km_total=Sum("km_calculado"),
        )
        retornos_pendentes = apply_frota_filters(
            frota_open_departures_queryset(cd, unificado=unificado),
            placa,
            motorista,
        ).count()
        placa_rows = list(
            checklists.exclude(placa="")
            .values("placa")
            .annotate(
                count=Count("id"),
                saidas=Count("id", filter=Q(tipo_checklist="saida")),
                retornos=Count("id", filter=Q(tipo_checklist="retorno")),
                manutencao=Count("id", filter=Q(necessita_manutencao=True)),
                ocorrencias=Count("id", filter=issue_filter),
                km=Sum("km_calculado"),
                ultima_data=Max("data"),
            )
            .order_by("-count", "placa")[:8]
        )
        max_placa = max([row["count"] for row in placa_rows] or [1])
        for row in placa_rows:
            row["label"] = row.pop("placa")
            row["km"] = row["km"] or 0
            row["percent"] = int((row["count"] / max_placa) * 100) if max_placa else 0
        ocorrencia_map = [
            ("Pane mecanica", "pane_mecanica"),
            ("Furo de pneu", "furo_pneu"),
            ("Acidente", "acidente"),
            ("Multa", "multa"),
            ("Avaria na carga", "avaria_carga"),
            ("Vazamento", "vazamento_viagem"),
            ("Outro", "outra_ocorrencia"),
        ]
        ocorrencia_counts = checklists.aggregate(
            **{field: Count("id", filter=Q(**{field: True})) for _label, field in ocorrencia_map}
        )
        max_ocorrencia = max([ocorrencia_counts.get(field) or 0 for _label, field in ocorrencia_map] or [1])
        ocorrencia_rows = [
            {
                "label": label,
                "count": ocorrencia_counts.get(field) or 0,
                "percent": int(((ocorrencia_counts.get(field) or 0) / max_ocorrencia) * 100) if max_ocorrencia else 0,
            }
            for label, field in ocorrencia_map
        ]
        recentes_base = list(checklists.order_by("-data", "-id")[:8])
        return {
            "cards": [
                {"label": "Checklists registrados", "value": summary["total"] or 0},
                {"label": "Saídas registradas", "value": summary["saidas"] or 0},
                {"label": "Retornos registrados", "value": summary["retornos"] or 0},
                {"label": "Retornos pendentes", "value": retornos_pendentes},
                {"label": "Necessitam manutenção", "value": summary["manutencao"] or 0},
                {"label": "Intercalação pendente", "value": summary["intercalacao_pendente"] or 0},
                {"label": "Ocorrências marcadas", "value": summary["ocorrencias"] or 0},
                {"label": "Lacres abertos", "value": lacres.exclude(status__in=["resolvido", "cancelado"]).count()},
                {"label": "KM registrado", "value": summary["km_total"] or 0},
            ],
            "placa_rows": placa_rows,
            "ocorrencia_rows": ocorrencia_rows,
            "recentes": recentes_base,
        }

    return cached_runtime_value(cache_key, 20, build)


def build_melhorias_dashboard(cd):
    melhorias = MelhoriaSistema.objects.filter(cd_unidade=cd)
    total = melhorias.count()
    abertas = melhorias.exclude(status__in=["concluida", "descartada"]).count()
    urgentes = melhorias.filter(prioridade__in=["alta", "urgente"]).exclude(status__in=["concluida", "descartada"]).count()
    concluidas = melhorias.filter(status="concluida").count()
    status_rows = equipment_bar_rows(melhorias, MelhoriaSistema, "status", total)
    area_rows = equipment_bar_rows(melhorias, MelhoriaSistema, "area", total)
    return {
        "cards": [
            {"label": "Sugestoes registradas", "value": total},
            {"label": "Em aberto", "value": abertas},
            {"label": "Alta/Urgente", "value": urgentes},
            {"label": "Concluidas", "value": concluidas},
        ],
        "status_rows": status_rows,
        "area_rows": area_rows,
        "recentes": melhorias.order_by("-data", "-id")[:8],
    }


FOCUSED_MODULES = {
    "avarias",
    "chamados_saldo",
    "conferencias",
    "unitizadores",
    "paletes_vasilhames",
    "paletes_rede",
    "funcoes_turno",
    "pessoas_turno",
    "ressuprimento",
    "ressuprimento_painel",
    "separacao",
    "expedicao",
    "expedicao_planejamento",
    "solicitar_lancamento_manual_expedicao",
}


MODULE_GUIDES = {
    "avarias": {
        "purpose": "Produto vencido, avariado, sobra ou falta.",
        "decision": "Use quando a informação precisa virar histórico e entrar no fechamento/relatório.",
        "group": "tipo",
        "total_fields": [("caixas", "Caixas"), ("unidades", "Unidades"), ("valor_estimado", "Valor estimado")],
    },
    "chamados_saldo": {
        "purpose": "Produto com diferenca entre saldo fisico e sistema.",
        "decision": "Use para não perder produto parado por falta de saldo, quebra, ganho ou ajuste.",
        "group": "tipo_chamado",
        "total_fields": [("saldo_sistema", "Saldo sistema"), ("saldo_fisico", "Saldo fisico"), ("diferenca", "Diferenca")],
    },
    "conferencias": {
        "purpose": "Conferencia de produto com diferenca automatica.",
        "decision": "Use quando alguem precisa assumir a diferenca e acompanhar ate resolver.",
        "group": "setor",
        "total_fields": [("qtd_esperada", "Qtd. esperada"), ("qtd_conferida", "Qtd. conferida"), ("diferenca", "Diferenca")],
    },
    "unitizadores": {
        "purpose": "Envio e retorno de unitizadores por loja.",
        "decision": "Use para saber o que saiu, o que voltou e o que ainda esta pendente.",
        "group": "tipo_unitizador",
        "total_fields": [("quantidade", "Quantidade")],
    },
    "paletes_vasilhames": {
        "purpose": "Fechamento de paletes e vasilhames por turno.",
        "decision": "Use para registrar volume do dia sem misturar com pallets de mercadoria.",
        "group": "turno",
        "total_fields": [
            ("palete_pbr", "PBR"),
            ("palete_chep", "CHEP"),
            ("palete_chapatex", "Chapatex"),
            ("palete_descartavel", "Descartaveis"),
            ("vasilhame_coca", "Coca"),
            ("vasilhame_cerveja", "Cerveja"),
            ("palete_cheio", "Cheios"),
            ("palete_vazio", "Vazios"),
        ],
    },
    "paletes_rede": {
        "purpose": "Saldo de PBR, CHEP, descartavel e PBR 2 em CDs e lojas.",
        "decision": "Use para saber de onde retirar pallets quando fornecedor cobrar devolucao.",
        "group": "tipo_palete",
        "total_fields": [("quantidade", "Saldo total"), ("quebrados", "Quebrados"), ("reservados", "Reservados")],
    },
    "pessoas_turno": {
        "purpose": "Quem estava previsto e quem realmente esta trabalhando.",
        "decision": "Use para explicar falta de gente, cobertura e impacto nas ferias.",
        "group": "setor",
        "total_fields": [
            ("planejado", "Planejado"),
            ("ativos_dia", "Ativos"),
            ("atestados", "Atestados"),
            ("afastados", "Afastados"),
            ("ferias", "Ferias"),
            ("folgas", "Folgas"),
            ("faltas_sem_justificativa", "Faltas sem justificativa"),
        ],
    },
    "funcoes_turno": {
        "purpose": "Lista de cargos/funcoes usadas no quadro do dia.",
        "decision": "Cadastre uma vez para depois selecionar em Pessoas no Turno.",
        "group": "setor",
        "total_fields": [("quadro_padrao", "Quadro padrao")],
    },
    "ressuprimento": {
        "purpose": "Movimento de produto entre enderecos.",
        "decision": "Use quando precisar mostrar produto, endereco e responsavel.",
        "group": "status",
        "total_fields": [("quantidade", "Quantidade")],
    },
    "ressuprimento_painel": {
        "purpose": "Resumo de demanda, ocupacao e producao.",
        "decision": "Use quando precisar transformar lancamentos em indicadores.",
        "group": "tipo_painel",
        "total_fields": [("qtd_itens", "Itens"), ("qtd_volume", "Volume"), ("pendencia_itens", "Pend. itens"), ("produzido_volume", "Produzido")],
    },
    "separacao": {
        "purpose": "Producao de separacao por loja e categoria.",
        "decision": "Use quando a separacao entrar no piloto novamente.",
        "group": "categoria",
        "total_fields": [("unidades", "Unidades"), ("paletes", "Paletes"), ("separacao_picking_produzido", "Picking"), ("separacao_pulmao_produzido", "Pulmão")],
    },
    "expedicao": {
        "purpose": "Baixa do que saiu fisicamente do CD.",
        "decision": "Use para confirmar carga expedida e tirar do saldo da expedição.",
        "group": "periodo",
        "total_fields": [("qtd_paletes", "Paletes")],
    },
    "lacres_frota": {
        "purpose": "Erro de lacre informado pelo motorista.",
        "decision": "Use quando o lacre der erro no Ronilo, na loja, ou no fechamento da rota.",
        "group": "motivo",
        "total_fields": [],
    },
    "parametros_cd": {
        "purpose": "Listas usadas por CD: áreas, ruas, locais de estoque e atividades.",
        "decision": "Cadastre aqui quando um CD tiver uma rua, área, local ou atividade nova.",
        "group": "tipo",
        "total_fields": [],
    },
}


FORM_SECTIONS = {
    "avarias": [
        ("Produto", ["data", "gtin", "produto", "validade", "lote"]),
        ("Classificação", ["tipo", "origem", "destino", "status", "responsavel"]),
        ("Quantidade e observacao", ["caixas", "unidades", "valor_estimado", "observacao"]),
    ],
    "chamados_saldo": [
        ("Produto e endereço", ["data", "gtin", "produto", "endereco"]),
        ("Saldos e chamado", ["saldo_sistema", "saldo_fisico", "tipo_chamado", "prioridade", "numero_glpi"]),
        ("Acompanhamento", ["status", "responsavel", "observacao"]),
    ],
    "conferencias": [
        ("Conferencia", ["data", "setor", "loja"]),
        ("Produto e quantidades", ["gtin", "produto", "qtd_esperada", "qtd_conferida"]),
        ("Fechamento", ["status", "responsavel", "observacao"]),
    ],
    "unitizadores": [
        ("Movimento", ["data", "loja", "tipo_unitizador", "quantidade"]),
        ("Retorno", ["status", "data_retorno", "observacao"]),
    ],
    "paletes_vasilhames": [
        ("Identificação", ["data", "turno", "responsavel"]),
        ("Paletes", ["palete_pbr", "palete_chep", "palete_chapatex", "palete_descartavel", "palete_cheio", "palete_vazio"]),
        ("Vasilhames", ["vasilhame_coca", "vasilhame_cerveja", "observacao"]),
    ],
    "paletes_rede": [
        ("Local", ["data", "local_tipo", "local_codigo", "local_nome", "tipo_palete"]),
        ("Saldo", ["quantidade", "quebrados", "reservados"]),
        ("Responsável", ["responsavel", "observacao"]),
    ],
    "pessoas_turno": [
        ("Turno e setor", ["data", "setor", "turno", "funcao"]),
        ("Quadro do dia", ["quadro_atual", "planejado", "ativos_dia"]),
        ("Ausencias", ["atestados", "afastados", "ferias", "folgas", "faltas_sem_justificativa", "observacao"]),
    ],
    "funcoes_turno": [
        ("Funcao", ["nome", "setor", "turno"]),
        ("Padrao", ["quadro_padrao", "ativa", "observacao"]),
    ],
    "ressuprimento": [
        ("Produto", ["data", "gtin", "produto", "quantidade"]),
        ("Endereços", ["endereco_origem", "endereco_destino"]),
        ("Fechamento", ["responsavel", "status", "observacao"]),
    ],
    "ressuprimento_painel": [
        ("Painel", ["data", "tipo_painel", "setor", "area", "rua", "local_estoque", "atividade"]),
        ("Demanda e pendência", ["qtd_itens", "qtd_volume", "pendencia_itens", "pendencia_volume"]),
        ("Produção", ["produzido_itens", "produzido_volume", "picking", "pulmao", "observacao"]),
    ],
    "separacao": [
        ("Loja e categoria", ["data", "loja", "categoria", "setor"]),
        ("Volume separado", ["unidades", "paletes", "responsavel"]),
        ("Produtividade", ["onda_gerada", "total_a_produzir", "crossdocking_produzido", "separacao_pulmao_produzido", "separacao_picking_produzido"]),
        ("Recursos", ["recursos_picking", "horas_picking", "recursos_pulmao", "horas_pulmao", "observacao"]),
        ("Fechamento", ["status"]),
    ],
    "expedicao": [
        ("Carregamento", ["data", "loja", "qtd_paletes", "periodo"]),
        ("Veiculo", ["placa", "motorista"]),
        ("Fechamento", ["status", "observacao"]),
    ],
    "lacres_frota": [
        ("Rota", ["data", "motorista", "placa", "loja_destino"]),
        ("Lacre", ["lacre", "cd_lacre", "motivo"]),
        ("Acompanhamento", ["descricao", "status", "tratado_por", "observacao"]),
    ],
    "parametros_cd": [
        ("Configuração", ["tipo", "codigo", "nome"]),
        ("Organização", ["ordem", "ativo", "observacao"]),
    ],
}


def decimal_sum(queryset, field_name):
    try:
        return queryset.aggregate(total=Sum(field_name))["total"] or 0
    except Exception:
        return 0


def compact_value(value):
    if isinstance(value, Decimal):
        if value == value.to_integral_value():
            return int(value)
        return value.quantize(Decimal("0.01"))
    return value


def build_bar_rows(queryset, model, field_name, total):
    if not field_name:
        return []
    try:
        field = model._meta.get_field(field_name)
    except Exception:
        return []
    choices = dict(getattr(field, "choices", []) or [])
    rows = list(
        queryset.exclude(**{f"{field_name}": ""})
        .values(field_name)
        .annotate(total=Count("id"))
        .order_by("-total", field_name)[:8]
    )
    max_count = max([row["total"] for row in rows] or [1])
    return [
        {
            "label": choices.get(row[field_name], row[field_name] or "Não informado"),
            "count": row["total"],
            "percent": int((row["total"] / max_count) * 100) if max_count else 0,
        }
        for row in rows
    ]


def build_module_overview(module, cd):
    guide = MODULE_GUIDES.get(module.key)
    if not guide:
        return None

    def build():
        queryset = module.model.objects.filter(cd_unidade=cd)
        today_count = queryset.filter(data=timezone.localdate()).count() if hasattr(module.model, "data") else 0
        total = queryset.count()
        open_count = queryset.exclude(status__in=["concluido", "cancelado", "retornado"]).count() if "status" in module.fields else None
        total_cards = []
        for field_name, label in guide.get("total_fields", [])[:5]:
            if field_name in module.fields:
                total_cards.append({"label": label, "value": compact_value(decimal_sum(queryset, field_name))})
        cards = [
            {"label": "Registros", "value": total},
            {"label": "Lançados hoje", "value": today_count},
        ]
        if open_count is not None:
            cards.append({"label": "Pendentes/abertos", "value": open_count})
        cards.extend(total_cards)
        return {
            "purpose": guide["purpose"],
            "decision": guide["decision"],
            "cards": cards[:7],
            "group_title": "Distribuicao principal",
            "group_rows": build_bar_rows(queryset, module.model, guide.get("group"), total),
            "status_rows": build_bar_rows(queryset, module.model, "status", total) if "status" in module.fields and guide.get("group") != "status" else [],
        }

    return cached_runtime_value(("module_overview", module.key, str(cd), timezone.localdate().isoformat()), 30, build)


def form_sections_for_module(module, form):
    sections = FORM_SECTIONS.get(module.key)
    if not sections:
        return None
    used = set()
    result = []
    for title, names in sections:
        fields = [name for name in names if name in form.fields]
        used.update(fields)
        if fields:
            result.append({"title": title, "fields": fields})
    remaining = [name for name in form.fields if name not in used]
    if remaining:
        result.append({"title": "Outros dados", "fields": remaining})
    return result


def visible_module_fields(fields):
    fields = tuple(fields)
    if feature_enabled("colaboradores_setor_detalhado"):
        return fields
    return tuple(field for field in fields if field != "setor_detalhado")


PALETE_REDE_CDS = ["801", "805", "806"]


def normalize_palete_rede_store_codigo(codigo):
    texto = str(codigo or "").strip()
    if not texto:
        return ""
    if texto.isdigit():
        return texto.zfill(2)
    return texto.upper()


def clean_palete_rede_store_nome(nome, codigo):
    texto = str(nome or "").strip()
    codigo_sem_zero = str(codigo or "").lstrip("0") or str(codigo or "").strip()
    match = re.match(rf"^\s*0*{re.escape(codigo_sem_zero)}\s*[-–]\s*(.+)$", texto, re.IGNORECASE)
    return match.group(1).strip() if match else texto


def palete_rede_store_label(codigo, nome):
    nome_limpo = clean_palete_rede_store_nome(nome, codigo)
    return f"{codigo} - {nome_limpo}" if codigo else nome_limpo


def ensure_paletes_rede_loja_saldos(loja):
    codigo = normalize_palete_rede_store_codigo(loja.codigo)
    nome = palete_rede_store_label(codigo, loja.nome)
    existing = set(
        PaleteRedeSaldo.objects.filter(local_tipo="loja", local_codigo=codigo, local_nome=nome)
        .values_list("tipo_palete", flat=True)
    )
    missing = [
        PaleteRedeSaldo(
            local_tipo="loja",
            local_codigo=codigo,
            local_nome=nome,
            tipo_palete=tipo,
            cd_unidade="806",
            quantidade=0,
            quebrados=0,
            reservados=0,
        )
        for tipo, _label in PaleteRedeSaldo.TIPOS_PALETE
        if tipo not in existing
    ]
    if missing:
        PaleteRedeSaldo.objects.bulk_create(missing, ignore_conflicts=True)


def ensure_paletes_rede_base():
    expected = []
    for cd_codigo in PALETE_REDE_CDS:
        for tipo, _label in PaleteRedeSaldo.TIPOS_PALETE:
            expected.append(
                PaleteRedeSaldo(
                    local_tipo="cd",
                    local_codigo=cd_codigo,
                    local_nome=cd_codigo,
                    tipo_palete=tipo,
                    cd_unidade="806",
                    quantidade=0,
                    quebrados=0,
                    reservados=0,
                )
            )
    for loja in Loja.objects.filter(ativa=True).only("codigo", "nome").order_by("codigo", "nome"):
        codigo = normalize_palete_rede_store_codigo(loja.codigo)
        nome = palete_rede_store_label(codigo, loja.nome)
        for tipo, _label in PaleteRedeSaldo.TIPOS_PALETE:
            expected.append(
                PaleteRedeSaldo(
                    local_tipo="loja",
                    local_codigo=codigo,
                    local_nome=nome,
                    tipo_palete=tipo,
                    cd_unidade="806",
                    quantidade=0,
                    quebrados=0,
                    reservados=0,
                )
            )
    existing = set(
        PaleteRedeSaldo.objects.filter(local_tipo__in=["cd", "loja"]).values_list(
            "local_tipo", "local_codigo", "local_nome", "tipo_palete"
        )
    )
    missing = [
        saldo
        for saldo in expected
        if (saldo.local_tipo, saldo.local_codigo, saldo.local_nome, saldo.tipo_palete) not in existing
    ]
    if missing:
        PaleteRedeSaldo.objects.bulk_create(missing, ignore_conflicts=True)


def palete_rede_tipo_label(tipo):
    return dict(PaleteRedeSaldo.TIPOS_PALETE).get(tipo, tipo)


def paletes_rede_context(selected_local_tipo="", selected_local_key="", modal_mode=""):
    ensure_paletes_rede_base()
    local_order = {"cd": 0, "loja": 1, "outro": 2}
    tipo_order = {tipo: index for index, (tipo, _label) in enumerate(PaleteRedeSaldo.TIPOS_PALETE)}
    saldos = sorted(
        PaleteRedeSaldo.objects.all(),
        key=lambda saldo: (
            local_order.get(saldo.local_tipo, 9),
            saldo.local_codigo or "",
            saldo.local_nome or "",
            tipo_order.get(saldo.tipo_palete, 9),
        ),
    )
    totals = OrderedDict(
        (tipo, {"tipo": tipo, "label": label, "quantidade": 0, "quebrados": 0, "reservados": 0, "disponivel": 0})
        for tipo, label in PaleteRedeSaldo.TIPOS_PALETE
    )
    locais = OrderedDict()
    for saldo in saldos:
        total = totals.setdefault(
            saldo.tipo_palete,
            {"tipo": saldo.tipo_palete, "label": palete_rede_tipo_label(saldo.tipo_palete), "quantidade": 0, "quebrados": 0, "reservados": 0, "disponivel": 0},
        )
        total["quantidade"] += saldo.quantidade
        total["quebrados"] += saldo.quebrados
        total["reservados"] += saldo.reservados
        total["disponivel"] += saldo.disponivel
        key = (saldo.local_tipo, saldo.local_codigo, saldo.local_nome)
        local = locais.setdefault(
            key,
            {
                "tipo": saldo.get_local_tipo_display(),
                "codigo": saldo.local_codigo,
                "nome": saldo.local_label,
                "quantidade": 0,
                "quebrados": 0,
                "reservados": 0,
                "disponivel": 0,
                "saldos": [],
                "saldos_por_tipo": OrderedDict(),
                "key": f"{saldo.local_tipo}|{saldo.local_codigo}|{saldo.local_nome}",
                "tipo_param": saldo.local_tipo,
            },
        )
        local["quantidade"] += saldo.quantidade
        local["quebrados"] += saldo.quebrados
        local["reservados"] += saldo.reservados
        local["disponivel"] += saldo.disponivel
        local["saldos"].append(saldo)
        local["saldos_por_tipo"][saldo.tipo_palete] = saldo
    local_list = list(locais.values())
    cd_options = [local for local in local_list if local["key"].startswith("cd|")]
    loja_options = [local for local in local_list if local["key"].startswith("loja|")]
    selected_local_tipo = selected_local_tipo if selected_local_tipo in {"cd", "loja"} else "cd"
    option_pool = cd_options if selected_local_tipo == "cd" else loja_options
    if not selected_local_key or selected_local_key not in {local["key"] for local in option_pool}:
        selected_local_key = option_pool[0]["key"] if option_pool else ""
    selected_local = next((local for local in local_list if local["key"] == selected_local_key), None)
    modal_mode = modal_mode if modal_mode in {"ver", "inserir", "editar", "historico"} else ""
    return {
        "tipos": [{"value": tipo, "label": label} for tipo, label in PaleteRedeSaldo.TIPOS_PALETE],
        "local_tipos": [{"value": tipo, "label": label} for tipo, label in PaleteRedeSaldo.LOCAL_TIPOS],
        "totals": list(totals.values()),
        "locais": local_list,
        "cd_options": cd_options,
        "loja_options": loja_options,
        "selected_local_tipo": selected_local_tipo,
        "selected_local_key": selected_local_key,
        "selected_local": selected_local,
        "modal_mode": modal_mode,
        "saldos": saldos,
        "movimentacoes": PaleteRedeMovimentacao.objects.order_by("-data", "-criado_em", "-id")[:12],
        "grand_total": sum(item["quantidade"] for item in totals.values()),
        "grand_disponivel": sum(item["disponivel"] for item in totals.values()),
        "grand_quebrados": sum(item["quebrados"] for item in totals.values()),
        "grand_reservados": sum(item["reservados"] for item in totals.values()),
    }


def export_paletes_rede_xlsx():
    data_geracao = timezone.localtime(timezone.now())
    context = paletes_rede_context()
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Saldos da rede"
    title_fill = PatternFill("solid", fgColor="073B73")
    header_fill = PatternFill("solid", fgColor="D9EAF7")

    sheet.append(["Relatorio de Pallets da Rede"])
    sheet.append([f"Gerado em {data_geracao:%d/%m/%Y %H:%M}"])
    sheet.append([])
    sheet.append(["Total rede", context["grand_total"], "Disponivel", context["grand_disponivel"], "Reservado", context["grand_reservados"], "Quebrado", context["grand_quebrados"]])
    sheet.append([])
    headers = ["Tipo de local", "Local", "Tipo de pallet", "Total", "Disponivel", "Reservado", "Quebrado", "Responsavel"]
    sheet.append(headers)
    for local in context["locais"]:
        for saldo in local["saldos"]:
            sheet.append([
                local["tipo"],
                local["nome"],
                saldo.get_tipo_palete_display(),
                saldo.quantidade,
                saldo.disponivel,
                saldo.reservados,
                saldo.quebrados,
                saldo.responsavel,
            ])

    for cell in sheet[1]:
        cell.font = Font(bold=True, color="FFFFFF", size=14)
        cell.fill = title_fill
    for cell in sheet[6]:
        cell.font = Font(bold=True)
        cell.fill = header_fill
    sheet.freeze_panes = "A7"

    mov_sheet = workbook.create_sheet("Movimentacoes")
    mov_sheet.append(["Data", "Operacao", "Tipo", "Quantidade", "Origem", "Destino", "Solicitante", "Status", "Observacao"])
    for cell in mov_sheet[1]:
        cell.font = Font(bold=True)
        cell.fill = header_fill
    for mov in PaleteRedeMovimentacao.objects.order_by("-data", "-criado_em", "-id")[:300]:
        mov_sheet.append([
            mov.data.strftime("%d/%m/%Y") if mov.data else "",
            mov.get_operacao_display(),
            mov.get_tipo_palete_display(),
            mov.quantidade,
            mov.origem_resumo,
            mov.destino_nome,
            mov.solicitante,
            mov.get_status_display(),
            mov.observacao,
        ])

    for ws in workbook.worksheets:
        for column in ws.columns:
            max_length = max(len(str(cell.value or "")) for cell in column)
            ws.column_dimensions[column[0].column_letter].width = min(max(max_length + 2, 12), 42)

    output = io.BytesIO()
    workbook.save(output)
    output.seek(0)
    response = HttpResponse(
        output.read(),
        content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )
    response["Content-Disposition"] = 'attachment; filename="relatorio_pallets_rede.xlsx"'
    return response


def positive_int_from_post(request, name, default=0):
    try:
        return max(0, int(request.POST.get(name, default) or 0))
    except (TypeError, ValueError):
        return default


def loja_codigo(valor):
    texto = str(valor or "").strip()
    match = re.match(r"^\s*0*(\d+)", texto)
    return match.group(1).zfill(2) if match else texto.upper()


def format_currency_br(value):
    try:
        amount = Decimal(value or 0)
    except (InvalidOperation, TypeError, ValueError):
        amount = Decimal("0")
    return f"R$ {amount:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


def parse_currency_br(value):
    text = str(value or "").strip()
    if not text:
        return Decimal("0")
    text = re.sub(r"[^\d,.-]", "", text)
    if "," in text and "." in text:
        text = text.replace(".", "").replace(",", ".")
    elif "," in text:
        text = text.replace(",", ".")
    try:
        return Decimal(text or "0").quantize(Decimal("0.01"))
    except (InvalidOperation, ValueError):
        return Decimal("0")


def loja_label_map(cds):
    labels = {}
    for loja in Loja.objects.filter(cd_unidade__in=cds, ativa=True).order_by("codigo", "nome"):
        labels.setdefault(loja_codigo(loja.codigo), str(loja))
    return labels


def loja_label_from_value(valor, labels=None):
    labels = labels or {}
    return labels.get(loja_codigo(valor), str(valor or "").strip())


def planejamento_loja_keys(valor, labels=None):
    labels = labels or {}
    codigo = loja_codigo(valor)
    keys = {str(valor or "").strip(), codigo}
    label = labels.get(codigo)
    if label:
        keys.add(label)
        keys.add(label.split(" - ", 1)[-1])
    return {key for key in keys if key}


def first_by_loja_codigo(queryset, loja):
    codigo = loja_codigo(loja)
    return next((item for item in queryset if loja_codigo(item.loja) == codigo), None)


def current_pallet_queryset(cds):
    return ExpedicaoPlanejamento.objects.filter(cd_unidade__in=cds).exclude(status="cancelado")


def current_pallet_rows(cds):
    latest = OrderedDict()
    for item in current_pallet_queryset(cds).order_by("atualizado_em", "id"):
        latest[(item.cd_unidade, loja_codigo(item.loja))] = item
    return list(latest.values())


def current_pallet_for(cd_unidade, loja):
    return first_by_loja_codigo(
        current_pallet_rows([cd_unidade]),
        loja,
    )


def available_pallets_for(cd_unidade, loja, labels=None):
    planejamento = current_pallet_for(cd_unidade, loja)
    saldo = planejamento.qtd_paletes if planejamento else 0
    vinculado = ExpedicaoVinculo.objects.filter(
        cd_unidade=cd_unidade,
        loja__in=planejamento_loja_keys(loja, labels),
        status="vinculado",
    ).aggregate(total=Sum("qtd_paletes"))["total"] or 0
    return max(saldo - vinculado, 0)


def normalized_plate(value):
    return re.sub(r"[^A-Z0-9]", "", str(value or "").upper())


def normalized_text(value):
    return re.sub(r"\s+", " ", str(value or "").strip().upper())


def vehicle_is_platform(vehicle):
    return bool(vehicle and getattr(vehicle, "eh_plataforma", False))


def vehicle_platform_available(vehicle):
    return bool(vehicle and getattr(vehicle, "plataforma_disponivel", False))


def platform_vehicle_error_message(vehicle, prefix="Esta loja/operação exige caminhão plataforma."):
    if vehicle_is_platform(vehicle) and not vehicle_platform_available(vehicle):
        return f"{prefix} O veículo selecionado é plataforma, mas está marcado com plataforma quebrada/indisponível."
    return f"{prefix} Selecione um veículo cadastrado como Plataforma com plataforma operacional."


def latest_checklist_for_plate(plate, cds=None):
    normalized = normalized_plate(plate)
    if not normalized:
        return None
    qs = ChecklistFrota.objects.all()
    if cds:
        qs = qs.filter(cd_unidade__in=cds)
    for item in qs.order_by("-data", "-criado_em", "-id")[:300]:
        if item.tipo_checklist == "saida" and item.retorno_dispensado:
            continue
        if normalized_plate(item.placa) == normalized:
            return item
    return None


def vehicle_availability(vehicle, cds=None):
    status = getattr(vehicle, "status_operacional", "disponivel") or "disponivel"
    local = (getattr(vehicle, "local_atual", "") or "").strip()
    orientacao = (getattr(vehicle, "orientacao_frota", "") or "").strip()
    manual_labels = {
        "manutencao": ("Manutenção", "Veículo informado em manutenção", "danger", False),
        "limpeza": ("Limpeza", "Veículo informado em limpeza", "warning", False),
        "indisponivel": ("Indisponível", "Veículo informado como indisponível", "danger", False),
        "ocupado": ("Ocupado", "Veículo marcado como ocupado pela Frota", "warning", False),
        "em_rota": ("Em rota", "Veículo marcado em rota pela Frota", "warning", False),
    }
    if status in manual_labels:
        label, detail, tone, available = manual_labels[status]
        return {"status": label, "detail": orientacao or detail, "tone": tone, "available": available, "local": local or "-", "latest": None}

    latest = latest_checklist_for_plate(getattr(vehicle, "placa", ""), cds)
    if latest and latest.necessita_manutencao:
        return {
            "status": "Manutenção",
            "detail": latest.descricao_manutencao or "Checklist informou necessidade de manutenção.",
            "tone": "danger",
            "available": False,
            "local": latest.loja_destino or local or f"CD {latest.cd_unidade}",
            "latest": latest,
        }
    if latest and latest.tipo_checklist == "saida_loja":
        if latest.carga_retorno:
            return {
                "status": "Ocupado",
                "detail": latest.carga_retorno_descricao or "Motorista informou que está trazendo algo no caminhão.",
                "tone": "warning",
                "available": False,
                "local": latest.loja_destino or local or "-",
                "latest": latest,
            }
        return {
            "status": "Disponível",
            "detail": orientacao or "Saiu da loja sem carga/transferência informada. Pode receber nova tarefa.",
            "tone": "success",
            "available": True,
            "local": latest.loja_destino or local or "-",
            "latest": latest,
        }
    if latest and latest.tipo_checklist in {"saida", "chegada_loja"}:
        return {
            "status": "Em entrega",
            "detail": latest.loja_destino or "Viagem em andamento.",
            "tone": "warning",
            "available": False,
            "local": latest.loja_destino or local or "-",
            "latest": latest,
        }
    if latest and latest.tipo_checklist == "retorno":
        return {
            "status": "Disponível",
            "detail": orientacao or "Retorno ao CD registrado.",
            "tone": "success",
            "available": True,
            "local": local or f"CD {latest.cd_unidade}",
            "latest": latest,
        }
    return {"status": "Disponível", "detail": orientacao or "Sem impedimento registrado.", "tone": "success", "available": True, "local": local or "-", "latest": latest}


def vehicle_unavailable_message(vehicle, cds=None, plate=""):
    if not vehicle and plate:
        normalized = normalized_plate(plate)
        candidates = VeiculoFrota.objects.filter(ativo=True)
        if cds:
            candidates = candidates.filter(cd_unidade__in=cds)
        vehicle = next((item for item in candidates if normalized_plate(item.placa) == normalized), None)
    if not vehicle:
        latest = latest_checklist_for_plate(plate, cds)
        if latest and latest.tipo_checklist == "saida_loja" and latest.carga_retorno:
            return f"Placa {plate} não está disponível: ocupado ({latest.carga_retorno_descricao or 'Motorista informou que está trazendo algo no caminhão.'})."
        return ""
    availability = vehicle_availability(vehicle, cds)
    if availability["available"] or availability["status"] == "Em entrega":
        return ""
    return f"{vehicle.motorista} - {vehicle.placa} não está disponível: {availability['status']} ({availability['detail']})."


def frota_availability_rows(cds):
    rows = []
    seen_plates = set()
    vehicles = VeiculoFrota.objects.filter(cd_unidade__in=cds, ativo=True).order_by("-atualizado_em", "-data", "-id")
    for vehicle in vehicles:
        plate_key = normalized_plate(vehicle.placa)
        if plate_key and plate_key in seen_plates:
            continue
        if plate_key:
            seen_plates.add(plate_key)
        availability = vehicle_availability(vehicle, cds)
        latest = availability.get("latest")
        rows.append(
            {
                "vehicle": vehicle,
                "status": availability["status"],
                "detail": availability["detail"],
                "tone": availability["tone"],
                "available": availability["available"],
                "local": availability["local"],
                "latest_kind": latest.get_tipo_checklist_display() if latest else "-",
                "latest_date": latest.data if latest else None,
            }
        )
    return sorted(rows, key=lambda row: (row["vehicle"].motorista or "", normalized_plate(row["vehicle"].placa)))


def loja_requires_platform(cds, loja):
    codigo = loja_codigo(loja)
    for item in Loja.objects.filter(cd_unidade__in=cds, ativa=True).filter(Q(loja_plataforma=True) | Q(empilhadeira_indisponivel=True)):
        if loja_codigo(item.codigo) == codigo:
            return True
    return False


def platform_reason(cds, loja, manual_required=False, manual_reason=""):
    codigo = loja_codigo(loja)
    for item in Loja.objects.filter(cd_unidade__in=cds, ativa=True).filter(Q(loja_plataforma=True) | Q(empilhadeira_indisponivel=True)):
        if loja_codigo(item.codigo) == codigo:
            if item.empilhadeira_indisponivel and item.loja_plataforma:
                return "Loja plataforma e empilhadeira indisponível."
            if item.empilhadeira_indisponivel:
                return "Empilhadeira da loja indisponível: precisa de caminhão plataforma."
            return "Loja cadastrada como plataforma."
    if manual_required:
        return manual_reason or "Exceção operacional: precisa de caminhão plataforma."
    return ""


def unique_vehicle_options(queryset, cds=None):
    seen = set()
    vehicles = []
    for vehicle in queryset:
        plate_key = normalized_plate(vehicle.placa)
        key = plate_key or f"{normalized_text(vehicle.motorista)}|{normalized_text(vehicle.tipo_caminhao)}"
        if key in seen:
            continue
        seen.add(key)
        availability = vehicle_availability(vehicle, cds)
        vehicle.availability_available = availability["available"] or availability["status"] == "Em entrega"
        vehicle.availability_status = availability["status"]
        vehicle.availability_detail = availability["detail"]
        vehicles.append(vehicle)
    return sorted(vehicles, key=lambda item: (normalized_text(item.motorista), normalized_plate(item.placa)))


def expedicao_planejamento_context(cd, data_obj, can_unify=False, selected_loja=""):
    cds = ["801", "806"] if can_unify else [cd]
    lojas = loja_label_map(cds)
    planejamentos = current_pallet_rows(cds)
    for item in planejamentos:
        lojas.setdefault(loja_codigo(item.loja), loja_label_from_value(item.loja, lojas))
    by_key = {}
    for item in planejamentos:
        by_key[(item.cd_unidade, loja_codigo(item.loja))] = item
    vinculos_por_loja = {
        (row["cd_unidade"], loja_codigo(row["loja"])): row["total"] or 0
        for row in ExpedicaoVinculo.objects.filter(cd_unidade__in=cds, status="vinculado")
        .values("cd_unidade", "loja")
        .annotate(total=Sum("qtd_paletes"))
    }
    rows = []
    total_cd = {cd_value: 0 for cd_value in cds}
    total_vinculado_cd = {cd_value: 0 for cd_value in cds}
    total_livre_cd = {cd_value: 0 for cd_value in cds}
    lojas_com_saldo = 0
    for codigo, label in lojas.items():
        cd_values = {}
        total = 0
        row_has_obs = False
        for cd_value in cds:
            item = by_key.get((cd_value, codigo))
            value = item.qtd_paletes if item else 0
            vinculado = vinculos_por_loja.get((cd_value, codigo), 0)
            livre = max(value - vinculado, 0)
            cd_values[cd_value] = {
                "item": item,
                "qtd": value,
                "vinculado": vinculado,
                "livre": livre,
                "expedido": 0,
                "restante": value,
                "pode_remontar": False,
                "qtd_remontavel": 0,
                "observacao": item.observacao if item else "",
                "status": item.status if item else "",
                "carregado_em": item.carregado_em if item else None,
            }
            total_cd[cd_value] += value
            total_vinculado_cd[cd_value] += vinculado
            total_livre_cd[cd_value] += livre
            total += value
            if item and item.observacao:
                row_has_obs = True
        if total:
            lojas_com_saldo += 1
        rows.append({
            "codigo": codigo,
            "loja": label,
            "cds": cd_values,
            "total": total,
            "total_vinculado": sum(value["vinculado"] for value in cd_values.values()),
            "total_livre": sum(value["livre"] for value in cd_values.values()),
            "qtd_remontavel": 0,
            "tem_observacao": row_has_obs,
        })
    selected = loja_label_from_value(selected_loja, lojas) if selected_loja else (rows[0]["loja"] if rows else "")
    selected_codigo = loja_codigo(selected)
    selected_row = next((row for row in rows if row["codigo"] == selected_codigo), None)
    selected = selected_row["loja"] if selected_row else selected
    visible_rows = [selected_row] if selected_row else rows[:1]
    vinculos = (
        ExpedicaoVinculo.objects.filter(cd_unidade__in=cds, loja__in=planejamento_loja_keys(selected, lojas), status="vinculado")
        .order_by("cd_unidade", "periodo", "criado_em", "id")
        if selected
        else []
    )
    return {
        "data": data_obj,
        "cds": cds,
        "rows": rows,
        "visible_rows": visible_rows,
        "selected_loja": selected,
        "totals": total_cd,
        "totals_vinculados": total_vinculado_cd,
        "totals_livres": total_livre_cd,
        "grand_total": sum(total_cd.values()),
        "grand_vinculado": sum(total_vinculado_cd.values()),
        "grand_livre": sum(total_livre_cd.values()),
        "lojas_com_saldo": lojas_com_saldo,
        "lojas_remontaveis": 0,
        "total_remontavel": 0,
        "vinculos": vinculos,
    }


def expedicao_distribuicao_context(cd, data_obj, can_unify=False, selected_loja=""):
    base = expedicao_planejamento_context(cd, data_obj, can_unify, selected_loja)
    cds = base["cds"]
    expedicoes = Expedicao.objects.filter(data=data_obj, cd_unidade__in=cds)
    expedido = {}
    total_expedido_cd = {cd_value: 0 for cd_value in cds}
    for row in expedicoes.values("cd_unidade", "loja").annotate(total=Sum("qtd_paletes")):
        total = row["total"] or 0
        expedido[(row["cd_unidade"], loja_codigo(row["loja"]))] = expedido.get((row["cd_unidade"], loja_codigo(row["loja"])), 0) + total
        if row["cd_unidade"] in total_expedido_cd:
            total_expedido_cd[row["cd_unidade"]] += total
    for row in base["rows"]:
        for cd_value in cds:
            info = row["cds"].get(cd_value) or {"qtd": 0}
            qtd_expedida = expedido.get((cd_value, row["codigo"]), 0)
            info["expedido"] = qtd_expedida
            info["restante"] = info.get("qtd") or 0
            row["cds"][cd_value] = info
    selected_row = next((row for row in base["rows"] if row["codigo"] == loja_codigo(base["selected_loja"])), None)
    base["visible_rows"] = [selected_row] if selected_row else base["rows"][:1]
    base["recentes"] = expedicoes.filter(loja=base["selected_loja"]).order_by("-criado_em", "-id")[:8] if base["selected_loja"] else []
    base["vinculos"] = ExpedicaoVinculo.objects.filter(
        data=data_obj,
        cd_unidade__in=cds,
        status="vinculado",
    ).order_by("loja", "cd_unidade", "periodo", "criado_em", "id")
    reservas_abertas_qs = ExpedicaoVinculo.objects.filter(
        cd_unidade__in=cds,
        status="vinculado",
    ).order_by("-data", "loja", "cd_unidade", "periodo", "criado_em", "id")
    reservas_abertas = list(reservas_abertas_qs[:150])
    base["reservas_abertas"] = reservas_abertas
    base["reservas_abertas_total"] = sum(item.qtd_paletes or 0 for item in reservas_abertas)
    base["totals_expedidos"] = total_expedido_cd
    base["grand_expedido"] = sum(total_expedido_cd.values())
    base["totals_vinculados"] = {
        cd_value: sum((row["cds"].get(cd_value) or {}).get("vinculado", 0) for row in base["rows"])
        for cd_value in cds
    }
    base["totals_livres"] = {
        cd_value: sum((row["cds"].get(cd_value) or {}).get("livre", 0) for row in base["rows"])
        for cd_value in cds
    }
    base["grand_vinculado"] = sum(base["totals_vinculados"].values())
    base["grand_livre"] = sum(base["totals_livres"].values())
    base["grand_disponivel"] = base["grand_livre"]
    return base


def lojas_prontas_context(cd, data_obj, can_unify=False, selected_loja="", can_view_values=False):
    can_view_values = can_view_values and feature_enabled("faturamento_expedicao")
    base = expedicao_planejamento_context(cd, data_obj, can_unify, selected_loja)
    cds = base["cds"]
    single_cd = cds[0] if len(cds) == 1 else ""
    cd_profiles = {
        "801": {
            "title": "Fluxo do CD 801",
            "subtitle": "Informe apenas o que já foi finalizado para carregar.",
            "mode": "saldo_box",
            "field_label": "Qtd. pronta agora",
            "button_label": "Enviar para Frota",
            "extra_label": "Observação de remontagem",
            "rule": "A quantidade pronta baixa do saldo em box e entra na fila da Frota.",
        },
        "806": {
            "title": "Fluxo do CD 806",
            "subtitle": "Informe a loja já conferida e pronta.",
            "mode": "saldo_pronto",
            "field_label": "Pallets prontos",
            "button_label": "Avisar Frota",
            "extra_label": "Observação opcional",
            "rule": "A quantidade informada alimenta o saldo pronto e entra na fila da Frota.",
        },
        "805": {
            "title": "Fluxo do CD 805",
            "subtitle": "Regra preparada para parametrização própria do CD 805.",
            "mode": "preparado",
            "field_label": "Pallets prontos",
            "button_label": "Avisar Frota",
            "extra_label": "Observação opcional",
            "rule": "Por enquanto o CD 805 fica separado para receber uma regra própria depois.",
        },
    }
    base["ready_load_single_cd"] = single_cd
    base["ready_load_profile"] = cd_profiles.get(single_cd) or {
        "title": "Fluxo por CD",
        "subtitle": "Escolha o CD operacional para o sistema aplicar a regra certa.",
        "mode": "unificado",
        "field_label": "Qtd. pallets prontos",
        "button_label": "Avisar Frota",
        "extra_label": "Observação opcional",
        "rule": "CD 806 alimenta saldo pronto. CD 801 baixa do saldo em box quando a loja fica pronta.",
    }
    base["ready_load_flow_steps"] = [
        {"label": "Informar loja pronta", "detail": "Expedição confirma a loja liberada."},
        {"label": "Frota vincula caminhão", "detail": "Frota escolhe motorista e placa."},
        {"label": "Gerencial acompanha", "detail": "Supervisão vê o que está a caminho."},
    ]
    for row in base["rows"]:
        rules = []
        for cd_value in cds:
            cd_info = row["cds"].get(cd_value) or {}
            saldo_atual = cd_info.get("qtd") or 0
            if cd_value == "806":
                rules.append(
                    {
                        "cd": cd_value,
                        "title": "CD 806",
                        "label": "Pronto alimenta saldo",
                        "saldo_atual": saldo_atual,
                        "text": "Ao avisar a Frota, a quantidade informada vira o saldo pronto desta loja.",
                    }
                )
            elif cd_value == "801":
                rules.append(
                    {
                        "cd": cd_value,
                        "title": "CD 801",
                        "label": "Pronto baixa do box",
                        "saldo_atual": saldo_atual,
                        "text": "Ao avisar a Frota, a quantidade pronta sai do saldo em box desta loja.",
                    }
                )
        row["ready_load_rules"] = rules
    if cds == ["806"]:
        base["ready_load_rule_title"] = "Regra do CD 806"
        base["ready_load_rule_text"] = "Aqui a loja pronta e a carga liberada sao a mesma etapa: o aviso alimenta o saldo pronto e entra na fila da Frota."
    elif cds == ["801"]:
        base["ready_load_rule_title"] = "Regra do CD 801"
        base["ready_load_rule_text"] = "Aqui existe saldo em box antes da liberacao: o aviso baixa do saldo em box e envia apenas a quantidade pronta para a Frota."
    else:
        base["ready_load_rule_title"] = "Regras por CD"
        base["ready_load_rule_text"] = "CD 806 alimenta saldo pronto. CD 801 baixa do saldo em box quando a loja fica pronta para carregar."
    solicitacoes = list(
        SolicitacaoCargaPronta.objects.select_related("criado_por", "tratado_por")
        .filter(cd_unidade__in=cds, data=data_obj)
        .order_by("-criado_em", "-id")[:80]
    )
    faturamentos = list(
        ExpedicaoFaturamento.objects.filter(cd_unidade__in=cds, data=data_obj)
        .exclude(status="cancelado")
        .order_by("-atualizado_em", "-id")
    )
    faturamento_por_chave = {}
    for faturamento in faturamentos:
        chave = (faturamento.cd_unidade, loja_codigo(faturamento.loja), faturamento.periodo)
        resumo = faturamento_por_chave.setdefault(
            chave,
            {"valor": Decimal("0"), "cargas": [], "notas": [], "status": set(), "registros": 0},
        )
        resumo["valor"] += faturamento.valor_carga or Decimal("0")
        resumo["registros"] += 1
        if faturamento.carga_bluesoft:
            resumo["cargas"].append(faturamento.carga_bluesoft)
        if faturamento.nota_fiscal:
            resumo["notas"].append(faturamento.nota_fiscal)
        resumo["status"].add(faturamento.get_status_display())
    faturamento_por_loja = {}
    for (cd_value, loja_key, _periodo), resumo in faturamento_por_chave.items():
        geral = faturamento_por_loja.setdefault(
            (cd_value, loja_key),
            {"valor": Decimal("0"), "cargas": [], "notas": [], "status": set(), "registros": 0},
        )
        geral["valor"] += resumo["valor"]
        geral["registros"] += resumo["registros"]
        geral["cargas"].extend(resumo["cargas"])
        geral["notas"].extend(resumo["notas"])
        geral["status"].update(resumo["status"])

    def aplicar_faturamento(item):
        resumo = faturamento_por_chave.get((item.cd_unidade, loja_codigo(item.loja), item.periodo))
        resumo = resumo or faturamento_por_loja.get((item.cd_unidade, loja_codigo(item.loja)))
        item.valor_faturado_total = resumo["valor"] if resumo and can_view_values else Decimal("0")
        item.valor_faturado_label = format_currency_br(item.valor_faturado_total) if resumo and resumo["valor"] and can_view_values else ""
        item.cargas_bluesoft = ", ".join(dict.fromkeys(resumo["cargas"])) if resumo and can_view_values else ""
        item.notas_fiscais = ", ".join(dict.fromkeys(resumo["notas"])) if resumo and can_view_values else ""
        item.status_faturamento = ", ".join(sorted(resumo["status"])) if resumo and can_view_values else ""
        item.faturamento_registros = resumo["registros"] if resumo and can_view_values else 0

    for item in solicitacoes:
        aplicar_faturamento(item)

    def deduplicar_avisos_ativos(items):
        vistos = set()
        resultado = []
        for item in items:
            if item.status in {"pronta", "em_carregamento"}:
                chave = (item.cd_unidade, loja_codigo(item.loja), item.periodo, item.status)
                if chave in vistos:
                    continue
                vistos.add(chave)
            resultado.append(item)
        return resultado

    solicitacoes_visiveis = deduplicar_avisos_ativos(solicitacoes)

    if single_cd:
        cd_order = {single_cd: 0}
        for cd_value in ["801", "806", "805"]:
            cd_order.setdefault(cd_value, len(cd_order))
    else:
        cd_order = {"801": 0, "806": 1, "805": 2}
    status_order = {"pronta": 0, "em_carregamento": 1, "carregada": 2, "cancelada": 3}
    periodo_order = {"manha": 0, "tarde": 1}

    def fila_loja_sort_key(item):
        codigo = loja_codigo(item.loja)
        try:
            codigo_ordem = int(codigo)
        except (TypeError, ValueError):
            codigo_ordem = 9999
        return (
            codigo_ordem,
            codigo or "",
            cd_order.get(item.cd_unidade, 9),
            item.loja or "",
            periodo_order.get(item.periodo, 9),
            status_order.get(item.status, 9),
            item.pk or 0,
        )

    solicitacoes_recentes = list(solicitacoes_visiveis)
    solicitacoes_prontas = sorted([item for item in solicitacoes_visiveis if item.status == "pronta"], key=fila_loja_sort_key)
    for item in solicitacoes_visiveis:
        item.ready_siblings = [
            other for other in solicitacoes_prontas
            if other.pk != item.pk
        ][:12]
    solicitacoes_em_andamento = sorted([item for item in solicitacoes_visiveis if item.status == "em_carregamento"], key=fila_loja_sort_key)
    solicitacoes_historico = sorted([item for item in solicitacoes_visiveis if item.status in {"carregada", "cancelada"}], key=lambda item: item.atualizado_em or item.criado_em or timezone.now(), reverse=True)
    base["solicitacoes"] = solicitacoes_prontas
    base["prontas"] = solicitacoes_prontas
    base["em_andamento"] = solicitacoes_em_andamento
    base["historico"] = solicitacoes_historico[:24]
    base["pendentes"] = len(solicitacoes_prontas)
    base["em_andamento_total"] = len(solicitacoes_em_andamento)
    base["historico_total"] = len(solicitacoes_historico)
    base["recentes"] = solicitacoes_recentes[:12]
    base["cargas_designadas"] = solicitacoes_em_andamento[:8]
    base["can_view_billing_values"] = can_view_values
    base["faturamentos_visiveis"] = [
        {
            "cd": faturamento.cd_unidade,
            "loja": faturamento.loja,
            "motorista": faturamento.motorista,
            "placa": faturamento.placa,
            "qtd_paletes": faturamento.qtd_paletes,
            "valor": format_currency_br(faturamento.valor_carga),
            "status": faturamento.get_status_display(),
        }
        for faturamento in faturamentos
        if can_view_values and faturamento.valor_carga
    ][:12]
    base["solicitacoes_por_cd"] = [
        {
            "cd": cd_value,
            "items": [item for item in solicitacoes_prontas if item.cd_unidade == cd_value],
            "andamento": [item for item in solicitacoes_em_andamento if item.cd_unidade == cd_value],
            "pendentes": sum(1 for item in solicitacoes_prontas if item.cd_unidade == cd_value),
        }
        for cd_value in cds
    ]
    selected_codigo = loja_codigo(base.get("selected_loja", ""))
    loja_keys = planejamento_loja_keys(base.get("selected_loja", ""), loja_label_map(cds)) if selected_codigo else []
    timeline = []
    if selected_codigo:
        for cd_value in cds:
            planejamento = first_by_loja_codigo(
                ExpedicaoPlanejamento.objects.filter(cd_unidade=cd_value).exclude(status="cancelado").order_by("-atualizado_em", "-id"),
                base["selected_loja"],
            )
            if planejamento:
                if cd_value == "806":
                    detail = f"CD 806 tem {planejamento.qtd_paletes} pallet(s) prontos para a Frota."
                else:
                    detail = f"CD 801 tem {planejamento.qtd_paletes} pallet(s) ainda em box/saldo operacional."
                timeline.append(
                    {
                        "when": planejamento.atualizado_em,
                        "cd": cd_value,
                        "status": "saldo",
                        "title": "Saldo atual",
                        "detail": detail,
                    }
                )
        for item in solicitacoes:
            if loja_codigo(item.loja) == selected_codigo:
                timeline.append(
                    {
                        "when": item.atualizado_em,
                        "cd": item.cd_unidade,
                        "status": item.status,
                        "title": item.get_status_display(),
                        "detail": f"{item.qtd_paletes} pallet(s) - {item.get_periodo_display()}",
                    }
                )
        for vinculo in ExpedicaoVinculo.objects.filter(cd_unidade__in=cds, data=data_obj, loja__in=loja_keys).order_by("-atualizado_em", "-id")[:20]:
            timeline.append(
                {
                    "when": vinculo.atualizado_em,
                    "cd": vinculo.cd_unidade,
                    "status": vinculo.status,
                    "title": "A caminho" if vinculo.status == "vinculado" else vinculo.get_status_display(),
                    "detail": f"{vinculo.motorista or 'Motorista'} - {vinculo.placa or 'sem placa'} - {vinculo.qtd_paletes} pallet(s)",
                }
            )
        for expedicao in Expedicao.objects.filter(cd_unidade__in=cds, data=data_obj, loja__in=loja_keys).order_by("-atualizado_em", "-id")[:20]:
            timeline.append(
                {
                    "when": expedicao.atualizado_em,
                    "cd": expedicao.cd_unidade,
                    "status": "carregada",
                    "title": "Carregamento confirmado",
                    "detail": f"{expedicao.motorista or 'Motorista'} - {expedicao.placa or 'sem placa'} - {expedicao.qtd_paletes} pallet(s)",
                }
            )
    base["linha_tempo"] = sorted(timeline, key=lambda item: item["when"] or timezone.now(), reverse=True)[:12]
    base["alertas_operacionais"] = []
    for row in base["visible_rows"]:
        for cd_value, info in row.get("cds", {}).items():
            if cd_value == "801" and (info.get("qtd") or 0) == 0:
                base["alertas_operacionais"].append(f"CD 801: esta loja não tem saldo em box cadastrado.")
    if not base["alertas_operacionais"]:
        base["alertas_operacionais"].append("Nenhuma inconsistencia simples encontrada para a loja selecionada.")
    return base


def truck_request_ready_load_suggestions(solicitacao, include_values=False):
    statuses = ["pronta", "em_carregamento"]
    qs = SolicitacaoCargaPronta.objects.filter(
        cd_unidade=solicitacao.cd_unidade,
        data=solicitacao.data_necessidade,
        status__in=statuses,
    )
    if not qs.exists():
        qs = SolicitacaoCargaPronta.objects.filter(
            cd_unidade=solicitacao.cd_unidade,
            status__in=statuses,
            data__gte=timezone.localdate() - timedelta(days=2),
        )
    faturamentos = ExpedicaoFaturamento.objects.filter(
        cd_unidade=solicitacao.cd_unidade,
        data=solicitacao.data_necessidade,
    ).exclude(status="cancelado")
    faturamento_por_loja = {}
    for faturamento in faturamentos:
        row = faturamento_por_loja.setdefault(loja_codigo(faturamento.loja), {"valor": Decimal("0"), "cargas": []})
        row["valor"] += faturamento.valor_carga or Decimal("0")
        if faturamento.carga_bluesoft:
            row["cargas"].append(faturamento.carga_bluesoft)
    grouped = OrderedDict()
    for item in qs.order_by("loja", "periodo", "-criado_em"):
        row = grouped.setdefault(
            loja_codigo(item.loja),
            {"loja": item.loja, "pallets": 0, "periodos": set(), "status": set(), "valor": Decimal("0"), "cargas": []},
        )
        row["pallets"] += item.qtd_paletes or 0
        row["periodos"].add(item.get_periodo_display())
        row["status"].add(item.get_status_display())
        faturamento = faturamento_por_loja.get(loja_codigo(item.loja))
        if faturamento:
            row["valor"] = faturamento["valor"]
            row["cargas"] = faturamento["cargas"]
    suggestions = []
    for row in grouped.values():
        suggestions.append(
            {
                "loja": row["loja"],
                "pallets": row["pallets"],
                "periodos": ", ".join(sorted(row["periodos"])),
                "status": ", ".join(sorted(row["status"])),
                "valor": format_currency_br(row["valor"]) if include_values and row["valor"] else "",
                "cargas": ", ".join(dict.fromkeys(row["cargas"])),
            }
        )
    return suggestions


PALLET_HISTORY_ACTIONS = {
    "planejamento_expedicao_salvo",
    "planejamento_expedicao_zerado",
    "planejamento_expedicao_concluido",
    "expedicao_carga_vinculada",
    "expedicao_vinculo_confirmado",
    "expedicao_vinculo_cancelado",
    "expedicao_caminhao_salva",
    "expedicao_caminhao_trocado",
    "carregamento_manual_solicitado",
    "carregamento_manual_aprovado",
    "carregamento_manual_recusado",
    "carregamento_manual_cancelado",
    "registro_editado",
    "registro_excluido",
}


def audit_actor(log):
    return log.usuario_nome or (log.user.get_full_name() or log.user.username if log.user else "Sistema")


def audit_store(log):
    return (
        (log.dados_depois or {}).get("loja")
        or (log.dados_antes or {}).get("loja")
        or ""
    )


def audit_data_matches(log, data_obj):
    data_ref = str((log.dados_depois or {}).get("data") or (log.dados_antes or {}).get("data") or "")
    if not data_ref:
        return True
    return data_ref[:10] == data_obj.isoformat()


def pallet_history_text(log):
    actor = audit_actor(log)
    before = log.dados_antes or {}
    after = log.dados_depois or {}
    loja = after.get("loja") or before.get("loja") or ""
    qtd_antes = before.get("qtd_paletes", "")
    qtd_depois = after.get("qtd_paletes", "")
    cd_label = f"CD {log.cd_unidade}" if log.cd_unidade else "CD"

    if log.acao == "planejamento_expedicao_salvo":
        return f"{actor} salvou saldo de pallets em {cd_label}. {log.detalhe}"
    if log.acao == "planejamento_expedicao_zerado":
        return f"{actor} zerou saldo em {cd_label}. {log.detalhe}"
    if log.acao == "planejamento_expedicao_concluido":
        return f"{actor} concluiu carregamento em {cd_label}. {log.detalhe}"
    if log.acao == "expedicao_carga_vinculada":
        return f"{actor} vinculou caminhão em {cd_label}. {log.detalhe}"
    if log.acao == "expedicao_vinculo_confirmado":
        return f"{actor} confirmou carregamento em {cd_label}. {log.detalhe}"
    if log.acao == "expedicao_vinculo_cancelado":
        return f"{actor} cancelou vínculo em {cd_label}. {log.detalhe}"
    if log.acao == "expedicao_caminhao_salva":
        return f"{actor} lançou carregamento manual em {cd_label}. {log.detalhe}"
    if log.acao == "expedicao_caminhao_trocado":
        return f"{actor} trocou motorista/placa em {cd_label}. {log.detalhe}"
    if log.acao == "carregamento_manual_solicitado":
        return f"{actor} solicitou lançamento manual em {cd_label}. {log.detalhe}"
    if log.acao == "carregamento_manual_aprovado":
        return f"{actor} aprovou lançamento manual em {cd_label}. {log.detalhe}"
    if log.acao == "carregamento_manual_recusado":
        return f"{actor} recusou lançamento manual em {cd_label}. {log.detalhe}"
    if log.acao == "carregamento_manual_cancelado":
        return f"{actor} cancelou solicitação manual em {cd_label}. {log.detalhe}"
    if log.acao == "registro_editado" and log.modulo == "expedicao_planejamento" and (qtd_antes or qtd_depois):
        return f"{actor} alterou saldo da {loja or 'loja'} em {cd_label} de {qtd_antes or 0} para {qtd_depois or 0} pallet(s)."
    if log.acao == "registro_excluido" and log.modulo == "expedicao_planejamento":
        return f"{actor} excluiu saldo da {loja or 'loja'} em {cd_label}."
    return f"{actor}: {log.detalhe or log.acao}"


def pallet_store_history(cd, data_obj, selected_loja="", can_unify=False, limit=12):
    if not selected_loja:
        return []
    cds = ["801", "806"] if can_unify else [cd]
    selected_norm = normalize(selected_loja)
    logs = AuditLog.objects.select_related("user").filter(
        acao__in=PALLET_HISTORY_ACTIONS,
        cd_unidade__in=cds,
    ).order_by("-criado_em")[:160]
    history = []
    for log in logs:
        if log.acao in {"registro_editado", "registro_excluido"} and log.modulo != "expedicao_planejamento":
            continue
        loja_ref = audit_store(log)
        if selected_norm not in normalize(log.detalhe) and selected_norm != normalize(loja_ref):
            continue
        history.append({
            "quando": timezone.localtime(log.criado_em),
            "cd": log.cd_unidade,
            "acao": log.acao,
            "texto": pallet_history_text(log),
        })
        if len(history) >= limit:
            break
    return history


@login_required
def module_list(request, key):
    if key == "painel_gerencial":
        return redirect("painel_gestao")
    aliased_key = module_key_alias(key)
    if aliased_key != key:
        return redirect("module_list", key=aliased_key)
    if key == "separacao_produtividade":
        return redirect("module_list", key="separacao")
    module = get_object_or_404_module(key)
    if not can_access_module(request.user, module):
        messages.error(request, "Você não tem acesso a esta tela.")
        return redirect("dashboard")
    if module.key == "colaboradores_hub":
        return colaboradores_hub(request)
    module = replace(
        module,
        fields=visible_module_fields(module.fields),
        list_display=visible_module_fields(module.list_display),
    )

    cd = current_cd(request)
    profile = ensure_profile(request.user)
    cd_unificado_ativo = str(cd) == UNIFIED_CD
    frota_unificada = module.key in {"checklist_frota", "lacres_frota"} and cd_unificado_ativo and can_view_unified_cd(request.user)
    escala_frota_unificada = module.key == "escala_veiculos_frota" and cd_unificado_ativo and can_view_unified_cd(request.user)
    expedicao_planejamento_unificado = module.key in {"expedicao_planejamento", "carregamento_veiculos", "relatorio_paletes_cd"} and cd_unificado_ativo and can_view_unified_cd(request.user)
    expedicao_distribuicao_unificada = module.key in {"expedicao", "solicitar_lancamento_manual_expedicao"} and cd_unificado_ativo and can_view_unified_cd(request.user)
    lojas_prontas_unificadas = module.key == "lojas_prontas_carregamento" and cd_unificado_ativo and can_view_unified_cd(request.user)
    aprovacoes_carregamento_unificadas = module.key == "aprovacoes_carregamento" and cd_unificado_ativo and can_view_unified_cd(request.user)
    frota_scope_label = "801-806" if frota_unificada or escala_frota_unificada else cd
    can_create_module = can_create_records(request.user, module, profile)
    can_consult_module = can_consult_records(request.user, module)
    can_chart_module = can_view_module_charts(request.user, module)
    improvement_admin = module.key == "melhorias_sistema" and can_manage_improvements(request.user)
    if module.key == "melhorias_sistema" and not improvement_admin:
        can_consult_module = True
        can_chart_module = False
    can_export_current_module = can_export_module(request.user, module)
    can_use_frota_admin = user_has_perm(request.user, "painel_frota") or user_has_perm(request.user, "editar_checklist_frota")
    can_manage_checklist = user_has_perm(request.user, "editar_checklist_frota") or user_has_perm(request.user, "editar_registros")
    can_close_frota_return = (
        user_has_perm(request.user, "encerrar_retorno_frota")
        or user_has_perm(request.user, "editar_checklist_frota")
        or (profile and profile.cargo in {"master", "supervisor", "supervisor_frota", "gerente"})
        or getattr(request.user, "is_superuser", False)
    )
    if module.key in {"checklist_frota", "checklist_frota_itens"}:
        ensure_checklist_structure(cd)
    frota_admin_requested = module.key == "checklist_frota" and request.GET.get("painel") == "frota"
    if frota_admin_requested and not can_use_frota_admin:
        messages.error(request, "Seu usuário não tem permissão para abrir o Painel da Frota.")
        return redirect("module_list", key=key)
    frota_admin_view = frota_admin_requested and can_use_frota_admin
    motorista_frota = module.key == "checklist_frota" and can_create_module and not frota_admin_view
    frota_panel_only = module.key == "checklist_frota" and not motorista_frota and not frota_admin_view
    can_view_checklist_model = module.key == "checklist_frota" and not frota_admin_view
    show_saved_records = not motorista_frota and module.key not in {"carregamento_veiculos", "relatorio_paletes_cd", "aprovacoes_carregamento", "solicitacao_caminhoes", "solicitar_lancamento_manual_expedicao", "lojas_prontas_carregamento"} and can_consult_module and (module.key != "checklist_frota" or frota_admin_view)
    if module.key == "melhorias_sistema" and not improvement_admin:
        show_saved_records = False
    allowed_sectors = managed_sectors_for_user(request.user) if module.key in {"funcoes_turno", "pessoas_turno"} else None
    form_fields = visible_module_fields(module.fields)
    if module.key == "melhorias_sistema" and not improvement_admin:
        form_fields = ("data", "titulo", "tipo_solicitacao", "area", "descricao", "impacto", "observacao")
    if module.key == "faturamento_expedicao":
        form_fields = ("data", "loja", "placa", "motorista", "valor_carga", "periodo")
    FormClass = build_model_form(module.model, form_fields)
    selected_departure = None
    requested_departure_id = request.POST.get("saida_referencia_id") if request.method == "POST" else request.GET.get("retorno_saida")
    if module.key == "checklist_frota" and requested_departure_id:
        selected_departure = frota_open_departures_queryset(cd, unificado=frota_unificada).filter(pk=requested_departure_id).first()
        if selected_departure is not None and motorista_frota and not driver_can_handle_checklist_record(request.user, selected_departure, selected_departure.data):
            messages.error(request, "Esse check-list pertence a outro motorista. Abra apenas a sua rota vinculada pela Frota.")
            return redirect("module_list", key=key)
    checklist_data_obj = parse_iso_date(request.POST.get("data") or request.GET.get("data"), timezone.localdate())
    motorista_rota_ativa = driver_route_from_fleet_links(request.user, cd, checklist_data_obj) if motorista_frota else None

    if request.method == "POST" and module.key == "checklist_frota" and request.POST.get("action") == "dispensar_retorno":
        if not can_close_frota_return:
            messages.error(request, "Seu usuário não tem permissão para encerrar retornos pendentes.")
            return redirect("module_list", key=key)
        departure = get_object_or_404(frota_open_departures_queryset(cd, unificado=frota_unificada), pk=request.POST.get("saida_id"))
        motivo = request.POST.get("motivo", "").strip()
        if not motivo:
            messages.error(request, "Informe o motivo para encerrar o retorno sem check-list.")
            return redirect(url_with_query(reverse("module_list", kwargs={"key": key}), {"painel": "frota"}))
        departure.retorno_dispensado = True
        departure.retorno_dispensado_motivo = motivo
        departure.retorno_dispensado_em = timezone.now()
        departure.retorno_dispensado_por = request.user
        departure.save(
            update_fields=[
                "retorno_dispensado",
                "retorno_dispensado_motivo",
                "retorno_dispensado_em",
                "retorno_dispensado_por",
                "atualizado_em",
            ]
        )
        log_action(
            request,
            "retorno_frota_dispensado",
            module.key,
            departure,
            f"{departure.motorista} - {departure.placa}: {motivo}",
            departure.cd_unidade,
        )
        messages.success(request, "Retorno pendente encerrado com justificativa e registrado na auditoria.")
        return redirect(url_with_query(reverse("module_list", kwargs={"key": key}), {"painel": "frota"}))

    if request.method == "POST" and module.key == "checklist_frota_itens" and request.POST.get("action") == "salvar_grupo_checklist":
        if not can_manage_checklist:
            messages.error(request, "Seu usuário não tem permissão para editar os grupos do check-list.")
            return redirect("module_list", key=key)
        group = get_object_or_404(cd_queryset(ChecklistFrotaGrupo, cd), pk=request.POST.get("grupo_id"))
        group.titulo = request.POST.get("titulo", "").strip() or group.titulo
        group.dica = request.POST.get("dica", "").strip()
        posted_kind = request.POST.get("tipo_checklist")
        if posted_kind in {"saida", "chegada_loja", "saida_loja", "retorno", "ambos"}:
            group.tipo_checklist = posted_kind
        try:
            group.ordem = max(0, int(request.POST.get("ordem", group.ordem)))
        except (TypeError, ValueError):
            pass
        group.ativo = request.POST.get("ativo") == "on"
        group.mostrar_motorista = request.POST.get("mostrar_motorista") == "on"
        group.save()
        group.itens.update(grupo=group.titulo)
        log_action(request, "checklist_grupo_editado", module.key, group, f"Grupo do check-list: {group}", cd)
        messages.success(request, "Grupo do check-list atualizado.")
        return redirect("module_list", key=key)

    if request.method == "POST" and module.key == "checklist_frota_itens" and request.POST.get("action") == "restaurar_item_checklist":
        if not can_manage_checklist:
            messages.error(request, "Seu usuário não tem permissão para restaurar itens do check-list.")
            return redirect("module_list", key=key)
        item = get_object_or_404(cd_queryset(ChecklistFrotaItem, cd), pk=request.POST.get("item_id"))
        antes = serializable_dict(item)
        item.ativo = True
        item.save(update_fields=["ativo", "atualizado_em"])
        log_action(request, "checklist_item_restaurado", module.key, item, f"Item do check-list: {item}", antes=antes, depois=serializable_dict(item))
        messages.success(request, "Item restaurado no check-list.")
        return redirect(module_anchor_url(key, "registros-salvos"))

    if request.method == "POST" and module.key == "checklist_frota_itens" and request.POST.get("action") == "atualizar_itens_grupo_checklist":
        if not can_manage_checklist:
            messages.error(request, "Seu usuário não tem permissão para alterar itens do check-list.")
            return redirect("module_list", key=key)
        group = get_object_or_404(cd_queryset(ChecklistFrotaGrupo, cd), pk=request.POST.get("grupo_id"))
        bulk_action = request.POST.get("bulk_action")
        if bulk_action == "desativar":
            updated = group.itens.filter(ativo=True).update(ativo=False)
            label = "desativados"
        elif bulk_action == "restaurar":
            updated = group.itens.filter(ativo=False).update(ativo=True)
            label = "restaurados"
        else:
            messages.error(request, "Escolha uma ação válida para os itens do grupo.")
            return redirect("module_list", key=key)
        log_action(request, "checklist_itens_grupo_atualizados", module.key, group, f"{group}: {updated} item(ns) {label}.", cd)
        messages.success(request, f"{updated} item(ns) {label} no grupo {group.titulo}.")
        return redirect(module_anchor_url(key, "registros-salvos"))

    if request.method == "POST" and module.key == "checklist_frota_itens" and request.POST.get("action") == "atualizar_itens_selecionados_checklist":
        if not can_manage_checklist:
            messages.error(request, "Seu usuario nao tem permissao para alterar itens do check-list.")
            return redirect("module_list", key=key)
        item_ids = [value for value in request.POST.getlist("item_ids") if value]
        bulk_action = request.POST.get("bulk_action")
        if not item_ids:
            messages.warning(request, "Selecione pelo menos um item do check-list.")
            return redirect(module_anchor_url(key, "registros-salvos"))
        itens = cd_queryset(ChecklistFrotaItem, cd).filter(pk__in=item_ids)
        if bulk_action == "desativar":
            updated = itens.update(ativo=False)
            label = "desativado(s)"
        elif bulk_action == "restaurar":
            updated = itens.update(ativo=True)
            label = "restaurado(s)"
        else:
            messages.error(request, "Escolha uma acao valida para os itens selecionados.")
            return redirect(module_anchor_url(key, "registros-salvos"))
        log_action(request, "checklist_itens_selecionados_atualizados", module.key, detalhe=f"{updated} item(ns) {label}.", cd_unidade=cd)
        messages.success(request, f"{updated} item(ns) {label}.")
        return redirect(module_anchor_url(key, "registros-salvos"))

    if request.method == "POST" and module.key == "paletes_rede" and request.POST.get("action") == "paletes_rede_adicionar_loja":
        if not (
            can_create_module
            or user_has_perm(request.user, "adicionar_loja_paletes_rede")
            or user_has_perm(request.user, "editar_registros")
            or user_has_perm(request.user, "corrigir_saldo_paletes")
        ):
            messages.error(request, "Seu usuario nao tem permissao para adicionar lojas nos pallets da rede.")
            return redirect("module_list", key=key)
        codigo = normalize_palete_rede_store_codigo(request.POST.get("loja_codigo"))
        nome = clean_palete_rede_store_nome(request.POST.get("loja_nome", ""), codigo)
        cd_referencia = request.POST.get("cd_referencia", "806").strip()
        if cd_referencia not in PALETE_REDE_CDS:
            cd_referencia = "806"
        if not codigo or not nome:
            messages.error(request, "Informe o codigo e o nome da loja.")
            return redirect(module_anchor_url(key, "paletes-rede-adicionar-loja"))
        label = palete_rede_store_label(codigo, nome)
        with transaction.atomic():
            loja, created = Loja.objects.get_or_create(
                cd_unidade=cd_referencia,
                codigo=codigo,
                defaults={"nome": nome, "grupo": "outros", "ativa": True, "criado_por": request.user},
            )
            if not created and (loja.nome != nome or not loja.ativa):
                loja.nome = nome
                loja.ativa = True
                loja.save(update_fields=["nome", "ativa", "atualizado_em"])
            for tipo, _label in PaleteRedeSaldo.TIPOS_PALETE:
                PaleteRedeSaldo.objects.get_or_create(
                    local_tipo="loja",
                    local_codigo=codigo,
                    local_nome=label,
                    tipo_palete=tipo,
                    defaults={"cd_unidade": "806", "quantidade": 0, "quebrados": 0, "reservados": 0, "criado_por": request.user},
                )
        log_action(request, "paletes_rede_loja_adicionada", module.key, loja, f"Loja adicionada aos pallets da rede: {label}", cd_unidade=cd)
        messages.success(request, f"Loja {label} adicionada aos pallets da rede com saldos zerados.")
        return redirect(module_anchor_url(key, "paletes-rede-adicionar-loja"))

    if request.method == "POST" and module.key == "paletes_rede" and request.POST.get("action") == "paletes_rede_saldo_rapido":
        if not (
            user_has_perm(request.user, "editar_saldo_paletes_rede")
            or user_has_perm(request.user, "editar_registros")
            or user_has_perm(request.user, "corrigir_saldo_paletes")
        ):
            messages.error(request, "Seu usuário não tem permissão para alterar o saldo de pallets da rede.")
            return redirect("module_list", key=key)
        saldo = get_object_or_404(PaleteRedeSaldo, pk=request.POST.get("saldo_id"))
        antes = serializable_dict(saldo)
        saldo_modo = request.POST.get("saldo_modo", "editar")
        if saldo_modo == "inserir":
            saldo.quantidade = saldo.quantidade + positive_int_from_post(request, "quantidade_adicionar", 0)
        else:
            saldo.quantidade = positive_int_from_post(request, "quantidade", saldo.quantidade)
            saldo.quebrados = positive_int_from_post(request, "quebrados", saldo.quebrados)
            saldo.reservados = positive_int_from_post(request, "reservados", saldo.reservados)
        saldo.reservados = min(saldo.reservados, max(saldo.quantidade - saldo.quebrados, 0))
        saldo.responsavel = request.POST.get("responsavel", "").strip() or saldo.responsavel
        saldo.save(update_fields=["quantidade", "quebrados", "reservados", "responsavel", "atualizado_em"])
        log_action(request, "paletes_rede_saldo_atualizado", module.key, saldo, str(saldo), antes=antes, depois=serializable_dict(saldo))
        messages.success(request, "Saldo de pallets da rede atualizado.")
        local_tipo = saldo.local_tipo if saldo.local_tipo in {"cd", "loja"} else "cd"
        local_key = f"{saldo.local_tipo}|{saldo.local_codigo}|{saldo.local_nome}"
        modal_mode = "inserir" if saldo_modo == "inserir" else "editar"
        return redirect(url_with_query(module_anchor_url(key, "paletes-rede-modal"), {"local_tipo": local_tipo, "local_key": local_key, "palete_modal": modal_mode}))

    if request.method == "POST" and module.key == "paletes_rede" and request.POST.get("action") == "paletes_rede_movimentar":
        if not (can_create_module or user_has_perm(request.user, "movimentar_paletes_rede")):
            messages.error(request, "Seu usuário não tem permissão para movimentar pallets da rede.")
            return redirect("module_list", key=key)
        operacao = request.POST.get("operacao")
        tipo_palete = request.POST.get("tipo_palete")
        if operacao not in {"reserva", "transferencia", "retirada"} or tipo_palete not in dict(PaleteRedeSaldo.TIPOS_PALETE):
            messages.error(request, "Informe a operação e o tipo de pallet.")
            return redirect(module_anchor_url(key, "paletes-rede-movimentacao"))
        destino_tipo = request.POST.get("destino_tipo") or "fornecedor"
        destino_codigo = request.POST.get("destino_codigo", "").strip()
        destino_nome = request.POST.get("destino_nome", "").strip()
        if operacao in {"reserva", "retirada"}:
            destino_tipo = "fornecedor"
        if destino_tipo == "cd":
            match_cd = re.search(r"\b(801|805|806)\b", f"{destino_codigo} {destino_nome}")
            if match_cd:
                destino_codigo = match_cd.group(1)
                destino_nome = destino_codigo
        if operacao in {"reserva", "retirada"} and not destino_nome:
            messages.error(request, "Informe o fornecedor para registrar a reserva ou retirada.")
            return redirect(module_anchor_url(key, "paletes-rede-movimentacao"))
        if operacao == "transferencia" and not destino_nome:
            messages.error(request, "Informe o destino para transferir os pallets.")
            return redirect(module_anchor_url(key, "paletes-rede-movimentacao"))
        fontes = []
        total = 0
        for saldo in PaleteRedeSaldo.objects.filter(tipo_palete=tipo_palete).order_by("local_tipo", "local_codigo", "local_nome"):
            qtd = positive_int_from_post(request, f"fonte_{saldo.pk}")
            if not qtd:
                continue
            if qtd > saldo.disponivel:
                messages.error(request, f"Saldo insuficiente em {saldo.local_label}. Disponível: {saldo.disponivel}.")
                return redirect(module_anchor_url(key, "paletes-rede-movimentacao"))
            fontes.append({"id": saldo.pk, "local": saldo.local_label, "quantidade": qtd})
            total += qtd
        if total <= 0:
            messages.error(request, "Informe pelo menos uma origem com quantidade maior que zero.")
            return redirect(module_anchor_url(key, "paletes-rede-movimentacao"))
        with transaction.atomic():
            origem_labels = []
            for fonte in fontes:
                saldo = PaleteRedeSaldo.objects.select_for_update().get(pk=fonte["id"])
                qtd = fonte["quantidade"]
                origem_labels.append(f"{saldo.local_label}: {qtd}")
                if operacao == "reserva":
                    saldo.reservados += qtd
                    saldo.save(update_fields=["reservados", "atualizado_em"])
                else:
                    saldo.quantidade = max(saldo.quantidade - qtd, 0)
                    saldo.save(update_fields=["quantidade", "atualizado_em"])
            if operacao == "transferencia":
                destino, _created = PaleteRedeSaldo.objects.get_or_create(
                    local_tipo=destino_tipo,
                    local_codigo=destino_codigo,
                    local_nome=destino_nome,
                    tipo_palete=tipo_palete,
                    defaults={"cd_unidade": "806", "quantidade": 0},
                )
                destino.quantidade += total
                destino.save(update_fields=["quantidade", "atualizado_em"])
            movimento = PaleteRedeMovimentacao.objects.create(
                cd_unidade=cd if cd in {"801", "806"} else "806",
                data=timezone.localdate(),
                operacao=operacao,
                tipo_palete=tipo_palete,
                quantidade=total,
                origem_resumo=", ".join(origem_labels),
                destino_tipo=destino_tipo,
                destino_codigo=destino_codigo,
                destino_nome=destino_nome,
                solicitante=request.POST.get("solicitante", "").strip() or pessoa_nome(request.user),
                observacao=request.POST.get("observacao", "").strip(),
                fontes=fontes,
            )
        log_action(request, "paletes_rede_movimentacao", module.key, movimento, str(movimento), cd_unidade=movimento.cd_unidade)
        messages.success(request, f"{movimento.get_operacao_display()} registrada: {total} pallet(s) {palete_rede_tipo_label(tipo_palete)}.")
        return redirect(module_anchor_url(key, "paletes-rede-relatorio"))

    if request.method == "POST" and module.key == "expedicao_planejamento" and request.POST.get("action") == "salvar_planejamento_expedicao":
        if not can_create_module:
            messages.error(request, "Seu usuario nao tem permissao para preencher esta tela.")
            return redirect("module_list", key=key)
        data_obj = timezone.localdate()
        loja = request.POST.get("loja", "").strip()
        if not loja:
            messages.error(request, "Selecione uma loja para salvar.")
            return redirect(url_with_query(reverse('module_list', kwargs={'key': key}), {'data': data_obj.isoformat()}))
        try:
            qtd = max(0, int(request.POST.get("qtd_paletes") or 0))
        except (TypeError, ValueError):
            qtd = 0
        obs = request.POST.get("observacao", "").strip()
        pode_remontar = False
        qtd_remontavel = 0
        cd_alvo, cd_error = operation_cd_from_post(request, cd)
        if cd_error:
            messages.error(request, cd_error)
            return redirect(url_with_query(reverse('module_list', kwargs={'key': key}), {'data': data_obj.isoformat(), 'loja': loja}))
        labels = loja_label_map([cd_alvo])
        loja_normalizada = loja_label_from_value(loja, labels)
        codigo_loja = loja_codigo(loja_normalizada)
        with transaction.atomic():
            registros_logicos = [
                item
                for item in ExpedicaoPlanejamento.objects.select_for_update()
                .filter(cd_unidade=cd_alvo)
                .order_by("atualizado_em", "id")
                if loja_codigo(item.loja) == codigo_loja
            ]
            planejamento = registros_logicos[-1] if registros_logicos else ExpedicaoPlanejamento(cd_unidade=cd_alvo, data=data_obj)
            antes = serializable_dict(planejamento) if planejamento.pk else None
            planejamento.cd_unidade = cd_alvo
            planejamento.data = data_obj
            planejamento.loja = loja_normalizada
            planejamento.qtd_paletes = qtd
            planejamento.status = "planejado" if qtd else "cancelado"
            planejamento.pode_remontar = pode_remontar
            planejamento.qtd_remontavel = qtd_remontavel if pode_remontar else 0
            planejamento.carregado_em = None
            planejamento.observacao = obs
            planejamento.criado_por = request.user
            planejamento.save()
            duplicados = [item.pk for item in registros_logicos if item.pk != planejamento.pk]
            if duplicados:
                ExpedicaoPlanejamento.objects.filter(pk__in=duplicados).delete()
        notify_pallet_balance_saved(planejamento, actor=request.user)
        log_action(request, "planejamento_expedicao_salvo", module.key, planejamento, detalhe=f"{loja_normalizada}: {qtd} pallets em {data_obj:%d/%m/%Y}", cd_unidade=cd_alvo, antes=antes, depois=serializable_dict(planejamento))
        messages.success(request, f"Pallets da loja {loja_normalizada} salvos no CD {cd_alvo}.")
        return redirect(url_with_query(reverse('module_list', kwargs={'key': key}), {'data': data_obj.isoformat(), 'loja': loja_normalizada}))

    if request.method == "POST" and module.key in {"expedicao_planejamento", "relatorio_paletes_cd"} and request.POST.get("action") == "zerar_planejamento_expedicao":
        wants_json = request.headers.get("x-requested-with") == "XMLHttpRequest"
        if not (user_has_perm(request.user, "corrigir_saldo_paletes") or user_has_perm(request.user, "editar_registros")):
            if wants_json:
                return JsonResponse({"ok": False, "message": "Seu usuário não tem permissão para zerar saldo desta loja."}, status=403)
            messages.error(request, "Seu usuario nao tem permissao para zerar saldo desta loja.")
            return redirect("module_list", key=key)
        data_obj = timezone.localdate()
        loja = request.POST.get("loja", "").strip()
        cd_alvo = request.POST.get("cd_alvo", cd).strip()
        if cd_alvo not in {"801", "806"}:
            cd_alvo = cd
        codigo_loja = loja_codigo(loja)
        obj = next(
            (
                item
                for item in ExpedicaoPlanejamento.objects.filter(cd_unidade=cd_alvo).order_by("-atualizado_em", "-id")
                if loja_codigo(item.loja) == codigo_loja
            ),
            None,
        )
        if not obj:
            if wants_json:
                return JsonResponse({"ok": False, "message": "Não encontrei saldo cadastrado para zerar nesta loja."}, status=404)
            messages.warning(request, "Nao encontrei saldo cadastrado para zerar nesta loja.")
            return redirect(url_with_query(reverse('module_list', kwargs={'key': key}), {'data': data_obj.isoformat(), 'loja': loja, 'open_saldo': '1'}) + "#lojas-com-saldo")
        antes = serializable_dict(obj)
        qtd_anterior = obj.qtd_paletes
        obj.qtd_paletes = 0
        obj.qtd_remontavel = 0
        obj.pode_remontar = False
        obj.status = "cancelado"
        obj.observacao = (obj.observacao + f"\nSaldo zerado manualmente: {qtd_anterior} pallets por {request.user.get_full_name() or request.user.username}.").strip()
        obj.data = data_obj
        obj.save(update_fields=["data", "qtd_paletes", "qtd_remontavel", "pode_remontar", "status", "observacao", "atualizado_em"])
        notify_pallet_balance_saved(obj, actor=request.user, zerado=True)
        log_action(request, "planejamento_expedicao_zerado", module.key, obj, f"CD {cd_alvo} {loja}: {qtd_anterior} pallets zerados", cd_unidade=cd_alvo, antes=antes, depois=serializable_dict(obj))
        if wants_json:
            return JsonResponse({
                "ok": True,
                "message": f"Saldo da loja {loja} no CD {cd_alvo} zerado.",
                "cd": cd_alvo,
                "loja": loja,
                "qtd_anterior": qtd_anterior,
            })
        messages.success(request, f"Saldo da loja {loja} no CD {cd_alvo} zerado.")
        return redirect(url_with_query(reverse('module_list', kwargs={'key': key}), {'data': data_obj.isoformat(), 'loja': loja, 'open_saldo': '1'}) + "#lojas-com-saldo")

    if request.method == "POST" and module.key == "carregamento_veiculos" and request.POST.get("action") == "vincular_carga_expedicao":
        if not user_has_perm(request.user, "vincular_cargas_expedicao"):
            messages.error(request, "Seu usuario nao tem permissao para vincular cargas a caminhão.")
            return redirect("module_list", key=key)
        data_obj = parse_iso_date(request.POST.get("data"), timezone.localdate())
        loja = request.POST.get("loja", "").strip()
        cds_vinculo = ["801", "806"] if expedicao_planejamento_unificado else [cd]
        loja = loja_label_from_value(loja, loja_label_map(cds_vinculo))
        quantidades = {}
        for cd_item in cds_vinculo:
            raw_value = request.POST.get(f"qtd_paletes_cd_{cd_item}")
            if raw_value is None and cd_item == cd:
                raw_value = request.POST.get("qtd_paletes")
            try:
                quantidades[cd_item] = max(0, int(raw_value or 0))
            except (TypeError, ValueError):
                quantidades[cd_item] = 0
        qtd_total = sum(quantidades.values())
        if not loja or qtd_total <= 0:
            messages.error(request, "Selecione a loja e informe uma quantidade maior que zero em pelo menos um CD.")
            return redirect(url_with_query(reverse('module_list', kwargs={'key': key}), {'data': data_obj.isoformat(), 'loja': loja}))
        veiculo = VeiculoFrota.objects.filter(ativo=True, pk=request.POST.get("veiculo_frota")).first()
        placa = request.POST.get("placa", "").strip() or (veiculo.placa if veiculo else "")
        motorista = request.POST.get("motorista", "").strip() or (veiculo.motorista if veiculo else "")
        if not placa and not motorista:
            messages.error(request, "Selecione ou informe motorista/placa para vincular a carga.")
            return redirect(url_with_query(reverse('module_list', kwargs={'key': key}), {'data': data_obj.isoformat(), 'loja': loja}))
        unavailable_message = vehicle_unavailable_message(veiculo, cds_vinculo, placa)
        if unavailable_message:
            messages.error(request, unavailable_message)
            return redirect(url_with_query(reverse('module_list', kwargs={'key': key}), {'data': data_obj.isoformat(), 'loja': loja}))
        cds_com_qtd = [cd_item for cd_item, qtd in quantidades.items() if qtd > 0]
        exige_plataforma_manual = request.POST.get("exigir_plataforma") == "1"
        motivo_plataforma = request.POST.get("motivo_plataforma", "").strip()
        exige_plataforma = exige_plataforma_manual or loja_requires_platform(cds_com_qtd, loja)
        if exige_plataforma and not vehicle_platform_available(veiculo):
            messages.error(request, platform_vehicle_error_message(veiculo))
            return redirect(url_with_query(reverse('module_list', kwargs={'key': key}), {'data': data_obj.isoformat(), 'loja': loja}))
        motivo_plataforma_final = platform_reason(cds_com_qtd, loja, exige_plataforma_manual, motivo_plataforma)
        vinculos_criados = []
        with transaction.atomic():
            for cd_item, qtd in quantidades.items():
                if qtd <= 0:
                    continue
                planejamento = first_by_loja_codigo(
                    ExpedicaoPlanejamento.objects.select_for_update().filter(cd_unidade=cd_item).exclude(status="cancelado"),
                    loja,
                )
                saldo = planejamento.qtd_paletes if planejamento else 0
                vinculado = ExpedicaoVinculo.objects.select_for_update().filter(
                    cd_unidade=cd_item,
                    loja__in=planejamento_loja_keys(loja, loja_label_map(cds_vinculo)),
                    status="vinculado",
                ).aggregate(total=Sum("qtd_paletes"))["total"] or 0
                livre = max(saldo - vinculado, 0)
                if qtd > livre:
                    messages.error(request, f"Quantidade maior que o saldo livre do CD {cd_item}. Livre: {livre} pallet(s).")
                    return redirect(url_with_query(reverse('module_list', kwargs={'key': key}), {'data': data_obj.isoformat(), 'loja': loja}))
            for cd_item, qtd in quantidades.items():
                if qtd <= 0:
                    continue
                vinculos_criados.append(ExpedicaoVinculo.objects.create(
                    cd_unidade=cd_item,
                    data=data_obj,
                    loja=loja,
                    qtd_paletes=qtd,
                    placa=placa,
                    motorista=motorista,
                    periodo=request.POST.get("periodo") or "manha",
                    observacao=request.POST.get("observacao", "").strip(),
                    exige_plataforma=exige_plataforma,
                    motivo_plataforma=motivo_plataforma_final,
                    criado_por=request.user,
                ))
        detalhes = ", ".join(f"CD {v.cd_unidade}: {v.qtd_paletes}" for v in vinculos_criados)
        for vinculo in vinculos_criados:
            log_action(request, "expedicao_carga_vinculada", module.key, vinculo, f"{loja}: {vinculo.qtd_paletes} pallets para {motorista or placa}", cd_unidade=vinculo.cd_unidade)
        notify_expedition_load(vinculos_criados, "Carga vinculada", "Carga vinculada", actor=request.user)
        messages.success(request, f"Carga da loja {loja} reservada para o caminhão. O saldo só será baixado quando a Expedição confirmar o carregamento. {detalhes}")
        return redirect(url_with_query(reverse('module_list', kwargs={'key': key}), {'data': data_obj.isoformat(), 'loja': loja}))

    if request.method == "POST" and module.key == "carregamento_veiculos" and request.POST.get("action") == "vincular_cargas_multiplas_expedicao":
        if not user_has_perm(request.user, "vincular_cargas_expedicao"):
            messages.error(request, "Seu usuário não tem permissão para vincular cargas a caminhão.")
            return redirect("module_list", key=key)
        data_obj = parse_iso_date(request.POST.get("data"), timezone.localdate())
        cds_vinculo = ["801", "806"] if expedicao_planejamento_unificado else [cd]
        labels = loja_label_map(cds_vinculo)
        veiculo = VeiculoFrota.objects.filter(ativo=True, pk=request.POST.get("veiculo_frota")).first()
        placa = request.POST.get("placa", "").strip() or (veiculo.placa if veiculo else "")
        motorista = request.POST.get("motorista", "").strip() or (veiculo.motorista if veiculo else "")
        if not placa and not motorista:
            messages.error(request, "Selecione ou informe motorista/placa para vincular a carga.")
            return redirect("module_list", key=key)
        unavailable_message = vehicle_unavailable_message(veiculo, cds_vinculo, placa)
        if unavailable_message:
            messages.error(request, unavailable_message)
            return redirect("module_list", key=key)
        try:
            total_linhas = int(request.POST.get("multi_total") or 0)
        except (TypeError, ValueError):
            total_linhas = 0
        pedidos = []
        for index in range(1, total_linhas + 1):
            if request.POST.get(f"multi_ativo_{index}") != "1":
                continue
            loja_multi = loja_label_from_value(request.POST.get(f"multi_loja_{index}", "").strip(), labels)
            if not loja_multi:
                continue
            for cd_item in cds_vinculo:
                try:
                    qtd = max(0, int(request.POST.get(f"multi_qtd_{index}_{cd_item}") or 0))
                except (TypeError, ValueError):
                    qtd = 0
                if qtd:
                    pedidos.append((loja_multi, cd_item, qtd))
        if not pedidos:
            messages.error(request, "Selecione pelo menos uma loja e informe quantidade maior que zero.")
            return redirect("module_list", key=key)
        exige_plataforma_manual = request.POST.get("exigir_plataforma") == "1"
        motivo_plataforma = request.POST.get("motivo_plataforma", "").strip()
        exige_plataforma = exige_plataforma_manual or any(
            loja_requires_platform([cd_item], loja_multi) for loja_multi, cd_item, _qtd in pedidos
        )
        if exige_plataforma and not vehicle_platform_available(veiculo):
            messages.error(request, platform_vehicle_error_message(veiculo, "Uma das lojas selecionadas exige caminhão plataforma."))
            return redirect("module_list", key=key)
        vinculos_criados = []
        with transaction.atomic():
            for loja_multi, cd_item, qtd in pedidos:
                livre = available_pallets_for(cd_item, loja_multi, labels)
                if qtd > livre:
                    messages.error(request, f"{loja_multi} no CD {cd_item}: quantidade maior que o saldo livre. Livre: {livre} pallet(s).")
                    return redirect(url_with_query(reverse('module_list', kwargs={'key': key}), {'data': data_obj.isoformat(), 'loja': loja_multi}))
            for loja_multi, cd_item, qtd in pedidos:
                vinculos_criados.append(ExpedicaoVinculo.objects.create(
                    cd_unidade=cd_item,
                    data=data_obj,
                    loja=loja_multi,
                    qtd_paletes=qtd,
                    placa=placa,
                    motorista=motorista,
                    periodo=request.POST.get("periodo") or "manha",
                    observacao="Carga composta com mais de uma loja.",
                    exige_plataforma=exige_plataforma,
                    motivo_plataforma=(
                        platform_reason([cd_item], loja_multi, exige_plataforma_manual, motivo_plataforma)
                        if exige_plataforma
                        else ""
                    ),
                    criado_por=request.user,
                ))
        por_loja = OrderedDict()
        for vinculo in vinculos_criados:
            por_loja.setdefault(loja_codigo(vinculo.loja), []).append(vinculo)
            log_action(request, "expedicao_carga_vinculada", module.key, vinculo, f"{vinculo.loja}: {vinculo.qtd_paletes} pallets para {motorista or placa}", cd_unidade=vinculo.cd_unidade)
        for grupo in por_loja.values():
            notify_expedition_load(grupo, "Carga vinculada", "Carga vinculada", actor=request.user)
        primeira_loja = vinculos_criados[0].loja
        messages.success(request, f"{len(vinculos_criados)} carga(s) reservada(s) para o mesmo caminhão. O saldo só será baixado quando a Expedição confirmar o carregamento.")
        return redirect(url_with_query(reverse('module_list', kwargs={'key': key}), {'data': data_obj.isoformat(), 'loja': primeira_loja}))

    if request.method == "POST" and module.key in {"expedicao_planejamento", "relatorio_paletes_cd"} and request.POST.get("action") == "concluir_planejamento_expedicao":
        if not (user_has_perm(request.user, "editar_registros") or user_has_perm(request.user, "expedicao_planejamento")):
            messages.error(request, "Seu usuario nao tem permissao para concluir carregamento.")
            return redirect("module_list", key=key)
        data_obj = timezone.localdate()
        loja = request.POST.get("loja", "").strip()
        cd_alvo = request.POST.get("cd_alvo", cd).strip()
        if cd_alvo not in {"801", "806"}:
            cd_alvo = cd
        loja = loja_label_from_value(loja, loja_label_map([cd_alvo]))
        obj = first_by_loja_codigo(ExpedicaoPlanejamento.objects.filter(cd_unidade=cd_alvo).exclude(status="cancelado"), loja)
        if not obj:
            messages.warning(request, "Nao encontrei pallets cadastrados para concluir.")
            return redirect(url_with_query(reverse('module_list', kwargs={'key': key}), {'data': data_obj.isoformat(), 'loja': loja}))
        qtd_anterior = obj.qtd_paletes
        obj.qtd_paletes = 0
        obj.qtd_remontavel = 0
        obj.pode_remontar = False
        obj.status = "distribuido"
        obj.carregado_em = timezone.now()
        obj.observacao = (obj.observacao + f"\nCarregamento concluido: {qtd_anterior} pallets zerados por {request.user.get_full_name() or request.user.username}.").strip()
        obj.save(update_fields=["qtd_paletes", "qtd_remontavel", "pode_remontar", "status", "carregado_em", "observacao", "atualizado_em"])
        log_action(request, "planejamento_expedicao_concluido", module.key, obj, f"CD {cd_alvo} {loja}: {qtd_anterior} pallets zerados", cd_unidade=cd_alvo)
        messages.success(request, f"Carregamento da loja {loja} no CD {cd_alvo} concluido e zerado.")
        return redirect(url_with_query(reverse('module_list', kwargs={'key': key}), {'data': data_obj.isoformat(), 'loja': loja}))

    if request.method == "POST" and module.key == "expedicao" and request.POST.get("action") == "confirmar_vinculo_expedicao":
        if not can_create_module:
            messages.error(request, "Seu usuario nao tem permissao para confirmar carregamento.")
            return redirect("module_list", key=key)
        data_obj = parse_iso_date(request.POST.get("data"), timezone.localdate())
        loja = request.POST.get("loja", "").strip()
        valor_carga = parse_currency_br(request.POST.get("valor_carga"))
        carga_bluesoft = request.POST.get("carga_bluesoft", "").strip()
        nota_fiscal = request.POST.get("nota_fiscal", "").strip()
        faturamento_observacao = request.POST.get("faturamento_observacao", "").strip()
        if not can_view_billing_value(request.user):
            valor_carga = Decimal("0")
            carga_bluesoft = ""
            nota_fiscal = ""
            faturamento_observacao = ""
        require_load_value = system_rule_enabled("obrigar_valor_confirmar_carregamento") and can_view_billing_value(request.user)
        if require_load_value and valor_carga <= 0:
            messages.error(request, "Informe o valor faturado antes de confirmar o carregamento.")
            return redirect(url_with_query(reverse('module_list', kwargs={'key': key}), {'data': data_obj.isoformat(), 'loja': loja, 'open_saldo': '1'}) + "#lojas-com-saldo")
        escopo_cds = ["801", "806"] if (expedicao_distribuicao_unificada and user_has_perm(request.user, "trocar_cd")) else [cd]
        with transaction.atomic():
            vinculo = get_object_or_404(
                ExpedicaoVinculo.objects.select_for_update().filter(cd_unidade__in=escopo_cds, status="vinculado"),
                pk=request.POST.get("vinculo_id"),
            )
            planejamento = ExpedicaoPlanejamento.objects.select_for_update().filter(
                cd_unidade=vinculo.cd_unidade,
            ).exclude(status="cancelado")
            planejamento = first_by_loja_codigo(planejamento, vinculo.loja)
            vinculo_criado_por_loja_pronta = "aviso_de_loja_pronta" in normalize(vinculo.observacao)
            if (not planejamento or planejamento.qtd_paletes < vinculo.qtd_paletes) and not vinculo_criado_por_loja_pronta:
                disponivel = planejamento.qtd_paletes if planejamento else 0
                messages.error(request, f"Saldo insuficiente para confirmar esta carga. Disponível: {disponivel} pallet(s).")
                return redirect(url_with_query(reverse('module_list', kwargs={'key': key}), {'data': data_obj.isoformat(), 'loja': loja or vinculo.loja}))
            expedicao = Expedicao.objects.create(
                cd_unidade=vinculo.cd_unidade,
                data=vinculo.data,
                loja=vinculo.loja,
                qtd_paletes=vinculo.qtd_paletes,
                placa=vinculo.placa,
                motorista=vinculo.motorista,
                periodo=vinculo.periodo,
                status="concluido",
                observacao=(vinculo.observacao + f"\nConfirmado a partir de carga vinculada #{vinculo.pk}.").strip(),
                criado_por=request.user,
            )
            if planejamento:
                planejamento.qtd_paletes = max(0, planejamento.qtd_paletes - vinculo.qtd_paletes)
                if planejamento.qtd_paletes == 0:
                    planejamento.status = "distribuido"
                    planejamento.carregado_em = timezone.now()
                    planejamento.pode_remontar = False
                    planejamento.qtd_remontavel = 0
                else:
                    planejamento.status = "planejado"
                    planejamento.qtd_remontavel = min(planejamento.qtd_remontavel, planejamento.qtd_paletes)
                planejamento.save(update_fields=["qtd_paletes", "status", "carregado_em", "pode_remontar", "qtd_remontavel", "atualizado_em"])
            vinculo.status = "carregado"
            vinculo.carregado_em = timezone.now()
            vinculo.expedicao = expedicao
            vinculo.save(update_fields=["status", "carregado_em", "expedicao", "atualizado_em"])
            sync_ready_notice_after_vinculo_closed(vinculo, request.user)
            faturamento = None
            if valor_carga > 0 or carga_bluesoft or nota_fiscal or faturamento_observacao:
                faturamento_status = "nf_emitida" if valor_carga > 0 or carga_bluesoft or nota_fiscal else "aguardando"
                faturado_em = timezone.now() if faturamento_status == "nf_emitida" else None
                faturamento = ExpedicaoFaturamento.objects.filter(vinculo=vinculo).order_by("-id").first()
                if faturamento:
                    faturamento.cd_unidade = vinculo.cd_unidade
                    faturamento.data = vinculo.data
                    faturamento.loja = vinculo.loja
                    faturamento.placa = vinculo.placa
                    faturamento.motorista = vinculo.motorista
                    faturamento.qtd_paletes = vinculo.qtd_paletes
                    faturamento.valor_carga = valor_carga
                    faturamento.periodo = vinculo.periodo
                    faturamento.carga_bluesoft = carga_bluesoft
                    faturamento.nota_fiscal = nota_fiscal
                    faturamento.status = faturamento_status
                    faturamento.observacao = faturamento_observacao
                    faturamento.faturado_em = faturado_em
                    faturamento.save(update_fields=[
                        "cd_unidade",
                        "data",
                        "loja",
                        "placa",
                        "motorista",
                        "qtd_paletes",
                        "valor_carga",
                        "periodo",
                        "carga_bluesoft",
                        "nota_fiscal",
                        "status",
                        "observacao",
                        "faturado_em",
                        "atualizado_em",
                    ])
                else:
                    faturamento = ExpedicaoFaturamento.objects.create(
                        vinculo=vinculo,
                        cd_unidade=vinculo.cd_unidade,
                        data=vinculo.data,
                        loja=vinculo.loja,
                        placa=vinculo.placa,
                        motorista=vinculo.motorista,
                        qtd_paletes=vinculo.qtd_paletes,
                        valor_carga=valor_carga,
                        periodo=vinculo.periodo,
                        carga_bluesoft=carga_bluesoft,
                        nota_fiscal=nota_fiscal,
                        status=faturamento_status,
                        observacao=faturamento_observacao,
                        faturado_em=faturado_em,
                        criado_por=request.user,
                    )
        log_action(request, "expedicao_vinculo_confirmado", module.key, expedicao, f"{vinculo.loja}: {vinculo.qtd_paletes} pallets", cd_unidade=vinculo.cd_unidade)
        notify_expedition_load(
            [vinculo],
            "Carga confirmada",
            "Carga confirmada na expedição",
            actor=request.user,
            permissions=["notificar_alteracao_carga", "receber_alertas_expedicao"],
            include_driver=False,
        )
        if faturamento and faturamento.valor_carga:
            valor_label = format_currency_br(faturamento.valor_carga)
            url_valor = url_with_query(reverse('module_list', args=['lojas_prontas_carregamento']), {'data': data_obj.isoformat(), 'loja': vinculo.loja, 'modo': 'fila'})
            for user in User.objects.filter(is_active=True).select_related("perfil_krill"):
                if user.pk == request.user.pk or not can_view_billing_value(user):
                    continue
                profile_destino = ensure_profile(user)
                if vinculo.cd_unidade and profile_destino and not can_view_unified_cd(user) and profile_destino.cd_padrao != vinculo.cd_unidade:
                    continue
                create_system_notification(
                    user,
                    "Valor BlueSoft informado",
                    f"CD {vinculo.cd_unidade}: {vinculo.loja} carregada com {vinculo.qtd_paletes} pallet(s). Valor: {valor_label}.",
                    url_valor,
                    "frota",
                    vinculo.cd_unidade,
                )
            messages.success(request, f"Carga da loja {vinculo.loja} confirmada, saldo baixado e valor {valor_label} enviado para acompanhamento.")
        else:
            messages.success(request, f"Carga da loja {vinculo.loja} confirmada e saldo do CD {vinculo.cd_unidade} baixado.")
        return redirect(url_with_query(reverse('module_list', kwargs={'key': key}), {'data': data_obj.isoformat(), 'loja': loja or vinculo.loja}))

    if request.method == "POST" and module.key in {"expedicao", "carregamento_veiculos", "relatorio_paletes_cd"} and request.POST.get("action") == "cancelar_vinculo_expedicao":
        if not (user_has_perm(request.user, "editar_registros") or user_has_perm(request.user, "vincular_cargas_expedicao")):
            messages.error(request, "Seu usuario nao tem permissao para cancelar vinculos.")
            return redirect("module_list", key=key)
        data_obj = parse_iso_date(request.POST.get("data"), timezone.localdate())
        loja = request.POST.get("loja", "").strip()
        escopo_cds = ["801", "806"] if ((expedicao_distribuicao_unificada or expedicao_planejamento_unificado) and user_has_perm(request.user, "trocar_cd")) else [cd]
        vinculo = get_object_or_404(ExpedicaoVinculo.objects.filter(cd_unidade__in=escopo_cds, status="vinculado"), pk=request.POST.get("vinculo_id"))
        antes = serializable_dict(vinculo)
        motivo = request.POST.get("motivo_cancelamento", "").strip()
        if motivo:
            vinculo.observacao = (vinculo.observacao + f"\nVinculo cancelado: {motivo}").strip()
        vinculo.status = "cancelado"
        vinculo.save(update_fields=["status", "observacao", "atualizado_em"])
        sync_ready_notice_after_vinculo_closed(vinculo, request.user)
        log_action(request, "expedicao_vinculo_cancelado", module.key, vinculo, f"{vinculo.loja}: {vinculo.qtd_paletes} pallets", cd_unidade=vinculo.cd_unidade, antes=antes, depois=serializable_dict(vinculo))
        notify_expedition_load(
            [vinculo],
            "Carga alterada",
            "Vínculo cancelado",
            actor=request.user,
            permissions=["notificar_alteracao_carga", "receber_alertas_expedicao"],
        )
        messages.success(request, "Vinculo cancelado. O saldo voltou a ficar livre para outro caminhão.")
        return redirect(url_with_query(reverse('module_list', kwargs={'key': key}), {'data': data_obj.isoformat(), 'loja': loja or vinculo.loja}))

    if request.method == "POST" and module.key == "solicitacao_caminhoes":
        action = request.POST.get("action")
        can_manage_truck_requests = user_has_perm(request.user, "tratar_solicitacao_caminhoes")
        if action == "solicitar_caminhoes_cd":
            if not can_create_module:
                messages.error(request, "Seu usuário não tem permissão para solicitar caminhões.")
                return redirect("module_list", key=key)
            data_obj = parse_iso_date(request.POST.get("data"), timezone.localdate())
            data_necessidade = parse_iso_date(request.POST.get("data_necessidade"), timezone.localdate() + timedelta(days=1))
            cd_solicitante, cd_error = operation_cd_from_post(request, cd, "cd_origem")
            if cd_error:
                messages.error(request, "Escolha qual CD está solicitando os caminhões.")
                return redirect("module_list", key=key)
            cd_destino = request.POST.get("cd_destino", "").strip()
            if cd_destino not in {"801", "806"}:
                cd_destino = cd_values(cd)[0] if len(cd_values(cd)) == 1 else "806"
            try:
                qtd = max(1, int(request.POST.get("qtd_caminhoes") or 1))
            except (TypeError, ValueError):
                qtd = 1
            periodo = request.POST.get("periodo") or "manha"
            if periodo not in {"manha", "tarde", "dia_todo"}:
                periodo = "manha"
            precisa_plataforma = request.POST.get("precisa_plataforma") == "on"
            try:
                qtd_plataforma = max(0, int(request.POST.get("qtd_plataforma") or 0))
            except (TypeError, ValueError):
                qtd_plataforma = 0
            if not precisa_plataforma:
                qtd_plataforma = 0
            motivo = request.POST.get("motivo", "").strip()
            observacao = request.POST.get("observacao", "").strip()
            solicitacao = SolicitacaoCaminhaoCD.objects.create(
                cd_unidade=cd_solicitante,
                data=data_obj,
                data_necessidade=data_necessidade,
                cd_destino=cd_destino,
                qtd_caminhoes=qtd,
                precisa_plataforma=precisa_plataforma,
                qtd_plataforma=qtd_plataforma,
                periodo=periodo,
                motivo=motivo,
                observacao=observacao,
                criado_por=request.user,
            )
            log_action(
                request,
                "solicitacao_caminhoes_criada",
                module.key,
                solicitacao,
                f"CD {cd_solicitante} pediu {qtd} caminhões para receber no CD {cd_destino} em {data_necessidade:%d/%m/%Y}",
                cd_unidade=cd_solicitante,
            )
            notify_truck_request_created(solicitacao, actor=request.user)
            messages.success(request, "Solicitação enviada para a Frota.")
            return redirect("module_list", key=key)

        if action in {"responder_solicitacao_caminhoes", "cancelar_solicitacao_caminhoes"}:
            escopo = ["801", "806"] if can_manage_truck_requests or can_view_unified_cd(request.user) else [cd]
            solicitacao = get_object_or_404(
                SolicitacaoCaminhaoCD.objects.select_related("criado_por", "atendido_por").filter(cd_unidade__in=escopo),
                pk=request.POST.get("solicitacao_id"),
            )
            if action == "cancelar_solicitacao_caminhoes":
                if solicitacao.criado_por_id != request.user.id and not can_manage_truck_requests:
                    messages.error(request, "Seu usuário não tem permissão para cancelar esta solicitação.")
                    return redirect("module_list", key=key)
                if solicitacao.status not in {"pendente", "em_andamento"}:
                    messages.warning(request, "Esta solicitação já foi tratada.")
                    return redirect("module_list", key=key)
                antes = serializable_dict(solicitacao)
                solicitacao.status = "cancelada"
                solicitacao.resposta = request.POST.get("resposta", "").strip() or "Solicitação cancelada."
                solicitacao.atendido_por = request.user
                solicitacao.atendido_em = timezone.now()
                solicitacao.lido_pelo_solicitante_em = None
                solicitacao.save(update_fields=["status", "resposta", "atendido_por", "atendido_em", "lido_pelo_solicitante_em", "atualizado_em"])
                log_action(request, "solicitacao_caminhoes_cancelada", module.key, solicitacao, solicitacao.resposta, cd_unidade=solicitacao.cd_unidade, antes=antes, depois=serializable_dict(solicitacao))
                messages.success(request, "Solicitação cancelada.")
                return redirect("module_list", key=key)

            if not can_manage_truck_requests:
                messages.error(request, "Seu usuário não tem permissão para responder solicitações de caminhões.")
                return redirect("module_list", key=key)
            status = request.POST.get("status") or "em_andamento"
            if status not in {"em_andamento", "atendida", "recusada"}:
                status = "em_andamento"
            antes = serializable_dict(solicitacao)
            solicitacao.status = status
            solicitacao.resposta = request.POST.get("resposta", "").strip()
            veiculo_ids = [value for value in request.POST.getlist("motoristas_enviados") if value]
            veiculos_designados = list(VeiculoFrota.objects.filter(ativo=True, pk__in=veiculo_ids).select_related("usuario_motorista"))
            if solicitacao.precisa_plataforma and any(not vehicle_platform_available(veiculo) for veiculo in veiculos_designados):
                messages.error(request, "Este pedido precisa de plataforma. Envie somente veículos plataforma com plataforma operacional.")
                return redirect("module_list", key=key)
            for veiculo in veiculos_designados:
                unavailable_message = vehicle_unavailable_message(veiculo, [solicitacao.cd_unidade, solicitacao.cd_destino], veiculo.placa)
                if unavailable_message:
                    messages.error(request, unavailable_message)
                    return redirect("module_list", key=key)
            if veiculos_designados:
                solicitacao.motoristas_enviados = "\n".join(
                    f"{veiculo.motorista} - {veiculo.placa}{' - Plataforma' if veiculo.eh_plataforma else ''}{' indisponível' if veiculo.eh_plataforma and not veiculo.plataforma_operacional else ''}"
                    for veiculo in veiculos_designados
                )
            solicitacao.atendido_por = request.user
            solicitacao.atendido_em = timezone.now()
            solicitacao.lido_pelo_solicitante_em = None
            solicitacao.save(update_fields=["status", "resposta", "motoristas_enviados", "atendido_por", "atendido_em", "lido_pelo_solicitante_em", "atualizado_em"])
            log_action(request, "solicitacao_caminhoes_respondida", module.key, solicitacao, f"{solicitacao.get_status_display()}: {solicitacao.resposta}", cd_unidade=solicitacao.cd_unidade, antes=antes, depois=serializable_dict(solicitacao))
            for veiculo in veiculos_designados:
                motorista_user = getattr(veiculo, "usuario_motorista", None)
                if motorista_user and motorista_user.is_active:
                    create_system_notification(
                        motorista_user,
                        "Veículo solicitado para CD",
                        f"Você foi indicado para atender o CD {solicitacao.cd_destino} em {solicitacao.data_necessidade:%d/%m/%Y} ({solicitacao.get_periodo_display()}). Pedido feito pelo CD {solicitacao.cd_unidade}.",
                        reverse("module_list", args=["checklist_frota"]),
                        "frota",
                        solicitacao.cd_unidade,
                    )
            if solicitacao.criado_por:
                texto = f"Frota marcou seu pedido de {solicitacao.qtd_caminhoes} caminhão(ões) como {solicitacao.get_status_display()}."
                if solicitacao.motoristas_enviados:
                    texto += f" Motoristas enviados: {solicitacao.motoristas_enviados.replace(chr(10), '; ')}."
                if solicitacao.resposta:
                    texto += f" Resposta: {solicitacao.resposta}"
                create_system_notification(
                    solicitacao.criado_por,
                    "Solicitação de caminhões atualizada",
                    texto,
                    reverse("module_list", args=["solicitacao_caminhoes"]),
                    "frota",
                    solicitacao.cd_unidade,
                )
            messages.success(request, "Solicitação atualizada.")
            return redirect("module_list", key=key)

    if request.method == "POST" and module.key == "lojas_prontas_carregamento":
        action = request.POST.get("action")
        data_obj = parse_iso_date(request.POST.get("data"), timezone.localdate())
        loja_postada = request.POST.get("loja", "").strip()
        loja = loja_label_from_value(loja_postada, loja_label_map(["801", "806"]))
        retorno = url_with_query(reverse('module_list', kwargs={'key': key}), {'data': data_obj.isoformat(), 'loja': loja})
        if action == "registrar_loja_pronta_carregamento":
            if not user_has_perm(request.user, "lojas_prontas_carregamento"):
                messages.error(request, "Seu usuário não tem permissão para avisar loja pronta.")
                return redirect(retorno)
            cd_operacao, cd_error = operation_cd_from_post(request, cd)
            if cd_error:
                messages.error(request, cd_error)
                return redirect(retorno)
            loja = loja_label_from_value(loja_postada, loja_label_map([cd_operacao]))
            retorno = url_with_query(reverse('module_list', kwargs={'key': key}), {'data': data_obj.isoformat(), 'loja': loja})
            if not loja:
                messages.error(request, "Selecione a loja que está pronta para carregar.")
                return redirect(retorno)
            try:
                qtd_paletes = max(0, int(request.POST.get("qtd_paletes") or 0))
            except (TypeError, ValueError):
                qtd_paletes = 0
            periodo = request.POST.get("periodo") if request.POST.get("periodo") in {"manha", "tarde"} else "manha"
            abater_saldo_801 = cd_operacao == "801" or request.POST.get("abater_saldo_801", "off") == "on"
            saldo_restante_801 = None
            if cd_operacao == "801" and request.POST.get("saldo_restante_801", "").strip():
                try:
                    saldo_restante_801 = max(0, int(request.POST.get("saldo_restante_801") or 0))
                except (TypeError, ValueError):
                    messages.error(request, "Informe um saldo restante válido para o box do CD 801.")
                    return redirect(retorno)
            if cd_operacao == "801" and qtd_paletes and saldo_restante_801 is None:
                messages.error(
                    request,
                    "Informe quanto ficou no box do CD 801 depois da conferência/remontagem.",
                )
                return redirect(retorno)
            if cd_operacao == "801" and qtd_paletes:
                labels_cd = loja_label_map([cd_operacao])
                loja_normalizada = loja_label_from_value(loja, labels_cd)
                codigo_loja = loja_codigo(loja_normalizada)
                registros_logicos = [
                    item
                    for item in ExpedicaoPlanejamento.objects.filter(cd_unidade=cd_operacao).order_by("atualizado_em", "id")
                    if loja_codigo(item.loja) == codigo_loja
                ]
                saldo_atual = registros_logicos[-1].qtd_paletes if registros_logicos else 0
                if qtd_paletes > saldo_atual:
                    messages.error(
                        request,
                        f"Antes de enviar para a Frota, informe ou corrija o saldo em box do CD 801. Saldo atual: {saldo_atual} pallet(s).",
                    )
                    return redirect(retorno)
            observacao_pronta = request.POST.get("observacao", "").strip()
            codigo_solicitacao = loja_codigo(loja)
            solicitacoes_ativas_mesma_loja = [
                item
                for item in SolicitacaoCargaPronta.objects.filter(
                    cd_unidade=cd_operacao,
                    data=data_obj,
                    periodo=periodo,
                    status="pronta",
                ).order_by("-atualizado_em", "-id")
                if loja_codigo(item.loja) == codigo_solicitacao
            ]
            solicitacao = solicitacoes_ativas_mesma_loja[0] if solicitacoes_ativas_mesma_loja else None
            solicitacao_criada = solicitacao is None
            antes_solicitacao = serializable_dict(solicitacao) if solicitacao else None
            if solicitacao:
                solicitacao.loja = loja
                solicitacao.qtd_paletes = qtd_paletes
                solicitacao.observacao = observacao_pronta
                solicitacao.tratado_por = None
                solicitacao.tratado_em = None
                solicitacao.save(update_fields=["loja", "qtd_paletes", "observacao", "tratado_por", "tratado_em", "atualizado_em"])
                duplicados = [item.pk for item in solicitacoes_ativas_mesma_loja[1:]]
                if duplicados:
                    SolicitacaoCargaPronta.objects.filter(pk__in=duplicados).update(
                        status="cancelada",
                        resposta="Aviso duplicado substituído pelo registro mais recente.",
                        tratado_por=request.user,
                        tratado_em=timezone.now(),
                    )
            else:
                solicitacao = SolicitacaoCargaPronta.objects.create(
                    cd_unidade=cd_operacao,
                    data=data_obj,
                    loja=loja,
                    qtd_paletes=qtd_paletes,
                    periodo=periodo,
                    observacao=observacao_pronta,
                    criado_por=request.user,
                )
            planejamento_auto = None
            if cd_operacao in {"801", "806"}:
                labels_cd = loja_label_map([cd_operacao])
                loja_normalizada = loja_label_from_value(loja, labels_cd)
                codigo_loja = loja_codigo(loja_normalizada)
                with transaction.atomic():
                    registros_logicos = [
                        item
                        for item in ExpedicaoPlanejamento.objects.select_for_update()
                        .filter(cd_unidade=cd_operacao)
                        .order_by("atualizado_em", "id")
                        if loja_codigo(item.loja) == codigo_loja
                    ]
                    saldo_atual = registros_logicos[-1].qtd_paletes if registros_logicos else 0
                    if cd_operacao == "806":
                        novo_saldo = qtd_paletes
                    elif saldo_restante_801 is not None:
                        novo_saldo = saldo_restante_801
                    elif abater_saldo_801:
                        novo_saldo = max(0, saldo_atual - qtd_paletes)
                    else:
                        novo_saldo = saldo_atual
                    planejamento_auto = next((item for item in registros_logicos if item.data == data_obj), None)
                    if planejamento_auto is None:
                        planejamento_auto = registros_logicos[-1] if registros_logicos else ExpedicaoPlanejamento(cd_unidade=cd_operacao)
                    planejamento_auto.cd_unidade = cd_operacao
                    planejamento_auto.data = data_obj
                    planejamento_auto.loja = loja_normalizada
                    planejamento_auto.qtd_paletes = novo_saldo
                    planejamento_auto.status = "planejado" if novo_saldo else "cancelado"
                    planejamento_auto.pode_remontar = False
                    planejamento_auto.qtd_remontavel = 0
                    planejamento_auto.carregado_em = None
                    if cd_operacao == "806":
                        planejamento_auto.observacao = "Saldo alimentado pela tela Lojas Prontas para Carregar."
                    elif saldo_restante_801 is not None:
                        planejamento_auto.observacao = f"Saldo final informado pela Expedição após remontagem. Saldo anterior: {saldo_atual}; pronto: {qtd_paletes}."
                    elif abater_saldo_801:
                        planejamento_auto.observacao = f"Saldo abatido pela tela Lojas Prontas para Carregar. Saldo anterior: {saldo_atual}."
                    else:
                        planejamento_auto.observacao = f"Loja pronta avisada sem abater saldo. Saldo atual mantido: {saldo_atual}."
                    planejamento_auto.criado_por = request.user
                    planejamento_auto.save()
                    duplicados = [item.pk for item in registros_logicos if item.pk != planejamento_auto.pk]
                    if duplicados:
                        ExpedicaoPlanejamento.objects.filter(pk__in=duplicados).delete()
            log_action(
                request,
                "loja_pronta_carregamento_criada" if solicitacao_criada else "loja_pronta_carregamento_atualizada",
                module.key,
                solicitacao,
                f"CD {cd_operacao} avisou loja pronta: {loja} ({qtd_paletes} pallet(s)).",
                cd_operacao,
                antes=antes_solicitacao,
                depois=serializable_dict(solicitacao),
            )
            if planejamento_auto:
                log_action(
                    request,
                    "saldo_paletes_atualizado_por_loja_pronta",
                    "expedicao_planejamento",
                    planejamento_auto,
                    f"CD {cd_operacao} - {loja}: aviso de {qtd_paletes} pallet(s), saldo atual {planejamento_auto.qtd_paletes}.",
                    cd_unidade=cd_operacao,
                    depois=serializable_dict(planejamento_auto),
                )
            notify_users_by_permission(
                "notificar_loja_pronta_carregamento",
                "Loja pronta para carregar",
                f"CD {cd_operacao}: {loja} pronta para carregar. Pallets: {qtd_paletes or 'nao informado'}.",
                retorno,
                "frota",
                cd_operacao,
                exclude_user=request.user,
            )
            if planejamento_auto:
                messages.success(request, f"Aviso enviado para a Frota e saldo do CD {cd_operacao} atualizado.")
            else:
                messages.success(request, "Aviso enviado para a Frota.")
            return redirect(retorno)
        if action == "vincular_loja_pronta_multi_cd":
            if not (user_has_perm(request.user, "acompanhar_lojas_prontas_carregamento") and user_has_perm(request.user, "vincular_cargas_expedicao")):
                messages.error(request, "Seu usuario nao tem permissao para vincular loja pronta ao caminhao.")
                return redirect(retorno)
            escopo = ["801", "806"] if can_view_unified_cd(request.user) else [cd]
            posted_ids = []
            for id_value in request.POST.getlist("solicitacao_ids") + request.POST.getlist("solicitacao_extra_ids"):
                try:
                    parsed_id = int(id_value)
                except (TypeError, ValueError):
                    continue
                if parsed_id not in posted_ids:
                    posted_ids.append(parsed_id)
            solicitacao = None
            if request.POST.get("solicitacao_id"):
                solicitacao = get_object_or_404(SolicitacaoCargaPronta, pk=request.POST.get("solicitacao_id"), cd_unidade__in=escopo)
            elif posted_ids:
                solicitacao = get_object_or_404(SolicitacaoCargaPronta, pk=posted_ids[0], cd_unidade__in=escopo)
            if not solicitacao:
                messages.error(request, "Selecione ao menos uma loja pronta para vincular ao caminhão.")
                return redirect(url_with_query(reverse('module_list', kwargs={'key': key}), {'data': data_obj.isoformat(), 'modo': 'fila'}))
            retorno = url_with_query(reverse('module_list', kwargs={'key': key}), {'data': solicitacao.data.isoformat(), 'loja': solicitacao.loja, 'modo': 'fila'})
            ids_solicitacoes = []
            for extra_pk in [solicitacao.pk] + posted_ids:
                if extra_pk not in ids_solicitacoes:
                    ids_solicitacoes.append(extra_pk)
            solicitacoes_vinculo = list(
                SolicitacaoCargaPronta.objects.filter(
                    pk__in=ids_solicitacoes,
                    cd_unidade__in=escopo,
                    status="pronta",
                    data=solicitacao.data,
                ).order_by("cd_unidade", "loja", "id")
            )
            if len(solicitacoes_vinculo) != len(ids_solicitacoes):
                messages.warning(request, "Um dos avisos selecionados ja foi tratado ou nao pertence a sua permissao de CD.")
                return redirect(retorno)
            veiculo = VeiculoFrota.objects.filter(ativo=True, pk=request.POST.get("veiculo_frota")).first()
            placa = request.POST.get("placa", "").strip() or (veiculo.placa if veiculo else "")
            motorista = request.POST.get("motorista", "").strip() or (veiculo.motorista if veiculo else "")
            orientacao_frota = request.POST.get("orientacao_frota", "").strip() or request.POST.get("observacao", "").strip()
            if not placa and not motorista:
                messages.error(request, "Selecione ou informe motorista/placa para vincular a loja pronta.")
                return redirect(retorno)
            cds_vinculo = [item.cd_unidade for item in solicitacoes_vinculo]
            unavailable_message = vehicle_unavailable_message(veiculo, cds_vinculo, placa)
            if unavailable_message:
                messages.error(request, unavailable_message)
                return redirect(retorno)
            qtd_por_solicitacao = {}
            ordem_por_solicitacao = {}
            for item in solicitacoes_vinculo:
                posted_qtd = request.POST.get(f"qtd_paletes_{item.pk}")
                if posted_qtd is None and item.pk == solicitacao.pk:
                    posted_qtd = request.POST.get("qtd_paletes")
                try:
                    qtd_item = max(0, int(posted_qtd or item.qtd_paletes or 0))
                except (TypeError, ValueError):
                    qtd_item = 0
                if qtd_item <= 0:
                    messages.error(request, f"Informe a quantidade de pallets do CD {item.cd_unidade}.")
                    return redirect(retorno)
                if item.qtd_paletes and qtd_item > item.qtd_paletes:
                    messages.error(request, f"Quantidade maior que o aviso do CD {item.cd_unidade}. Avisado: {item.qtd_paletes} pallet(s).")
                    return redirect(retorno)
                qtd_por_solicitacao[item.pk] = qtd_item
                try:
                    ordem_por_solicitacao[item.pk] = max(0, int(request.POST.get(f"ordem_entrega_{item.pk}") or 0))
                except (TypeError, ValueError):
                    ordem_por_solicitacao[item.pk] = 0
            if any(loja_requires_platform([item.cd_unidade], item.loja) for item in solicitacoes_vinculo) and not vehicle_platform_available(veiculo):
                messages.error(request, platform_vehicle_error_message(veiculo))
                return redirect(retorno)
            vinculos_criados = []
            solicitacoes_atualizadas = []
            with transaction.atomic():
                solicitacoes_lock = {
                    item.pk: item for item in SolicitacaoCargaPronta.objects.select_for_update().filter(pk__in=[s.pk for s in solicitacoes_vinculo])
                }
                for item_base in solicitacoes_vinculo:
                    item = solicitacoes_lock[item_base.pk]
                    qtd_item = qtd_por_solicitacao[item.pk]
                    observacao_vinculo = f"Vínculo criado a partir do aviso de loja pronta #{item.pk}."
                    if orientacao_frota:
                        observacao_vinculo = f"{orientacao_frota}\n{observacao_vinculo}"
                    vinculo = ExpedicaoVinculo.objects.create(
                        cd_unidade=item.cd_unidade,
                        data=item.data,
                        loja=item.loja,
                        qtd_paletes=qtd_item,
                        placa=placa,
                        motorista=motorista,
                        periodo=item.periodo,
                        observacao=observacao_vinculo,
                        exige_plataforma=loja_requires_platform([item.cd_unidade], item.loja),
                        motivo_plataforma=platform_reason([item.cd_unidade], item.loja),
                        ordem_entrega=ordem_por_solicitacao.get(item.pk, 0),
                        orientacao_frota=orientacao_frota,
                        criado_por=request.user,
                    )
                    antes = serializable_dict(item)
                    item.status = "em_carregamento"
                    item.resposta = f"Vinculada ao caminhao {placa or motorista}: {qtd_item} pallet(s)."
                    item.tratado_por = request.user
                    item.tratado_em = timezone.now()
                    item.save(update_fields=["status", "resposta", "tratado_por", "tratado_em", "atualizado_em"])
                    vinculos_criados.append(vinculo)
                    solicitacoes_atualizadas.append((item, antes, qtd_item, vinculo))
            for item, antes, qtd_item, vinculo in solicitacoes_atualizadas:
                log_action(request, "loja_pronta_vinculada_caminhao", module.key, item, item.resposta, item.cd_unidade, antes=antes, depois=serializable_dict(item))
                log_action(request, "expedicao_carga_vinculada", module.key, vinculo, f"{item.loja}: {qtd_item} pallets para {motorista or placa}", cd_unidade=vinculo.cd_unidade)
            notify_expedition_load(vinculos_criados, "Carga vinculada", "Carga vinculada a partir de loja pronta", actor=request.user, include_driver=False)
            motorista_user = getattr(veiculo, "usuario_motorista", None)
            if motorista_user and motorista_user.is_active and motorista_user.pk != request.user.pk:
                detalhes_motorista = "; ".join(
                    f"CD {v.cd_unidade} - {v.loja}: {v.qtd_paletes} pallet(s)"
                    for v in vinculos_criados
                )
                create_system_notification(
                    motorista_user,
                    "Carga definida pela Frota",
                    f"Voce foi direcionado para carregar: {detalhes_motorista}.",
                    reverse("module_list", args=["checklist_frota"]),
                    "frota",
                    next(iter({v.cd_unidade for v in vinculos_criados}), ""),
                    {
                        "cds": [v.cd_unidade for v in vinculos_criados],
                        "lojas": [v.loja for v in vinculos_criados],
                        "placa": placa,
                        "motorista": motorista,
                    },
                )
            total_vinculado = sum(v.qtd_paletes for v in vinculos_criados)
            cds_label = " + ".join(f"CD {cd_item}" for cd_item in sorted({v.cd_unidade for v in vinculos_criados}))
            messages.success(request, f"Loja pronta vinculada ao caminhao em {cds_label}: {total_vinculado} pallet(s). O saldo sera baixado quando a Expedicao confirmar o carregamento.")
            return redirect(retorno)
        if action == "corrigir_loja_pronta_carregamento":
            if not user_has_perm(request.user, "acompanhar_lojas_prontas_carregamento"):
                messages.error(request, "Seu usuário não tem permissão para corrigir avisos de loja pronta.")
                return redirect(retorno)
            escopo = ["801", "806"] if can_view_unified_cd(request.user) else [cd]
            solicitacao = get_object_or_404(SolicitacaoCargaPronta, pk=request.POST.get("solicitacao_id"), cd_unidade__in=escopo)
            retorno = url_with_query(reverse('module_list', kwargs={'key': key}), {'data': solicitacao.data.isoformat(), 'loja': solicitacao.loja, 'modo': 'fila'})
            if solicitacao.status == "carregada":
                messages.error(request, "Carga já carregada não pode ser corrigida por esta fila.")
                return redirect(retorno)
            try:
                nova_qtd = max(0, int(request.POST.get("qtd_paletes") or 0))
            except (TypeError, ValueError):
                nova_qtd = 0
            if nova_qtd <= 0:
                messages.error(request, "Informe uma quantidade maior que zero.")
                return redirect(retorno)
            saldo_restante_801 = None
            if solicitacao.cd_unidade == "801" and request.POST.get("saldo_restante_801", "").strip():
                try:
                    saldo_restante_801 = max(0, int(request.POST.get("saldo_restante_801")))
                except (TypeError, ValueError):
                    messages.error(request, "Informe um saldo restante válido para o CD 801.")
                    return redirect(retorno)
            with transaction.atomic():
                solicitacao = SolicitacaoCargaPronta.objects.select_for_update().get(pk=solicitacao.pk)
                antes = serializable_dict(solicitacao)
                qtd_anterior = solicitacao.qtd_paletes
                planejamento = apply_ready_notice_quantity_adjustment(solicitacao, qtd_anterior, nova_qtd, request.user, saldo_restante_801)
                solicitacao.qtd_paletes = nova_qtd
                if solicitacao.status == "em_carregamento":
                    for vinculo in active_links_for_ready_notice(solicitacao).select_for_update():
                        vinculo.qtd_paletes = nova_qtd
                        vinculo.observacao = f"{vinculo.observacao}\nQuantidade corrigida na fila para {nova_qtd} pallet(s).".strip()
                        vinculo.save(update_fields=["qtd_paletes", "observacao", "atualizado_em"])
                    solicitacao.resposta = f"Quantidade corrigida para {nova_qtd} pallet(s)."
                solicitacao.tratado_por = request.user
                solicitacao.tratado_em = timezone.now()
                solicitacao.save(update_fields=["qtd_paletes", "resposta", "tratado_por", "tratado_em", "atualizado_em"])
            log_action(request, "loja_pronta_quantidade_corrigida", module.key, solicitacao, f"{solicitacao.loja}: {qtd_anterior} -> {nova_qtd} pallet(s).", solicitacao.cd_unidade, antes=antes, depois=serializable_dict(solicitacao))
            if planejamento:
                log_action(request, "saldo_paletes_corrigido_por_loja_pronta", "expedicao_planejamento", planejamento, f"CD {solicitacao.cd_unidade} - {solicitacao.loja}: saldo {planejamento.qtd_paletes}.", solicitacao.cd_unidade, depois=serializable_dict(planejamento))
            messages.success(request, "Quantidade corrigida e saldo ajustado.")
            return redirect(retorno)
        if action == "liberar_troca_caminhao_loja_pronta":
            if not (user_has_perm(request.user, "acompanhar_lojas_prontas_carregamento") and user_has_perm(request.user, "vincular_cargas_expedicao")):
                messages.error(request, "Seu usuário não tem permissão para trocar caminhão.")
                return redirect(retorno)
            escopo = ["801", "806"] if can_view_unified_cd(request.user) else [cd]
            solicitacao = get_object_or_404(SolicitacaoCargaPronta, pk=request.POST.get("solicitacao_id"), cd_unidade__in=escopo)
            retorno = url_with_query(reverse('module_list', kwargs={'key': key}), {'data': solicitacao.data.isoformat(), 'loja': solicitacao.loja, 'modo': 'fila'})
            if solicitacao.status != "em_carregamento":
                messages.warning(request, "Somente cargas em carregamento podem voltar para troca de caminhão.")
                return redirect(retorno)
            motivo = request.POST.get("motivo", "").strip() or "Liberada para troca de caminhão."
            with transaction.atomic():
                solicitacao = SolicitacaoCargaPronta.objects.select_for_update().get(pk=solicitacao.pk)
                antes = serializable_dict(solicitacao)
                vinculos = list(active_links_for_ready_notice(solicitacao).select_for_update())
                for vinculo in vinculos:
                    vinculo.status = "cancelado"
                    vinculo.observacao = f"{vinculo.observacao}\nVínculo cancelado para troca: {motivo}".strip()
                    vinculo.save(update_fields=["status", "observacao", "atualizado_em"])
                solicitacao.status = "pronta"
                solicitacao.resposta = f"Troca de caminhão liberada: {motivo}"
                solicitacao.tratado_por = request.user
                solicitacao.tratado_em = timezone.now()
                solicitacao.save(update_fields=["status", "resposta", "tratado_por", "tratado_em", "atualizado_em"])
            log_action(request, "loja_pronta_troca_caminhao_liberada", module.key, solicitacao, solicitacao.resposta, solicitacao.cd_unidade, antes=antes, depois=serializable_dict(solicitacao))
            for vinculo in vinculos:
                log_action(request, "expedicao_vinculo_cancelado_troca", module.key, vinculo, f"{vinculo.loja}: {motivo}", vinculo.cd_unidade)
            messages.success(request, "Carga liberada para escolher outro caminhão.")
            return redirect(retorno)
        if action == "vincular_loja_pronta_carregamento":
            if not (user_has_perm(request.user, "acompanhar_lojas_prontas_carregamento") and user_has_perm(request.user, "vincular_cargas_expedicao")):
                messages.error(request, "Seu usuário não tem permissão para vincular loja pronta ao caminhão.")
                return redirect(retorno)
            escopo = ["801", "806"] if can_view_unified_cd(request.user) else [cd]
            solicitacao = get_object_or_404(SolicitacaoCargaPronta, pk=request.POST.get("solicitacao_id"), cd_unidade__in=escopo)
            retorno = url_with_query(reverse('module_list', kwargs={'key': key}), {'data': solicitacao.data.isoformat(), 'loja': solicitacao.loja})
            if solicitacao.status != "pronta":
                messages.warning(request, "Este aviso já foi tratado. Atualize a tela para conferir o status.")
                return redirect(retorno)
            veiculo = VeiculoFrota.objects.filter(ativo=True, pk=request.POST.get("veiculo_frota")).first()
            placa = request.POST.get("placa", "").strip() or (veiculo.placa if veiculo else "")
            motorista = request.POST.get("motorista", "").strip() or (veiculo.motorista if veiculo else "")
            if not placa and not motorista:
                messages.error(request, "Selecione ou informe motorista/placa para vincular a loja pronta.")
                return redirect(retorno)
            unavailable_message = vehicle_unavailable_message(veiculo, [solicitacao.cd_unidade], placa)
            if unavailable_message:
                messages.error(request, unavailable_message)
                return redirect(retorno)
            try:
                qtd_paletes = max(0, int(request.POST.get("qtd_paletes") or solicitacao.qtd_paletes or 0))
            except (TypeError, ValueError):
                qtd_paletes = 0
            if qtd_paletes <= 0:
                messages.error(request, "Informe a quantidade de pallets que será vinculada ao caminhão.")
                return redirect(retorno)
            if solicitacao.qtd_paletes and qtd_paletes > solicitacao.qtd_paletes:
                messages.error(request, f"Quantidade maior que o aviso. Avisado: {solicitacao.qtd_paletes} pallet(s).")
                return redirect(retorno)
            exige_plataforma = loja_requires_platform([solicitacao.cd_unidade], solicitacao.loja)
            if exige_plataforma and not vehicle_platform_available(veiculo):
                messages.error(request, platform_vehicle_error_message(veiculo))
                return redirect(retorno)
            with transaction.atomic():
                observacao_manual = request.POST.get("observacao", "").strip()
                observacao_vinculo = f"Vínculo criado a partir do aviso de loja pronta #{solicitacao.pk}."
                if observacao_manual:
                    observacao_vinculo = f"{observacao_manual}\n{observacao_vinculo}"
                vinculo = ExpedicaoVinculo.objects.create(
                    cd_unidade=solicitacao.cd_unidade,
                    data=solicitacao.data,
                    loja=solicitacao.loja,
                    qtd_paletes=qtd_paletes,
                    placa=placa,
                    motorista=motorista,
                    periodo=solicitacao.periodo,
                    observacao=observacao_vinculo,
                    exige_plataforma=exige_plataforma,
                    motivo_plataforma=platform_reason([solicitacao.cd_unidade], solicitacao.loja),
                    criado_por=request.user,
                )
                antes = serializable_dict(solicitacao)
                solicitacao.status = "em_carregamento"
                solicitacao.resposta = f"Vinculada ao caminhão {placa or motorista}: {qtd_paletes} pallet(s)."
                solicitacao.tratado_por = request.user
                solicitacao.tratado_em = timezone.now()
                solicitacao.save(update_fields=["status", "resposta", "tratado_por", "tratado_em", "atualizado_em"])
            log_action(request, "loja_pronta_vinculada_caminhao", module.key, solicitacao, solicitacao.resposta, solicitacao.cd_unidade, antes=antes, depois=serializable_dict(solicitacao))
            log_action(request, "expedicao_carga_vinculada", module.key, vinculo, f"{solicitacao.loja}: {qtd_paletes} pallets para {motorista or placa}", cd_unidade=vinculo.cd_unidade)
            notify_expedition_load([vinculo], "Carga vinculada", "Carga vinculada a partir de loja pronta", actor=request.user)
            messages.success(request, "Loja pronta vinculada ao caminhão. O saldo será baixado quando a Expedição confirmar o carregamento.")
            return redirect(retorno)
        if action == "atualizar_loja_pronta_carregamento":
            if not user_has_perm(request.user, "acompanhar_lojas_prontas_carregamento") and request.POST.get("status") != "cancelada":
                messages.error(request, "Seu usuário não tem permissão para tratar avisos de loja pronta.")
                return redirect(retorno)
            escopo = ["801", "806"] if can_view_unified_cd(request.user) else [cd]
            solicitacao = get_object_or_404(SolicitacaoCargaPronta, pk=request.POST.get("solicitacao_id"), cd_unidade__in=escopo)
            antes = serializable_dict(solicitacao)
            status = request.POST.get("status")
            if status not in {"pronta", "em_carregamento", "carregada", "cancelada"}:
                status = solicitacao.status
            solicitacao.status = status
            solicitacao.resposta = request.POST.get("resposta", "").strip()
            solicitacao.tratado_por = request.user
            solicitacao.tratado_em = timezone.now()
            solicitacao.save(update_fields=["status", "resposta", "tratado_por", "tratado_em", "atualizado_em"])
            if status == "cancelada":
                active_links_for_ready_notice(solicitacao).update(status="cancelado")
            elif status == "carregada":
                active_links_for_ready_notice(solicitacao).update(status="carregado", carregado_em=solicitacao.tratado_em)
            log_action(
                request,
                "loja_pronta_carregamento_tratada",
                module.key,
                solicitacao,
                f"{solicitacao.loja}: {solicitacao.get_status_display()}",
                solicitacao.cd_unidade,
                antes=antes,
                depois=serializable_dict(solicitacao),
            )
            if solicitacao.criado_por_id and solicitacao.criado_por_id != request.user.id:
                create_system_notification(
                    solicitacao.criado_por,
                    "Retorno da Frota",
                    f"{solicitacao.loja}: {solicitacao.get_status_display()}. {solicitacao.resposta}".strip(),
                    retorno,
                    "expedicao",
                    solicitacao.cd_unidade,
                )
            messages.success(request, "Aviso atualizado.")
            return redirect(url_with_query(reverse('module_list', kwargs={'key': key}), {'data': solicitacao.data.isoformat(), 'loja': solicitacao.loja, 'modo': 'fila'}))

    if request.method == "POST" and module.key in {"expedicao", "solicitar_lancamento_manual_expedicao"} and request.POST.get("action") == "solicitar_lancamento_manual_expedicao":
        if not user_has_perm(request.user, "solicitar_lancamento_manual_expedicao"):
            messages.error(request, "Seu usuario nao tem permissao para solicitar lançamento manual.")
            return redirect("module_list", key=key)
        data_obj = parse_iso_date(request.POST.get("data"), timezone.localdate())
        loja = request.POST.get("loja", "").strip()
        cd_solicitacao = cd
        if expedicao_distribuicao_unificada:
            cd_postado = request.POST.get("cd_destino", "").strip()
            if cd_postado in {"801", "806"}:
                cd_solicitacao = cd_postado
            else:
                messages.error(request, "Escolha o CD do pedido manual.")
                return redirect(url_with_query(reverse('module_list', kwargs={'key': key}), {'data': data_obj.isoformat(), 'loja': loja}))
        loja = loja_label_from_value(loja, loja_label_map([cd_solicitacao]))
        try:
            qtd = max(0, int(request.POST.get("qtd_paletes") or 0))
        except (TypeError, ValueError):
            qtd = 0
        periodo = request.POST.get("periodo") or "manha"
        if periodo not in {"manha", "tarde"}:
            periodo = "manha"
        veiculo = VeiculoFrota.objects.filter(cd_unidade=cd_solicitacao, ativo=True, pk=request.POST.get("veiculo_frota")).first()
        placa = request.POST.get("placa", "").strip() or (veiculo.placa if veiculo else "")
        motorista = request.POST.get("motorista", "").strip() or (veiculo.motorista if veiculo else "")
        motivo = request.POST.get("motivo", "").strip()
        if not loja or qtd <= 0:
            messages.error(request, "Selecione a loja e informe uma quantidade maior que zero.")
            return redirect(url_with_query(reverse('module_list', kwargs={'key': key}), {'data': data_obj.isoformat(), 'loja': loja}))
        if not placa and not motorista:
            messages.error(request, "Informe motorista ou placa para a Frota analisar o pedido.")
            return redirect(url_with_query(reverse('module_list', kwargs={'key': key}), {'data': data_obj.isoformat(), 'loja': loja}))
        unavailable_message = vehicle_unavailable_message(veiculo, [cd_solicitacao], placa)
        if unavailable_message:
            messages.error(request, unavailable_message)
            return redirect(url_with_query(reverse('module_list', kwargs={'key': key}), {'data': data_obj.isoformat(), 'loja': loja}))
        if not motivo:
            messages.error(request, "Informe o motivo do lançamento manual.")
            return redirect(url_with_query(reverse('module_list', kwargs={'key': key}), {'data': data_obj.isoformat(), 'loja': loja}))
        exige_plataforma_manual = request.POST.get("exigir_plataforma") == "1"
        motivo_plataforma = request.POST.get("motivo_plataforma", "").strip()
        exige_plataforma = exige_plataforma_manual or loja_requires_platform([cd_solicitacao], loja)
        if exige_plataforma and not vehicle_platform_available(veiculo):
            messages.error(request, platform_vehicle_error_message(veiculo, "Esta loja/operação exige caminhão plataforma."))
            return redirect(url_with_query(reverse('module_list', kwargs={'key': key}), {'data': data_obj.isoformat(), 'loja': loja}))
        solicitacao = SolicitacaoCarregamentoManual.objects.create(
            cd_unidade=cd_solicitacao,
            data=data_obj,
            loja=loja,
            qtd_paletes=qtd,
            placa=placa,
            motorista=motorista,
            periodo=periodo,
            exige_plataforma=exige_plataforma,
            motivo_plataforma=platform_reason([cd_solicitacao], loja, exige_plataforma_manual, motivo_plataforma),
            motivo=motivo,
            criado_por=request.user,
        )
        log_action(request, "carregamento_manual_solicitado", module.key, solicitacao, f"{loja}: {qtd} pallet(s) para {motorista or placa}", cd_unidade=cd_solicitacao)
        notify_users_by_permission(
            "aprovar_lancamento_manual_expedicao",
            "Pedido manual de carregamento",
            f"{loja}: {qtd} pallet(s) para {motorista or placa}. Aguardando aprovação.",
            reverse("module_list", args=["aprovacoes_carregamento"]),
            "frota",
            cd_solicitacao,
            exclude_user=request.user,
        )
        messages.success(request, "Solicitação enviada para aprovação da Frota.")
        return redirect(url_with_query(reverse('module_list', kwargs={'key': key}), {'data': data_obj.isoformat(), 'loja': loja, 'open_saldo': '1'}) + "#lojas-com-saldo")

    if request.method == "POST" and module.key == "aprovacoes_carregamento" and request.POST.get("action") in {"aprovar_carregamento_manual", "recusar_carregamento_manual", "cancelar_solicitacao_carregamento"}:
        action = request.POST.get("action")
        escopo_cds = ["801", "806"] if aprovacoes_carregamento_unificadas else [cd]
        solicitacao = get_object_or_404(
            SolicitacaoCarregamentoManual.objects.select_related("criado_por").filter(cd_unidade__in=escopo_cds),
            pk=request.POST.get("solicitacao_id"),
        )
        if action == "cancelar_solicitacao_carregamento":
            if solicitacao.criado_por_id != request.user.id and not user_has_perm(request.user, "aprovar_lancamento_manual_expedicao"):
                messages.error(request, "Seu usuario nao tem permissao para cancelar esta solicitação.")
                return redirect("module_list", key=key)
            if solicitacao.status != "pendente":
                messages.warning(request, "Esta solicitação já foi tratada.")
                return redirect("module_list", key=key)
            antes = serializable_dict(solicitacao)
            solicitacao.status = "cancelado"
            solicitacao.resposta = request.POST.get("resposta", "").strip() or "Solicitação cancelada."
            solicitacao.aprovado_por = request.user
            solicitacao.aprovado_em = timezone.now()
            solicitacao.lido_pelo_solicitante_em = None
            solicitacao.save(update_fields=["status", "resposta", "aprovado_por", "aprovado_em", "lido_pelo_solicitante_em", "atualizado_em"])
            log_action(request, "carregamento_manual_cancelado", module.key, solicitacao, f"{solicitacao.loja}: {solicitacao.qtd_paletes} pallet(s)", cd_unidade=solicitacao.cd_unidade, antes=antes, depois=serializable_dict(solicitacao))
            if solicitacao.criado_por:
                create_system_notification(
                    solicitacao.criado_por,
                    "Pedido de carregamento cancelado",
                    solicitacao.resposta,
                    reverse("minhas_solicitacoes"),
                    "expedicao",
                    solicitacao.cd_unidade,
                )
            messages.success(request, "Solicitação cancelada.")
            return redirect("module_list", key=key)
        if not user_has_perm(request.user, "aprovar_lancamento_manual_expedicao"):
            messages.error(request, "Seu usuario nao tem permissao para aprovar ou recusar carregamentos.")
            return redirect("module_list", key=key)
        if solicitacao.status != "pendente":
            messages.warning(request, "Esta solicitação já foi tratada.")
            return redirect("module_list", key=key)
        resposta = request.POST.get("resposta", "").strip()
        antes = serializable_dict(solicitacao)
        if action == "recusar_carregamento_manual":
            solicitacao.status = "recusado"
            solicitacao.resposta = resposta or "Solicitação recusada pela Frota."
            solicitacao.aprovado_por = request.user
            solicitacao.aprovado_em = timezone.now()
            solicitacao.lido_pelo_solicitante_em = None
            solicitacao.save(update_fields=["status", "resposta", "aprovado_por", "aprovado_em", "lido_pelo_solicitante_em", "atualizado_em"])
            log_action(request, "carregamento_manual_recusado", module.key, solicitacao, f"{solicitacao.loja}: {solicitacao.qtd_paletes} pallet(s)", cd_unidade=solicitacao.cd_unidade, antes=antes, depois=serializable_dict(solicitacao))
            if solicitacao.criado_por:
                create_system_notification(
                    solicitacao.criado_por,
                    "Pedido de carregamento recusado",
                    solicitacao.resposta,
                    reverse("minhas_solicitacoes"),
                    "expedicao",
                    solicitacao.cd_unidade,
                )
            messages.success(request, "Solicitação recusada e registrada.")
            return redirect("module_list", key=key)
        with transaction.atomic():
            solicitacao = SolicitacaoCarregamentoManual.objects.select_for_update().get(pk=solicitacao.pk)
            planejamento = ExpedicaoPlanejamento.objects.select_for_update().filter(
                cd_unidade=solicitacao.cd_unidade,
            ).exclude(status="cancelado")
            planejamento = first_by_loja_codigo(planejamento, solicitacao.loja)
            vinculado = ExpedicaoVinculo.objects.select_for_update().filter(
                cd_unidade=solicitacao.cd_unidade,
                loja__in=planejamento_loja_keys(solicitacao.loja, loja_label_map([solicitacao.cd_unidade])),
                status="vinculado",
            ).aggregate(total=Sum("qtd_paletes"))["total"] or 0
            saldo_livre = max((planejamento.qtd_paletes if planejamento else 0) - vinculado, 0)
            if solicitacao.qtd_paletes > saldo_livre:
                messages.error(request, f"Saldo livre insuficiente no CD {solicitacao.cd_unidade}. Livre: {saldo_livre} pallet(s).")
                return redirect("module_list", key=key)
            expedicao = Expedicao.objects.create(
                cd_unidade=solicitacao.cd_unidade,
                data=solicitacao.data,
                loja=solicitacao.loja,
                qtd_paletes=solicitacao.qtd_paletes,
                placa=solicitacao.placa,
                motorista=solicitacao.motorista,
                periodo=solicitacao.periodo,
                status="concluido",
                observacao=(
                    f"Lançamento manual aprovado pela Frota. Motivo: {solicitacao.motivo}"
                    + (f"\nResposta: {resposta}" if resposta else "")
                ),
                criado_por=request.user,
            )
            planejamento.qtd_paletes = max(0, planejamento.qtd_paletes - solicitacao.qtd_paletes)
            if planejamento.qtd_paletes == 0:
                planejamento.status = "distribuido"
                planejamento.carregado_em = timezone.now()
                planejamento.pode_remontar = False
                planejamento.qtd_remontavel = 0
            else:
                planejamento.status = "planejado"
                planejamento.qtd_remontavel = min(planejamento.qtd_remontavel, planejamento.qtd_paletes)
            planejamento.save(update_fields=["qtd_paletes", "status", "carregado_em", "pode_remontar", "qtd_remontavel", "atualizado_em"])
            solicitacao.status = "concluido"
            solicitacao.resposta = resposta or "Aprovado pela Frota e lançado na expedição."
            solicitacao.aprovado_por = request.user
            solicitacao.aprovado_em = timezone.now()
            solicitacao.expedicao = expedicao
            solicitacao.lido_pelo_solicitante_em = None
            solicitacao.save(update_fields=["status", "resposta", "aprovado_por", "aprovado_em", "expedicao", "lido_pelo_solicitante_em", "atualizado_em"])
        log_action(request, "carregamento_manual_aprovado", module.key, solicitacao, f"{solicitacao.loja}: {solicitacao.qtd_paletes} pallet(s)", cd_unidade=solicitacao.cd_unidade, antes=antes, depois=serializable_dict(solicitacao))
        if solicitacao.criado_por:
            create_system_notification(
                solicitacao.criado_por,
                "Pedido de carregamento aprovado",
                solicitacao.resposta,
                reverse("minhas_solicitacoes"),
                "expedicao",
                solicitacao.cd_unidade,
            )
        notify_manual_loading_approved(solicitacao, actor=request.user)
        messages.success(request, "Solicitação aprovada, carregamento lançado e saldo baixado.")
        return redirect("module_list", key=key)

    if request.method == "POST" and module.key == "expedicao" and request.POST.get("action") == "salvar_expedicao_caminhao":
        if not user_has_perm(request.user, "lancar_manual_sem_aprovacao"):
            messages.error(request, "Seu usuario nao tem permissao para lançar carregamento manual sem aprovação.")
            return redirect("module_list", key=key)
        data_obj = parse_iso_date(request.POST.get("data"), timezone.localdate())
        cd_expedicao, cd_error = operation_cd_from_post(request, cd)
        if cd_error:
            messages.error(request, cd_error)
            return redirect("module_list", key=key)
        loja = request.POST.get("loja", "").strip()
        loja = loja_label_from_value(loja, loja_label_map([cd_expedicao]))
        if not loja:
            messages.error(request, "Selecione uma loja para expedir.")
            return redirect(url_with_query(reverse('module_list', kwargs={'key': key}), {'data': data_obj.isoformat()}))
        try:
            qtd = max(0, int(request.POST.get("qtd_paletes") or 0))
        except (TypeError, ValueError):
            qtd = 0
        if qtd <= 0:
            messages.error(request, "Informe uma quantidade de pallets maior que zero.")
            return redirect(url_with_query(reverse('module_list', kwargs={'key': key}), {'data': data_obj.isoformat(), 'loja': loja}))
        veiculo = VeiculoFrota.objects.filter(cd_unidade=cd_expedicao, ativo=True, pk=request.POST.get("veiculo_frota")).first()
        placa = request.POST.get("placa", "").strip() or (veiculo.placa if veiculo else "")
        motorista = request.POST.get("motorista", "").strip() or (veiculo.motorista if veiculo else "")
        unavailable_message = vehicle_unavailable_message(veiculo, [cd_expedicao], placa)
        if unavailable_message:
            messages.error(request, unavailable_message)
            return redirect(url_with_query(reverse('module_list', kwargs={'key': key}), {'data': data_obj.isoformat(), 'loja': loja}))
        with transaction.atomic():
            planejamento = ExpedicaoPlanejamento.objects.select_for_update().filter(
                cd_unidade=cd_expedicao,
            ).exclude(status="cancelado")
            planejamento = first_by_loja_codigo(planejamento, loja)
            if not planejamento or planejamento.qtd_paletes <= 0:
                messages.error(request, "Cadastre saldo em Pallets por CD antes de lançar o caminhão desta loja.")
                return redirect(url_with_query(reverse('module_list', kwargs={'key': key}), {'data': data_obj.isoformat(), 'loja': loja}))
            vinculado = ExpedicaoVinculo.objects.select_for_update().filter(
                cd_unidade=cd_expedicao,
                loja__in=planejamento_loja_keys(loja, loja_label_map([cd_expedicao])),
                status="vinculado",
            ).aggregate(total=Sum("qtd_paletes"))["total"] or 0
            saldo_livre = max(planejamento.qtd_paletes - vinculado, 0)
            if qtd > saldo_livre:
                messages.error(request, f"Quantidade maior que o saldo livre no CD {cd_expedicao}. Livre: {saldo_livre} pallet(s).")
                return redirect(url_with_query(reverse('module_list', kwargs={'key': key}), {'data': data_obj.isoformat(), 'loja': loja}))
            obj = Expedicao.objects.create(
                cd_unidade=cd_expedicao,
                data=data_obj,
                loja=loja,
                qtd_paletes=qtd,
                placa=placa,
                motorista=motorista,
                periodo=request.POST.get("periodo") or "manha",
                status="concluido",
                observacao=request.POST.get("observacao", "").strip(),
                criado_por=request.user,
            )
            planejamento.qtd_paletes = max(0, planejamento.qtd_paletes - qtd)
            if planejamento.qtd_paletes == 0:
                planejamento.status = "distribuido"
                planejamento.carregado_em = timezone.now()
                planejamento.pode_remontar = False
                planejamento.qtd_remontavel = 0
            else:
                planejamento.status = "planejado"
                planejamento.qtd_remontavel = min(planejamento.qtd_remontavel, planejamento.qtd_paletes)
            planejamento.save(update_fields=["qtd_paletes", "status", "carregado_em", "pode_remontar", "qtd_remontavel", "atualizado_em"])
        log_action(request, "expedicao_caminhao_salva", module.key, obj, f"{loja}: {qtd} pallets", cd_unidade=cd_expedicao)
        messages.success(request, f"Expedição da loja {loja} salva e saldo do CD {cd_expedicao} baixado.")
        return redirect(url_with_query(reverse('module_list', kwargs={'key': key}), {'data': data_obj.isoformat(), 'loja': loja}))

    if request.method == "POST" and module.key == "expedicao" and request.POST.get("action") == "trocar_expedicao_caminhao":
        if not user_has_perm(request.user, "editar_registros"):
            messages.error(request, "Seu usuario nao tem permissao para trocar motorista ou placa.")
            return redirect("module_list", key=key)
        data_obj = parse_iso_date(request.POST.get("data"), timezone.localdate())
        loja = request.POST.get("loja", "").strip()
        escopo_cds = ["801", "806"] if expedicao_distribuicao_unificada else [cd]
        registro = get_object_or_404(Expedicao.objects.filter(cd_unidade__in=escopo_cds), pk=request.POST.get("expedicao_id"))
        antes = serializable_dict(registro)
        veiculo = VeiculoFrota.objects.filter(cd_unidade=registro.cd_unidade, ativo=True, pk=request.POST.get("veiculo_frota")).first()
        nova_placa = request.POST.get("placa", "").strip() or (veiculo.placa if veiculo else registro.placa)
        novo_motorista = request.POST.get("motorista", "").strip() or (veiculo.motorista if veiculo else registro.motorista)
        motivo = request.POST.get("motivo_transferencia", "").strip()
        retorno = url_with_query(reverse('module_list', kwargs={'key': key}), {'data': data_obj.isoformat(), 'loja': loja or registro.loja})
        if not nova_placa and not novo_motorista:
            messages.error(request, "Informe o novo motorista, a nova placa ou selecione um cadastro.")
            return redirect(retorno)
        unavailable_message = vehicle_unavailable_message(veiculo, [registro.cd_unidade], nova_placa)
        if unavailable_message:
            messages.error(request, unavailable_message)
            return redirect(retorno)
        if nova_placa == registro.placa and novo_motorista == registro.motorista:
            messages.warning(request, "Nada foi alterado. O motorista e a placa informados sao os mesmos do registro.")
            return redirect(retorno)
        if loja_requires_platform([registro.cd_unidade], registro.loja) and not vehicle_platform_available(veiculo):
            messages.error(request, platform_vehicle_error_message(veiculo, "Esta loja exige caminhão plataforma."))
            return redirect(retorno)
        usuario = request.user.get_full_name() or request.user.username
        historico = (
            f"Troca de caminhão em {timezone.localtime():%d/%m/%Y %H:%M} por {usuario}: "
            f"{registro.motorista or '-'} / {registro.placa or '-'} -> {novo_motorista or '-'} / {nova_placa or '-'}"
        )
        if motivo:
            historico += f". Motivo: {motivo}"
        registro.motorista = novo_motorista
        registro.placa = nova_placa
        registro.observacao = (registro.observacao + "\n" + historico).strip()
        registro.save(update_fields=["motorista", "placa", "observacao", "atualizado_em"])
        log_action(
            request,
            "expedicao_caminhao_trocado",
            module.key,
            registro,
            historico,
            cd_unidade=registro.cd_unidade,
            antes=antes,
            depois=serializable_dict(registro),
        )
        messages.success(request, "Motorista/placa do carregamento atualizados sem alterar o saldo de pallets.")
        return redirect(retorno)

    if request.method == "POST" and not can_create_module:
        messages.error(request, "Seu usuário não tem permissão para preencher esta tela.")
        return redirect("module_list", key=key)

    if request.method == "POST" and module.key == "ferias_colaboradores" and request.POST.get("action") == "salvar_cores_ferias":
        if not user_has_perm(request.user, "editar_registros"):
            messages.error(request, "Seu usuário não tem permissão para alterar as cores das férias.")
            return redirect("module_list", key=key)
        payload = {}
        for status_key in DEFAULT_FERIAS_COLORS:
            color = request.POST.get(f"cor_{status_key}", "").strip()
            if color.startswith("#") and len(color) in {4, 7}:
                payload[status_key] = color
        set_config(FERIAS_COLORS_CONFIG, json.dumps(payload, ensure_ascii=False), request.user, "Cores do painel de férias")
        messages.success(request, "Cores das férias atualizadas.")
        return redirect("module_list", key=key)

    if request.method == "POST":
        post_data = request.POST
        if module.key == "faturamento_expedicao" and request.POST.get("veiculo_frota"):
            post_data = request.POST.copy()
            veiculo = VeiculoFrota.objects.filter(ativo=True, pk=request.POST.get("veiculo_frota")).first()
            if veiculo:
                post_data["placa"] = veiculo.placa
                post_data["motorista"] = veiculo.motorista
        if module.key == "checklist_frota" and motorista_rota_ativa:
            post_data = request.POST.copy()
            post_data["tipo_checklist"] = motorista_rota_ativa["next_type"]
            post_data["motorista"] = motorista_rota_ativa.get("motorista", "")
            post_data["placa"] = motorista_rota_ativa.get("placa", "")
            post_data["veiculo"] = motorista_rota_ativa.get("veiculo", "")
            current_stop = motorista_rota_ativa.get("current_stop")
            post_data["loja_destino"] = current_stop["loja"] if current_stop else motorista_rota_ativa.get("route_label", "")
        form = FormClass(post_data, cd_unidade=cd, allowed_sectors=allowed_sectors)
        if module.key == "checklist_frota" and motorista_rota_ativa:
            apply_driver_route_to_checklist_form(form, motorista_rota_ativa)
        if form.is_valid():
            if (
                module.key == "checklist_frota"
                and form.cleaned_data.get("tipo_checklist") == "retorno"
                and requested_departure_id
                and selected_departure is None
            ):
                messages.error(request, "Essa saída já recebeu retorno, foi encerrada ou não pertence ao CD selecionado.")
                return redirect("module_list", key=key)
            obj = form.save(commit=False)
            if hasattr(obj, "cd_unidade"):
                obj.cd_unidade = cd
                if module.key == "escala_veiculos_frota" and str(cd) == UNIFIED_CD:
                    veiculo = (
                        VeiculoFrota.objects.filter(ativo=True, placa__iexact=getattr(obj, "placa", ""))
                        .order_by("cd_unidade", "id")
                        .first()
                    )
                    obj.cd_unidade = veiculo.cd_unidade if veiculo else "801"
            if hasattr(obj, "criado_por"):
                obj.criado_por = request.user
            if module.key == "melhorias_sistema" and not improvement_admin:
                obj.solicitante = pessoa_nome(request.user)
                obj.prioridade = "media"
                obj.status = "sugestao"
                obj.responsavel = ""
                obj.previsao = None
                obj.decisao = ""
            if module.key == "recebimentos" and getattr(obj, "nota_fiscal", ""):
                duplicated = module.model.objects.filter(
                    cd_unidade=cd,
                    nota_fiscal=obj.nota_fiscal,
                    fornecedor=obj.fornecedor,
                ).exists()
                if duplicated:
                    messages.warning(request, "Atenção: já existe recebimento com esta NF e fornecedor neste CD.")
            if module.key == "checklist_frota":
                route_error = validate_driver_route_checklist(request, obj, motorista_rota_ativa)
                if route_error:
                    messages.error(request, route_error)
                    return redirect("module_list", key=key)
                if motorista_frota and not motorista_rota_ativa and not driver_can_handle_checklist_record(request.user, obj, obj.data):
                    messages.error(request, "Esse check-list nÃ£o pertence ao seu login. Procure a Frota se a escala estiver incorreta.")
                    return redirect("module_list", key=key)
                if obj.tipo_checklist == "saida":
                    open_departure = matching_open_departure(
                        cd,
                        placa=obj.placa,
                        motorista=obj.motorista,
                        unificado=can_view_unified_cd(request.user),
                    )
                    if open_departure is not None:
                        messages.error(
                            request,
                            "Já existe uma saída aberta para esta placa/motorista. Registre o retorno ou encerre a pendência antes de lançar nova saída.",
                        )
                        return redirect("module_list", key=key)
                    obj.acompanhar_retorno = True
                elif obj.tipo_checklist == "retorno":
                    departure = selected_departure
                    if departure is None:
                        departure = (
                            frota_open_departures_queryset(cd)
                            .filter(placa__iexact=obj.placa.strip(), motorista__iexact=obj.motorista.strip())
                            .first()
                        )
                    obj.acompanhar_retorno = False
                    if departure is not None:
                        if motorista_frota and not driver_can_handle_checklist_record(request.user, departure, departure.data):
                            messages.error(request, "Esse retorno pertence a outro motorista. Abra apenas a sua rota vinculada pela Frota.")
                            return redirect("module_list", key=key)
                        obj.saida_referencia = departure
                        obj.cd_unidade = departure.cd_unidade
                        obj.motorista = departure.motorista
                        obj.veiculo = departure.veiculo
                        obj.placa = departure.placa
                        obj.loja_destino = departure.loja_destino
                        if not obj.horario_saida:
                            obj.horario_saida = departure.horario_saida
                else:
                    departure = (
                        frota_open_departures_queryset(cd)
                        .filter(placa__iexact=obj.placa.strip(), motorista__iexact=obj.motorista.strip())
                        .first()
                    )
                    obj.acompanhar_retorno = False
                    if departure is not None:
                        if motorista_frota and not driver_can_handle_checklist_record(request.user, departure, departure.data):
                            messages.error(request, "Essa etapa pertence a outro motorista. Abra apenas a sua rota vinculada pela Frota.")
                            return redirect("module_list", key=key)
                        obj.cd_unidade = departure.cd_unidade
                        obj.motorista = departure.motorista
                        obj.veiculo = departure.veiculo
                        obj.placa = departure.placa
                        obj.loja_destino = departure.loja_destino
                        if not obj.horario_saida:
                            obj.horario_saida = departure.horario_saida
                        if obj.tipo_checklist == "saida_loja" and not obj.horario_chegada_loja:
                            arrival = (
                                ChecklistFrota.objects.filter(
                                    cd_unidade=departure.cd_unidade,
                                    tipo_checklist="chegada_loja",
                                    placa__iexact=departure.placa,
                                    motorista__iexact=departure.motorista,
                                    data=departure.data,
                                )
                                .order_by("-criado_em", "-id")
                                .first()
                            )
                            if arrival and arrival.horario_chegada_loja:
                                obj.horario_chegada_loja = arrival.horario_chegada_loja
                obj.itens_personalizados = checklist_custom_payload(request, cd)
            if module.key == "checklist_frota_itens":
                if obj.grupo_config_id:
                    obj.grupo = obj.grupo_config.titulo
                if not obj.grupo:
                    obj.grupo = "Itens adicionais"
            if module.key == "faturamento_expedicao":
                obj.status = "nf_emitida" if getattr(obj, "valor_carga", 0) else "aguardando"
                obj.faturado_em = timezone.now() if obj.status == "nf_emitida" else None
            try:
                obj.save()
            except IntegrityError:
                if module.key == "checklist_frota" and obj.tipo_checklist == "retorno":
                    messages.error(request, "Esse retorno já foi registrado. Atualize a tela para conferir.")
                    return redirect("module_list", key=key)
                raise
            log_action(request, "registro_criado", module.key, obj, f"{module.title}: {obj}", cd)
            if module.key == "checklist_frota":
                notify_driver_checklist(obj, actor=request.user)
            if module.key == "faturamento_expedicao" and obj.status in {"nf_emitida", "liberado_ronilo"}:
                url = url_with_query(reverse('module_list', args=['lojas_prontas_carregamento']), {'data': obj.data.isoformat(), 'loja': obj.loja, 'modo': 'fila'})
                valor = f"R$ {obj.valor_carga:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
                detalhe = f" Valor: {valor}." if obj.valor_carga else ""
                if obj.observacao:
                    detalhe += f" Obs.: {obj.observacao}."
                notify_users_by_permission(
                    "visualizar_valor_faturamento",
                    "Faturamento concluído",
                    f"CD {obj.cd_unidade}: {obj.loja} faturada.{detalhe}",
                    url,
                    "faturamento",
                    obj.cd_unidade,
                    exclude_user=request.user,
                )
            if module.key == "checklist_frota" and obj.tipo_checklist == "retorno" and obj.saida_referencia_id:
                messages.success(request, "Retorno registrado e viagem concluída.")
            elif module.key == "checklist_frota" and obj.tipo_checklist == "retorno":
                messages.warning(request, "Retorno registrado, mas nenhuma saída aberta correspondente foi encontrada.")
            elif module.key == "checklist_frota" and obj.tipo_checklist == "chegada_loja":
                messages.success(request, "Chegada na loja registrada.")
            elif module.key == "checklist_frota" and obj.tipo_checklist == "saida_loja":
                messages.success(request, "Saída da loja registrada.")
            elif module.key == "checklist_frota" and getattr(obj, "intercala_cd", False) and not getattr(obj, "intercalacao_concluida", False):
                messages.warning(request, "Saída registrada. O retorno e a intercalação ficaram pendentes para conclusão.")
            elif module.key == "checklist_frota":
                messages.success(request, "Saída registrada. O sistema lembrará o motorista de preencher o retorno.")
            elif module.key == "melhorias_sistema" and not improvement_admin:
                messages.success(request, "Solicitação enviada para análise da gestão do sistema.")
            else:
                messages.success(request, "Registro salvo com sucesso.")
            return redirect("module_list", key=key)
    else:
        initial = None
        if module.key == "checklist_frota" and selected_departure is not None:
            initial = {
                "tipo_checklist": "retorno",
                "data": timezone.localdate(),
                "motorista": selected_departure.motorista,
                "veiculo": selected_departure.veiculo,
                "placa": selected_departure.placa,
                "loja_destino": selected_departure.loja_destino,
                "intercala_cd": selected_departure.intercala_cd,
                "cd_intercalacao": selected_departure.cd_intercalacao,
                "intercalacao_concluida": selected_departure.intercalacao_concluida,
                "observacao_intercalacao": selected_departure.observacao_intercalacao,
                "km_inicial": selected_departure.km_inicial,
                "horario_saida": selected_departure.horario_saida,
            }
        elif module.key == "checklist_frota":
            initial = {
                "tipo_checklist": "saida",
                "data": timezone.localdate(),
                **checklist_vehicle_initial_for_user(request.user, cd),
            }
        form = FormClass(cd_unidade=cd, initial=initial, allowed_sectors=allowed_sectors)
        if module.key == "checklist_frota" and motorista_rota_ativa:
            apply_driver_route_to_checklist_form(form, motorista_rota_ativa)
    apply_checklist_field_labels(form, cd)

    q = request.GET.get("q", "").strip()
    loja_planejamento = request.GET.get("loja", "").strip()
    frota_placa = request.GET.get("placa", "").strip()
    frota_motorista = request.GET.get("motorista", "").strip()

    queryset = module.model.objects.all() if frota_unificada or escala_frota_unificada or expedicao_planejamento_unificado or expedicao_distribuicao_unificada or aprovacoes_carregamento_unificadas or lojas_prontas_unificadas else module.model.objects.filter(cd_unidade=cd)
    frota_outros_cds = 0
    if module.key == "melhorias_sistema" and not improvement_admin:
        queryset = queryset.filter(criado_por=request.user)
    if module.key in {"funcoes_turno", "pessoas_turno"} and allowed_sectors is not None:
        queryset = queryset.filter(setor__in=allowed_sectors)
    if module.key == "checklist_frota_itens":
        queryset = queryset.select_related("grupo_config").order_by("grupo_config__ordem", "grupo", "ordem", "titulo")
    if module.key == "checklist_frota":
        queryset = apply_frota_filters(queryset, frota_placa, frota_motorista)
        if not frota_unificada:
            outros_cds = module.model.objects.exclude(cd_unidade=cd)
            outros_cds = apply_frota_filters(outros_cds, frota_placa, frota_motorista)
            frota_outros_cds = outros_cds.count()
    if module.key == "escala_veiculos_frota":
        queryset = queryset.filter(ativo=True).order_by("placa", "-fim", "-inicio", "-id")
    if module.key in {"expedicao_planejamento", "relatorio_paletes_cd"}:
        queryset = queryset.filter(data=parse_iso_date(request.GET.get("data"), timezone.localdate())).order_by("loja", "cd_unidade")
    if module.key == "expedicao":
        queryset = queryset.filter(data=parse_iso_date(request.GET.get("data"), timezone.localdate()))
        if loja_planejamento:
            queryset = queryset.filter(loja__in=planejamento_loja_keys(loja_planejamento, loja_label_map(["801", "806"])))
        queryset = queryset.order_by("-data", "loja", "periodo", "placa")
    if module.key == "aprovacoes_carregamento":
        queryset = queryset.order_by("status", "-criado_em", "-id")

    export_format = request.GET.get("exportar")
    if export_format and not can_export_current_module:
        messages.error(request, "Seu usuário não tem permissão para exportar dados desta tela.")
        return redirect("module_list", key=key)
    if export_format == "relatorio_rede_xlsx" and module.key == "paletes_rede":
        response = export_paletes_rede_xlsx()
        log_action(request, "paletes_rede_xlsx", module.key, detalhe="Relatorio de pallets da rede")
        return response
    should_load_rows = (
        bool(export_format)
        or show_saved_records
        or module.key in {"aprovacoes_carregamento", "checklist_frota_itens"}
        or (module.key == "checklist_frota" and frota_admin_view)
    )
    if should_load_rows:
        raw_limit = 500 if export_format else 300 if q else 160
        if q:
            queryset = [obj for obj in queryset[:raw_limit] if object_matches_search(obj, q)]
        else:
            queryset = list(queryset[:raw_limit])
    else:
        queryset = []
    if module.key == "escala_veiculos_frota":
        escala_unica = []
        placas_vistas = set()
        for escala in queryset:
            placa = normalize_vehicle_key(getattr(escala, "placa", ""))
            chave = placa or f"escala:{escala.pk}"
            if chave in placas_vistas:
                continue
            placas_vistas.add(chave)
            escala_unica.append(escala)
        queryset = escala_unica
    if export_format == "csv":
        response = export_module_csv(module, queryset, frota_scope_label, q)
        log_action(request, "modulo_csv", module.key, detalhe=f"{module.title} CD {frota_scope_label}")
        return response
    if export_format == "xlsx":
        response = export_module_xlsx(module, queryset, frota_scope_label, q)
        log_action(request, "modulo_xlsx", module.key, detalhe=f"{module.title} CD {frota_scope_label}")
        return response
    if export_format == "docx" and module.key == "materiais_frota":
        response = export_module_docx(module, queryset, frota_scope_label, q)
        log_action(request, "modulo_docx", module.key, detalhe=f"{module.title} CD {frota_scope_label}")
        return response
    if export_format == "powerbi_frota" and module.key == "checklist_frota":
        response = export_frota_powerbi_package(frota_scope_label, queryset, q)
        log_action(request, "powerbi_frota_export", module.key, detalhe=f"{module.title} CD {frota_scope_label}")
        return response

    checklist_item_editor = checklist_item_editor_context(queryset, cd) if module.key == "checklist_frota_itens" else None
    planejamento_data = parse_iso_date(request.GET.get("data"), timezone.localdate())
    queryset = queryset[:120]

    checklist_form_visible = module.key == "checklist_frota" and can_create_module and not frota_admin_view
    checklist_preview_visible = can_view_checklist_model and not motorista_frota
    checklist_initial = getattr(form, "initial", {}) if module.key == "checklist_frota" else {}
    checklist_form_groups = checklist_frota_groups(motorista=motorista_frota, cd=cd, initial=checklist_initial) if checklist_form_visible else None
    checklist_preview_groups = checklist_frota_groups(motorista=True, cd=cd, initial=checklist_initial) if checklist_preview_visible else None
    checklist_shared_custom_groups = checklist_frota_custom_groups(cd) if checklist_form_visible or checklist_preview_visible else None
    frota_checklist_groups = group_frota_checklists(queryset) if module.key == "checklist_frota" and frota_admin_view else []
    frota_open_trips = (
        frota_open_trip_data(cd, unificado=frota_unificada or motorista_frota, limit=50, driver_user=request.user if motorista_frota else None)
        if module.key == "checklist_frota"
        else None
    )
    frota_closed_returns = frota_closed_return_history(cd, unificado=frota_unificada, limit=30) if module.key == "checklist_frota" else None
    motorista_cargas = driver_linked_loads(request.user) if motorista_frota else []
    veiculos_frota_escala_options = []
    if module.key == "veiculos_frota":
        veiculos_frota_escala_options = [
            {
                "label": f"{escala.placa} - {escala.motorista_responsavel} ({escala.inicio:%d/%m} a {escala.fim:%d/%m})",
                "placa": escala.placa,
                "motorista": escala.motorista_responsavel,
                "usuario_id": escala.usuario_responsavel_id or "",
            }
            for escala in (
                cd_queryset(EscalaVeiculoFrota, cd)
                .select_related("usuario_responsavel")
                .filter(ativo=True)
                .order_by("-fim", "-inicio", "placa")[:80]
            )
        ]

    ctx = context_base(request)
    if module.key == "checklist_frota_itens" and user_has_perm(request.user, "editar_checklist_frota"):
        ctx["can_delete"] = True
    if module.key == "expedicao_planejamento" and user_has_perm(request.user, "corrigir_saldo_paletes"):
        ctx["can_delete"] = True
    can_manage_truck_requests = user_has_perm(request.user, "tratar_solicitacao_caminhoes")
    truck_request_scope = ["801", "806"] if can_manage_truck_requests or can_view_unified_cd(request.user) else [cd]
    solicitacoes_caminhoes = []
    caminhoes_recebendo_cd = []
    if module.key == "solicitacao_caminhoes":
        status_order = {"pendente": 0, "em_andamento": 1, "atendida": 2, "recusada": 3, "cancelada": 4}
        solicitacoes_caminhoes = list(
            SolicitacaoCaminhaoCD.objects.select_related("criado_por", "atendido_por")
            .filter(cd_unidade__in=truck_request_scope)
            .order_by("-data_necessidade", "-criado_em")[:80]
        )
        solicitacoes_caminhoes.sort(key=lambda item: status_order.get(item.status, 9))
        for solicitacao in solicitacoes_caminhoes:
            sugestoes = truck_request_ready_load_suggestions(solicitacao, can_view_billing_value(request.user))
            solicitacao.lojas_prontas_sugeridas = sugestoes
            solicitacao.pallets_prontos_sugeridos = sum(item["pallets"] for item in sugestoes)
        destino_scope = ["801", "806"] if can_view_unified_cd(request.user) else cd_values(cd)
        caminhoes_recebendo_cd = list(
            SolicitacaoCaminhaoCD.objects.select_related("criado_por", "atendido_por")
            .filter(
                cd_destino__in=destino_scope,
                data_necessidade__gte=timezone.localdate() - timedelta(days=1),
                status__in=["em_andamento", "atendida"],
            )
            .exclude(motoristas_enviados="")
            .order_by("data_necessidade", "-atendido_em", "-criado_em")[:20]
        )
    pallet_history_loja = loja_planejamento
    if module.key in {"expedicao_planejamento", "carregamento_veiculos", "relatorio_paletes_cd", "expedicao"} and not pallet_history_loja:
        pallet_history_cds = ["801", "806"] if (expedicao_distribuicao_unificada or expedicao_planejamento_unificado) else [cd]
        primeira_loja = Loja.objects.filter(cd_unidade__in=pallet_history_cds, ativa=True).order_by("codigo", "nome").first()
        if primeira_loja:
            pallet_history_loja = str(primeira_loja)
        else:
            pallet_history_loja = (
                current_pallet_queryset(pallet_history_cds)
                .order_by("loja")
                .values_list("loja", flat=True)
                .first()
                or ""
            )
    vehicle_options_scope = ["801", "806"]
    ctx.update(
        {
            "module": module,
            "form": form,
            "rows": queryset,
            "q": q,
            "motorista_frota": motorista_frota,
            "frota_panel_only": frota_panel_only,
            "frota_admin_view": frota_admin_view,
            "frota_checklist_groups": frota_checklist_groups,
            "frota_open_trips": frota_open_trips,
            "frota_closed_returns": frota_closed_returns,
            "motorista_cargas": motorista_cargas,
            "motorista_rota_ativa": motorista_rota_ativa,
            "veiculos_frota_escala_options": veiculos_frota_escala_options,
            "selected_departure": selected_departure,
            "can_close_frota_return": can_close_frota_return,
            "improvement_admin": improvement_admin,
            "frota_unificada": frota_unificada,
            "expedicao_planejamento": expedicao_planejamento_context(cd, planejamento_data, expedicao_planejamento_unificado, loja_planejamento) if module.key in {"expedicao_planejamento", "carregamento_veiculos"} else None,
            "relatorio_paletes_cd": expedicao_distribuicao_context(cd, planejamento_data, expedicao_planejamento_unificado, loja_planejamento) if module.key == "relatorio_paletes_cd" else None,
            "expedicao_distribuicao": expedicao_distribuicao_context(cd, planejamento_data, expedicao_distribuicao_unificada, loja_planejamento) if module.key in {"expedicao", "solicitar_lancamento_manual_expedicao"} else None,
            "lojas_prontas_carregamento": lojas_prontas_context(cd, planejamento_data, lojas_prontas_unificadas, loja_planejamento, can_view_billing_value(request.user) and feature_enabled("faturamento_expedicao")) if module.key == "lojas_prontas_carregamento" else None,
            "paletes_rede": paletes_rede_context(request.GET.get("local_tipo"), request.GET.get("local_key"), request.GET.get("palete_modal")) if module.key == "paletes_rede" else None,
            "pallet_store_history": pallet_store_history(
                cd,
                planejamento_data,
                pallet_history_loja,
                expedicao_distribuicao_unificada if module.key == "expedicao" else expedicao_planejamento_unificado,
            ) if module.key in {"expedicao_planejamento", "carregamento_veiculos", "relatorio_paletes_cd", "expedicao", "solicitar_lancamento_manual_expedicao"} else [],
            "veiculos_frota": unique_vehicle_options(
                VeiculoFrota.objects.filter(
                    cd_unidade__in=vehicle_options_scope,
                    ativo=True,
                ).order_by("-atualizado_em", "-data", "-id"),
                vehicle_options_scope,
            ) if module.key in {"expedicao", "expedicao_planejamento", "carregamento_veiculos", "solicitar_lancamento_manual_expedicao", "solicitacao_caminhoes", "lojas_prontas_carregamento", "faturamento_expedicao"} else [],
            "frota_scope_label": frota_scope_label,
            "frota_placa": frota_placa,
            "frota_motorista": frota_motorista,
            "frota_outros_cds": frota_outros_cds,
            "can_create_module": can_create_module,
            "can_consult_module": can_consult_module,
            "can_chart_module": can_chart_module,
            "can_export_module": can_export_current_module,
            "can_link_expedition_loads": user_has_perm(request.user, "vincular_cargas_expedicao"),
            "can_request_manual_load": user_has_perm(request.user, "solicitar_lancamento_manual_expedicao"),
            "can_notify_ready_loads": user_has_perm(request.user, "lojas_prontas_carregamento"),
            "can_manage_ready_loads": user_has_perm(request.user, "acompanhar_lojas_prontas_carregamento"),
            "can_view_billing_values": can_view_billing_value(request.user) and feature_enabled("faturamento_expedicao"),
            "ready_load_queue_mode": module.key == "lojas_prontas_carregamento" and request.GET.get("modo") == "fila",
            "can_approve_manual_load": user_has_perm(request.user, "aprovar_lancamento_manual_expedicao"),
            "can_direct_manual_load": user_has_perm(request.user, "lancar_manual_sem_aprovacao"),
            "can_add_network_pallet_store": user_has_perm(request.user, "adicionar_loja_paletes_rede"),
            "can_move_network_pallets": user_has_perm(request.user, "movimentar_paletes_rede"),
            "can_manage_pallet_balance": user_has_perm(request.user, "editar_saldo_paletes_rede") or user_has_perm(request.user, "corrigir_saldo_paletes") or user_has_perm(request.user, "editar_registros"),
            "can_manage_truck_requests": can_manage_truck_requests,
            "solicitacoes_caminhoes": solicitacoes_caminhoes,
            "caminhoes_recebendo_cd": caminhoes_recebendo_cd,
            "solicitacoes_caminhoes_pendentes": sum(1 for item in solicitacoes_caminhoes if item.status == "pendente"),
            "solicitacao_caminhoes_amanha": timezone.localdate() + timedelta(days=1),
            "show_saved_records": show_saved_records,
            "can_edit_module": (module.key == "melhorias_sistema" and improvement_admin) or (module.key != "melhorias_sistema" and user_has_perm(request.user, "editar_registros")) or (
                module.key in {"checklist_frota", "checklist_frota_itens"} and user_has_perm(request.user, "editar_checklist_frota")
            ) or (
                module.key in {"expedicao_planejamento", "relatorio_paletes_cd"} and user_has_perm(request.user, "corrigir_saldo_paletes")
            ),
            "equipment_stats": build_equipamentos_dashboard(cd) if module.key == "equipamentos" and (can_chart_module or user_has_perm(request.user, "saude_equipamentos")) else None,
            "checklist_frota_groups": checklist_form_groups,
            "checklist_custom_groups": checklist_shared_custom_groups if checklist_form_visible else None,
            "checklist_preview_groups": checklist_preview_groups,
            "checklist_preview_custom_groups": checklist_shared_custom_groups if checklist_preview_visible else None,
            "checklist_item_editor": checklist_item_editor,
            "ferias_colors": ferias_colors() if module.key == "ferias_colaboradores" else None,
            "ferias_alertas": ferias_alertas(cd) if module.key == "ferias_colaboradores" else None,
            "capacidade_operacao": build_capacidade_operacao(cd) if module.key == "ferias_colaboradores" and (user_has_perm(request.user, "capacidade_operacao") or user_has_perm(request.user, "mapa_calor_ferias")) else None,
            "ferias_heatmap": build_ferias_heatmap(cd) if module.key == "ferias_colaboradores" and user_has_perm(request.user, "mapa_calor_ferias") else None,
            "frota_stats": build_frota_dashboard(cd, frota_placa, frota_motorista, frota_unificada) if module.key == "checklist_frota" and frota_admin_view and can_chart_module else None,
            "melhoria_stats": build_melhorias_dashboard(cd) if module.key == "melhorias_sistema" and improvement_admin and can_chart_module else None,
            "module_overview": build_module_overview(module, cd) if can_chart_module else None,
            "module_form_sections": form_sections_for_module(module, form) if can_create_module else None,
            "module_stacked": module.key in FOCUSED_MODULES,
        }
    )
    template_name = MODULE_SCREEN_TEMPLATES.get(module.key, "painel/module_list.html")
    return render(request, template_name, ctx)

def get_object_or_404_module(key):
    module = MODULE_BY_KEY.get(key)
    if not module:
        raise ValueError("Modulo inexistente")
    return customize_module(module)


def editable_module_queryset(request, module):
    if module.key in {"checklist_frota", "lacres_frota"} and can_view_unified_cd(request.user):
        return module.model.objects.all()
    profile = ensure_profile(request.user)
    if module.key == "expedicao_planejamento" and user_has_perm(request.user, "corrigir_saldo_paletes") and (
        can_view_unified_cd(request.user)
        or request.user.is_superuser
        or (profile and profile.cargo == "master")
    ):
        return module.model.objects.all()
    queryset = cd_queryset(module.model, current_cd(request))
    allowed_sectors = managed_sectors_for_user(request.user)
    if module.key in {"funcoes_turno", "pessoas_turno"} and allowed_sectors is not None:
        queryset = queryset.filter(setor__in=allowed_sectors)
    return queryset


@login_required
def module_edit(request, key, pk):
    module = get_object_or_404_module(key)
    if not can_access_module(request.user, module):
        messages.error(request, "Você não tem acesso a esta tela.")
        return redirect("dashboard")
    can_edit_this = user_has_perm(request.user, "editar_registros") or (
        module.key in {"checklist_frota", "checklist_frota_itens"} and user_has_perm(request.user, "editar_checklist_frota")
    ) or (
        module.key == "expedicao_planejamento" and user_has_perm(request.user, "corrigir_saldo_paletes")
    )
    if module.key == "melhorias_sistema":
        can_edit_this = can_manage_improvements(request.user)
    if not can_edit_this:
        messages.error(request, "Seu usuário não tem permissão para editar registros.")
        return redirect("module_list", key=key)
    obj = get_object_or_404(editable_module_queryset(request, module), pk=pk)
    bloqueio = acquire_edit_lock(request, module.key, pk)
    if bloqueio:
        messages.warning(
            request,
            f"Este registro está sendo editado por {bloqueio.get('usuario', 'outro usuário')}. Tente novamente em alguns minutos.",
        )
        return redirect("module_list", key=key)
    antes = serializable_dict(obj)
    cd = current_cd(request)
    if module.key in {"checklist_frota", "checklist_frota_itens"}:
        ensure_checklist_structure(cd)
    FormClass = build_model_form(module.model, module.fields)
    allowed_sectors = managed_sectors_for_user(request.user) if module.key in {"funcoes_turno", "pessoas_turno"} else None
    if request.method == "POST":
        bloqueio = acquire_edit_lock(request, module.key, pk)
        if bloqueio:
            messages.warning(request, "Outro usuário assumiu a edição deste registro. A alteração não foi salva.")
            return redirect("module_list", key=key)
        form = FormClass(request.POST, instance=obj, cd_unidade=cd, allowed_sectors=allowed_sectors)
        if form.is_valid():
            saved = form.save(commit=False)
            if module.key == "checklist_frota_itens":
                if saved.grupo_config_id:
                    saved.grupo = saved.grupo_config.titulo
                if not saved.grupo:
                    saved.grupo = "Itens adicionais"
            if module.key == "melhorias_sistema":
                if antes.get("status") != saved.status or antes.get("decisao") != saved.decisao:
                    saved.lido_pelo_solicitante_em = None
            saved.save()
            release_edit_lock(request, module.key, pk)
            log_action(request, "registro_editado", module.key, saved, f"{module.title}: {saved}", antes=antes, depois=serializable_dict(saved))
            messages.success(request, "Registro atualizado.")
            return redirect("module_list", key=key)
    else:
        form = FormClass(instance=obj, cd_unidade=cd, allowed_sectors=allowed_sectors)
    apply_checklist_field_labels(form, cd)
    ctx = context_base(request)
    ctx.update({"module": module, "form": form, "obj": obj})
    return render(request, "painel/module_form.html", ctx)


@login_required
@require_POST
def module_delete(request, key, pk):
    module = get_object_or_404_module(key)
    if not can_access_module(request.user, module):
        messages.error(request, "Você não tem acesso a esta tela.")
        return redirect("dashboard")
    can_delete_this = user_has_perm(request.user, "excluir_registros") or (
        module.key == "checklist_frota_itens" and user_has_perm(request.user, "editar_checklist_frota")
    ) or (
        module.key == "expedicao_planejamento" and user_has_perm(request.user, "corrigir_saldo_paletes")
    )
    if module.key == "melhorias_sistema":
        can_delete_this = can_manage_improvements(request.user) and user_has_perm(request.user, "excluir_registros")
    if not can_delete_this:
        messages.error(request, "Seu usuário não tem permissão para excluir registros.")
        return redirect("module_list", key=key)
    obj = get_object_or_404(editable_module_queryset(request, module), pk=pk)
    detail = str(obj)
    antes = serializable_dict(obj)
    if module.key == "checklist_frota_itens":
        obj.ativo = False
        obj.save(update_fields=["ativo", "atualizado_em"])
        log_action(request, "checklist_item_desativado", module.key, obj, f"{module.title}: {detail}", antes=antes, depois=serializable_dict(obj))
        messages.success(request, "Item desativado no check-list. Ele pode ser restaurado depois.")
        return redirect(module_anchor_url(key, "registros-salvos"))
    obj.delete()
    log_action(request, "registro_excluido", module.key, detalhe=f"{module.title}: {detail}", antes=antes)
    messages.success(request, "Registro excluido.")
    return redirect("module_list", key=key)


@login_required
def usuarios(request):
    if not can_manage_access(request.user):
        messages.error(request, "Você não tem acesso à gestão de acesso.")
        return redirect("dashboard")

    if request.method == "POST":
        action = request.POST.get("action")
        if action == "create":
            form = UsuarioForm(request.POST)
            if form.is_valid():
                first_name = form.cleaned_data["nome"].strip()
                last_name = form.cleaned_data["sobrenome"].strip()
                permissoes = [perm for perm in form.cleaned_data["permissoes"] if perm in active_permission_keys()]
                copiar_de = request.POST.get("copiar_permissoes_de")
                if copiar_de:
                    origem = User.objects.filter(pk=copiar_de).select_related("perfil_krill").first()
                    if origem and hasattr(origem, "perfil_krill"):
                        permissoes = [perm for perm in origem.perfil_krill.permissoes if perm in active_permission_keys()]
                user = User.objects.create_user(
                    username=form.cleaned_data["login"].strip(),
                    password=form.cleaned_data["senha"],
                    first_name=first_name,
                    last_name=last_name,
                    is_staff=form.cleaned_data["cargo"] in {"master", "gerente"},
                    is_superuser=form.cleaned_data["cargo"] == "master",
                )
                PerfilAcesso.objects.update_or_create(
                    user=user,
                    defaults={
                        "cargo": form.cleaned_data["cargo"],
                        "cd_padrao": form.cleaned_data["cd_padrao"],
                        "permissoes": permissoes,
                    },
                )
                log_action(request, "usuario_criado", "gestao_acesso", detalhe=user.username)
                messages.success(request, "Usuário criado.")
                return redirect("usuarios")
        elif action == "update":
            user = get_object_or_404(User, pk=request.POST.get("user_id"))
            profile = ensure_profile(user)
            novo_ativo = request.POST.get("is_active") == "on"
            novo_cargo = request.POST.get("cargo", profile.cargo)
            if user.pk == request.user.pk and not novo_ativo:
                messages.error(request, "Você não pode pausar o próprio login.")
                return redirect("usuarios")
            if would_block_last_active_master(user, novo_ativo):
                messages.error(request, "Não é possível pausar o último usuário master ativo.")
                return redirect("usuarios")
            if profile.cargo == "master" and novo_cargo != "master" and would_block_last_active_master(user, False):
                messages.error(request, "Não é possível remover o cargo master do último master ativo.")
                return redirect("usuarios")
            user.first_name = request.POST.get("first_name", "").strip()
            user.last_name = request.POST.get("last_name", "").strip()
            profile.cargo = novo_cargo
            cd_padrao = request.POST.get("cd_padrao", profile.cd_padrao)
            profile.cd_padrao = cd_padrao if cd_padrao in {"806", "801", UNIFIED_CD} else normalized_profile_cd(profile)
            copiar_de = request.POST.get("copiar_permissoes_de")
            if copiar_de:
                origem = User.objects.filter(pk=copiar_de).select_related("perfil_krill").first()
                if origem and hasattr(origem, "perfil_krill"):
                    profile.permissoes = [perm for perm in origem.perfil_krill.permissoes if perm in active_permission_keys()]
            else:
                profile.permissoes = [perm for perm in request.POST.getlist("permissoes") if perm in active_permission_keys()]
            profile.save()
            was_active = user.is_active
            user.is_active = novo_ativo
            user.is_staff = profile.cargo in {"master", "gerente"}
            user.is_superuser = profile.cargo == "master"
            user.save()
            if was_active and not user.is_active:
                terminate_user_sessions(user)
            log_action(request, "usuario_atualizado", "gestao_acesso", detalhe=f"{user.username} - dados e acessos")
            messages.success(request, "Usuário atualizado.")
            return redirect("usuarios")
        elif action in {"pause_login", "resume_login"}:
            user = get_object_or_404(User, pk=request.POST.get("user_id"))
            novo_ativo = action == "resume_login"
            if user.pk == request.user.pk and not novo_ativo:
                messages.error(request, "Você não pode pausar o próprio login.")
                return redirect("usuarios")
            if would_block_last_active_master(user, novo_ativo):
                messages.error(request, "Não é possível pausar o último usuário master ativo.")
                return redirect("usuarios")
            user.is_active = novo_ativo
            user.save(update_fields=["is_active"])
            if novo_ativo:
                log_action(request, "login_reativado", "gestao_acesso", detalhe=user.username)
                messages.success(request, f"Login {user.username} reativado.")
            else:
                terminate_user_sessions(user)
                log_action(request, "login_pausado", "gestao_acesso", detalhe=user.username)
                messages.success(request, f"Login {user.username} pausado e sessões abertas encerradas.")
            return redirect("usuarios")
        elif action == "reset_password":
            user = get_object_or_404(User, pk=request.POST.get("user_id"))
            senha = request.POST.get("senha", "").strip()
            gerada = request.POST.get("gerar_temporaria") == "1"
            if gerada and not senha:
                senha = generate_temp_password()
            if len(senha) >= 6:
                user.set_password(senha)
                user.save()
                log_action(
                    request,
                    "senha_redefinida_pelo_master",
                    "gestao_acesso",
                    detalhe=f"{user.username} - {'temporaria' if gerada else 'manual'}",
                )
                if gerada:
                    messages.success(request, f"Senha temporária de {user.username}: {senha}. Ela aparece somente agora.")
                else:
                    messages.success(request, "Senha redefinida.")
            else:
                messages.error(request, "A senha precisa ter pelo menos 6 caracteres.")
            return redirect("usuarios")
        elif action == "reset_password_request":
            solicitacao = get_object_or_404(SolicitacaoSenha, pk=request.POST.get("request_id"), status__in=["pendente", "em_andamento"])
            senha = request.POST.get("senha", "").strip()
            gerada = request.POST.get("gerar_temporaria") == "1"
            if gerada and not senha:
                senha = generate_temp_password()
            if not solicitacao.usuario:
                messages.error(request, "Este pedido não encontrou um usuário cadastrado. Confira o login informado.")
                return redirect("usuarios")
            if len(senha) >= 6:
                solicitacao.usuario.set_password(senha)
                solicitacao.usuario.save()
                solicitacao.status = "atendida"
                solicitacao.atendido_por = request.user
                solicitacao.atendido_em = timezone.now()
                solicitacao.observacao = "Senha redefinida pelo master."
                solicitacao.save(update_fields=["status", "atendido_por", "atendido_em", "observacao"])
                log_action(
                    request,
                    "senha_redefinida_pelo_master",
                    "gestao_acesso",
                    detalhe=f"{solicitacao.usuario.username} - solicitacao de senha - {'temporaria' if gerada else 'manual'}",
                )
                if gerada:
                    messages.success(request, f"Senha temporária de {solicitacao.usuario.username}: {senha}. Ela aparece somente agora.")
                else:
                    messages.success(request, "Senha redefinida e solicitação atendida.")
            else:
                messages.error(request, "A senha precisa ter pelo menos 6 caracteres.")
            return redirect("usuarios")
        elif action == "cancel_password_request":
            solicitacao = get_object_or_404(SolicitacaoSenha, pk=request.POST.get("request_id"), status__in=["pendente", "em_andamento"])
            solicitacao.status = "cancelada"
            solicitacao.atendido_por = request.user
            solicitacao.atendido_em = timezone.now()
            solicitacao.observacao = request.POST.get("observacao", "").strip() or "Cancelada pelo master."
            solicitacao.save(update_fields=["status", "atendido_por", "atendido_em", "observacao"])
            log_action(
                request,
                "solicitacao_senha_cancelada",
                "gestao_acesso",
                detalhe=solicitacao.login_informado,
            )
            messages.success(request, "Solicitação cancelada.")
            return redirect("usuarios")
        elif action == "progress_password_request":
            solicitacao = get_object_or_404(SolicitacaoSenha, pk=request.POST.get("request_id"), status__in=["pendente", "em_andamento"])
            solicitacao.status = "em_andamento"
            solicitacao.atendido_por = request.user
            solicitacao.atendido_em = timezone.now()
            solicitacao.observacao = request.POST.get("observacao", "").strip() or "Pedido em andamento."
            solicitacao.save(update_fields=["status", "atendido_por", "atendido_em", "observacao"])
            log_action(request, "solicitacao_senha_em_andamento", "gestao_acesso", detalhe=solicitacao.login_informado)
            messages.success(request, "Pedido marcado como em andamento.")
            return redirect("usuarios")
        elif action == "answer_password_request":
            solicitacao = get_object_or_404(SolicitacaoSenha, pk=request.POST.get("request_id"), status__in=["pendente", "em_andamento"])
            resposta = request.POST.get("observacao", "").strip()
            if not resposta:
                messages.error(request, "Digite uma resposta para registrar no pedido.")
                return redirect("usuarios")
            solicitacao.status = "respondida"
            solicitacao.atendido_por = request.user
            solicitacao.atendido_em = timezone.now()
            solicitacao.observacao = resposta
            solicitacao.save(update_fields=["status", "atendido_por", "atendido_em", "observacao"])
            log_action(request, "solicitacao_senha_respondida", "gestao_acesso", detalhe=solicitacao.login_informado)
            messages.success(request, "Resposta registrada no pedido.")
            return redirect("usuarios")
        elif action in {"progress_access_request", "answer_access_request", "cancel_access_request", "finish_access_request"}:
            solicitacao = get_object_or_404(SolicitacaoAcesso, pk=request.POST.get("request_id"), status__in=["pendente", "em_andamento"])
            resposta = request.POST.get("resposta", "").strip()
            if action == "progress_access_request":
                solicitacao.status = "em_andamento"
                solicitacao.resposta = resposta or "Pedido em andamento."
            elif action == "answer_access_request":
                if not resposta:
                    messages.error(request, "Digite uma resposta para registrar no pedido de acesso.")
                    return redirect("usuarios")
                solicitacao.status = "respondida"
                solicitacao.resposta = resposta
            elif action == "finish_access_request":
                solicitacao.status = "atendida"
                solicitacao.resposta = resposta or "Pedido atendido."
            else:
                solicitacao.status = "cancelada"
                solicitacao.resposta = resposta or "Pedido cancelado pela gestão de acesso."
            solicitacao.atendido_por = request.user
            solicitacao.atendido_em = timezone.now()
            solicitacao.save(update_fields=["status", "resposta", "atendido_por", "atendido_em"])
            log_action(request, f"solicitacao_acesso_{solicitacao.status}", "gestao_acesso", detalhe=solicitacao.nome)
            messages.success(request, "Pedido de acesso atualizado.")
            return redirect("usuarios")
    else:
        initial = {"permissoes": ["painel"]}
        from_access = request.GET.get("from_access")
        if from_access:
            pedido = SolicitacaoAcesso.objects.filter(pk=from_access).first()
            if pedido:
                partes_nome = pedido.nome.split(" ", 1)
                initial.update(
                    {
                        "nome": partes_nome[0],
                        "sobrenome": partes_nome[1] if len(partes_nome) > 1 else "",
                        "login": pedido.login_desejado,
                        "cargo": "motorista" if pedido.cargo.lower() == "motorista" else "assistente",
                        "cd_padrao": pedido.cd_unidade or current_cd(request),
                    }
                )
        form = UsuarioForm(initial=initial)

    users = User.objects.select_related("perfil_krill").order_by("username")
    usuarios_resumo = [user_access_summary(user) for user in users]
    usuarios_alertas = sum(1 for item in usuarios_resumo if item["warnings"])
    usuarios_ativos = sum(1 for item in usuarios_resumo if item["user"].is_active)
    solicitacoes_senha = SolicitacaoSenha.objects.select_related("usuario", "atendido_por").filter(status__in=["pendente", "em_andamento"])[:50]
    solicitacoes_acesso = SolicitacaoAcesso.objects.select_related("usuario", "atendido_por").filter(status__in=["pendente", "em_andamento"])[:50]
    solicitacoes_pendentes_total = len(solicitacoes_senha) + len(solicitacoes_acesso)
    historico_senhas = SolicitacaoSenha.objects.select_related("usuario", "atendido_por").exclude(status="pendente")[:20]
    historico_acessos = SolicitacaoAcesso.objects.select_related("usuario", "atendido_por").exclude(status="pendente")[:20]
    historico_seguranca = AuditLog.objects.select_related("user").filter(acao__in=SECURITY_AUDIT_ACTIONS)[:40]
    ctx = context_base(request)
    ctx.update(
        {
            "form": form,
            "users": users,
            "usuarios_resumo": usuarios_resumo,
            "usuarios_alertas": usuarios_alertas,
            "usuarios_ativos": usuarios_ativos,
            "solicitacoes_pendentes_total": solicitacoes_pendentes_total,
            "permissions": active_permissions(),
            "permission_groups": grouped_permissions(),
            "permission_presets": permission_presets(),
            "create_permission_values": form["permissoes"].value() or [],
            "solicitacoes_senha": solicitacoes_senha,
            "solicitacoes_acesso": solicitacoes_acesso,
            "historico_senhas": historico_senhas,
            "historico_acessos": historico_acessos,
            "historico_seguranca": historico_seguranca,
        }
    )
    return render(request, "painel/usuarios.html", ctx)


@login_required
def preferencias(request):
    if not user_has_perm(request.user, "preferencias"):
        messages.error(request, "Você não tem acesso à tela Minha tela.")
        return redirect("dashboard")
    modules = [customize_module(module) for module in MODULES if can_access_module(request.user, module)]
    profile = ensure_profile(request.user)
    if request.method == "POST":
        form = PreferenciasForm(modules, request.POST)
        if form.is_valid():
            profile.abas_ocultas = form.cleaned_data["abas_ocultas"]
            profile.save(update_fields=["abas_ocultas", "atualizado_em"])
            messages.success(request, "Sua tela foi atualizada.")
            return redirect("dashboard")
    else:
        form = PreferenciasForm(modules, initial={"abas_ocultas": profile.abas_ocultas})
    ctx = context_base(request)
    ctx.update({"form": form})
    return render(request, "painel/preferencias.html", ctx)


@login_required
def apresentacao_sistema(request):
    if not can_manage_system_features(request.user):
        messages.error(request, "Esta apresentação é restrita ao usuário master.")
        return redirect("dashboard")
    ctx = context_base(request)
    return render(request, "painel/apresentacao_sistema.html", ctx)


@login_required
def treinamento_sistema(request):
    if not can_manage_system_features(request.user):
        messages.error(request, "O treinamento rápido é restrito ao usuário master.")
        return redirect("dashboard")
    ctx = context_base(request)
    return render(request, "painel/treinamento_sistema.html", ctx)


@login_required
def recursos_sistema(request):
    if not can_manage_system_features(request.user):
        messages.error(request, "Você não tem acesso para alterar recursos do sistema.")
        return redirect("dashboard")

    if request.method == "POST":
        rule_key = request.POST.get("rule")
        if rule_key:
            enabled = request.POST.get("enabled") == "on"
            rule = SYSTEM_RULES.get(rule_key)
            if not rule:
                messages.error(request, "Regra não encontrada.")
                return redirect("recursos_sistema")
            set_config(rule["config"], "sim" if enabled else "nao", request.user, rule["title"])
            log_action(request, "regra_sistema_alterada", "recursos_sistema", detalhe=f"{rule['title']}: {'ligada' if enabled else 'desligada'}")
            messages.success(request, f"{rule['title']} {'habilitada' if enabled else 'desabilitada'}.")
            return redirect("recursos_sistema")
        feature_key = request.POST.get("feature")
        enabled = request.POST.get("enabled") == "on"
        feature = OPTIONAL_FEATURES.get(feature_key)
        if not feature:
            messages.error(request, "Recurso não encontrado.")
            return redirect("recursos_sistema")
        set_config(feature["config"], "sim" if enabled else "nao", request.user, f"Recurso {feature['title']}")
        clear_runtime_permission_cache()
        if not enabled:
            disabled = feature["permissions"]
            for profile in PerfilAcesso.objects.all():
                current = list(profile.permissoes or [])
                cleaned = [perm for perm in current if perm not in disabled]
                if cleaned != current:
                    profile.permissoes = cleaned
                    profile.save(update_fields=["permissoes", "atualizado_em"])
        if hasattr(request.user, "_krill_profile_cache"):
            delattr(request.user, "_krill_profile_cache")
        if hasattr(request.user, "_krill_permission_set_cache"):
            delattr(request.user, "_krill_permission_set_cache")
        log_action(request, "recurso_sistema_alterado", "recursos_sistema", detalhe=f"{feature['title']}: {'ligado' if enabled else 'desligado'}")
        messages.success(request, f"{feature['title']} {'habilitado' if enabled else 'desabilitado'}.")
        return redirect("recursos_sistema")

    features = []
    for key, feature in OPTIONAL_FEATURES.items():
        features.append(
            {
                "key": key,
                "title": feature["title"],
                "description": feature["description"],
                "enabled": feature_enabled(key),
                "permissions": [(perm, dict(ALL_PERMISSIONS).get(perm, perm)) for perm in feature["permissions"]],
            }
        )
    system_rules = []
    for key, rule in SYSTEM_RULES.items():
        system_rules.append(
            {
                "key": key,
                "title": rule["title"],
                "description": rule["description"],
                "enabled": system_rule_enabled(key),
            }
        )
    ctx = context_base(request)
    ctx.update(
        {
            "features": features,
            "system_rules": system_rules,
            "webpush_enabled": webpush_is_enabled(),
            "webpush_public_key": webpush_public_key(),
            "webpush_missing": not webpush_is_enabled(),
        }
    )
    return render(request, "painel/recursos_sistema.html", ctx)


@login_required
def personalizar_interface(request):
    if not user_has_perm(request.user, "personalizar_interface"):
        messages.error(request, "Você não tem acesso para personalizar nomes de abas e painéis.")
        return redirect("dashboard")

    if request.method == "POST":
        action = request.POST.get("action", "save")
        if action == "reset":
            set_config(INTERFACE_LABELS_CONFIG, "{}", request.user, "Rótulos personalizados da interface")
            clear_interface_labels_cache()
            log_action(request, "interface_rotulos_reset", "personalizar_interface", detalhe="Rótulos restaurados para o padrão")
            messages.success(request, "Nomes restaurados para o padrão.")
            return redirect("personalizar_interface")

        module_labels = {}
        module_icons = {}
        for module in MODULES:
            label = request.POST.get(f"module_{module.key}", "").strip()
            if label and label != module.title:
                module_labels[module.key] = label
            icon = clean_interface_icon(request.POST.get(f"icon_{module.key}"), module.icon)
            if icon != module.icon:
                module_icons[module.key] = icon

        panel_labels = {}
        panel_icons = {}
        for key, default in DEFAULT_PANEL_LABELS.items():
            label = request.POST.get(f"panel_{key}", "").strip()
            if label and label != default:
                panel_labels[key] = label
            default_icon = DEFAULT_PANEL_ICONS.get(key, key[:2].upper())
            icon = clean_interface_icon(request.POST.get(f"panel_icon_{key}"), default_icon)
            if icon != default_icon:
                panel_icons[key] = icon

        menu_labels = {}
        menu_keys = request.POST.getlist("menu_key")
        menu_values = request.POST.getlist("menu_label")
        for key, label in zip(menu_keys, menu_values):
            key = key.strip()
            label = label.strip()
            if key and label and label != key:
                menu_labels[key] = label
        for label in request.POST.getlist("new_menu_label"):
            label = label.strip()
            key = interface_menu_key(label)
            if key and label:
                menu_labels[key] = label

        valid_menu_keys = {item.strip() for item in request.POST.getlist("menu_key") if item.strip()}
        valid_menu_keys.update(menu_labels.keys())
        module_menus = {}
        for module in MODULES:
            selected_menu = request.POST.get(f"module_menu_{module.key}", "").strip()
            default_menu = MODULE_NAV_GROUPS.get(module.key, module.menu)
            if selected_menu in valid_menu_keys and selected_menu != default_menu:
                module_menus[module.key] = selected_menu

        available_cards = available_card_links(request.user)
        available_card_map = {card["id"]: card for card in available_cards}
        valid_card_ids = set(available_card_map)
        hubs = []
        for slot in request.POST.getlist("hub_slot"):
            if request.POST.get(f"hub_delete_{slot}") == "on":
                continue
            title = request.POST.get(f"hub_title_{slot}", "").strip()
            if not title:
                continue
            key = request.POST.get(f"hub_key_{slot}", "").strip() or interface_hub_key(title)
            if not key:
                continue
            cards = [item for item in request.POST.getlist(f"hub_cards_{slot}") if item in valid_card_ids]
            if not cards:
                continue
            selected_card_rows = [available_card_map[item] for item in cards]
            selected_menu = request.POST.get(f"hub_menu_{slot}", "").strip() or "Principal"
            if selected_menu not in valid_menu_keys:
                selected_menu = "Principal"
            description = request.POST.get(f"hub_description_{slot}", "").strip() or default_hub_description(title, selected_card_rows)
            hubs.append(
                {
                    "key": key,
                    "title": title,
                    "icon": clean_interface_icon(request.POST.get(f"hub_icon_{slot}"), "CT"),
                    "menu": selected_menu,
                    "description": description,
                    "hide_menu_cards": request.POST.get(f"hub_hide_menu_cards_{slot}") == "on",
                    "cards": cards,
                }
            )

        payload = {
            "modules": module_labels,
            "panels": panel_labels,
            "menus": menu_labels,
            "module_menus": module_menus,
            "icons": module_icons,
            "panel_icons": panel_icons,
            "hubs": hubs,
        }
        set_config(
            INTERFACE_LABELS_CONFIG,
            json.dumps(payload, ensure_ascii=False),
            request.user,
            "Rótulos personalizados da interface",
        )
        clear_interface_labels_cache()
        log_action(request, "interface_rotulos_salvos", "personalizar_interface", detalhe=f"{len(module_labels)} abas, {len(panel_labels)} painéis, {len(hubs)} centrais")
        messages.success(request, "Nomes de abas e painéis atualizados.")
        return redirect("personalizar_interface")

    labels = interface_labels()
    menus = []
    seen = set()
    for key, default in DEFAULT_NAV_GROUPS.items():
        menus.append({"key": key, "label": labels["menus"].get(key, default)})
        seen.add(key)
    for module in MODULES:
        if module.menu not in seen:
            seen.add(module.menu)
            menus.append({"key": module.menu, "label": labels["menus"].get(module.menu, module.menu)})
    assigned_custom_menus = set(labels["module_menus"].values())
    for key, label in labels["menus"].items():
        if key not in seen and (key.startswith("custom_") or key in assigned_custom_menus):
            seen.add(key)
            menus.append({"key": key, "label": label})

    central_options = available_card_links(request.user)
    saved_hubs = []
    for idx, hub in enumerate(labels["hubs"]):
        if not isinstance(hub, dict):
            continue
        saved_hubs.append(
            {
                "slot": str(idx),
                "key": hub.get("key") or interface_hub_key(hub.get("title")),
                "title": hub.get("title", ""),
                "icon": clean_interface_icon(hub.get("icon"), "CT"),
                "menu": hub.get("menu") or "Principal",
                "description": hub.get("description", ""),
                "hide_menu_cards": bool(hub.get("hide_menu_cards")),
                "cards": set(hub.get("cards", [])),
            }
        )
    saved_hubs.append(
        {
            "slot": "new",
            "key": "",
            "title": "",
            "icon": "CT",
            "menu": "Principal",
        "description": "",
        "hide_menu_cards": True,
        "cards": set(),
        "is_new": True,
        }
    )

    ctx = context_base(request)
    ctx.update(
        {
            "modules_config": [
                {
                    "key": module.key,
                    "title": module.title,
                    "menu": MODULE_NAV_GROUPS.get(module.key, module.menu),
                    "label": labels["modules"].get(module.key, module.title),
                    "icon": clean_interface_icon(labels["icons"].get(module.key), module.icon),
                    "selected_menu": labels["module_menus"].get(module.key, MODULE_NAV_GROUPS.get(module.key, module.menu)),
                }
                for module in MODULES
            ],
            "panel_config": [
                {
                    "key": key,
                    "title": title,
                    "label": labels["panels"].get(key, title),
                    "icon": clean_interface_icon(labels["panel_icons"].get(key), DEFAULT_PANEL_ICONS.get(key, key[:2].upper())),
                }
                for key, title in DEFAULT_PANEL_LABELS.items()
            ],
            "menu_config": menus,
            "central_options": central_options,
            "hub_config": saved_hubs,
        }
    )
    return render(request, "painel/personalizar_interface.html", ctx)


@login_required
def perfil_usuario(request):
    user = request.user
    if request.method == "POST":
        form = PerfilUsuarioForm(request.POST)
        if form.is_valid():
            nome_antes = {"nome": user.first_name, "sobrenome": user.last_name}
            user.first_name = form.cleaned_data["nome"].strip()
            user.last_name = form.cleaned_data["sobrenome"].strip()
            nome_depois = {"nome": user.first_name, "sobrenome": user.last_name}
            troca_senha = bool(form.cleaned_data.get("nova_senha"))
            if troca_senha:
                if not user.check_password(form.cleaned_data["senha_atual"]):
                    form.add_error("senha_atual", "Senha atual incorreta.")
                else:
                    user.set_password(form.cleaned_data["nova_senha"])
            if not form.errors:
                user.save()
                if nome_antes != nome_depois:
                    log_action(
                        request,
                        "nome_perfil_alterado",
                        "perfil",
                        detalhe=user.username,
                        antes=nome_antes,
                        depois=nome_depois,
                    )
                if troca_senha:
                    log_action(request, "senha_alterada_pelo_usuario", "perfil", detalhe=user.username)
                if not troca_senha and nome_antes == nome_depois:
                    log_action(request, "perfil_atualizado", "perfil", detalhe="Usuário atualizou o próprio perfil")
                messages.success(request, "Perfil atualizado. Entre novamente se você alterou a senha.")
                if troca_senha:
                    logout(request)
                    return redirect("login")
                return redirect("perfil_usuario")
    else:
        form = PerfilUsuarioForm(initial={"nome": user.first_name, "sobrenome": user.last_name})
    ctx = context_base(request)
    ctx.update({"form": form})
    return render(request, "painel/perfil_usuario.html", ctx)


@login_required
def minhas_solicitacoes(request):
    if request.method == "POST" and request.POST.get("action") == "mark_read":
        if request.POST.get("tipo") == "melhoria":
            MelhoriaSistema.objects.filter(criado_por=request.user, pk=request.POST.get("request_id")).update(lido_pelo_solicitante_em=timezone.now())
        elif request.POST.get("tipo") == "carregamento":
            SolicitacaoCarregamentoManual.objects.filter(criado_por=request.user, pk=request.POST.get("request_id")).update(lido_pelo_solicitante_em=timezone.now())
        elif request.POST.get("tipo") == "acesso":
            SolicitacaoAcesso.objects.filter(usuario=request.user, pk=request.POST.get("request_id")).update(lido_pelo_usuario_em=timezone.now())
        else:
            SolicitacaoSenha.objects.filter(usuario=request.user, pk=request.POST.get("request_id")).update(lido_pelo_usuario_em=timezone.now())
        messages.success(request, "Solicitação marcada como lida.")
        return redirect("minhas_solicitacoes")
    solicitacoes_senha = [
        {"tipo": "senha", "obj": item, "data": item.criado_em}
        for item in SolicitacaoSenha.objects.filter(usuario=request.user).order_by("-criado_em")[:50]
    ]
    solicitacoes_acesso = [
        {"tipo": "acesso", "obj": item, "data": item.criado_em}
        for item in SolicitacaoAcesso.objects.filter(usuario=request.user).order_by("-criado_em")[:50]
    ]
    solicitacoes_melhoria = [
        {"tipo": "melhoria", "obj": item, "data": item.criado_em}
        for item in MelhoriaSistema.objects.filter(criado_por=request.user).order_by("-criado_em")[:50]
    ]
    solicitacoes_carregamento = [
        {"tipo": "carregamento", "obj": item, "data": item.criado_em}
        for item in SolicitacaoCarregamentoManual.objects.filter(criado_por=request.user).order_by("-criado_em")[:50]
    ]
    solicitacoes = sorted(solicitacoes_senha + solicitacoes_acesso + solicitacoes_melhoria + solicitacoes_carregamento, key=lambda item: item["data"], reverse=True)[:50]
    ctx = context_base(request)
    ctx.update({"solicitacoes": solicitacoes})
    return render(request, "painel/minhas_solicitacoes.html", ctx)


@login_required
def auditoria(request):
    if not (user_has_perm(request.user, "painel_master") or user_has_perm(request.user, "auditoria_acessos")):
        messages.error(request, "Você não tem acesso à auditoria de acessos.")
        return redirect("dashboard")
    logs = AuditLog.objects.all()
    cd = request.GET.get("cd", "")
    usuario = request.GET.get("usuario", "")
    busca = request.GET.get("q", "").strip()
    if cd in {"801", "806"}:
        logs = logs.filter(cd_unidade=cd)
    if usuario:
        logs = logs.filter(Q(usuario_nome__icontains=usuario) | Q(user__username__icontains=usuario))
    if busca:
        logs = logs.filter(
            Q(usuario_nome__icontains=busca)
            | Q(user__username__icontains=busca)
            | Q(acao__icontains=busca)
            | Q(modulo__icontains=busca)
            | Q(detalhe__icontains=busca)
            | Q(computador__icontains=busca)
            | Q(ip__icontains=busca)
        )
    ctx = context_base(request)
    ctx.update(
        {
            "logs": logs[:300],
            "cd_filtro": cd,
            "usuario_filtro": usuario,
            "busca_filtro": busca,
            "usuarios_ativos": active_user_sessions(),
            "ultimos_acessos": recent_access_logs(),
        }
    )
    return render(request, "painel/auditoria.html", ctx)


REPORT_TYPES = [
    ("geral", "Tudo junto"),
    ("gestao_360", "Gestão 360"),
    ("abertura", "Gerencial - Abertura"),
    ("recebimento", "Recebimento"),
    ("ressuprimento_demanda", "Ressuprimento - Demanda"),
    ("ressuprimento_ocupacao", "Ressuprimento - Ocupacao"),
    ("ressuprimento_producao", "Ressuprimento - Produção"),
    ("separacao", "Separação"),
    ("expedicao", "Expedição"),
    ("qualidade", "Qualidade e apoio"),
]


def choice_label(model, field_name, value):
    field = model._meta.get_field(field_name)
    return dict(field.choices).get(value, value)


def build_report_data(cd, inicio, fim):
    fim_obj = parse_iso_date(fim)
    recebimentos_qs = Recebimento.objects.filter(cd_unidade=cd, data__range=[inicio, fim])
    agenda_qs = RecebimentoAgenda.objects.filter(cd_unidade=cd, data__range=[inicio, fim])
    separacao_qs = Separacao.objects.filter(cd_unidade=cd, data__range=[inicio, fim])
    expedicao_qs = Expedicao.objects.filter(cd_unidade=cd, data__range=[inicio, fim])
    prestadores_qs = Prestador.objects.filter(cd_unidade=cd, data__range=[inicio, fim])
    conferencias_qs = Conferencia.objects.filter(cd_unidade=cd, data__range=[inicio, fim])
    avarias_qs = Avaria.objects.filter(cd_unidade=cd, data__range=[inicio, fim])
    chamados_qs = ChamadoSaldo.objects.filter(cd_unidade=cd, data__range=[inicio, fim])
    pessoas_qs = PessoaTurno.objects.filter(cd_unidade=cd, data__range=[inicio, fim])
    equipamentos_qs = Equipamento.objects.filter(cd_unidade=cd)
    manutencoes_qs = EquipamentoManutencao.objects.filter(cd_unidade=cd, data__range=[inicio, fim])
    ocorrencias_qs = OcorrenciaOperacional.objects.filter(cd_unidade=cd, data__range=[inicio, fim])
    aprovacoes_qs = AprovacaoOperacional.objects.filter(cd_unidade=cd, data__range=[inicio, fim])
    fechamentos_qs = FechamentoDia.objects.filter(cd_unidade=cd, data__range=[inicio, fim])
    melhorias_qs = MelhoriaSistema.objects.filter(cd_unidade=cd)
    materiais_pedido_qs = MaterialConsumo.objects.filter(cd_unidade=cd, precisa_pedir=True)
    pendencias_qs = Pendencia.objects.filter(cd_unidade=cd).exclude(status="concluido")
    gestao_360 = build_gestao_360(cd, fim_obj)

    resumo = [
        ("Score operacional", f"{gestao_360['score']['score']}% - {gestao_360['score']['nivel']}"),
        ("Qualidade dos dados", f"{gestao_360['qualidade']['score']}%"),
        ("Veículos agendados", agenda_qs.filter(agendado=True).count()),
        ("Veículos não agendados", agenda_qs.filter(agendado=False).count()),
        ("NF recebidas", recebimentos_qs.count()),
        ("Paletes recebidos", recebimentos_qs.aggregate(total=Sum("paletes"))["total"] or 0),
        ("Valor recebido", recebimentos_qs.aggregate(total=Sum("valor"))["total"] or 0),
        ("Prestadores no CD", prestadores_qs.count()),
        ("Pessoas planejadas", pessoas_qs.aggregate(total=Sum("planejado"))["total"] or 0),
        ("Pessoas ativas", pessoas_qs.aggregate(total=Sum("ativos_dia"))["total"] or 0),
        ("Unidades separadas", separacao_qs.aggregate(total=Sum("unidades"))["total"] or 0),
        ("Paletes expedidos", expedicao_qs.aggregate(total=Sum("qtd_paletes"))["total"] or 0),
        ("Divergências", conferencias_qs.exclude(diferenca=0).count()),
        ("Avarias abertas no período", avarias_qs.exclude(status="concluido").count()),
        ("Chamados saldo", chamados_qs.count()),
        ("Materiais para pedido", materiais_pedido_qs.count()),
        ("Equipamentos parados", equipamentos_qs.filter(status__in=["manutencao", "perdido"]).count()),
        ("Manutencoes de equipamento", manutencoes_qs.count()),
        ("Ocorrências críticas/altas", ocorrencias_qs.filter(severidade__in=["critica", "alta"]).exclude(status__in=["resolvido", "cancelado"]).count()),
        ("Aprovacoes pendentes", aprovacoes_qs.filter(status="pendente").count()),
        ("Fechamentos gerados", fechamentos_qs.count()),
    ]

    recebimento_empresas = list(
        recebimentos_qs.values("fornecedor")
        .annotate(paletes_total=Sum("paletes"), valor_total=Sum("valor"))
        .order_by("-paletes_total")[:20]
    )
    recebimento_pagamentos = list(
        recebimentos_qs.values("forma_pagamento")
        .annotate(valor_total=Sum("valor"), paletes_total=Sum("paletes"))
        .order_by("-valor_total")
    )
    for item in recebimento_pagamentos:
        item["forma_pagamento_label"] = choice_label(Recebimento, "forma_pagamento", item["forma_pagamento"])

    separacao_categorias = list(
        separacao_qs.values("categoria")
        .annotate(unidades_total=Sum("unidades"), paletes_total=Sum("paletes"))
        .order_by("-unidades_total")
    )
    for item in separacao_categorias:
        item["categoria_label"] = separacao_categoria_label(item["categoria"])

    separacao_lojas = list(
        separacao_qs.values("loja")
        .annotate(unidades_total=Sum("unidades"), paletes_total=Sum("paletes"))
        .order_by("-unidades_total")[:25]
    )

    expedicao_lojas = list(
        expedicao_qs.values("loja")
        .annotate(paletes_total=Sum("qtd_paletes"))
        .order_by("-paletes_total")[:25]
    )
    max_expedicao = max([item["paletes_total"] or 0 for item in expedicao_lojas] or [1])
    for item in expedicao_lojas:
        item["percentual"] = int(((item["paletes_total"] or 0) / max_expedicao) * 100) if max_expedicao else 0

    expedicao_periodos = list(
        expedicao_qs.values("periodo")
        .annotate(paletes_total=Sum("qtd_paletes"))
        .order_by("periodo")
    )
    for item in expedicao_periodos:
        item["periodo_label"] = choice_label(Expedicao, "periodo", item["periodo"])

    avarias_tipos = list(
        avarias_qs.values("tipo")
        .annotate(caixas_total=Sum("caixas"), unidades_total=Sum("unidades"), valor_total=Sum("valor_estimado"))
        .order_by("-valor_total")
    )
    for item in avarias_tipos:
        item["tipo_label"] = choice_label(Avaria, "tipo", item["tipo"])

    chamados_status = list(
        chamados_qs.values("status")
        .annotate(saldo_fisico_total=Sum("saldo_fisico"), saldo_sistema_total=Sum("saldo_sistema"))
        .order_by("status")
    )
    for item in chamados_status:
        item["status_label"] = choice_label(ChamadoSaldo, "status", item["status"])

    pessoas_setores = list(
        pessoas_qs.values("setor")
        .annotate(
            planejado_total=Sum("planejado"),
            ativos_total=Sum("ativos_dia"),
            atestados_total=Sum("atestados"),
            afastados_total=Sum("afastados"),
            ferias_total=Sum("ferias"),
            folgas_total=Sum("folgas"),
            faltas_sem_justificativa_total=Sum("faltas_sem_justificativa"),
        )
        .order_by("setor")
    )
    for item in pessoas_setores:
        item["setor_label"] = choice_label(PessoaTurno, "setor", item["setor"])
        item["cobertura"] = pct(item["ativos_total"] or 0, item["planejado_total"] or 0)

    equipamentos_status = choice_rows(
        Equipamento,
        "status",
        list(equipamentos_qs.values("status").annotate(total=Count("id")).order_by("-total")),
    )
    ocorrencias_tipo = choice_rows(
        OcorrenciaOperacional,
        "tipo",
        list(ocorrencias_qs.values("tipo").annotate(total=Count("id")).order_by("-total")),
    )
    ocorrencias_setor = choice_rows(
        OcorrenciaOperacional,
        "setor",
        list(ocorrencias_qs.values("setor").annotate(total=Count("id")).order_by("-total")),
    )
    aprovacoes_status = choice_rows(
        AprovacaoOperacional,
        "status",
        list(aprovacoes_qs.values("status").annotate(total=Count("id")).order_by("-total")),
    )
    melhorias_status = choice_rows(
        MelhoriaSistema,
        "status",
        list(melhorias_qs.values("status").annotate(total=Count("id")).order_by("-total")),
    )

    return {
        "resumo": resumo,
        "gestao_360": gestao_360,
        "recebimento_empresas": recebimento_empresas,
        "recebimento_pagamentos": recebimento_pagamentos,
        "recebimentos_detalhe": recebimentos_qs.order_by("data", "fornecedor")[:200],
        "agenda_periodo": agenda_qs.order_by("data", "empresa")[:200],
        "agenda_amanha": RecebimentoAgenda.objects.filter(
            cd_unidade=cd,
            data=parse_iso_date(fim) + timezone.timedelta(days=1),
        ).order_by("empresa"),
        "separacao_categorias": separacao_categorias,
        "separacao_lojas": separacao_lojas,
        "separacao_detalhe": separacao_qs.order_by("data", "loja")[:200],
        "expedicao_lojas": expedicao_lojas,
        "expedicao_periodos": expedicao_periodos,
        "expedicao_detalhe": expedicao_qs.order_by("data", "loja")[:200],
        "prestadores": prestadores_qs.order_by("data", "empresa")[:100],
        "conferencias_divergentes": conferencias_qs.exclude(diferenca=0).order_by("data", "documento")[:100],
        "avarias_tipos": avarias_tipos,
        "avarias_abertas": avarias_qs.exclude(status="concluido").order_by("data", "produto")[:100],
        "chamados_abertos": chamados_qs.exclude(status="concluido").order_by("data", "produto")[:100],
        "materiais_pedido": materiais_pedido_qs.order_by("material")[:100],
        "pendencias_abertas": pendencias_qs.order_by("data")[:100],
        "pessoas_setores": pessoas_setores,
        "equipamentos_status": equipamentos_status,
        "manutencoes_equipamentos": manutencoes_qs.order_by("data", "patrimonio")[:100],
        "ocorrencias_tipo": ocorrencias_tipo,
        "ocorrencias_setor": ocorrencias_setor,
        "ocorrencias_relatorio": ocorrencias_qs.order_by("-data", "-severidade")[:150],
        "aprovacoes_status": aprovacoes_status,
        "aprovacoes_relatorio": aprovacoes_qs.order_by("-data")[:100],
        "fechamentos_relatorio": fechamentos_qs.order_by("-data")[:60],
        "melhorias_status": melhorias_status,
        "melhorias_relatorio": melhorias_qs.exclude(status__in=["concluida", "descartada"]).order_by("-prioridade", "previsao")[:100],
    }


def section_enabled(tipo, section):
    return tipo == "geral" or tipo == section or (tipo == "qualidade" and section == "apoio") or (tipo == "gestao_360" and section == "gestao")


def write_section(writer, title, headers, rows):
    writer.writerow([])
    writer.writerow([title])
    writer.writerow(headers)
    for row in rows:
        writer.writerow(row)


def report_export_sections(tipo, data):
    sections = []
    if section_enabled(tipo, "gestao"):
        gestao = data.get("gestao_360", {})
        sections.extend(
            [
                (
                    "Gestão 360 - Score operacional",
                    ["Indicador", "Valor", "Detalhe"],
                    [[card["label"], card["value"], card["detail"]] for card in gestao.get("cards", [])],
                ),
                (
                    "Gestão 360 - Gargalos por setor",
                    ["Setor", "Pendências", "Ocorrências", "Pessoas faltando", "Divergências"],
                    [
                        [row["setor"], row["pendencias"], row["ocorrencias"], row["pessoas_faltando"], row["divergencias"]]
                        for row in gestao.get("setores", [])
                    ],
                ),
                (
                    "Gestão 360 - Qualidade dos dados",
                    ["Area", "Indicador", "Valor", "Acao sugerida"],
                    [[row["area"], row["indicador"], row["valor"], row["acao"]] for row in gestao.get("qualidade", {}).get("checks", [])],
                ),
                (
                    "Gestão 360 - Roteiro Power BI",
                    ["Camada", "Tabelas", "Uso"],
                    [[row["camada"], row["tabelas"], row["uso"]] for row in gestao.get("trilha_bi", [])],
                ),
                (
                    "Gestão 360 - Inovações sugeridas",
                    ["Prioridade", "Tema", "Ganho"],
                    [[row["prioridade"], row["tema"], row["ganho"]] for row in gestao.get("inovacoes", [])],
                ),
            ]
        )
    if tipo in {"geral", "recebimento"}:
        visual = data.get("visual_recebimento", {})
        sections.extend(
            [
                (
                    "Painel Recebimento - Eficiencia na conferencia",
                    ["Conferente", "Paletes", "Minutos", "Palete/min", "Sem agendamento"],
                    [
                        [r["conferente"], r["paletes"], r["minutos"], r["palete_minuto"], r["sem_agendamento"]]
                        for r in visual.get("eficiencia", [])
                    ],
                ),
                (
                    "Painel Recebimento - Paletes por periodo",
                    ["Data", "Paletes"],
                    [[r["data"], r["total"] or 0] for r in visual.get("paletes_periodo", [])],
                ),
            ]
        )
    if tipo in {"geral", "ressuprimento_demanda"}:
        visual = data.get("visual_demanda", {})
        sections.append(
            (
                "Painel Ressuprimento - Demanda por setor",
                ["Setor", "Qtd. volume", "Qtd. itens", "Pendência volume", "Pendência itens"],
                [
                    [r["label"], r["qtd_volume"] or 0, r["qtd_itens"] or 0, r["pendencia_volume"] or 0, r["pendencia_itens"] or 0]
                    for r in visual.get("setores", [])
                ],
            )
        )
    if tipo in {"geral", "ressuprimento_ocupacao"}:
        visual = data.get("visual_ocupacao", {})
        sections.append(
            (
                "Painel Ressuprimento - Ocupacao por rua",
                ["Rua", "Qtd. itens", "Produzido itens", "Pendência itens", "% produzido"],
                [
                    [r["label"], r["qtd_itens"] or 0, r["produzido_itens"] or 0, r["pendencia_itens"] or 0, r["percentual_produzido"]]
                    for r in visual.get("ruas", [])
                ],
            )
        )
    if tipo in {"geral", "ressuprimento_producao"}:
        visual = data.get("visual_producao", {})
        sections.append(
            (
                "Painel Ressuprimento - Produção por setor",
                ["Setor", "Simulação", "Pendência", "Quantidade gerada", "Picking", "Pulmão", "% produzido"],
                [
                    [r["label"], r["qtd_volume"] or 0, r["pendencia_volume"] or 0, r["produzido_volume"] or 0, r["picking"] or 0, r["pulmao"] or 0, r["percentual_produzido"]]
                    for r in visual.get("setores", [])
                ],
            )
        )
    if section_enabled(tipo, "recebimento"):
        sections.extend(
            [
                (
                    "Recebimento por fornecedor",
                    ["Fornecedor", "Paletes", "Valor"],
                    [[r["fornecedor"], r["paletes_total"] or 0, r["valor_total"] or 0] for r in data["recebimento_empresas"]],
                ),
                (
                    "Agenda de recebimento",
                    ["Data", "Empresa", "Tipo", "Paletes", "Agendado"],
                    [[r.data, r.empresa, r.tipo_caminhao, r.paletes, "Sim" if r.agendado else "Não"] for r in data["agenda_periodo"]],
                ),
            ]
        )
    if section_enabled(tipo, "separacao"):
        sections.extend(
            [
                (
                    "Separação por categoria",
                    ["Categoria", "Unidades", "Paletes"],
                    [[r["categoria_label"], r["unidades_total"] or 0, r["paletes_total"] or 0] for r in data["separacao_categorias"]],
                ),
                (
                    "Separação por loja",
                    ["Loja", "Unidades", "Paletes"],
                    [[r["loja"], r["unidades_total"] or 0, r["paletes_total"] or 0] for r in data["separacao_lojas"]],
                ),
            ]
        )
    if section_enabled(tipo, "expedicao"):
        sections.extend(
            [
                (
                    "Expedição por loja",
                    ["Loja", "Paletes"],
                    [[r["loja"], r["paletes_total"] or 0] for r in data["expedicao_lojas"]],
                ),
                (
                    "Expedição por período",
                    ["Periodo", "Paletes"],
                    [[r["periodo_label"], r["paletes_total"] or 0] for r in data["expedicao_periodos"]],
                ),
            ]
        )
    if section_enabled(tipo, "apoio"):
        sections.extend(
            [
                (
                    "Avarias abertas",
                    ["Data", "Produto", "Tipo", "Caixas", "Unidades", "Status"],
                    [[r.data, r.produto, r.get_tipo_display(), r.caixas, r.unidades, r.get_status_display()] for r in data["avarias_abertas"]],
                ),
                (
                    "Chamados GLPI abertos",
            ["Data", "Produto", "GLPI", "Status", "Responsável"],
                    [[r.data, r.produto, r.numero_glpi, r.get_status_display(), r.responsavel] for r in data["chamados_abertos"]],
                ),
                (
                    "Materiais para pedido",
                    ["Material", "Estoque atual", "Estoque minimo", "Unidade"],
                    [[r.material, r.estoque_atual, r.estoque_minimo, r.unidade] for r in data["materiais_pedido"]],
                ),
            ]
        )
    return sections


def xlsx_value(value):
    if isinstance(value, Decimal):
        return float(value)
    return value


def number_value(value):
    if value is None:
        return 0
    if isinstance(value, Decimal):
        return float(value)
    return value


def percent_of(value, total):
    value = number_value(value)
    total = number_value(total)
    if not total:
        return 0
    return min(int(round((value / total) * 100)), 100)


def build_visual_report_data(cd, inicio, fim):
    recebimentos = Recebimento.objects.filter(cd_unidade=cd, data__range=[inicio, fim])
    agenda = RecebimentoAgenda.objects.filter(cd_unidade=cd, data__range=[inicio, fim])
    conferentes = RecebimentoConferente.objects.filter(cd_unidade=cd, data__range=[inicio, fim])
    ressuprimento_painel = RessuprimentoPainel.objects.filter(cd_unidade=cd, data__range=[inicio, fim])
    separacao_prod = list(SeparacaoProdutividade.objects.filter(cd_unidade=cd, data__range=[inicio, fim]))
    separacao_prod += list(
        Separacao.objects.filter(cd_unidade=cd, data__range=[inicio, fim]).exclude(
            total_a_produzir=0,
            separacao_picking_produzido=0,
            separacao_pulmao_produzido=0,
        )
    )

    paletes_por_data = list(recebimentos.values("data").annotate(total=Sum("paletes")).order_by("data"))
    max_paletes_data = max([row["total"] or 0 for row in paletes_por_data] or [1])
    for row in paletes_por_data:
        row["percentual"] = percent_of(row["total"], max_paletes_data)

    eficiencia_rows = []
    for row in conferentes.order_by("-paletes", "conferente"):
        eficiencia_rows.append(
            {
                "conferente": row.conferente,
                "paletes": row.paletes,
                "minutos": row.minutos_trabalhados,
                "palete_minuto": row.palete_minuto,
                "sem_agendamento": row.sem_agendamento,
            }
        )

    recebimento_cards = {
        "total_agendado": agenda.filter(agendado=True).count(),
        "entrada_recebimento": recebimentos.count(),
        "paletes_recebidos": recebimentos.aggregate(total=Sum("paletes"))["total"] or 0,
        "pix": recebimentos.filter(forma_pagamento="pix").aggregate(total=Sum("valor"))["total"] or 0,
        "cartao": recebimentos.filter(forma_pagamento__in=["debito", "cartao"]).aggregate(total=Sum("valor"))["total"] or 0,
        "sem_agendamento": conferentes.aggregate(total=Sum("sem_agendamento"))["total"] or 0,
    }

    def painel_rows(tipo_painel, group_field):
        rows = list(
            ressuprimento_painel.filter(tipo_painel=tipo_painel)
            .values(group_field)
            .annotate(
                qtd_itens=Sum("qtd_itens"),
                qtd_volume=Sum("qtd_volume"),
                pendencia_itens=Sum("pendencia_itens"),
                pendencia_volume=Sum("pendencia_volume"),
                produzido_itens=Sum("produzido_itens"),
                produzido_volume=Sum("produzido_volume"),
                picking=Sum("picking"),
                pulmao=Sum("pulmao"),
            )
            .order_by("-qtd_volume", group_field)
        )
        max_volume = max([number_value(row["qtd_volume"]) for row in rows] or [1])
        for row in rows:
            row["label"] = row[group_field] or "Não informado"
            row["percentual_volume"] = percent_of(row["qtd_volume"], max_volume)
            row["percentual_produzido"] = percent_of(row["produzido_volume"], row["qtd_volume"])
        return rows

    demanda_rows = painel_rows("demanda", "setor")
    ocupacao_rows = painel_rows("ocupacao", "rua")
    producao_rows = painel_rows("producao", "setor")

    demanda_total_volume = sum(number_value(row["qtd_volume"]) for row in demanda_rows)
    demanda_total_itens = sum(number_value(row["qtd_itens"]) for row in demanda_rows)
    demanda_pend_itens = sum(number_value(row["pendencia_itens"]) for row in demanda_rows)
    demanda_pend_volume = sum(number_value(row["pendencia_volume"]) for row in demanda_rows)
    ocupacao_total_itens = sum(number_value(row["qtd_itens"]) for row in ocupacao_rows)
    ocupacao_produzido = sum(number_value(row["produzido_itens"]) for row in ocupacao_rows)
    ocupacao_pendente = sum(number_value(row["pendencia_itens"]) for row in ocupacao_rows)
    producao_simulacao = sum(number_value(row["qtd_volume"]) for row in producao_rows)
    producao_pendencia = sum(number_value(row["pendencia_volume"]) for row in producao_rows)
    producao_gerada = sum(number_value(row["produzido_volume"]) for row in producao_rows)
    producao_picking = sum(number_value(row.separacao_picking_produzido) for row in separacao_prod)
    producao_pulmao = sum(number_value(row.separacao_pulmao_produzido) for row in separacao_prod)

    produtividade = {
        "onda_gerada": sum(number_value(row.onda_gerada) for row in separacao_prod),
        "total_a_produzir": sum(number_value(row.total_a_produzir) for row in separacao_prod),
        "crossdocking": sum(number_value(row.crossdocking_produzido) for row in separacao_prod),
        "picking": producao_picking,
        "pulmao": producao_pulmao,
        "produtividade_picking": sum(number_value(row.produtividade_picking) for row in separacao_prod),
        "produtividade_pulmao": sum(number_value(row.produtividade_pulmao) for row in separacao_prod),
    }

    return {
        "visual_recebimento": {"cards": recebimento_cards, "eficiencia": eficiencia_rows, "paletes_periodo": paletes_por_data},
        "visual_demanda": {
            "cards": {
                "qtd_itens": demanda_total_itens,
                "qtd_volume": demanda_total_volume,
                "pendencia_itens": demanda_pend_itens,
                "pendencia_volume": demanda_pend_volume,
            },
            "setores": demanda_rows,
        },
        "visual_ocupacao": {
            "cards": {
                "qtd_itens": ocupacao_total_itens,
                "produzido": ocupacao_produzido,
                "pendente": ocupacao_pendente,
                "percentual": percent_of(ocupacao_produzido, ocupacao_total_itens),
            },
            "ruas": ocupacao_rows,
        },
        "visual_producao": {
            "cards": {
                "pendencia": producao_pendencia,
                "disponivel": producao_gerada,
                "ressuprimento": producao_picking + producao_pulmao,
                "simulacao": producao_simulacao,
            },
            "setores": producao_rows,
            "produtividade": produtividade,
        },
    }


def gerencial_abertura_data(cd, data):
    agenda = RecebimentoAgenda.objects.filter(cd_unidade=cd, data=data)
    recebimentos = Recebimento.objects.filter(cd_unidade=cd, data=data)
    separacoes = Separacao.objects.filter(cd_unidade=cd, data=data)
    ressuprimentos = Ressuprimento.objects.filter(cd_unidade=cd, data=data)
    expedicoes = Expedicao.objects.filter(cd_unidade=cd, data=data)
    pessoas = PessoaTurno.objects.filter(cd_unidade=cd, data=data).order_by("funcao")
    conferentes = RecebimentoConferente.objects.filter(cd_unidade=cd, data=data).order_by("conferente")
    ressuprimento_painel = RessuprimentoPainel.objects.filter(cd_unidade=cd, data=data)
    separacao_prod = list(SeparacaoProdutividade.objects.filter(cd_unidade=cd, data=data))
    separacao_prod += list(
        Separacao.objects.filter(cd_unidade=cd, data=data).exclude(
            total_a_produzir=0,
            separacao_picking_produzido=0,
            separacao_pulmao_produzido=0,
        )
    )

    veiculos_agendados = agenda.filter(agendado=True).count()
    veiculos_pendentes = agenda.filter(agendado=True).exclude(empresa__in=recebimentos.values("fornecedor")).count()
    veiculos_descarregados = recebimentos.values("fornecedor").distinct().count()
    volume_agendado = agenda.filter(agendado=True).aggregate(total=Sum("paletes"))["total"] or 0
    volume_descarregado = recebimentos.aggregate(total=Sum("paletes"))["total"] or 0
    volume_pendente = max((volume_agendado or 0) - (volume_descarregado or 0), 0)

    pedidos_demanda = separacoes.values("loja").distinct().count()
    pedidos_pendentes = separacoes.exclude(status="concluido").values("loja").distinct().count()
    pedidos_geracao = separacoes.filter(status="concluido").values("loja").distinct().count()
    pedidos_realizando = separacoes.filter(status="andamento").values("loja").distinct().count()
    pulmao = separacoes.filter(categoria__icontains="pulmao").aggregate(total=Sum("unidades"))["total"] or 0
    picking = separacoes.exclude(categoria__icontains="pulmao").aggregate(total=Sum("unidades"))["total"] or 0

    ressuprimento_demanda = ressuprimentos.count()
    ressuprimento_pendente = ressuprimentos.exclude(status="concluido").count()
    ressuprimento_geracao = ressuprimentos.filter(status="concluido").count()
    ressuprimento_realizando = ressuprimentos.filter(status="andamento").count()

    lojas_expedidas = expedicoes.values("loja").distinct().count()
    cargas_carregadas = expedicoes.count()
    cargas_realizadas = expedicoes.filter(status="concluido").count()
    sem_agendamento_total = conferentes.aggregate(total=Sum("sem_agendamento"))["total"] or 0

    eficiencia_rows = [
        [row.conferente, row.paletes, row.cargas, row.minutos_trabalhados, row.palete_minuto, row.sem_agendamento]
        for row in conferentes
    ]

    ress_setor_rows = []
    for row in ressuprimento_painel.values("tipo_painel", "setor").annotate(
        itens=Sum("qtd_itens"),
        volume=Sum("qtd_volume"),
        pendencia_itens_total=Sum("pendencia_itens"),
        pendencia_volume_total=Sum("pendencia_volume"),
        produzido_itens_total=Sum("produzido_itens"),
        produzido_volume_total=Sum("produzido_volume"),
        picking_total=Sum("picking"),
        pulmao_total=Sum("pulmao"),
    ).order_by("tipo_painel", "setor"):
        volume = row["volume"] or 0
        produzido = row["produzido_volume_total"] or 0
        percentual = round((produzido / volume) * 100, 2) if volume else 0
        ress_setor_rows.append(
            [
                row["tipo_painel"],
                row["setor"],
                row["itens"] or 0,
                volume,
                row["pendencia_itens_total"] or 0,
                row["pendencia_volume_total"] or 0,
                row["produzido_itens_total"] or 0,
                produzido,
                row["picking_total"] or 0,
                row["pulmao_total"] or 0,
                percentual,
            ]
        )

    ress_rua_rows = list(
        ressuprimento_painel.exclude(rua="")
        .values_list("tipo_painel", "rua", "qtd_itens", "qtd_volume", "pendencia_itens", "produzido_itens", "produzido_volume")
        .order_by("tipo_painel", "rua")
    )

    produtividade_rows = [
        [
            row.setor,
            row.onda_gerada,
            row.total_a_produzir,
            row.crossdocking_produzido,
            row.separacao_pulmao_produzido,
            row.separacao_picking_produzido,
            row.recursos_picking,
            row.horas_picking,
            row.produtividade_picking,
            row.recursos_pulmao,
            row.horas_pulmao,
            row.produtividade_pulmao,
        ]
        for row in sorted(separacao_prod, key=lambda item: item.setor or "")
    ]

    pessoas_rows = list(
        pessoas.values_list("funcao", "quadro_atual", "planejado", "ativos_dia", "atestados", "afastados", "ferias", "folgas", "faltas_sem_justificativa")
    )
    pessoas_total = [
        "Total",
        sum(row[1] for row in pessoas_rows),
        sum(row[2] for row in pessoas_rows),
        sum(row[3] for row in pessoas_rows),
        sum(row[4] for row in pessoas_rows),
        sum(row[5] for row in pessoas_rows),
        sum(row[6] for row in pessoas_rows),
        sum(row[7] for row in pessoas_rows),
        sum(row[8] for row in pessoas_rows),
    ]

    return {
        "recebimento": [
            ["Veiculos", veiculos_agendados, veiculos_descarregados, veiculos_pendentes],
            ["Volume (Pallets)", volume_agendado, volume_descarregado, volume_pendente],
        ],
        "separacao": [
            ["Pedidos", pedidos_demanda, pedidos_pendentes, pedidos_geracao, pedidos_realizando, pulmao, picking],
            ["Ressuprimento", ressuprimento_demanda, ressuprimento_pendente, ressuprimento_geracao, ressuprimento_realizando, 0, 0],
            ["Produtividade Picking", picking, 0, picking, 0, 0, picking],
            ["Produtividade Pulmão", pulmao, 0, pulmao, 0, pulmao, 0],
        ],
        "expedicao": [
            ["Lojas Expedidas", lojas_expedidas, lojas_expedidas, lojas_expedidas],
            ["Cargas Carregadas", cargas_carregadas, cargas_realizadas, lojas_expedidas],
        ],
        "pessoas": pessoas_rows,
        "pessoas_total": pessoas_total,
        "backlog": [
            ["Recebimento _ Veiculos", veiculos_pendentes],
            ["Recebido sem agendamento", sem_agendamento_total],
            ["Separação Total", pedidos_pendentes],
            ["Veiculos Expedidos", lojas_expedidas],
            ["Veiculos Carregados", cargas_realizadas],
        ],
        "eficiencia_recebimento": eficiencia_rows,
        "ressuprimento_setor": ress_setor_rows,
        "ressuprimento_rua": ress_rua_rows,
        "produtividade_separacao": produtividade_rows,
    }


def style_row(sheet, row_index, fill="D9E1F2", bold=True):
    row_fill = PatternFill("solid", fgColor=fill)
    for cell in sheet[row_index]:
        cell.font = Font(bold=bold)
        cell.fill = row_fill


def append_section(sheet, title, headers, rows):
    sheet.append([title])
    style_row(sheet, sheet.max_row)
    sheet.append(headers)
    style_row(sheet, sheet.max_row, fill="E8EDF9")
    for row in rows:
        sheet.append([xlsx_value(value) for value in row])


def export_abertura_xlsx(cd, data, user):
    data_obj = parse_iso_date(data)
    report = gerencial_abertura_data(cd, data_obj)
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Abertura"

    sheet.append([f"Data: {data_obj:%d/%m/%Y}"])
    sheet.append([f"Unidade: CD {cd}"])
    sheet.append([f"Responsável: {user.get_full_name() or user.username}"])
    sheet.append([])
    sheet.append(["Relatório Gerencial - Abertura"])
    style_row(sheet, sheet.max_row, fill="D9E1F2")
    sheet.append([])

    append_section(sheet, "RECEBIMENTO", ["Indicador", "Agendado", "Descarregado", "Motivo Pendência"], report["recebimento"])
    append_section(sheet, "SEPARAÇÃO (PRODUTIVIDADE)", ["Indicador", "Demanda Total", "Pendência", "Geração Atual", "Realizando", "Pulmão", "Picking"], report["separacao"])
    append_section(sheet, "EXPEDIÇÃO", ["Indicador", "Planejado", "Realizado", "Lojas Atendidas"], report["expedicao"])
    append_section(
        sheet,
        "PESSOAS NO TURNO",
        ["Função", "Quadro Atual", "Planejado", "Ativos Dia", "Atestados", "Afastados", "Férias", "Folgas", "Faltas sem justificativa"],
        report["pessoas"],
    )
    sheet.append(report["pessoas_total"])
    style_row(sheet, sheet.max_row, fill="F3F6FC")
    append_section(sheet, "BACKLOG TOTAL DO DIA", ["Indicador", "Quantidade"], report["backlog"])
    append_section(sheet, "EFICIÊNCIA RECEBIMENTO", ["Conferente", "Paletes", "Cargas", "Minutos", "Palete/min", "Sem agendamento"], report["eficiencia_recebimento"])
    append_section(sheet, "RESSUPRIMENTO POR SETOR", ["Tipo", "Setor", "Qtd. itens", "Qtd. volume", "Pend. itens", "Pend. volume", "Prod. itens", "Prod. volume", "Picking", "Pulmão", "% produzido"], report["ressuprimento_setor"])
    append_section(sheet, "RESSUPRIMENTO POR RUA", ["Tipo", "Rua", "Qtd. itens", "Qtd. volume", "Pend. itens", "Prod. itens", "Prod. volume"], report["ressuprimento_rua"])
    append_section(sheet, "PRODUTIVIDADE SEPARAÇÃO", ["Setor", "Onda gerada", "Total a produzir", "Crossdocking", "Pulmão produzido", "Picking produzido", "Rec. picking", "Horas picking", "Prod. picking", "Rec. pulmão", "Horas pulmão", "Prod. pulmão"], report["produtividade_separacao"])

    for column in sheet.columns:
        max_length = max(len(str(cell.value or "")) for cell in column)
        sheet.column_dimensions[column[0].column_letter].width = min(max(max_length + 2, 12), 34)

    output = io.BytesIO()
    workbook.save(output)
    response = HttpResponse(
        output.getvalue(),
        content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )
    response["Content-Disposition"] = f'attachment; filename="relatorio_gerencial_abertura_cd_{cd}_{data}.xlsx"'
    return response


def powerbi_output_dir():
    configured = os.environ.get("KRILL_POWERBI_DIR")
    if configured:
        return Path(configured)
    if os.name == "nt":
        return Path(r"C:\Krill_CD_Web\powerbi_dados")
    return settings.BASE_DIR / "powerbi_dados"


def csv_text(headers, rows):
    buffer = io.StringIO()
    writer = csv.writer(buffer, delimiter=";", lineterminator="\n")
    writer.writerow(headers)
    for row in rows:
        writer.writerow([xlsx_value(value) for value in row])
    return "\ufeff" + buffer.getvalue()


def write_powerbi_file(folder, filename, headers, rows):
    content = csv_text(headers, rows)
    path = folder / filename
    path.write_text(content, encoding="utf-8-sig")
    return path, content.encode("utf-8-sig")


def build_powerbi_tables(cd, inicio, fim, data):
    tables = []
    visual_recebimento = data["visual_recebimento"]
    visual_demanda = data["visual_demanda"]
    visual_ocupacao = data["visual_ocupacao"]
    visual_producao = data["visual_producao"]

    tables.append(
        (
            "recebimento_resumo.csv",
            ["cd", "inicio", "fim", "indicador", "valor"],
            [[cd, inicio, fim, key, value] for key, value in visual_recebimento["cards"].items()],
        )
    )
    tables.append(
        (
            "recebimento_eficiencia.csv",
            ["cd", "inicio", "fim", "conferente", "paletes", "minutos", "palete_minuto", "sem_agendamento"],
            [
                [cd, inicio, fim, row["conferente"], row["paletes"], row["minutos"], row["palete_minuto"], row["sem_agendamento"]]
                for row in visual_recebimento["eficiencia"]
            ],
        )
    )
    tables.append(
        (
            "recebimento_paletes_periodo.csv",
            ["cd", "inicio", "fim", "data", "paletes", "percentual"],
            [[cd, inicio, fim, row["data"], row["total"] or 0, row["percentual"]] for row in visual_recebimento["paletes_periodo"]],
        )
    )
    tables.append(
        (
            "ressuprimento_demanda.csv",
            ["cd", "inicio", "fim", "setor", "qtd_itens", "qtd_volume", "pendencia_itens", "pendencia_volume", "percentual_volume"],
            [
                [cd, inicio, fim, row["label"], row["qtd_itens"] or 0, row["qtd_volume"] or 0, row["pendencia_itens"] or 0, row["pendencia_volume"] or 0, row["percentual_volume"]]
                for row in visual_demanda["setores"]
            ],
        )
    )
    tables.append(
        (
            "ressuprimento_ocupacao.csv",
            ["cd", "inicio", "fim", "rua", "qtd_itens", "produzido_itens", "pendencia_itens", "qtd_volume", "percentual_produzido"],
            [
                [cd, inicio, fim, row["label"], row["qtd_itens"] or 0, row["produzido_itens"] or 0, row["pendencia_itens"] or 0, row["qtd_volume"] or 0, row["percentual_produzido"]]
                for row in visual_ocupacao["ruas"]
            ],
        )
    )
    tables.append(
        (
            "ressuprimento_producao.csv",
            ["cd", "inicio", "fim", "setor", "simulacao", "pendencia", "quantidade_gerada", "picking", "pulmao", "percentual_produzido"],
            [
                [cd, inicio, fim, row["label"], row["qtd_volume"] or 0, row["pendencia_volume"] or 0, row["produzido_volume"] or 0, row["picking"] or 0, row["pulmao"] or 0, row["percentual_produzido"]]
                for row in visual_producao["setores"]
            ],
        )
    )
    tables.append(
        (
            "separacao_produtividade.csv",
            ["cd", "inicio", "fim", "indicador", "valor"],
            [[cd, inicio, fim, key, value] for key, value in visual_producao["produtividade"].items()],
        )
    )

    gestao = data.get("gestao_360", {})
    tables.append(
        (
            "fato_kpis_gerenciais.csv",
            ["cd", "inicio", "fim", "indicador", "valor"],
            [[cd, inicio, fim, indicador, valor] for indicador, valor in data.get("resumo", [])],
        )
    )
    tables.append(
        (
            "dq_qualidade_dados.csv",
            ["cd", "inicio", "fim", "area", "indicador", "valor", "acao_sugerida"],
            [[cd, inicio, fim, row["area"], row["indicador"], row["valor"], row["acao"]] for row in gestao.get("qualidade", {}).get("checks", [])],
        )
    )
    tables.append(
        (
            "gestao_gargalos_setor.csv",
            ["cd", "inicio", "fim", "setor", "pendencias", "ocorrencias", "pessoas_faltando", "divergencias"],
            [[cd, inicio, fim, row["setor"], row["pendencias"], row["ocorrencias"], row["pessoas_faltando"], row["divergencias"]] for row in gestao.get("setores", [])],
        )
    )
    tables.append(
        (
            "dim_modelo_powerbi.csv",
            ["camada", "tabelas", "uso"],
            [[row["camada"], row["tabelas"], row["uso"]] for row in gestao.get("trilha_bi", [])],
        )
    )
    tables.append(
        (
            "f_ocorrencias.csv",
            ["cd", "data", "tipo", "setor", "titulo", "severidade", "status", "recorrente", "responsavel"],
            [
                [row.cd_unidade, row.data, row.get_tipo_display(), row.get_setor_display(), row.titulo, row.get_severidade_display(), row.get_status_display(), "Sim" if row.recorrente else "Nao", row.responsavel]
                for row in OcorrenciaOperacional.objects.filter(cd_unidade=cd, data__range=[inicio, fim]).order_by("data")
            ],
        )
    )
    tables.append(
        (
            "f_pessoas_turno.csv",
            ["cd", "data", "setor", "turno", "funcao", "planejado", "ativos", "atestados", "afastados", "ferias", "folgas", "faltas_sem_justificativa"],
            [
                [
                    row.cd_unidade,
                    row.data,
                    row.get_setor_display(),
                    row.get_turno_display(),
                    row.funcao,
                    row.planejado,
                    row.ativos_dia,
                    row.atestados,
                    row.afastados,
                    row.ferias,
                    row.folgas,
                    row.faltas_sem_justificativa,
                ]
                for row in PessoaTurno.objects.filter(cd_unidade=cd, data__range=[inicio, fim]).order_by("data")
            ],
        )
    )
    tables.append(
        (
            "f_equipamentos.csv",
            ["cd", "data", "colaborador", "setor", "tipo", "equipamento", "patrimonio", "status"],
            [
                [row.cd_unidade, row.data, row.colaborador, row.setor, row.get_tipo_display(), row.equipamento, row.patrimonio, row.get_status_display()]
                for row in Equipamento.objects.filter(cd_unidade=cd).order_by("colaborador")
            ],
        )
    )
    tables.append(
        (
            "f_materiais_consumo.csv",
            ["cd", "material", "categoria", "estoque_atual", "estoque_minimo", "unidade", "fornecedor", "precisa_pedir"],
            [
                [row.cd_unidade, row.material, row.categoria, row.estoque_atual, row.estoque_minimo, row.unidade, row.fornecedor, "Sim" if row.precisa_pedir else "Nao"]
                for row in MaterialConsumo.objects.filter(cd_unidade=cd).order_by("material")
            ],
        )
    )
    tables.append(
        (
            "f_avarias.csv",
            ["cd", "data", "gtin", "produto", "tipo", "caixas", "unidades", "valor_estimado", "status", "responsavel"],
            [
                [row.cd_unidade, row.data, row.gtin, row.produto, row.get_tipo_display(), row.caixas, row.unidades, row.valor_estimado, row.get_status_display(), row.responsavel]
                for row in Avaria.objects.filter(cd_unidade=cd, data__range=[inicio, fim]).order_by("data")
            ],
        )
    )
    tables.append(
        (
            "f_glpi_saldo.csv",
            ["cd", "data", "gtin", "produto", "saldo_sistema", "saldo_fisico", "diferenca", "tipo_chamado", "numero_glpi", "status", "responsavel"],
            [
                [row.cd_unidade, row.data, row.gtin, row.produto, row.saldo_sistema, row.saldo_fisico, row.diferenca, row.get_tipo_chamado_display(), row.numero_glpi, row.get_status_display(), row.responsavel]
                for row in ChamadoSaldo.objects.filter(cd_unidade=cd, data__range=[inicio, fim]).order_by("data")
            ],
        )
    )
    tables.append(
        (
            "f_fechamentos.csv",
            ["cd", "data", "turno", "responsavel", "status", "resumo_gerencial"],
            [
                [row.cd_unidade, row.data, row.get_turno_display(), row.responsavel, row.get_status_display(), row.resumo_gerencial]
                for row in FechamentoDia.objects.filter(cd_unidade=cd, data__range=[inicio, fim]).order_by("data")
            ],
        )
    )

    abertura = gerencial_abertura_data(cd, parse_iso_date(fim))
    abertura_rows = []
    for section in ["recebimento", "separacao", "expedicao", "backlog"]:
        for row in abertura[section]:
            padded = [cd, fim, section, *row]
            abertura_rows.append(padded + [""] * (10 - len(padded)))
    tables.append(
        (
            "gerencial_abertura.csv",
            ["cd", "data", "secao", "campo_1", "campo_2", "campo_3", "campo_4", "campo_5", "campo_6", "campo_7"],
            abertura_rows,
        )
    )
    return tables


def export_powerbi_package(cd, inicio, fim, data):
    folder = powerbi_output_dir()
    folder.mkdir(parents=True, exist_ok=True)
    files = []
    for filename, headers, rows in build_powerbi_tables(cd, inicio, fim, data):
        files.append(write_powerbi_file(folder, filename, headers, rows))

    readme = (
        "Modelo de Teste - BASE POWER BI\n\n"
        f"CD: {cd}\n"
        f"Periodo: {inicio} a {fim}\n"
        f"Pasta atualizada: {folder}\n\n"
        "Use estes CSVs como fonte no Power BI Desktop.\n"
        "Sugestão de modelo: use as tabelas f_* como fatos e crie dimensões de data, CD, loja, produto, setor e colaborador.\n"
        "O arquivo dq_qualidade_dados.csv mostra pontos que precisam ser corrigidos antes de uma apresentação executiva.\n"
        "O arquivo dim_modelo_powerbi.csv descreve a arquitetura recomendada para evoluir para modelo estrela.\n"
        "Quando gerar novamente pelo sistema, atualize o Power BI para puxar os dados novos.\n"
    )
    readme_path = folder / "LEIA_ME_POWER_BI.txt"
    readme_path.write_text(readme, encoding="utf-8")
    files.append((readme_path, readme.encode("utf-8")))

    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as package:
        for path, content in files:
            package.writestr(path.name, content)

    response = HttpResponse(output.getvalue(), content_type="application/zip")
    response["Content-Disposition"] = f'attachment; filename="powerbi_dados_cd_{cd}_{inicio}_{fim}.zip"'
    return response


def export_report_csv(cd, inicio, fim, tipo, data):
    response = HttpResponse(content_type="text/csv; charset=utf-8-sig")
    response["Content-Disposition"] = f'attachment; filename="relatorio_{tipo}_cd_{cd}_{inicio}_{fim}.csv"'
    response.write("\ufeff")
    writer = csv.writer(response, delimiter=";")
    writer.writerow([f"Relatório {dict(REPORT_TYPES).get(tipo, tipo)}", f"CD {cd}", f"{inicio} a {fim}"])
    writer.writerow([])
    writer.writerow(["Indicador", "Valor"])
    writer.writerows(data["resumo"])

    if section_enabled(tipo, "gestao"):
        gestao = data.get("gestao_360", {})
        write_section(
            writer,
            "Gestão 360 - Score operacional",
            ["Indicador", "Valor", "Detalhe"],
            [[card["label"], card["value"], card["detail"]] for card in gestao.get("cards", [])],
        )
        write_section(
            writer,
            "Gestão 360 - Gargalos por setor",
            ["Setor", "Pendências", "Ocorrências", "Pessoas faltando", "Divergências"],
            [[row["setor"], row["pendencias"], row["ocorrencias"], row["pessoas_faltando"], row["divergencias"]] for row in gestao.get("setores", [])],
        )
        write_section(
            writer,
            "Gestão 360 - Qualidade dos dados",
            ["Área", "Indicador", "Valor", "Ação sugerida"],
            [[row["area"], row["indicador"], row["valor"], row["acao"]] for row in gestao.get("qualidade", {}).get("checks", [])],
        )

    if section_enabled(tipo, "recebimento"):
        write_section(
            writer,
            "Recebimento por fornecedor",
            ["Fornecedor", "Paletes", "Valor"],
            [[r["fornecedor"], r["paletes_total"] or 0, r["valor_total"] or 0] for r in data["recebimento_empresas"]],
        )
        write_section(
            writer,
            "Agenda de recebimento",
            ["Data", "Empresa", "Tipo", "Paletes", "Agendado"],
            [[r.data, r.empresa, r.tipo_caminhao, r.paletes, "Sim" if r.agendado else "Não"] for r in data["agenda_periodo"]],
        )

    if section_enabled(tipo, "separacao"):
        write_section(
            writer,
            "Separação por categoria",
            ["Categoria", "Unidades", "Paletes"],
            [[r["categoria_label"], r["unidades_total"] or 0, r["paletes_total"] or 0] for r in data["separacao_categorias"]],
        )
        write_section(
            writer,
            "Separação por loja",
            ["Loja", "Unidades", "Paletes"],
            [[r["loja"], r["unidades_total"] or 0, r["paletes_total"] or 0] for r in data["separacao_lojas"]],
        )

    if section_enabled(tipo, "expedicao"):
        write_section(
            writer,
            "Expedição por loja",
            ["Loja", "Paletes"],
            [[r["loja"], r["paletes_total"] or 0] for r in data["expedicao_lojas"]],
        )
        write_section(
            writer,
            "Expedição por período",
            ["Período", "Paletes"],
            [[r["periodo_label"], r["paletes_total"] or 0] for r in data["expedicao_periodos"]],
        )

    if section_enabled(tipo, "apoio"):
        write_section(
            writer,
            "Avarias abertas",
            ["Data", "Produto", "Tipo", "Caixas", "Unidades", "Status"],
            [[r.data, r.produto, r.get_tipo_display(), r.caixas, r.unidades, r.get_status_display()] for r in data["avarias_abertas"]],
        )
        write_section(
            writer,
            "Chamados GLPI abertos",
            ["Data", "Produto", "GLPI", "Status", "Responsável"],
            [[r.data, r.produto, r.numero_glpi, r.get_status_display(), r.responsavel] for r in data["chamados_abertos"]],
        )
        write_section(
            writer,
            "Materiais para pedido",
            ["Material", "Estoque atual", "Estoque mínimo", "Unidade"],
            [[r.material, r.estoque_atual, r.estoque_minimo, r.unidade] for r in data["materiais_pedido"]],
        )
    return response


def export_report_xlsx(cd, inicio, fim, tipo, data):
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Relatório"
    title_fill = PatternFill("solid", fgColor="D9EAF7")
    header_fill = PatternFill("solid", fgColor="EDF2F7")

    sheet.append([f"Relatório {dict(REPORT_TYPES).get(tipo, tipo)}", f"CD {cd}", f"{inicio} a {fim}"])
    for cell in sheet[1]:
        cell.font = Font(bold=True)
        cell.fill = title_fill
    sheet.append([])
    sheet.append(["Indicador", "Valor"])
    for cell in sheet[3]:
        cell.font = Font(bold=True)
        cell.fill = header_fill
    for row in data["resumo"]:
        sheet.append([xlsx_value(value) for value in row])

    for title, headers, rows in report_export_sections(tipo, data):
        sheet.append([])
        sheet.append([title])
        sheet[sheet.max_row][0].font = Font(bold=True)
        sheet[sheet.max_row][0].fill = title_fill
        sheet.append(headers)
        for cell in sheet[sheet.max_row]:
            cell.font = Font(bold=True)
            cell.fill = header_fill
        for row in rows:
            sheet.append([xlsx_value(value) for value in row])

    for column in sheet.columns:
        max_length = max(len(str(cell.value or "")) for cell in column)
        sheet.column_dimensions[column[0].column_letter].width = min(max(max_length + 2, 12), 45)

    output = io.BytesIO()
    workbook.save(output)
    response = HttpResponse(
        output.getvalue(),
        content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )
    response["Content-Disposition"] = f'attachment; filename="relatorio_{tipo}_cd_{cd}_{inicio}_{fim}.xlsx"'
    return response


@login_required
def relatorios(request):
    if not user_has_perm(request.user, "relatorios"):
        messages.error(request, "Você não tem acesso aos relatórios.")
        return redirect("dashboard")
    cd = current_cd(request)
    inicio = request.GET.get("inicio") or timezone.localdate().isoformat()
    fim = request.GET.get("fim") or inicio
    tipo = request.GET.get("tipo", "geral")
    modo_painel = request.GET.get("modo") == "painel"
    if tipo not in {key for key, _label in REPORT_TYPES}:
        tipo = "geral"
    report_data = build_report_data(cd, inicio, fim)
    report_data.update(build_visual_report_data(cd, inicio, fim))
    overview = operational_overview(cd, fim)
    export_format = request.GET.get("exportar")
    if export_format == "csv":
        response = export_report_csv(cd, inicio, fim, tipo, report_data)
        log_action(request, "relatorio_csv", "relatorios", detalhe=f"{inicio} a {fim}")
        return response
    if export_format == "xlsx":
        if tipo == "abertura":
            response = export_abertura_xlsx(cd, fim, request.user)
            log_action(request, "relatorio_abertura_xlsx", "relatorios", detalhe=fim)
            return response
        response = export_report_xlsx(cd, inicio, fim, tipo, report_data)
        log_action(request, "relatorio_xlsx", "relatorios", detalhe=f"{inicio} a {fim}")
        return response
    if export_format == "powerbi":
        response = export_powerbi_package(cd, inicio, fim, report_data)
        log_action(request, "powerbi_export", "relatorios", detalhe=f"{inicio} a {fim}")
        return response
    ctx = context_base(request)
    ctx.update(
        {
            "inicio": inicio,
            "fim": fim,
            "tipo": tipo,
            "modo_painel": modo_painel,
            "report_types": REPORT_TYPES,
            "mostrar_gestao": section_enabled(tipo, "gestao"),
            "mostrar_recebimento": section_enabled(tipo, "recebimento"),
            "mostrar_visual_recebimento": tipo in {"geral", "recebimento"},
            "mostrar_visual_demanda": tipo in {"geral", "ressuprimento_demanda"},
            "mostrar_visual_ocupacao": tipo in {"geral", "ressuprimento_ocupacao"},
            "mostrar_visual_producao": tipo in {"geral", "ressuprimento_producao"},
            "mostrar_separacao": section_enabled(tipo, "separacao"),
            "mostrar_expedicao": section_enabled(tipo, "expedicao"),
            "mostrar_apoio": section_enabled(tipo, "apoio"),
            **report_data,
            **overview,
        }
    )
    return render(request, "painel/relatorios.html", ctx)


FIELD_ALIASES = {
    "data": ["data", "dt", "emissao"],
    "codigo": ["codigo", "cod_loja", "loja"],
    "nome": ["nome", "nome_loja", "descricao_loja"],
    "grupo": ["grupo", "tipo_loja"],
    "gtin": ["gtin", "ean", "codigo_barras"],
    "descricao": ["descricao", "produto", "nome_produto"],
    "fornecedor": ["fornecedor", "empresa"],
    "nota_fiscal": ["nf", "nota", "nota_fiscal"],
    "produto": ["produto", "descricao", "item"],
    "paletes": ["paletes", "pallets", "qtd_paletes"],
    "loja": ["loja", "codigo_loja", "destino"],
    "placa": ["placa"],
    "motorista": ["motorista"],
    "qtd_paletes": ["qtd_paletes", "paletes", "pallets"],
    "colaborador": ["colaborador", "nome"],
    "setor": ["setor"],
    "material": ["material", "item"],
    "quantidade": ["quantidade", "qtd"],
}


def module_scores(columns):
    norm_columns = {normalize(col) for col in columns}
    scores = []
    for module in MODULES:
        score = 0
        for field in module.fields:
            aliases = FIELD_ALIASES.get(field, [field])
            if any(normalize(alias) in norm_columns for alias in aliases):
                score += 1
        scores.append((score, module))
    return sorted(scores, key=lambda item: item[0], reverse=True)


def infer_module(columns):
    return module_scores(columns)[0]


def read_uploaded_dataframe(path):
    ext = Path(path).suffix.lower()
    if ext in {".xlsx", ".xls"}:
        return pd.read_excel(path)
    return pd.read_csv(path, sep=None, engine="python", encoding="utf-8-sig")


def temp_import_dir():
    path = Path(settings.BASE_DIR) / "importacoes_temp"
    path.mkdir(parents=True, exist_ok=True)
    return path


def parse_value(field, value):
    if pd.isna(value):
        return None
    if isinstance(field, django_models.BooleanField):
        text = normalize(value)
        return text in {"sim", "s", "true", "1", "ativo", "agendado"}
    if isinstance(field, django_models.DateField):
        parsed = pd.to_datetime(value, dayfirst=True, errors="coerce")
        return parsed.date() if not pd.isna(parsed) else None
    if isinstance(field, django_models.DecimalField):
        try:
            return Decimal(str(value).replace(",", "."))
        except (InvalidOperation, ValueError):
            return Decimal("0")
    if isinstance(field, (django_models.IntegerField, django_models.PositiveIntegerField)):
        try:
            return int(float(str(value).replace(",", ".")))
        except ValueError:
            return 0
    return str(value).strip()


@login_required
def importar(request):
    if not user_has_perm(request.user, "importar_planilhas"):
        messages.error(request, "Você não tem acesso à importação.")
        return redirect("dashboard")
    resultado = None
    preview = None
    if request.method == "POST":
        if request.POST.get("confirmar") == "1":
            token = request.POST.get("token", "")
            module_key = request.POST.get("module_key", "")
            temp_file = temp_import_dir() / token
            module = MODULE_BY_KEY.get(module_key)
            if not temp_file.exists() or not module:
                messages.error(request, "Arquivo temporário não encontrado. Envie a planilha novamente.")
            else:
                df = read_uploaded_dataframe(temp_file)
                created = 0
                column_map = {normalize(col): col for col in df.columns}
                for _idx, row in df.iterrows():
                    obj = module.model(cd_unidade=current_cd(request), criado_por=request.user)
                    has_value = False
                    for field_name in module.fields:
                        try:
                            model_field = module.model._meta.get_field(field_name)
                        except Exception:
                            continue
                        source_col = None
                        for alias in FIELD_ALIASES.get(field_name, [field_name]):
                            source_col = column_map.get(normalize(alias))
                            if source_col:
                                break
                        if source_col is None:
                            continue
                        value = parse_value(model_field, row[source_col])
                        if value is not None:
                            setattr(obj, field_name, value)
                            has_value = True
                    if has_value:
                        obj.save()
                        created += 1
                resultado = {"module": module, "created": created, "score": "", "arquivo": temp_file.name}
                log_action(request, "planilha_importada", "importacao", detalhe=f"{temp_file.name} -> {module.title}: {created}")
                messages.success(request, f"Importei {created} registros em {module.title}.")
                try:
                    temp_file.unlink()
                except OSError:
                    pass
        elif request.FILES.get("arquivo"):
            arquivo = request.FILES["arquivo"]
            ext = os.path.splitext(arquivo.name)[1].lower()
            token = f"{uuid.uuid4().hex}{ext}"
            temp_file = temp_import_dir() / token
            with temp_file.open("wb") as handle:
                for chunk in arquivo.chunks():
                    handle.write(chunk)
            df = read_uploaded_dataframe(temp_file)
            scores = module_scores(df.columns)
            score, module = scores[0]
            if score == 0:
                messages.error(request, "Não consegui identificar para onde essa planilha deve ir.")
            preview = {
                "token": token,
                "arquivo": arquivo.name,
                "module": module,
                "score": score,
                "scores": scores[:5],
                "columns": list(df.columns),
                "rows": df.head(10).fillna("").astype(str).values.tolist(),
                "total": len(df),
            }
    ctx = context_base(request)
    ctx.update({"resultado": resultado, "preview": preview, "modules": MODULES})
    return render(request, "painel/importar.html", ctx)


@login_required
def corrigir_cd(request):
    if not user_has_perm(request.user, "corrigir_cd"):
        messages.error(request, "Você não tem acesso à correção de CD.")
        return redirect("dashboard")
    preview = []
    if request.method == "POST":
        origem = request.POST.get("origem")
        destino = request.POST.get("destino")
        inicio = request.POST.get("inicio")
        fim = request.POST.get("fim")
        aplicar = request.POST.get("aplicar") == "1"
        if origem in {"801", "806"} and destino in {"801", "806"} and origem != destino:
            for module in MODULES:
                qs = module.model.objects.filter(cd_unidade=origem)
                if inicio and fim and "data" in [f.name for f in module.model._meta.fields]:
                    qs = qs.filter(data__range=[inicio, fim])
                count = qs.count()
                if count:
                    preview.append((module.title, count))
                    if aplicar:
                        qs.update(cd_unidade=destino)
            if aplicar:
                log_action(request, "corrigir_cd", "banco_dados", detalhe=f"{origem} -> {destino}; {inicio} a {fim}")
                messages.success(request, "Registros transferidos para o CD correto.")
                return redirect("corrigir_cd")
    ctx = context_base(request)
    ctx.update({"preview": preview})
    return render(request, "painel/corrigir_cd.html", ctx)
