from django.contrib import admin

from . import models


for model in [
    models.PerfilAcesso,
    models.AuditLog,
    models.SolicitacaoSenha,
    models.SolicitacaoAcesso,
    models.ConfiguracaoSistema,
    models.BackupLog,
    models.Loja,
    models.ProdutoGtin,
    models.ParametroCD,
    models.Pendencia,
    models.RecebimentoAgenda,
    models.Recebimento,
    models.Prestador,
    models.FuncaoTurno,
    models.PessoaTurno,
    models.Equipamento,
    models.EquipamentoManutencao,
    models.MaterialConsumo,
    models.Conferencia,
    models.Avaria,
    models.ChamadoSaldo,
    models.Unitizador,
    models.ControlePaleteVasilhame,
    models.Separacao,
    models.Ressuprimento,
    models.Expedicao,
    models.ExpedicaoPlanejamento,
    models.VeiculoFrota,
    models.SolicitacaoCarregamentoManual,
    models.ChecklistFrota,
    models.MaterialFrota,
    models.LacreFrota,
    models.OcorrenciaOperacional,
    models.AprovacaoOperacional,
    models.FechamentoDia,
    models.MelhoriaSistema,
]:
    admin.site.register(model)
