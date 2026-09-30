from datetime import timedelta

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from .forms import build_model_form
from .models import (
    ChecklistFrota,
    ChecklistFrotaGrupo,
    ChecklistFrotaItem,
    Equipamento,
    EquipamentoManutencao,
    Expedicao,
    ExpedicaoPlanejamento,
    ExpedicaoVinculo,
    FuncaoTurno,
    Loja,
    MelhoriaSistema,
    PaleteRedeMovimentacao,
    PaleteRedeSaldo,
    PerfilAcesso,
    SistemaNotificacao,
    SolicitacaoCargaPronta,
    SolicitacaoCarregamentoManual,
    SolicitacaoCaminhaoCD,
    VeiculoFrota,
)
from .registry import MODULE_BY_KEY
from .utils import set_config
from .views import (
    checklist_frota_groups,
    clear_runtime_permission_cache,
    driver_route_from_fleet_links,
    ensure_checklist_structure,
    managed_sectors_for_user,
)


class OperationalAccessTests(TestCase):
    def create_user(self, username, cargo, permissions):
        user = User.objects.create_user(username=username, password="Teste123")
        PerfilAcesso.objects.create(user=user, cargo=cargo, cd_padrao="806", permissoes=permissions)
        return user

    def test_trial_read_only_blocks_writes_after_30_days(self):
        user = self.create_user("piloto.consulta", "assistente", ["painel", "melhorias_sistema", "criar_registros", "consultar_registros"])
        set_config("piloto_teste_ativo", "sim")
        set_config("piloto_teste_inicio", (timezone.localdate() - timedelta(days=31)).isoformat())
        set_config("piloto_teste_dias", "30")
        set_config("piloto_teste_consulta_dias", "7")
        self.assertTrue(self.client.login(username="piloto.consulta", password="Teste123"))

        response = self.client.get(reverse("dashboard"))
        self.assertEqual(response.status_code, 200, response.get("Location", ""))
        self.assertContains(response, "modo somente consulta")

        response = self.client.post(
            reverse("module_list", args=["melhorias_sistema"]),
            {
                "data": timezone.localdate().isoformat(),
                "titulo": "Nao deve gravar",
                "tipo_solicitacao": "implantacao",
                "area": "frota",
                "descricao": "Teste",
                "impacto": "Teste",
            },
        )
        self.assertEqual(response.status_code, 403)
        self.assertFalse(MelhoriaSistema.objects.filter(titulo="Nao deve gravar").exists())
        set_config("piloto_teste_ativo", "nao")

    def test_demo_mode_blocks_regular_post_without_saving(self):
        self.create_user("demo.normal", "assistente", ["painel", "melhorias_sistema", "criar_registros", "consultar_registros"])
        self.assertTrue(self.client.login(username="demo.normal", password="Teste123"))
        self.client.cookies["modo_demo"] = "1"

        response = self.client.post(
            reverse("module_list", args=["melhorias_sistema"]),
            {
                "data": timezone.localdate().isoformat(),
                "titulo": "Nao deve gravar demo",
                "tipo_solicitacao": "implantacao",
                "area": "frota",
                "descricao": "Teste",
                "impacto": "Teste",
            },
            HTTP_REFERER=reverse("module_list", args=["melhorias_sistema"]),
        )

        self.assertEqual(response.status_code, 302)
        self.assertIn("demo_salvo=1", response["Location"])
        self.assertFalse(MelhoriaSistema.objects.filter(titulo="Nao deve gravar demo").exists())

    def test_demo_mode_ajax_returns_json_without_saving(self):
        self.create_user("demo.ajax", "assistente", ["painel", "melhorias_sistema", "criar_registros", "consultar_registros"])
        self.assertTrue(self.client.login(username="demo.ajax", password="Teste123"))
        self.client.cookies["modo_demo"] = "1"

        response = self.client.post(
            reverse("module_list", args=["melhorias_sistema"]),
            {
                "data": timezone.localdate().isoformat(),
                "titulo": "Nao deve gravar ajax",
                "tipo_solicitacao": "implantacao",
                "area": "frota",
                "descricao": "Teste",
                "impacto": "Teste",
            },
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
            HTTP_ACCEPT="application/json",
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["ok"], True)
        self.assertEqual(response.json()["simulado"], True)
        self.assertFalse(MelhoriaSistema.objects.filter(titulo="Nao deve gravar ajax").exists())

    def test_builtin_expedition_central_groups_operational_cards(self):
        user = self.create_user(
            "expedicao.central",
            "supervisor",
            [
                "painel",
                "lojas_prontas_carregamento",
                "expedicao_planejamento",
                "relatorio_paletes_cd",
                "expedicao",
                "faturamento_expedicao",
                "visualizar_valor_faturamento",
                "criar_registros",
                "consultar_registros",
            ],
        )
        set_config("feature_faturamento_expedicao", "sim")
        clear_runtime_permission_cache()
        self.client.force_login(user)

        response = self.client.get(reverse("central_personalizada", args=["hub_central_expedicao"]))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Central Expedi")
        self.assertContains(response, "Informar loja pronta")
        self.assertContains(response, "Ajustar saldo")
        self.assertContains(response, "Faturamento BlueSoft")

    def test_common_user_can_submit_but_not_manage_improvements(self):
        user = self.create_user(
            "supervisor.frota",
            "supervisor_frota",
            ["melhorias_sistema", "criar_registros", "consultar_registros", "ver_graficos_resumos", "editar_registros"],
        )
        self.client.force_login(user)
        url = reverse("module_list", args=["melhorias_sistema"])

        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Solicitar Melhoria")
        self.assertNotContains(response, "Gestão de Melhorias")
        self.assertNotContains(response, "Registros salvos")

        response = self.client.post(
            url,
            {
                "data": "2026-07-09",
                "titulo": "Pedido restrito",
                "tipo_solicitacao": "implantacao",
                "area": "frota",
                "descricao": "Teste",
                "impacto": "Teste",
                "status": "concluida",
                "responsavel": "Nao permitido",
            },
        )
        self.assertEqual(response.status_code, 302)
        item = MelhoriaSistema.objects.get(titulo="Pedido restrito")
        self.assertEqual(item.status, "sugestao")
        self.assertEqual(item.responsavel, "")
        self.assertEqual(item.criado_por, user)

    def test_access_management_shows_permission_presets(self):
        user = self.create_user("master.presets", "master", ["painel", "gestao_acesso", "gestao_acesso_total"])
        self.client.force_login(user)

        response = self.client.get(reverse("usuarios"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Usar modelo")
        self.assertContains(response, "Faturamento")
        self.assertContains(response, "Pallets da Rede")

    def test_specific_supervisor_cannot_submit_another_sector(self):
        user = self.create_user("frota.escopo", "supervisor_frota", ["funcoes_turno", "criar_registros"])
        module = MODULE_BY_KEY["funcoes_turno"]
        form_class = build_model_form(module.model, module.fields)
        form = form_class(
            {
                "data": "2026-07-09",
                "nome": "Funcao invalida",
                "setor": "recebimento",
                "turno": "dia",
                "quadro_padrao": 1,
                "ativa": True,
            },
            cd_unidade="806",
            allowed_sectors=managed_sectors_for_user(user),
        )
        self.assertFalse(form.is_valid())
        self.assertIn("setor", form.errors)

    def test_maintenance_uses_canonical_equipment_data(self):
        equipment = Equipamento.objects.create(
            cd_unidade="806",
            colaborador="Teste",
            setor="Frota",
            tipo="operacional",
            equipamento="Coletor 01",
            patrimonio="PAT-12-34",
        )
        maintenance = EquipamentoManutencao.objects.create(
            cd_unidade="806",
            equipamento_cadastrado=equipment,
            patrimonio="valor incorreto",
            equipamento="valor incorreto",
            tipo="outro",
            problema="Tela",
        )
        self.assertEqual(maintenance.patrimonio, "PAT-12-34")
        self.assertEqual(maintenance.equipamento, "Coletor 01")
        self.assertEqual(maintenance.tipo, "operacional")

    def test_return_closes_the_matching_departure(self):
        user = self.create_user("motorista.teste", "motorista", ["checklist_frota", "criar_registros"])
        VeiculoFrota.objects.create(
            cd_unidade="806",
            motorista="Motorista Teste",
            placa="TST1A23",
            tipo_caminhao="Truck",
            usuario_motorista=user,
            ativo=True,
        )
        store = Loja.objects.filter(cd_unidade="806", ativa=True).first()
        if store is None:
            store = Loja.objects.create(cd_unidade="806", codigo="99", nome="Loja teste", ativa=True)
        self.client.force_login(user)
        url = reverse("module_list", args=["checklist_frota"])
        base = {
            "data": "2026-07-09",
            "motorista": "Motorista Teste",
            "veiculo": "Truck",
            "placa": "TST1A23",
            "loja_destino": store.nome,
            "km_inicial": 100,
            "km_final": 0,
        }

        response = self.client.post(url, {**base, "tipo_checklist": "saida"})
        self.assertEqual(response.status_code, 302)
        departure = ChecklistFrota.objects.get(tipo_checklist="saida")
        self.assertTrue(departure.acompanhar_retorno)
        self.assertFalse(departure.documentacao_veiculo_ok)

        response = self.client.post(
            url,
            {**base, "tipo_checklist": "retorno", "km_final": 150, "saida_referencia_id": departure.pk},
        )
        self.assertEqual(response.status_code, 302)
        returned = ChecklistFrota.objects.get(tipo_checklist="retorno")
        self.assertEqual(returned.saida_referencia, departure)
        self.assertFalse(returned.documentacao_veiculo_ok)
        self.assertFalse(ChecklistFrota.objects.filter(pk=departure.pk, retorno_registrado__isnull=True).exists())

    def test_driver_cannot_create_new_departure_with_open_return(self):
        user = self.create_user("motorista.bloqueio", "motorista", ["checklist_frota", "criar_registros"])
        VeiculoFrota.objects.create(
            cd_unidade="806",
            motorista="Motorista Bloqueio",
            placa="BLQ1A23",
            tipo_caminhao="Truck",
            usuario_motorista=user,
            ativo=True,
        )
        store = Loja.objects.filter(cd_unidade="806", ativa=True).first()
        if store is None:
            store = Loja.objects.create(cd_unidade="806", codigo="98", nome="Loja bloqueio", ativa=True)
        self.client.force_login(user)
        url = reverse("module_list", args=["checklist_frota"])
        base = {
            "data": "2026-07-09",
            "tipo_checklist": "saida",
            "motorista": "Motorista Bloqueio",
            "veiculo": "Truck",
            "placa": "BLQ1A23",
            "loja_destino": store.nome,
            "km_inicial": 100,
            "km_final": 0,
            "horario_saida": "08:00",
        }

        first = self.client.post(url, base)
        self.assertEqual(first.status_code, 302)
        second = self.client.post(url, {**base, "horario_saida": "10:00"})
        self.assertEqual(second.status_code, 302)
        self.assertEqual(ChecklistFrota.objects.filter(tipo_checklist="saida", placa="BLQ1A23").count(), 1)

    def test_driver_cannot_register_return_for_another_driver(self):
        driver = self.create_user("teste.motorista", "motorista", ["checklist_frota", "criar_registros"])
        other = self.create_user("paulo.sergio.retorno", "motorista", ["checklist_frota", "criar_registros"])
        VeiculoFrota.objects.create(
            cd_unidade="806",
            motorista="Motorista Teste",
            placa="TST1A23",
            tipo_caminhao="Truck",
            usuario_motorista=driver,
            ativo=True,
        )
        VeiculoFrota.objects.create(
            cd_unidade="806",
            motorista="Paulo Sergio",
            placa="GJR4D46",
            tipo_caminhao="Truck",
            usuario_motorista=other,
            ativo=True,
        )
        departure = ChecklistFrota.objects.create(
            cd_unidade="806",
            data=timezone.localdate(),
            tipo_checklist="saida",
            motorista="Paulo Sergio",
            veiculo="Truck",
            placa="GJR4D46",
            loja_destino="01 - KRILL P. GRANDE",
            acompanhar_retorno=True,
            criado_por=other,
        )
        self.client.force_login(driver)

        response = self.client.post(
            reverse("module_list", args=["checklist_frota"]),
            {
                "data": timezone.localdate().isoformat(),
                "tipo_checklist": "retorno",
                "motorista": "Paulo Sergio",
                "veiculo": "Truck",
                "placa": "GJR4D46",
                "loja_destino": "01 - KRILL P. GRANDE",
                "km_final": 100,
                "saida_referencia_id": departure.pk,
            },
        )

        self.assertEqual(response.status_code, 302)
        departure.refresh_from_db()
        self.assertFalse(ChecklistFrota.objects.filter(saida_referencia=departure).exists())
        self.assertFalse(ChecklistFrota.objects.filter(tipo_checklist="retorno", placa="GJR4D46").exists())

    def test_driver_only_sees_own_open_return(self):
        driver = self.create_user("motorista.visao", "motorista", ["checklist_frota", "criar_registros"])
        other = self.create_user("motorista.outro", "motorista", ["checklist_frota", "criar_registros"])
        VeiculoFrota.objects.create(
            cd_unidade="806",
            motorista="Motorista Visao",
            placa="VIS1A23",
            tipo_caminhao="Truck",
            usuario_motorista=driver,
            ativo=True,
        )
        ChecklistFrota.objects.create(
            cd_unidade="806",
            data=timezone.localdate(),
            tipo_checklist="saida",
            motorista="Motorista Visao",
            veiculo="Truck",
            placa="VIS1A23",
            loja_destino="01 - KRILL P. GRANDE",
            acompanhar_retorno=True,
            criado_por=driver,
        )
        ChecklistFrota.objects.create(
            cd_unidade="806",
            data=timezone.localdate(),
            tipo_checklist="saida",
            motorista="Outro Motorista",
            veiculo="Truck",
            placa="OUT1A23",
            loja_destino="02 - MERI KRILL",
            acompanhar_retorno=True,
            criado_por=other,
        )
        self.client.force_login(driver)

        response = self.client.get(reverse("module_list", args=["checklist_frota"]))

        self.assertContains(response, "VIS1A23")
        self.assertNotContains(response, "OUT1A23")

    def test_driver_checklist_shows_store_arrival_and_departure_steps(self):
        user = self.create_user("allan.motorista", "motorista", ["checklist_frota", "criar_registros"])
        self.client.force_login(user)

        response = self.client.get(reverse("module_list", args=["checklist_frota"]))

        self.assertContains(response, 'value="chegada_loja"')
        self.assertContains(response, 'value="saida_loja"')
        self.assertContains(response, "Chegada na loja")
        self.assertContains(response, "Saída da loja")

    def test_manual_intercalation_fields_follow_system_rule(self):
        set_config("rule_checklist_intercalacao_manual", "nao")
        field_names = {field for group in checklist_frota_groups(motorista=True, cd="806") for field in group["fields"]}
        self.assertNotIn("intercala_cd", field_names)
        self.assertNotIn("cd_intercalacao", field_names)

        set_config("rule_checklist_intercalacao_manual", "sim")
        field_names = {field for group in checklist_frota_groups(motorista=True, cd="806") for field in group["fields"]}
        self.assertIn("intercala_cd", field_names)
        self.assertIn("cd_intercalacao", field_names)
        set_config("rule_checklist_intercalacao_manual", "nao")

    def test_driver_checklist_prefills_current_linked_load(self):
        user = self.create_user("allan.vinculado", "motorista", ["checklist_frota", "criar_registros"])
        VeiculoFrota.objects.create(
            cd_unidade="806",
            motorista="Allan Motorista",
            placa="ALL1A23",
            tipo_caminhao="Truck",
            usuario_motorista=user,
            ativo=True,
        )
        ExpedicaoVinculo.objects.create(
            cd_unidade="806",
            data=timezone.localdate(),
            loja="01 - KRILL P. GRANDE",
            qtd_paletes=10,
            placa="ALL1A23",
            motorista="Allan Motorista",
            status="vinculado",
        )
        self.client.force_login(user)

        response = self.client.get(reverse("module_list", args=["checklist_frota"]))

        self.assertContains(response, 'value="Allan Motorista"')
        self.assertContains(response, 'value="ALL1A23"')
        self.assertContains(response, 'value="01 - KRILL P. GRANDE"')
        self.assertNotContains(response, 'name="veiculo"')

    def test_checklist_group_items_can_be_disabled_and_restored_in_bulk(self):
        user = self.create_user("gestor.checklist", "master", ["checklist_frota_itens", "editar_checklist_frota", "editar_registros"])
        ensure_checklist_structure("806")
        group = ChecklistFrotaGrupo.objects.get(cd_unidade="806", codigo="saida_documentos_seguranca")
        total = group.itens.count()
        self.assertGreater(total, 0)
        self.client.force_login(user)
        url = reverse("module_list", args=["checklist_frota_itens"])

        response = self.client.post(url, {"action": "atualizar_itens_grupo_checklist", "grupo_id": group.pk, "bulk_action": "desativar"})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(group.itens.filter(ativo=True).count(), 0)

        response = self.client.post(url, {"action": "atualizar_itens_grupo_checklist", "grupo_id": group.pk, "bulk_action": "restaurar"})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(group.itens.filter(ativo=True).count(), total)

    def test_checklist_selected_items_can_be_disabled_and_restored(self):
        user = self.create_user("gestor.selecionados", "master", ["checklist_frota_itens", "editar_checklist_frota", "editar_registros"])
        ensure_checklist_structure("806")
        group = ChecklistFrotaGrupo.objects.get(cd_unidade="806", codigo="saida_documentos_seguranca")
        selected = list(group.itens.order_by("id")[:2])
        self.assertEqual(len(selected), 2)
        self.client.force_login(user)
        url = reverse("module_list", args=["checklist_frota_itens"])

        response = self.client.post(
            url,
            {
                "action": "atualizar_itens_selecionados_checklist",
                "bulk_action": "desativar",
                "item_ids": [str(item.pk) for item in selected],
            },
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(ChecklistFrotaItem.objects.filter(pk__in=[item.pk for item in selected], ativo=False).count(), 2)

        response = self.client.post(
            url,
            {
                "action": "atualizar_itens_selecionados_checklist",
                "bulk_action": "restaurar",
                "item_ids": [str(item.pk) for item in selected],
            },
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(ChecklistFrotaItem.objects.filter(pk__in=[item.pk for item in selected], ativo=True).count(), 2)

    def test_frota_hub_shows_vehicle_occupied_when_driver_reports_transfer_load(self):
        user = self.create_user("mauricio.frota", "supervisor_frota", ["painel_frota", "veiculos_frota", "vincular_cargas_expedicao"])
        VeiculoFrota.objects.create(
            cd_unidade="806",
            motorista="Motorista Transferencia",
            placa="TRF1A23",
            tipo_caminhao="Truck",
            ativo=True,
        )
        ChecklistFrota.objects.create(
            cd_unidade="806",
            data=timezone.localdate(),
            tipo_checklist="saida_loja",
            motorista="Motorista Transferencia",
            veiculo="Truck",
            placa="TRF1A23",
            loja_destino="01 - KRILL P. GRANDE",
            carga_retorno=True,
            carga_retorno_descricao="Transferencia para loja 02",
        )
        self.client.force_login(user)

        response = self.client.get(reverse("frota_hub"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Disponibilidade da Frota")
        self.assertContains(response, "Ocupado")
        self.assertContains(response, "Transferencia para loja 02")

    def test_tachograph_frequency_can_be_weekly_or_daily_by_vehicle(self):
        weekly_user = self.create_user("motorista.semanal", "motorista", ["checklist_frota", "criar_registros"])
        daily_user = self.create_user("motorista.diario", "motorista", ["checklist_frota", "criar_registros"])
        today = timezone.localdate()
        yesterday = today - timedelta(days=1)
        VeiculoFrota.objects.create(
            cd_unidade="806",
            motorista="Motorista Semanal",
            placa="SEM1A23",
            tipo_caminhao="Truck",
            usuario_motorista=weekly_user,
            tacografo_frequencia="semanal",
            ativo=True,
        )
        VeiculoFrota.objects.create(
            cd_unidade="806",
            motorista="Motorista Diario",
            placa="DIA1A23",
            tipo_caminhao="Truck",
            usuario_motorista=daily_user,
            tacografo_frequencia="diario",
            ativo=True,
        )
        ChecklistFrota.objects.create(
            cd_unidade="806",
            data=today,
            tipo_checklist="saida",
            motorista="Motorista Semanal",
            veiculo="Truck",
            placa="SEM1A23",
            tacografo_ok=True,
        )
        ChecklistFrota.objects.create(
            cd_unidade="806",
            data=yesterday,
            tipo_checklist="saida",
            motorista="Motorista Diario",
            veiculo="Truck",
            placa="DIA1A23",
            tacografo_ok=True,
        )

        self.client.force_login(weekly_user)
        response = self.client.get(reverse("module_list", args=["checklist_frota"]))
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, 'name="tacografo_ok"')

        self.client.force_login(daily_user)
        response = self.client.get(reverse("module_list", args=["checklist_frota"]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'name="tacografo_ok"')

    def test_network_pallets_can_transfer_from_cd_to_store(self):
        user = self.create_user("juliana.paletes", "analista", ["paletes_rede", "criar_registros", "consultar_registros", "editar_registros"])
        self.client.force_login(user)
        url = reverse("module_list", args=["paletes_rede"])

        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Pallets da Rede")
        origin = PaleteRedeSaldo.objects.get(local_tipo="cd", local_codigo="806", tipo_palete="pbr")
        origin.quantidade = 10
        origin.save(update_fields=["quantidade", "atualizado_em"])

        response = self.client.post(
            url,
            {
                "action": "paletes_rede_movimentar",
                "operacao": "transferencia",
                "tipo_palete": "pbr",
                "destino_tipo": "loja",
                "destino_codigo": "01",
                "destino_nome": "01 - KRILL P. GRANDE",
                f"fonte_{origin.pk}": "4",
            },
        )

        self.assertEqual(response.status_code, 302)
        origin.refresh_from_db()
        self.assertEqual(origin.quantidade, 6)
        destination = PaleteRedeSaldo.objects.get(local_tipo="loja", local_codigo="01", local_nome="01 - KRILL P. GRANDE", tipo_palete="pbr")
        self.assertEqual(destination.quantidade, 4)
        self.assertEqual(PaleteRedeMovimentacao.objects.filter(tipo_palete="pbr", quantidade=4).count(), 1)

    def test_network_pallets_can_add_store_with_all_pallet_types(self):
        user = self.create_user("juliana.lojas", "analista", ["paletes_rede", "criar_registros", "consultar_registros", "editar_registros"])
        self.client.force_login(user)
        url = reverse("module_list", args=["paletes_rede"])

        response = self.client.post(
            url,
            {
                "action": "paletes_rede_adicionar_loja",
                "cd_referencia": "806",
                "loja_codigo": "88",
                "loja_nome": "88 - KRILL TESTE",
            },
        )

        self.assertEqual(response.status_code, 302)
        self.assertTrue(Loja.objects.filter(cd_unidade="806", codigo="88", nome="KRILL TESTE", ativa=True).exists())
        saldos = PaleteRedeSaldo.objects.filter(local_tipo="loja", local_codigo="88", local_nome="88 - KRILL TESTE")
        self.assertEqual(saldos.count(), 4)
        self.assertEqual(set(saldos.values_list("tipo_palete", flat=True)), {"pbr", "chep", "descartavel", "pbr2"})
        self.assertTrue(all(saldo.quantidade == 0 for saldo in saldos))

    def test_network_pallets_local_modal_renders_and_insert_adds_balance(self):
        user = self.create_user(
            "juliana.modal",
            "analista",
            ["paletes_rede", "consultar_registros", "editar_saldo_paletes_rede"],
        )
        self.client.force_login(user)
        url = reverse("module_list", args=["paletes_rede"])
        self.client.get(url)
        saldo = PaleteRedeSaldo.objects.get(local_tipo="cd", local_codigo="806", tipo_palete="pbr")
        local_key = f"{saldo.local_tipo}|{saldo.local_codigo}|{saldo.local_nome}"

        response = self.client.get(f"{url}?local_tipo=cd&local_key={local_key}&palete_modal=ver")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Subtela de pallets da rede")
        self.assertContains(response, "network-pallet-close-x")
        self.assertContains(response, "Ver saldo")

        response = self.client.post(
            url,
            {
                "action": "paletes_rede_saldo_rapido",
                "saldo_id": str(saldo.pk),
                "saldo_modo": "inserir",
                "quantidade_adicionar": "7",
                "responsavel": "Juliana",
            },
        )
        self.assertEqual(response.status_code, 302)
        self.assertIn("palete_modal=inserir", response["Location"])
        self.assertLess(response["Location"].index("?"), response["Location"].index("#paletes-rede-modal"))
        saldo.refresh_from_db()
        self.assertEqual(saldo.quantidade, 7)

    def test_network_pallets_report_xlsx_downloads(self):
        user = self.create_user(
            "juliana.xlsx",
            "analista",
            ["paletes_rede", "consultar_registros", "exportar_dados"],
        )
        self.client.force_login(user)
        response = self.client.get(reverse("module_list", args=["paletes_rede"]) + "?exportar=relatorio_rede_xlsx")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
        self.assertIn("relatorio_pallets_rede.xlsx", response["Content-Disposition"])

    def test_network_pallets_specific_permissions_control_actions(self):
        user = self.create_user(
            "juliana.rede.permissoes",
            "analista",
            [
                "paletes_rede",
                "consultar_registros",
                "adicionar_loja_paletes_rede",
                "editar_saldo_paletes_rede",
                "movimentar_paletes_rede",
            ],
        )
        self.client.force_login(user)
        url = reverse("module_list", args=["paletes_rede"])
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, "Adicionar loja da rede")
        self.assertContains(response, "Reservar ou retirar para fornecedor")
        self.assertContains(response, "Saldo por CD e loja")
        self.assertNotContains(response, "<h2>Preencher</h2>", html=True)

        response = self.client.post(
            url,
            {
                "action": "paletes_rede_adicionar_loja",
                "cd_referencia": "806",
                "loja_codigo": "89",
                "loja_nome": "KRILL PERMISSAO",
            },
        )
        self.assertEqual(response.status_code, 302)
        self.assertTrue(Loja.objects.filter(cd_unidade="806", codigo="89", nome="KRILL PERMISSAO", ativa=True).exists())

        saldo = PaleteRedeSaldo.objects.get(local_tipo="cd", local_codigo="806", tipo_palete="pbr")
        response = self.client.post(
            url,
            {
                "action": "paletes_rede_saldo_rapido",
                "saldo_id": str(saldo.pk),
                "quantidade": "12",
                "quebrados": "2",
                "reservados": "1",
                "responsavel": "Juliana",
            },
        )
        self.assertEqual(response.status_code, 302)
        saldo.refresh_from_db()
        self.assertEqual(saldo.quantidade, 12)
        self.assertEqual(saldo.quebrados, 2)
        response = self.client.post(
            url,
            {
                "action": "paletes_rede_movimentar",
                "operacao": "reserva",
                "tipo_palete": "pbr",
                "destino_tipo": "fornecedor",
                "destino_nome": "Fornecedor teste",
                f"fonte_{saldo.pk}": "3",
            },
        )
        self.assertEqual(response.status_code, 302)
        saldo.refresh_from_db()
        self.assertEqual(saldo.reservados, 4)
        self.assertEqual(PaleteRedeMovimentacao.objects.filter(tipo_palete="pbr", quantidade=3).count(), 1)

    def test_store_with_broken_forklift_requires_platform_vehicle(self):
        user = self.create_user(
            "frota.empilhadeira",
            "supervisor_frota",
            ["carregamento_veiculos", "vincular_cargas_expedicao", "consultar_registros"],
        )
        loja = Loja.objects.create(
            cd_unidade="806",
            codigo="55",
            nome="LOJA SEM EMPILHADEIRA",
            empilhadeira_indisponivel=True,
            ativa=True,
        )
        loja_label = f"{loja.codigo} - {loja.nome}"
        data_obj = timezone.localdate()
        ExpedicaoPlanejamento.objects.create(cd_unidade="806", data=data_obj, loja=loja_label, qtd_paletes=4)
        truck = VeiculoFrota.objects.create(
            cd_unidade="806",
            data=data_obj,
            motorista="Motorista Truck",
            placa="TRK1A23",
            tipo_caminhao="Truck",
            ativo=True,
        )
        plataforma = VeiculoFrota.objects.create(
            cd_unidade="806",
            data=data_obj,
            motorista="Motorista Plataforma",
            placa="PLT1A23",
            tipo_caminhao="Plataforma",
            plataforma_operacional=True,
            ativo=True,
        )
        self.client.force_login(user)

        payload = {
            "action": "vincular_carga_expedicao",
            "data": data_obj.isoformat(),
            "loja": loja_label,
            "qtd_paletes": "4",
            "periodo": "manha",
        }
        response = self.client.post(reverse("module_list", args=["carregamento_veiculos"]), {**payload, "veiculo_frota": str(truck.pk)})
        self.assertEqual(response.status_code, 302)
        self.assertFalse(ExpedicaoVinculo.objects.filter(loja=loja_label).exists())

        response = self.client.post(reverse("module_list", args=["carregamento_veiculos"]), {**payload, "veiculo_frota": str(plataforma.pk)})
        self.assertEqual(response.status_code, 302)
        vinculo = ExpedicaoVinculo.objects.get(loja=loja_label)
        self.assertTrue(vinculo.exige_plataforma)
        self.assertIn("Empilhadeira", vinculo.motivo_plataforma)

    def test_network_pallets_read_only_user_cannot_change_balance(self):
        user = self.create_user("juliana.rede.consulta", "analista", ["paletes_rede", "consultar_registros"])
        self.client.force_login(user)
        url = reverse("module_list", args=["paletes_rede"])
        self.client.get(url)
        saldo = PaleteRedeSaldo.objects.get(local_tipo="cd", local_codigo="806", tipo_palete="pbr")

        response = self.client.post(
            url,
            {
                "action": "paletes_rede_saldo_rapido",
                "saldo_id": str(saldo.pk),
                "quantidade": "99",
            },
        )

        self.assertEqual(response.status_code, 302)
        saldo.refresh_from_db()
        self.assertNotEqual(saldo.quantidade, 99)

    def test_network_pallets_reserve_requires_supplier_and_blocks_balance(self):
        user = self.create_user(
            "juliana.fornecedor",
            "analista",
            ["paletes_rede", "consultar_registros", "movimentar_paletes_rede"],
        )
        self.client.force_login(user)
        url = reverse("module_list", args=["paletes_rede"])
        self.client.get(url)
        saldo = PaleteRedeSaldo.objects.get(local_tipo="cd", local_codigo="806", tipo_palete="chep")
        saldo.quantidade = 20
        saldo.save(update_fields=["quantidade", "atualizado_em"])

        response = self.client.post(
            url,
            {
                "action": "paletes_rede_movimentar",
                "operacao": "reserva",
                "tipo_palete": "chep",
                f"fonte_{saldo.pk}": "5",
            },
        )
        self.assertEqual(response.status_code, 302)
        saldo.refresh_from_db()
        self.assertEqual(saldo.reservados, 0)

        response = self.client.post(
            url,
            {
                "action": "paletes_rede_movimentar",
                "operacao": "reserva",
                "tipo_palete": "chep",
                "destino_nome": "Fornecedor CHEP",
                f"fonte_{saldo.pk}": "5",
            },
        )

        self.assertEqual(response.status_code, 302)
        saldo.refresh_from_db()
        self.assertEqual(saldo.reservados, 5)
        self.assertEqual(saldo.disponivel, 15)
        movimento = PaleteRedeMovimentacao.objects.get(tipo_palete="chep", quantidade=5)
        self.assertEqual(movimento.destino_tipo, "fornecedor")
        self.assertEqual(movimento.destino_nome, "Fornecedor CHEP")

    def test_ready_store_can_be_linked_to_vehicle(self):
        user = self.create_user(
            "frota.pronta",
            "supervisor_frota",
            ["lojas_prontas_carregamento", "acompanhar_lojas_prontas_carregamento", "vincular_cargas_expedicao"],
        )
        store = Loja.objects.filter(cd_unidade="806", ativa=True).first()
        if store is None:
            store = Loja.objects.create(cd_unidade="806", codigo="97", nome="Loja pronta", ativa=True)
        loja = str(store)
        ExpedicaoPlanejamento.objects.create(cd_unidade="806", data="2026-07-15", loja=loja, qtd_paletes=10)
        veiculo = VeiculoFrota.objects.create(cd_unidade="806", data="2026-07-15", motorista="Motorista Pronta", placa="RTP1A23", tipo_caminhao="Truck", ativo=True)
        solicitacao = SolicitacaoCargaPronta.objects.create(
            cd_unidade="806",
            data="2026-07-15",
            loja=loja,
            qtd_paletes=10,
            periodo="manha",
            criado_por=user,
        )
        self.client.force_login(user)
        response = self.client.post(
            reverse("module_list", args=["lojas_prontas_carregamento"]),
            {
                "action": "vincular_loja_pronta_carregamento",
                "solicitacao_id": solicitacao.pk,
                "data": "2026-07-15",
                "loja": loja,
                "veiculo_frota": veiculo.pk,
                "qtd_paletes": "10",
            },
        )
        self.assertEqual(response.status_code, 302)
        vinculo = ExpedicaoVinculo.objects.get(loja=loja)
        self.assertEqual(vinculo.qtd_paletes, 10)
        self.assertEqual(vinculo.placa, "RTP1A23")
        solicitacao.refresh_from_db()
        self.assertEqual(solicitacao.status, "em_carregamento")

    def test_ready_store_link_can_be_confirmed_after_balance_was_adjusted(self):
        user = self.create_user(
            "expedicao.confirma.pronta",
            "master",
            ["expedicao", "criar_registros", "consultar_registros", "vincular_cargas_expedicao"],
        )
        PerfilAcesso.objects.filter(user=user).update(cd_padrao="801")
        loja = "01 - KRILL P. GRANDE"
        data_obj = timezone.localdate()
        ExpedicaoPlanejamento.objects.create(cd_unidade="801", data=data_obj, loja=loja, qtd_paletes=0, status="distribuido")
        vinculo = ExpedicaoVinculo.objects.create(
            cd_unidade="801",
            data=data_obj,
            loja=loja,
            qtd_paletes=5,
            motorista="Allan",
            placa="STH8D95",
            periodo="manha",
            status="vinculado",
            observacao="Vinculo criado a partir do aviso de loja pronta #12.",
            criado_por=user,
        )
        self.client.force_login(user)
        session = self.client.session
        session["cd_unidade"] = "801"
        session.save()

        response = self.client.post(
            reverse("module_list", args=["expedicao"]),
            {
                "action": "confirmar_vinculo_expedicao",
                "vinculo_id": vinculo.pk,
                "data": data_obj.isoformat(),
                "loja": loja,
            },
        )

        self.assertEqual(response.status_code, 302)
        vinculo.refresh_from_db()
        self.assertEqual(vinculo.status, "carregado")
        self.assertTrue(Expedicao.objects.filter(loja=loja, qtd_paletes=5).exists())

    def test_pallet_summary_shows_and_cancels_open_reservations(self):
        user = self.create_user(
            "master.cancela.reserva",
            "master",
            ["relatorio_paletes_cd", "consultar_registros", "corrigir_saldo_paletes", "vincular_cargas_expedicao"],
        )
        loja = "14 - KRILL BERTIOGA"
        data_obj = timezone.localdate()
        reserva_data = data_obj - timedelta(days=4)
        ExpedicaoPlanejamento.objects.create(cd_unidade="806", data=reserva_data, loja=loja, qtd_paletes=20)
        vinculo = ExpedicaoVinculo.objects.create(
            cd_unidade="806",
            data=reserva_data,
            loja=loja,
            qtd_paletes=14,
            motorista="Motorista Teste",
            placa="ABC1D23",
            periodo="manha",
            status="vinculado",
            criado_por=user,
        )
        self.client.force_login(user)

        response = self.client.get(reverse("module_list", args=["relatorio_paletes_cd"]) + f"?data={data_obj.isoformat()}")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Reservas abertas")
        self.assertContains(response, loja)
        self.assertContains(response, "14 pallet")
        self.assertContains(response, reserva_data.strftime("%d/%m/%Y"))
        self.assertContains(response, "Cancelar reserva")

        response = self.client.post(
            reverse("module_list", args=["relatorio_paletes_cd"]),
            {
                "action": "cancelar_vinculo_expedicao",
                "vinculo_id": vinculo.pk,
                "data": data_obj.isoformat(),
                "loja": loja,
                "motivo_cancelamento": "Teste",
            },
        )
        self.assertEqual(response.status_code, 302)
        vinculo.refresh_from_db()
        self.assertEqual(vinculo.status, "cancelado")

    def test_ready_store_notice_and_queue_are_separate_modes(self):
        user = self.create_user(
            "master.fila.pronta",
            "master",
            ["lojas_prontas_carregamento", "acompanhar_lojas_prontas_carregamento", "vincular_cargas_expedicao", "consultar_registros"],
        )
        self.client.force_login(user)

        response = self.client.get(reverse("module_list", args=["lojas_prontas_carregamento"]) + "?modo=form")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Enviar para Frota")
        self.assertNotContains(response, "Fila de carregamento")

        response = self.client.get(reverse("module_list", args=["lojas_prontas_carregamento"]) + "?modo=fila")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Fila de carregamento")
        self.assertNotContains(response, "Loja selecionada")
        self.assertNotContains(response, ">Atualizar</button>")

    def test_fleet_can_open_ready_store_queue_without_notice_permission(self):
        user = self.create_user(
            "marcela.fila.pronta",
            "assistente",
            ["painel_frota", "acompanhar_lojas_prontas_carregamento", "vincular_cargas_expedicao", "consultar_registros"],
        )
        self.client.force_login(user)

        response = self.client.get(reverse("module_list", args=["lojas_prontas_carregamento"]) + "?modo=fila")

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Fila de carregamento")
        self.assertNotContains(response, "Você não tem acesso a esta tela")

    def test_frota_hub_ready_store_counter_uses_selected_day(self):
        user = self.create_user(
            "marcela.contador.pronta",
            "supervisor_frota",
            ["painel_frota", "acompanhar_lojas_prontas_carregamento", "vincular_cargas_expedicao", "consultar_registros"],
        )
        today = timezone.localdate()
        old_day = today - timedelta(days=1)
        for index in range(4):
            SolicitacaoCargaPronta.objects.create(
                cd_unidade="806",
                data=old_day,
                loja=f"{index + 1:02d} - LOJA ANTIGA",
                qtd_paletes=1,
                periodo="manha",
                status="pronta",
                criado_por=user,
            )
        self.client.force_login(user)

        response = self.client.get(reverse("frota_hub"), {"data": today.isoformat()})

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "0 pronta(s)")
        self.assertNotContains(response, "4 pronta(s)")

    def test_ready_store_queue_can_finish_loading_without_bluesoft_message(self):
        set_config("feature_faturamento_expedicao", "nao")
        user = self.create_user(
            "mauricio.finaliza.pronta",
            "supervisor_frota",
            ["painel_frota", "acompanhar_lojas_prontas_carregamento", "vincular_cargas_expedicao", "consultar_registros"],
        )
        SolicitacaoCargaPronta.objects.create(
            cd_unidade="806",
            data=timezone.localdate(),
            loja="01 - KRILL P. GRANDE",
            qtd_paletes=10,
            periodo="manha",
            status="em_carregamento",
            resposta="Vinculada ao caminhao STH8D95: 10 pallet(s).",
            criado_por=user,
        )
        self.client.force_login(user)

        response = self.client.get(reverse("module_list", args=["lojas_prontas_carregamento"]) + "?modo=fila")

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Concluir carregamento")
        self.assertNotContains(response, "Valores BlueSoft recebidos")
        self.assertNotContains(response, "Valor restrito")

    def test_supervisor_can_close_open_driver_checklist(self):
        user = self.create_user(
            "supervisor.fecha.retorno",
            "supervisor_frota",
            ["painel_frota", "checklist_frota", "consultar_registros"],
        )
        saida = ChecklistFrota.objects.create(
            cd_unidade="806",
            data=timezone.localdate(),
            tipo_checklist="saida",
            motorista="Allan",
            veiculo="Plataforma",
            placa="STH8D95",
            loja_destino="01 - KRILL P. GRANDE",
            acompanhar_retorno=True,
        )
        self.client.force_login(user)

        response = self.client.post(
            reverse("module_list", args=["checklist_frota"]),
            {
                "action": "dispensar_retorno",
                "saida_id": saida.pk,
                "motivo": "Encerrado pelo supervisor para corrigir demonstracao.",
            },
            follow=True,
        )

        self.assertEqual(response.status_code, 200)
        saida.refresh_from_db()
        self.assertTrue(saida.retorno_dispensado)

    def test_management_panel_only_shows_open_linked_trucks(self):
        user = self.create_user("leilane.gerencial", "assistente", ["painel_gestao", "visualizar_cds_unificados", "consultar_registros"])
        data_obj = timezone.localdate()
        ExpedicaoVinculo.objects.create(
            cd_unidade="806",
            data=data_obj,
            loja="14 - KRILL BERTIOGA",
            qtd_paletes=14,
            placa="GAQ4571",
            motorista="Godoi",
            status="vinculado",
        )
        ExpedicaoVinculo.objects.create(
            cd_unidade="806",
            data=data_obj,
            loja="14 - KRILL BERTIOGA",
            qtd_paletes=14,
            placa="STR6A35",
            motorista="Anderson",
            status="carregado",
        )
        self.client.force_login(user)

        response = self.client.get(reverse("painel_gestao"), {"inicio": data_obj.isoformat(), "fim": data_obj.isoformat(), "cd_escopo": "806"})

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Godoi - GAQ4571")
        self.assertContains(response, "A caminho")
        self.assertNotContains(response, "Anderson - STR6A35")
        self.assertNotContains(response, "Carregado")

    def test_management_panel_shows_current_store_pallet_balance(self):
        user = self.create_user("leilane.saldo.atual", "assistente", ["painel_gestao", "consultar_registros"])
        data_obj = timezone.localdate()
        Loja.objects.update_or_create(cd_unidade="806", codigo="01", defaults={"nome": "KRILL P. GRANDE", "ativa": True})
        ExpedicaoPlanejamento.objects.create(
            cd_unidade="806",
            data=data_obj - timedelta(days=5),
            loja="01 - KRILL P. GRANDE",
            qtd_paletes=12,
            status="planejado",
            criado_por=user,
        )
        self.client.force_login(user)

        response = self.client.get(reverse("painel_gestao"), {"inicio": data_obj.isoformat(), "fim": data_obj.isoformat(), "cd_escopo": "806"})

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Saldo atual por loja")
        self.assertContains(response, "01 - KRILL P. GRANDE")
        self.assertContains(response, "12 livre(s)")

    def test_unified_cd_model_forms_keep_store_selectors(self):
        Loja.objects.update_or_create(cd_unidade="801", codigo="01", defaults={"nome": "KRILL P. GRANDE", "ativa": True})
        Loja.objects.update_or_create(cd_unidade="806", codigo="02", defaults={"nome": "MERI KRILL", "ativa": True})
        form_class = build_model_form(ExpedicaoPlanejamento, ["data", "loja", "qtd_paletes"])

        form = form_class(cd_unidade="801+806")

        rendered = str(form["loja"])
        self.assertIn("<select", rendered)
        self.assertIn("01 - KRILL P. GRANDE", rendered)
        self.assertIn("02 - MERI KRILL", rendered)

    def test_cd806_ready_store_notice_updates_pallet_balance(self):
        user = self.create_user(
            "expedicao.pronta.806",
            "assistente",
            ["lojas_prontas_carregamento", "criar_registros", "consultar_registros"],
        )
        Loja.objects.update_or_create(cd_unidade="806", codigo="14", defaults={"nome": "KRILL BERTIOGA", "ativa": True})
        self.client.force_login(user)
        data_obj = timezone.localdate()

        response = self.client.post(
            reverse("module_list", args=["lojas_prontas_carregamento"]),
            {
                "action": "registrar_loja_pronta_carregamento",
                "data": data_obj.isoformat(),
                "loja": "14 - KRILL BERTIOGA",
                "qtd_paletes": "14",
                "periodo": "manha",
                "observacao": "carga pronta",
            },
        )

        self.assertEqual(response.status_code, 302)
        saldo = ExpedicaoPlanejamento.objects.get(cd_unidade="806", loja="14 - KRILL BERTIOGA")
        self.assertEqual(saldo.data, data_obj)
        self.assertEqual(saldo.qtd_paletes, 14)
        self.assertEqual(saldo.status, "planejado")

    def test_cd801_ready_store_notice_decrements_pallet_balance(self):
        user = self.create_user(
            "expedicao.pronta.801",
            "assistente",
            ["lojas_prontas_carregamento", "criar_registros", "consultar_registros"],
        )
        PerfilAcesso.objects.filter(user=user).update(cd_padrao="801")
        Loja.objects.update_or_create(cd_unidade="801", codigo="01", defaults={"nome": "KRILL P. GRANDE", "ativa": True})
        data_obj = timezone.localdate()
        ExpedicaoPlanejamento.objects.create(
            cd_unidade="801",
            data=data_obj,
            loja="01 - KRILL P. GRANDE",
            qtd_paletes=20,
            status="planejado",
            criado_por=user,
        )
        self.client.force_login(user)

        response = self.client.post(
            reverse("module_list", args=["lojas_prontas_carregamento"]),
            {
                "action": "registrar_loja_pronta_carregamento",
                "data": data_obj.isoformat(),
                "loja": "01 - KRILL P. GRANDE",
                "qtd_paletes": "10",
                "saldo_restante_801": "4",
                "periodo": "manha",
                "observacao": "parte liberada",
            },
        )

        self.assertEqual(response.status_code, 302)
        saldo = ExpedicaoPlanejamento.objects.get(cd_unidade="801", loja="01 - KRILL P. GRANDE")
        self.assertEqual(saldo.qtd_paletes, 4)
        self.assertEqual(saldo.status, "planejado")

    def test_cd801_ready_store_notice_requires_box_balance(self):
        user = self.create_user(
            "expedicao.pronta.sem.saldo",
            "assistente",
            ["lojas_prontas_carregamento", "criar_registros", "consultar_registros"],
        )
        PerfilAcesso.objects.filter(user=user).update(cd_padrao="801")
        Loja.objects.update_or_create(cd_unidade="801", codigo="28", defaults={"nome": "KRILL MARACANA", "ativa": True})
        self.client.force_login(user)
        data_obj = timezone.localdate()

        response = self.client.post(
            reverse("module_list", args=["lojas_prontas_carregamento"]),
            {
                "action": "registrar_loja_pronta_carregamento",
                "data": data_obj.isoformat(),
                "loja": "28 - KRILL MARACANA",
                "qtd_paletes": "10",
                "periodo": "manha",
            },
        )

        self.assertEqual(response.status_code, 302)
        self.assertFalse(SolicitacaoCargaPronta.objects.filter(loja="28 - KRILL MARACANA").exists())

    def test_driver_route_ignores_driver_default_cd(self):
        motorista = self.create_user(
            "allan.motorista",
            "motorista",
            ["checklist_frota", "criar_registros", "consultar_registros"],
        )
        PerfilAcesso.objects.filter(user=motorista).update(cd_padrao="801")
        data_obj = timezone.localdate()
        VeiculoFrota.objects.create(
            cd_unidade="806",
            data=data_obj,
            motorista="Allan",
            placa="STH8D95",
            tipo_caminhao="Plataforma",
            usuario_motorista=motorista,
            ativo=True,
        )
        ExpedicaoVinculo.objects.create(
            cd_unidade="806",
            data=data_obj,
            loja="01 - KRILL P. GRANDE",
            motorista="Allan",
            placa="STH8D95",
            qtd_paletes=16,
            periodo="manha",
            status="vinculado",
        )

        route = driver_route_from_fleet_links(motorista, "801", data_obj)

        self.assertIsNotNone(route)
        self.assertEqual(route["cds"], ["806"])
        self.assertEqual(route["stops"][0]["loja"], "01 - KRILL P. GRANDE")

    def test_driver_route_allows_driver_to_choose_pending_store(self):
        motorista = self.create_user(
            "allan.escolhe.loja",
            "motorista",
            ["checklist_frota", "criar_registros", "consultar_registros"],
        )
        data_obj = timezone.localdate()
        VeiculoFrota.objects.create(
            cd_unidade="806",
            data=data_obj,
            motorista="Allan",
            placa="STH8D95",
            tipo_caminhao="Plataforma",
            usuario_motorista=motorista,
            ativo=True,
        )
        for loja in ["01 - KRILL P. GRANDE", "16 - ROCHA PRAIA GRANDE"]:
            ExpedicaoVinculo.objects.create(
                cd_unidade="806",
                data=data_obj,
                loja=loja,
                motorista="Allan",
                placa="STH8D95",
                qtd_paletes=8,
                periodo="manha",
                status="vinculado",
            )
        ChecklistFrota.objects.create(
            cd_unidade="806",
            data=data_obj,
            tipo_checklist="saida",
            motorista="Allan",
            veiculo="Plataforma",
            placa="STH8D95",
            loja_destino="01 - KRILL P. GRANDE + 16 - ROCHA PRAIA GRANDE",
            acompanhar_retorno=True,
        )

        route = driver_route_from_fleet_links(motorista, "806", data_obj)

        self.assertEqual(route["next_type"], "chegada_loja")
        self.assertEqual({item["loja"] for item in route["candidate_stops"]}, {"01 - KRILL P. GRANDE", "16 - ROCHA PRAIA GRANDE"})

    def test_ready_store_bulk_link_multiple_stores_to_same_vehicle(self):
        user = self.create_user(
            "mauricio.bulk.pronta",
            "supervisor_frota",
            ["painel_frota", "acompanhar_lojas_prontas_carregamento", "vincular_cargas_expedicao", "consultar_registros", "visualizar_cds_unificados"],
        )
        PerfilAcesso.objects.filter(user=user).update(cd_padrao="801+806")
        data_obj = timezone.localdate()
        veiculo = VeiculoFrota.objects.create(cd_unidade="801", data=data_obj, motorista="Allan", placa="BLK1A23", tipo_caminhao="Plataforma", ativo=True)
        s1 = SolicitacaoCargaPronta.objects.create(cd_unidade="801", data=data_obj, loja="01 - KRILL P. GRANDE", qtd_paletes=10, periodo="manha", criado_por=user)
        s2 = SolicitacaoCargaPronta.objects.create(cd_unidade="806", data=data_obj, loja="16 - ROCHA PRAIA GRANDE", qtd_paletes=6, periodo="manha", criado_por=user)
        self.client.force_login(user)
        session = self.client.session
        session["cd_unidade"] = "801+806"
        session.save()

        response = self.client.post(
            reverse("module_list", args=["lojas_prontas_carregamento"]),
            {
                "action": "vincular_loja_pronta_multi_cd",
                "data": data_obj.isoformat(),
                "solicitacao_ids": [str(s1.pk), str(s2.pk)],
                f"qtd_paletes_{s1.pk}": "10",
                f"qtd_paletes_{s2.pk}": "6",
                "veiculo_frota": str(veiculo.pk),
            },
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(ExpedicaoVinculo.objects.filter(placa="BLK1A23", status="vinculado").count(), 2)
        self.assertEqual(SolicitacaoCargaPronta.objects.filter(status="em_carregamento").count(), 2)

    def test_ready_store_queue_uses_compact_load_builder(self):
        user = self.create_user(
            "supervisor.compacto",
            "supervisor_frota",
            ["painel_frota", "lojas_prontas_carregamento", "acompanhar_lojas_prontas_carregamento", "vincular_cargas_expedicao", "consultar_registros", "visualizar_cds_unificados"],
        )
        PerfilAcesso.objects.filter(user=user).update(cd_padrao="801+806")
        data_obj = timezone.localdate()
        SolicitacaoCargaPronta.objects.create(cd_unidade="801", data=data_obj, loja="01 - KRILL P. GRANDE", qtd_paletes=10, periodo="manha", criado_por=user)
        self.client.force_login(user)
        session = self.client.session
        session["cd_unidade"] = "801+806"
        session.save()

        response = self.client.get(reverse("module_list", args=["lojas_prontas_carregamento"]) + "?modo=fila")

        self.assertContains(response, "Montar carga")
        self.assertContains(response, "data-ready-load-pick")
        self.assertContains(response, "data-ready-load-open")
        self.assertContains(response, "ready-load-bulk-modal")
        self.assertNotContains(response, "Vincular caminhão por seleção")

    def test_ready_store_quantity_correction_updates_806_balance(self):
        user = self.create_user(
            "supervisor.corrige.pronta",
            "supervisor_frota",
            ["acompanhar_lojas_prontas_carregamento", "consultar_registros"],
        )
        data_obj = timezone.localdate()
        solicitacao = SolicitacaoCargaPronta.objects.create(cd_unidade="806", data=data_obj, loja="01 - KRILL P. GRANDE", qtd_paletes=10, periodo="manha", criado_por=user)
        ExpedicaoPlanejamento.objects.create(cd_unidade="806", data=data_obj, loja=solicitacao.loja, qtd_paletes=10, status="planejado")
        self.client.force_login(user)

        response = self.client.post(
            reverse("module_list", args=["lojas_prontas_carregamento"]),
            {
                "action": "corrigir_loja_pronta_carregamento",
                "solicitacao_id": solicitacao.pk,
                "data": data_obj.isoformat(),
                "loja": solicitacao.loja,
                "qtd_paletes": "7",
            },
        )

        self.assertEqual(response.status_code, 302)
        solicitacao.refresh_from_db()
        self.assertEqual(solicitacao.qtd_paletes, 7)
        self.assertEqual(ExpedicaoPlanejamento.objects.get(cd_unidade="806", loja=solicitacao.loja).qtd_paletes, 7)

    def test_ready_store_can_be_released_to_change_vehicle(self):
        user = self.create_user(
            "mauricio.troca.pronta",
            "supervisor_frota",
            ["acompanhar_lojas_prontas_carregamento", "vincular_cargas_expedicao", "consultar_registros"],
        )
        data_obj = timezone.localdate()
        solicitacao = SolicitacaoCargaPronta.objects.create(cd_unidade="806", data=data_obj, loja="01 - KRILL P. GRANDE", qtd_paletes=10, periodo="manha", status="em_carregamento", criado_por=user)
        ExpedicaoVinculo.objects.create(cd_unidade="806", data=data_obj, loja=solicitacao.loja, qtd_paletes=10, motorista="Allan", placa="STH8D95", periodo="manha", status="vinculado", observacao=f"Vinculo criado a partir do aviso de loja pronta #{solicitacao.pk}.")
        self.client.force_login(user)

        response = self.client.post(
            reverse("module_list", args=["lojas_prontas_carregamento"]),
            {
                "action": "liberar_troca_caminhao_loja_pronta",
                "solicitacao_id": solicitacao.pk,
                "data": data_obj.isoformat(),
                "loja": solicitacao.loja,
                "motivo": "Motorista alterado pela Frota.",
            },
        )

        self.assertEqual(response.status_code, 302)
        solicitacao.refresh_from_db()
        self.assertEqual(solicitacao.status, "pronta")
        self.assertEqual(ExpedicaoVinculo.objects.get(loja=solicitacao.loja).status, "cancelado")

    def test_cancel_ready_store_notice_releases_linked_reserved_balance(self):
        user = self.create_user(
            "mauricio.cancela.pronta",
            "supervisor_frota",
            ["acompanhar_lojas_prontas_carregamento", "vincular_cargas_expedicao", "consultar_registros"],
        )
        PerfilAcesso.objects.filter(user=user).update(cd_padrao="801")
        data_obj = timezone.localdate()
        solicitacao = SolicitacaoCargaPronta.objects.create(
            cd_unidade="801",
            data=data_obj,
            loja="01 - KRILL P. GRANDE",
            qtd_paletes=5,
            periodo="manha",
            status="em_carregamento",
            criado_por=user,
        )
        vinculo = ExpedicaoVinculo.objects.create(
            cd_unidade="801",
            data=data_obj,
            loja=solicitacao.loja,
            qtd_paletes=5,
            motorista="Allan",
            placa="STH8D95",
            periodo="manha",
            status="vinculado",
            observacao=f"Vinculo criado a partir do aviso de loja pronta #{solicitacao.pk}.",
            criado_por=user,
        )
        self.client.force_login(user)
        session = self.client.session
        session["cd_unidade"] = "801"
        session.save()

        response = self.client.post(
            reverse("module_list", args=["lojas_prontas_carregamento"]),
            {
                "action": "atualizar_loja_pronta_carregamento",
                "solicitacao_id": solicitacao.pk,
                "data": data_obj.isoformat(),
                "loja": solicitacao.loja,
                "status": "cancelada",
                "resposta": "Cancelado no teste.",
            },
        )

        self.assertEqual(response.status_code, 302)
        vinculo.refresh_from_db()
        self.assertEqual(vinculo.status, "cancelado")

    def test_unified_ready_store_notice_requires_operation_cd(self):
        user = self.create_user(
            "supervisor.pronta.unificada",
            "supervisor",
            ["lojas_prontas_carregamento", "visualizar_cds_unificados", "criar_registros", "consultar_registros"],
        )
        PerfilAcesso.objects.filter(user=user).update(cd_padrao="801+806")
        Loja.objects.update_or_create(cd_unidade="806", codigo="14", defaults={"nome": "KRILL BERTIOGA", "ativa": True})
        data_obj = timezone.localdate()
        self.client.force_login(user)

        response = self.client.post(
            reverse("module_list", args=["lojas_prontas_carregamento"]),
            {
                "action": "registrar_loja_pronta_carregamento",
                "data": data_obj.isoformat(),
                "loja": "14 - KRILL BERTIOGA",
                "qtd_paletes": "14",
                "periodo": "manha",
            },
        )

        self.assertEqual(response.status_code, 302)
        self.assertFalse(SolicitacaoCargaPronta.objects.exists())
        self.assertFalse(ExpedicaoPlanejamento.objects.exists())

    def test_unified_truck_request_requires_origin_cd(self):
        user = self.create_user(
            "supervisor.caminhoes.unificado",
            "supervisor",
            ["solicitacao_caminhoes", "visualizar_cds_unificados", "criar_registros", "consultar_registros"],
        )
        PerfilAcesso.objects.filter(user=user).update(cd_padrao="801+806")
        self.client.force_login(user)
        data_obj = timezone.localdate()

        response = self.client.post(
            reverse("module_list", args=["solicitacao_caminhoes"]),
            {
                "action": "solicitar_caminhoes_cd",
                "data": data_obj.isoformat(),
                "data_necessidade": (data_obj + timedelta(days=1)).isoformat(),
                "cd_destino": "806",
                "qtd_caminhoes": "2",
                "periodo": "manha",
            },
        )

        self.assertEqual(response.status_code, 302)
        self.assertFalse(SolicitacaoCaminhaoCD.objects.exists())

    def test_fleet_link_notifies_expedition_to_launch_in_bluesoft(self):
        frota_user = self.create_user(
            "frota.define.motorista",
            "supervisor_frota",
            ["carregamento_veiculos", "vincular_cargas_expedicao"],
        )
        inspecao_user = self.create_user(
            "vanessa.inspecao",
            "assistente",
            ["expedicao", "receber_alertas_expedicao"],
        )
        loja = "01 - KRILL P. GRANDE"
        data_obj = timezone.localdate()
        ExpedicaoPlanejamento.objects.create(cd_unidade="806", data=data_obj, loja=loja, qtd_paletes=16)
        veiculo = VeiculoFrota.objects.create(
            cd_unidade="806",
            data=data_obj,
            motorista="Alan Prost",
            placa="STH8D95",
            tipo_caminhao="DAF",
            ativo=True,
        )
        self.client.force_login(frota_user)

        response = self.client.post(
            reverse("module_list", args=["carregamento_veiculos"]),
            {
                "action": "vincular_carga_expedicao",
                "data": data_obj.isoformat(),
                "loja": loja,
                "veiculo_frota": str(veiculo.pk),
                "qtd_paletes": "16",
                "periodo": "manha",
            },
        )

        self.assertEqual(response.status_code, 302)
        self.assertTrue(ExpedicaoVinculo.objects.filter(loja=loja, motorista="Alan Prost").exists())
        notification = SistemaNotificacao.objects.get(usuario=inspecao_user)
        self.assertIn("BlueSoft", notification.mensagem)
        self.assertIn("Alan Prost", notification.mensagem)
        self.assertIn("16 pallet", notification.mensagem)

    def test_manual_loading_request_requires_fleet_approval(self):
        expedicao_user = self.create_user(
            "expedicao.806",
            "assistente",
            ["expedicao", "solicitar_lancamento_manual_expedicao", "criar_registros"],
        )
        frota_user = self.create_user(
            "mauricio.junior",
            "supervisor_frota",
            ["aprovacoes_carregamento", "aprovar_lancamento_manual_expedicao", "consultar_registros"],
        )
        ExpedicaoPlanejamento.objects.create(
            cd_unidade="806",
            data="2026-07-11",
            loja="01 - KRILL P. GRANDE",
            qtd_paletes=10,
        )

        self.client.force_login(expedicao_user)
        response = self.client.post(
            reverse("module_list", args=["expedicao"]),
            {
                "action": "solicitar_lancamento_manual_expedicao",
                "data": "2026-07-11",
                "loja": "01 - KRILL P. GRANDE",
                "qtd_paletes": 4,
                "placa": "ABC1D23",
                "motorista": "Motorista Teste",
                "periodo": "manha",
                "motivo": "Teste operacional",
            },
        )
        self.assertEqual(response.status_code, 302)
        solicitacao = SolicitacaoCarregamentoManual.objects.get()
        self.assertEqual(solicitacao.status, "pendente")
        self.assertEqual(Expedicao.objects.count(), 0)

        self.client.force_login(frota_user)
        response = self.client.get(reverse("module_list", args=["aprovacoes_carregamento"]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Fila de aprova")
        response = self.client.post(
            reverse("module_list", args=["aprovacoes_carregamento"]),
            {
                "action": "aprovar_carregamento_manual",
                "solicitacao_id": solicitacao.pk,
                "resposta": "Aprovado no teste",
            },
        )
        self.assertEqual(response.status_code, 302)
        solicitacao.refresh_from_db()
        planejamento = ExpedicaoPlanejamento.objects.get()
        self.assertEqual(solicitacao.status, "concluido")
        self.assertEqual(Expedicao.objects.get().qtd_paletes, 4)
        self.assertEqual(planejamento.qtd_paletes, 6)

    def test_pallet_balance_can_be_corrected_to_zero_when_store_has_old_rows(self):
        user = self.create_user(
            "saldo.master",
            "master",
            ["expedicao_planejamento", "criar_registros", "corrigir_saldo_paletes", "consultar_registros"],
        )
        loja = "01 - KRILL P. GRANDE"
        ExpedicaoPlanejamento.objects.create(
            cd_unidade="806",
            data="2026-07-10",
            loja=loja,
            qtd_paletes=80,
            status="planejado",
        )
        ExpedicaoPlanejamento.objects.create(
            cd_unidade="806",
            data="2026-07-11",
            loja=loja,
            qtd_paletes=80,
            status="planejado",
        )

        self.client.force_login(user)
        response = self.client.post(
            reverse("module_list", args=["expedicao_planejamento"]),
            {
                "action": "salvar_planejamento_expedicao",
                "loja": loja,
                "qtd_paletes": "0",
            },
        )

        self.assertEqual(response.status_code, 302)
        registros = list(ExpedicaoPlanejamento.objects.filter(cd_unidade="806", loja=loja))
        self.assertEqual(len(registros), 1)
        self.assertEqual(registros[0].qtd_paletes, 0)
        self.assertEqual(registros[0].status, "cancelado")

    def test_fleet_hub_groups_fleet_work_without_exposing_empty_sidebar(self):
        user = self.create_user(
            "mauricio.hub",
            "supervisor_frota",
            [
                "painel_frota",
                "checklist_frota",
                "carregamento_veiculos",
                "aprovacoes_carregamento",
                "veiculos_frota",
                "consultar_registros",
                "visualizar_cds_unificados",
                "acompanhar_lojas_prontas_carregamento",
                "vincular_cargas_expedicao",
            ],
        )
        self.client.force_login(user)

        response = self.client.get(reverse("frota_hub"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Central da Frota")
        self.assertContains(response, "Rotina da Frota")
        self.assertContains(response, "Carregamentos")
        self.assertContains(response, "Enviar caminh")
        self.assertContains(response, "Consultas e Cadastros")

    def test_cd_can_see_trucks_sent_to_its_destination(self):
        user = self.create_user(
            "leilane.cd806",
            "assistente",
            ["solicitacao_caminhoes", "consultar_registros"],
        )
        SolicitacaoCaminhaoCD.objects.create(
            cd_unidade="801",
            cd_destino="806",
            data=timezone.localdate(),
            data_necessidade=timezone.localdate(),
            qtd_caminhoes=1,
            periodo="manha",
            status="em_andamento",
            motoristas_enviados="Motorista Teste - ABC1D23",
            resposta="Indo para o CD 806.",
        )
        self.client.force_login(user)

        response = self.client.get(reverse("module_list", args=["solicitacao_caminhoes"]))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Caminhoes vindo para este CD")
        self.assertContains(response, "Motorista Teste - ABC1D23")
        self.assertContains(response, "Destino CD 806")

    def test_truck_request_response_accepts_multiple_drivers(self):
        user = self.create_user(
            "marcela.responde",
            "assistente",
            ["solicitacao_caminhoes", "tratar_solicitacao_caminhoes", "consultar_registros"],
        )
        solicitacao = SolicitacaoCaminhaoCD.objects.create(
            cd_unidade="806",
            cd_destino="806",
            data=timezone.localdate(),
            data_necessidade=timezone.localdate() + timedelta(days=1),
            qtd_caminhoes=6,
            periodo="manha",
            status="pendente",
            criado_por=user,
        )
        primeiro = VeiculoFrota.objects.create(
            cd_unidade="806",
            data=timezone.localdate(),
            motorista="Motorista Um",
            placa="AAA1A11",
            tipo_caminhao="Truck",
            ativo=True,
        )
        segundo = VeiculoFrota.objects.create(
            cd_unidade="806",
            data=timezone.localdate(),
            motorista="Motorista Dois",
            placa="BBB2B22",
            tipo_caminhao="Truck",
            ativo=True,
        )
        self.client.force_login(user)

        response = self.client.post(
            reverse("module_list", args=["solicitacao_caminhoes"]),
            {
                "action": "responder_solicitacao_caminhoes",
                "solicitacao_id": solicitacao.pk,
                "status": "em_andamento",
                "motoristas_enviados": [str(primeiro.pk), str(segundo.pk)],
                "resposta": "Enviar dois motoristas.",
            },
        )

        self.assertEqual(response.status_code, 302)
        solicitacao.refresh_from_db()
        self.assertIn("Motorista Um - AAA1A11", solicitacao.motoristas_enviados)
        self.assertIn("Motorista Dois - BBB2B22", solicitacao.motoristas_enviados)

    def test_truck_request_shows_ready_load_suggestions(self):
        user = self.create_user(
            "marcela.sugestao",
            "assistente",
            ["solicitacao_caminhoes", "tratar_solicitacao_caminhoes", "consultar_registros"],
        )
        SolicitacaoCaminhaoCD.objects.create(
            cd_unidade="806",
            cd_destino="806",
            data=timezone.localdate(),
            data_necessidade=timezone.localdate(),
            qtd_caminhoes=1,
            periodo="manha",
            status="pendente",
            criado_por=user,
        )
        SolicitacaoCargaPronta.objects.create(
            cd_unidade="806",
            data=timezone.localdate(),
            loja="01 - KRILL P. GRANDE",
            qtd_paletes=10,
            periodo="manha",
            status="pronta",
        )
        self.client.force_login(user)

        response = self.client.get(reverse("module_list", args=["solicitacao_caminhoes"]))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Lojas prontas sugeridas")
        self.assertContains(response, "01 - KRILL P. GRANDE")
        self.assertContains(response, "10 pallet(s)")
