from django.conf import settings
from django.contrib.auth.models import User
from django.db import models
from django.utils import timezone


CD_CHOICES = [
    ("806", "CD 806 - Japuí"),
    ("801", "CD 801"),
]

PERFIL_CD_CHOICES = CD_CHOICES + [
    ("801+806", "CD 801 + CD 806"),
]

STATUS_CHOICES = [
    ("aberto", "Aberto"),
    ("andamento", "Em andamento"),
    ("concluido", "Concluído"),
    ("cancelado", "Cancelado"),
]

SETOR_CHOICES = [
    ("recebimento", "Recebimento"),
    ("separacao", "Separação"),
    ("expedicao", "Expedição"),
    ("conferencia", "Conferência"),
    ("ressuprimento", "Ressuprimento"),
    ("operacao_compartilhada", "Operação compartilhada/Empilhadeira"),
    ("cpd", "CPD"),
    ("limpeza", "Limpeza"),
    ("frota", "Frota"),
    ("motorista", "Motorista"),
]

TURNO_CHOICES = [
    ("dia", "Dia"),
    ("noite", "Noite"),
]

SEPARACAO_CATEGORIAS = [
    ("806_geral", "806 - Mercearia líquida geral"),
    ("806_agua", "806 - Mercearia líquida água"),
    ("806_refrigerante", "806 - Mercearia líquida refrigerante"),
    ("806_cerveja", "806 - Mercearia líquida cerveja"),
    ("801_geral", "801 - Mercearia líquida geral"),
    ("801_agua", "801 - Mercearia líquida água"),
    ("801_refrigerante", "801 - Mercearia líquida refrigerante"),
    ("801_cerveja", "801 - Mercearia líquida cerveja"),
    ("801_outros", "801 - Outros produtos"),
]


def today():
    return timezone.localdate()


class PerfilAcesso(models.Model):
    CARGOS = [
        ("master", "Master"),
        ("gestor_cd", "Gestor de CD"),
        ("gerente", "Gerente"),
        ("supervisor", "Supervisor"),
        ("supervisor_recebimento", "Supervisor de Recebimento"),
        ("supervisor_separacao", "Supervisor de Separação"),
        ("supervisor_conferencia_expedicao", "Supervisor de Conferência e Expedição"),
        ("supervisor_frota", "Supervisor de Frota"),
        ("lider", "Líder"),
        ("lider_separacao", "Líder de Separação"),
        ("lider_conferencia_expedicao", "Líder de Conferência e Expedição"),
        ("lider_frota", "Líder de Frota"),
        ("analista", "Analista"),
        ("assistente", "Assistente"),
        ("motorista", "Motorista"),
        ("consulta", "Consulta"),
    ]

    user = models.OneToOneField(User, on_delete=models.CASCADE, related_name="perfil_krill")
    cargo = models.CharField(max_length=40, choices=CARGOS, default="assistente")
    cd_padrao = models.CharField(max_length=7, choices=PERFIL_CD_CHOICES, default="806")
    permissoes = models.JSONField(default=list, blank=True)
    abas_ocultas = models.JSONField(default=list, blank=True)
    criado_em = models.DateTimeField(auto_now_add=True)
    atualizado_em = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"{self.user.username} - {self.get_cargo_display()}"


class AuditLog(models.Model):
    user = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL)
    usuario_nome = models.CharField(max_length=160, blank=True)
    acao = models.CharField(max_length=80)
    modulo = models.CharField(max_length=80, blank=True)
    cd_unidade = models.CharField(max_length=7, choices=PERFIL_CD_CHOICES, blank=True)
    objeto_id = models.CharField(max_length=40, blank=True)
    detalhe = models.TextField(blank=True)
    dados_antes = models.JSONField(default=dict, blank=True)
    dados_depois = models.JSONField(default=dict, blank=True)
    computador = models.CharField(max_length=120, blank=True)
    ip = models.CharField(max_length=60, blank=True)
    criado_em = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-criado_em"]

    def __str__(self):
        return f"{self.criado_em:%d/%m/%Y %H:%M} - {self.acao}"


class SolicitacaoSenha(models.Model):
    STATUS = [
        ("pendente", "Pendente"),
        ("em_andamento", "Em andamento"),
        ("respondida", "Respondida"),
        ("atendida", "Atendida"),
        ("cancelada", "Cancelada"),
    ]

    usuario = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL)
    login_informado = models.CharField(max_length=150)
    status = models.CharField(max_length=20, choices=STATUS, default="pendente")
    observacao = models.CharField(max_length=240, blank=True)
    atendido_por = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        related_name="solicitacoes_senha_atendidas",
        on_delete=models.SET_NULL,
    )
    criado_em = models.DateTimeField(auto_now_add=True)
    atendido_em = models.DateTimeField(null=True, blank=True)
    lido_pelo_usuario_em = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-criado_em"]

    def __str__(self):
        return f"{self.login_informado} - {self.get_status_display()}"


class SolicitacaoAcesso(models.Model):
    TIPOS = [
        ("novo_login", "Novo login"),
        ("alterar_acesso", "Alterar acesso/permissoes"),
        ("reativar_login", "Reativar login"),
        ("outro", "Outro"),
    ]
    STATUS = [
        ("pendente", "Pendente"),
        ("em_andamento", "Em andamento"),
        ("respondida", "Respondida"),
        ("atendida", "Atendida"),
        ("cancelada", "Cancelada"),
    ]

    usuario = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL)
    tipo = models.CharField(max_length=30, choices=TIPOS, default="novo_login")
    nome = models.CharField(max_length=160)
    login_desejado = models.CharField(max_length=150, blank=True)
    cd_unidade = models.CharField(max_length=3, choices=CD_CHOICES, blank=True)
    cargo = models.CharField(max_length=80, blank=True)
    setor = models.CharField(max_length=80, blank=True)
    contato = models.CharField(max_length=120, blank=True)
    justificativa = models.TextField(blank=True)
    status = models.CharField(max_length=20, choices=STATUS, default="pendente")
    resposta = models.TextField(blank=True)
    atendido_por = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        related_name="solicitacoes_acesso_atendidas",
        on_delete=models.SET_NULL,
    )
    criado_em = models.DateTimeField(auto_now_add=True)
    atendido_em = models.DateTimeField(null=True, blank=True)
    lido_pelo_usuario_em = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-criado_em"]

    def __str__(self):
        return f"{self.get_tipo_display()} - {self.nome}"


class ConfiguracaoSistema(models.Model):
    chave = models.CharField(max_length=80, unique=True)
    valor = models.TextField(blank=True)
    descricao = models.CharField(max_length=220, blank=True)
    atualizado_por = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL)
    atualizado_em = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["chave"]

    def __str__(self):
        return self.chave


class BackupLog(models.Model):
    DESTINOS = [
        ("local", "Local"),
        ("rede_50", "Pasta .50"),
    ]
    STATUS = [
        ("ok", "OK"),
        ("erro", "Erro"),
    ]

    destino = models.CharField(max_length=20, choices=DESTINOS)
    status = models.CharField(max_length=10, choices=STATUS)
    arquivo = models.CharField(max_length=520, blank=True)
    tamanho_bytes = models.BigIntegerField(default=0)
    mensagem = models.TextField(blank=True)
    criado_por = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL)
    criado_em = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-criado_em"]

    def __str__(self):
        return f"{self.get_destino_display()} - {self.status} - {self.criado_em:%d/%m/%Y %H:%M}"


class BaseOperacional(models.Model):
    cd_unidade = models.CharField("CD", max_length=3, choices=CD_CHOICES, default="806")
    data = models.DateField("Data", default=today)
    observacao = models.TextField("Observação", blank=True)
    criado_por = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL)
    criado_em = models.DateTimeField(auto_now_add=True)
    atualizado_em = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True
        ordering = ["-data", "-id"]


class Loja(BaseOperacional):
    codigo = models.CharField("Código da loja", max_length=20)
    nome = models.CharField("Nome da loja", max_length=120)
    grupo = models.CharField(
        "Grupo",
        max_length=30,
        blank=True,
        choices=[("praia", "Praia"), ("bairro", "Bairro"), ("plataforma", "Plataforma"), ("outros", "Outros")],
    )
    loja_plataforma = models.BooleanField("Loja plataforma", default=False)
    empilhadeira_indisponivel = models.BooleanField("Empilhadeira indisponivel", default=False)
    ativa = models.BooleanField("Ativa", default=True)

    class Meta(BaseOperacional.Meta):
        constraints = [
            models.UniqueConstraint(fields=["cd_unidade", "codigo"], name="loja_unica_por_cd")
        ]

    def __str__(self):
        return f"{self.codigo} - {self.nome}"


class ProdutoGtin(BaseOperacional):
    gtin = models.CharField("GTIN", max_length=40)
    descricao = models.CharField("Produto", max_length=180)
    categoria = models.CharField("Categoria", max_length=60, blank=True)
    embalagem = models.CharField("Embalagem", max_length=60, blank=True)
    endereco = models.CharField("Endereço", max_length=60, blank=True)
    ativo = models.BooleanField("Ativo", default=True)

    class Meta(BaseOperacional.Meta):
        constraints = [
            models.UniqueConstraint(fields=["cd_unidade", "gtin"], name="gtin_unico_por_cd")
        ]

    def __str__(self):
        return f"{self.gtin} - {self.descricao}"


class ParametroCD(BaseOperacional):
    TIPOS = [
        ("area", "Área"),
        ("rua", "Rua"),
        ("local_estoque", "Local de estoque"),
        ("atividade", "Atividade"),
        ("categoria_separacao", "Categoria da separação"),
        ("setor_armazenagem", "Setor de armazenagem"),
    ]

    tipo = models.CharField("Tipo", max_length=30, choices=TIPOS)
    codigo = models.CharField("Código", max_length=80)
    nome = models.CharField("Nome", max_length=160)
    ordem = models.PositiveIntegerField("Ordem", default=0)
    ativo = models.BooleanField("Ativo", default=True)

    class Meta(BaseOperacional.Meta):
        ordering = ["cd_unidade", "tipo", "ordem", "nome"]
        constraints = [
            models.UniqueConstraint(fields=["cd_unidade", "tipo", "codigo"], name="parametro_cd_unico")
        ]

    def __str__(self):
        return f"CD {self.cd_unidade} - {self.get_tipo_display()} - {self.nome}"


class Pendencia(BaseOperacional):
    setor = models.CharField("Setor", max_length=60)
    descricao = models.CharField("Descrição", max_length=240)
    responsavel = models.CharField("Responsável", max_length=120, blank=True)
    status = models.CharField("Status", max_length=20, choices=STATUS_CHOICES, default="aberto")

    def __str__(self):
        return self.descricao


class RecebimentoAgenda(BaseOperacional):
    empresa = models.CharField("Empresa", max_length=120)
    tipo_caminhao = models.CharField("Tipo do caminhão", max_length=80, blank=True)
    paletes = models.PositiveIntegerField("Paletes", default=0)
    agendado = models.BooleanField("Agendado", default=True)
    periodo = models.CharField("Período", max_length=30, blank=True)

    def __str__(self):
        return f"{self.empresa} - {self.paletes} paletes"


class Recebimento(BaseOperacional):
    fornecedor = models.CharField("Fornecedor", max_length=120)
    nota_fiscal = models.CharField("Nota fiscal", max_length=60, blank=True)
    produto = models.CharField("Produto", max_length=180, blank=True)
    quantidade = models.DecimalField("Quantidade", max_digits=12, decimal_places=2, default=0)
    paletes = models.PositiveIntegerField("Paletes", default=0)
    motorista = models.CharField("Motorista", max_length=120, blank=True)
    agendado = models.BooleanField("Agendado", default=True)
    forma_pagamento = models.CharField(
        "Pagamento",
        max_length=30,
        blank=True,
        choices=[
            ("dinheiro", "Dinheiro"),
            ("debito", "Cartão de débito"),
            ("pix", "Pix"),
            ("nao_paga", "Não paga"),
            ("acordo_fernanda", "A/C Fernanda"),
            ("outro", "Outro"),
        ],
    )
    valor = models.DecimalField("Valor", max_digits=12, decimal_places=2, default=0)

    def __str__(self):
        return f"{self.fornecedor} - NF {self.nota_fiscal}"


class RecebimentoConferente(BaseOperacional):
    conferente = models.CharField("Conferente", max_length=120)
    paletes = models.PositiveIntegerField("Paletes", default=0)
    cargas = models.PositiveIntegerField("Cargas", default=0)
    minutos_trabalhados = models.PositiveIntegerField("Minutos trabalhados", default=0)
    sem_agendamento = models.PositiveIntegerField("Recebido sem agendamento", default=0)

    @property
    def palete_minuto(self):
        if not self.minutos_trabalhados:
            return 0
        return round(self.paletes / self.minutos_trabalhados, 2)

    def __str__(self):
        return f"{self.conferente} - {self.paletes} paletes"


class Prestador(BaseOperacional):
    empresa = models.CharField("Prestador/Empresa", max_length=120)
    responsavel = models.CharField("Responsável", max_length=120, blank=True)
    tarefa = models.CharField("Tarefa realizada", max_length=240)
    entrada = models.TimeField("Entrada", null=True, blank=True)
    saida = models.TimeField("Saída", null=True, blank=True)
    status = models.CharField("Status", max_length=20, choices=STATUS_CHOICES, default="concluido")

    def __str__(self):
        return self.empresa


class FuncaoTurno(BaseOperacional):
    nome = models.CharField("Função", max_length=120)
    setor = models.CharField("Setor", max_length=30, choices=SETOR_CHOICES, default="recebimento")
    setor_detalhado = models.CharField("Setor/rua detalhada", max_length=120, blank=True)
    turno = models.CharField("Turno padrão", max_length=20, choices=TURNO_CHOICES, default="dia")
    quadro_padrao = models.PositiveIntegerField("Quadro padrão", default=0)
    ativa = models.BooleanField("Ativa", default=True)

    class Meta(BaseOperacional.Meta):
        constraints = [
            models.UniqueConstraint(fields=["cd_unidade", "nome", "setor", "turno"], name="funcao_turno_unica_por_cd")
        ]

    def __str__(self):
        return f"{self.nome} - {self.get_setor_display()} - {self.get_turno_display()}"


class PessoaTurno(BaseOperacional):
    setor = models.CharField("Setor", max_length=30, choices=SETOR_CHOICES, default="recebimento")
    setor_detalhado = models.CharField("Setor/rua detalhada", max_length=120, blank=True)
    turno = models.CharField("Turno", max_length=20, choices=TURNO_CHOICES, default="dia")
    funcao = models.CharField("Função", max_length=120)
    quadro_atual = models.PositiveIntegerField("Quadro atual", default=0)
    planejado = models.PositiveIntegerField("Planejado", default=0)
    ativos_dia = models.PositiveIntegerField("Ativos no dia", default=0)
    atestados = models.PositiveIntegerField("Atestados", default=0)
    afastados = models.PositiveIntegerField("Afastados", default=0)
    ferias = models.PositiveIntegerField("Férias", default=0)
    folgas = models.PositiveIntegerField("Folgas", default=0)
    faltas_sem_justificativa = models.PositiveIntegerField("Faltas sem justificativa", default=0)

    def save(self, *args, **kwargs):
        if not self.planejado:
            self.planejado = self.quadro_atual
        if not self.ativos_dia:
            ausencias = self.atestados + self.afastados + self.ferias + self.folgas + self.faltas_sem_justificativa
            self.ativos_dia = max(self.planejado - ausencias, 0)
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.funcao} - {self.ativos_dia}/{self.planejado}"


class ColaboradorFerias(BaseOperacional):
    STATUS_FERIAS = [
        ("programada", "Férias programadas"),
        ("pre_ferias", "Próximo das férias"),
        ("em_ferias", "Em férias"),
        ("retornando", "Retornando"),
        ("retornado", "Retornou"),
        ("cancelada", "Cancelada"),
    ]

    colaborador = models.CharField("Colaborador", max_length=120)
    setor = models.CharField("Setor", max_length=30, choices=SETOR_CHOICES, default="recebimento")
    setor_detalhado = models.CharField("Setor/rua detalhada", max_length=120, blank=True)
    funcao = models.CharField("Função", max_length=120, blank=True)
    inicio_ferias = models.DateField("Início das férias")
    fim_ferias = models.DateField("Fim das férias")
    retorno_previsto = models.DateField("Retorno previsto")
    status = models.CharField("Status", max_length=20, choices=STATUS_FERIAS, default="programada")
    notificar_supervisor = models.BooleanField("Notificar supervisor", default=True)
    notificado_em = models.DateTimeField("Notificado em", null=True, blank=True)

    class Meta(BaseOperacional.Meta):
        ordering = ["inicio_ferias", "colaborador"]

    def save(self, *args, **kwargs):
        hoje = timezone.localdate()
        if self.status not in {"cancelada", "retornado"}:
            if self.inicio_ferias <= hoje <= self.fim_ferias:
                self.status = "em_ferias"
            elif hoje < self.inicio_ferias and (self.inicio_ferias - hoje).days <= 7:
                self.status = "pre_ferias"
            elif hoje > self.fim_ferias and hoje <= self.retorno_previsto:
                self.status = "retornando"
            elif hoje > self.retorno_previsto:
                self.status = "retornado"
            else:
                self.status = "programada"
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.colaborador} - {self.get_status_display()}"


class ColaboradorAusencia(BaseOperacional):
    TIPOS = [
        ("atestado", "Atestado"),
        ("afastamento", "Afastamento"),
        ("falta_sem_justificativa", "Falta sem justificativa"),
        ("folga", "Folga"),
        ("suspensao", "Suspensão"),
        ("outro", "Outro"),
    ]
    STATUS = [
        ("ativa", "Ativa"),
        ("programada", "Programada"),
        ("encerrada", "Encerrada"),
        ("cancelada", "Cancelada"),
    ]

    colaborador = models.CharField("Colaborador", max_length=120)
    setor = models.CharField("Setor", max_length=30, choices=SETOR_CHOICES, default="recebimento")
    setor_detalhado = models.CharField("Setor/rua detalhada", max_length=120, blank=True)
    funcao = models.CharField("Função", max_length=120, blank=True)
    tipo = models.CharField("Tipo de ausência", max_length=40, choices=TIPOS, default="atestado")
    inicio = models.DateField("Início")
    fim = models.DateField("Fim")
    status = models.CharField("Status", max_length=20, choices=STATUS, default="ativa")
    notificar_supervisor = models.BooleanField("Notificar supervisor", default=True)

    class Meta(BaseOperacional.Meta):
        ordering = ["-inicio", "colaborador"]

    def save(self, *args, **kwargs):
        hoje = timezone.localdate()
        if self.status not in {"cancelada", "encerrada"}:
            if self.inicio <= hoje <= self.fim:
                self.status = "ativa"
            elif hoje < self.inicio:
                self.status = "programada"
            elif hoje > self.fim:
                self.status = "encerrada"
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.colaborador} - {self.get_tipo_display()} ({self.inicio:%d/%m} a {self.fim:%d/%m})"


class Equipamento(BaseOperacional):
    TIPO_EQUIPAMENTO = [
        ("operacional", "Paleteira/coletor"),
        ("administrativo", "Computador administrativo"),
        ("empilhadeira", "Empilhadeira"),
        ("outro", "Outro equipamento"),
    ]

    colaborador = models.CharField("Colaborador", max_length=120)
    setor = models.CharField("Setor", max_length=60)
    tipo = models.CharField("Tipo", max_length=30, choices=TIPO_EQUIPAMENTO, default="operacional")
    equipamento = models.CharField("Equipamento/série", max_length=120, blank=True)
    patrimonio = models.CharField("Patrimônio/nome", max_length=120, blank=True)
    coletor_tipo = models.CharField("Coletor/tipo", max_length=120, blank=True)
    status = models.CharField(
        "Status",
        max_length=30,
        choices=[("em_uso", "Em uso"), ("devolvido", "Devolvido"), ("manutencao", "Manutenção"), ("perdido", "Perdido")],
        default="em_uso",
    )

    def __str__(self):
        return f"{self.colaborador} - {self.equipamento or self.patrimonio}"


class EquipamentoManutencao(BaseOperacional):
    DESTINOS = [
        ("ti", "TI"),
        ("interno", "Interno"),
        ("fornecedor", "Fornecedor"),
        ("outro", "Outro"),
    ]
    STATUS = [
        ("aberto", "Aberto"),
        ("enviado", "Enviado"),
        ("retornado", "Retornado"),
        ("sem_conserto", "Sem conserto"),
        ("cancelado", "Cancelado"),
    ]

    equipamento_cadastrado = models.ForeignKey(
        Equipamento,
        verbose_name="Equipamento cadastrado",
        null=True,
        blank=True,
        related_name="manutencoes",
        on_delete=models.SET_NULL,
    )
    patrimonio = models.CharField("Patrimônio", max_length=120)
    equipamento = models.CharField("Equipamento/serie", max_length=120, blank=True)
    tipo = models.CharField("Tipo", max_length=30, choices=Equipamento.TIPO_EQUIPAMENTO, default="operacional")
    problema = models.CharField("Problema relatado", max_length=240)
    destino = models.CharField("Destino", max_length=30, choices=DESTINOS, default="ti")
    responsavel = models.CharField("Responsavel", max_length=120, blank=True)
    data_retorno = models.DateField("Data retorno", null=True, blank=True)
    status = models.CharField("Status", max_length=30, choices=STATUS, default="aberto")

    def save(self, *args, **kwargs):
        if self.equipamento_cadastrado_id:
            self.patrimonio = self.equipamento_cadastrado.patrimonio
            self.equipamento = self.equipamento_cadastrado.equipamento
            self.tipo = self.equipamento_cadastrado.tipo
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.patrimonio} - {self.get_destino_display()}"


class MaterialConsumo(BaseOperacional):
    material = models.CharField("Material", max_length=120)
    categoria = models.CharField("Categoria", max_length=80, blank=True)
    estoque_atual = models.DecimalField("Estoque atual", max_digits=12, decimal_places=2, default=0)
    estoque_minimo = models.DecimalField("Estoque mínimo", max_digits=12, decimal_places=2, default=0)
    unidade = models.CharField("Unidade", max_length=30, blank=True)
    fornecedor = models.CharField("Fornecedor", max_length=120, blank=True)
    precisa_pedir = models.BooleanField("Precisa pedir", default=False)

    def save(self, *args, **kwargs):
        self.precisa_pedir = self.estoque_atual <= self.estoque_minimo
        super().save(*args, **kwargs)

    def __str__(self):
        return self.material


class Conferencia(BaseOperacional):
    setor = models.CharField("Setor", max_length=60, blank=True)
    loja = models.CharField("Loja", max_length=80, blank=True)
    documento = models.CharField("Pedido/NF/Carga", max_length=80, blank=True)
    gtin = models.CharField("GTIN", max_length=40, blank=True)
    produto = models.CharField("Produto", max_length=180, blank=True)
    qtd_esperada = models.DecimalField("Qtd. esperada", max_digits=12, decimal_places=2, default=0)
    qtd_conferida = models.DecimalField("Qtd. conferida", max_digits=12, decimal_places=2, default=0)
    diferenca = models.DecimalField("Diferença", max_digits=12, decimal_places=2, default=0)
    status = models.CharField("Status", max_length=30, choices=STATUS_CHOICES, default="aberto")
    responsavel = models.CharField("Responsável", max_length=120, blank=True)

    def save(self, *args, **kwargs):
        self.diferenca = self.qtd_conferida - self.qtd_esperada
        super().save(*args, **kwargs)

    def __str__(self):
        base = self.produto or self.gtin or self.loja or "Conferencia"
        return f"{self.data:%d/%m/%Y} - {base}"


class Avaria(BaseOperacional):
    gtin = models.CharField("GTIN", max_length=40, blank=True)
    produto = models.CharField("Produto", max_length=180)
    validade = models.DateField("Validade", null=True, blank=True)
    lote = models.CharField("Lote", max_length=80, blank=True)
    tipo = models.CharField(
        "Tipo",
        max_length=30,
        choices=[("vencido", "Vencido"), ("avaria", "Avaria"), ("sobra", "Sobra"), ("falta", "Falta"), ("outro", "Outro")],
        default="avaria",
    )
    origem = models.CharField("Origem", max_length=80, blank=True)
    caixas = models.DecimalField("Caixas", max_digits=12, decimal_places=2, default=0)
    unidades = models.DecimalField("Unidades", max_digits=12, decimal_places=2, default=0)
    valor_estimado = models.DecimalField("Valor estimado", max_digits=12, decimal_places=2, default=0)
    destino = models.CharField("Destino", max_length=120, blank=True)
    status = models.CharField("Status", max_length=20, choices=STATUS_CHOICES, default="aberto")
    responsavel = models.CharField("Responsável", max_length=120, blank=True)

    def __str__(self):
        return self.produto


class ChamadoSaldo(BaseOperacional):
    gtin = models.CharField("GTIN", max_length=40, blank=True)
    produto = models.CharField("Produto", max_length=180)
    endereco = models.CharField("Endereço", max_length=80, blank=True)
    saldo_sistema = models.DecimalField("Saldo sistema", max_digits=12, decimal_places=2, default=0)
    saldo_fisico = models.DecimalField("Saldo físico", max_digits=12, decimal_places=2, default=0)
    diferenca = models.DecimalField("Diferença", max_digits=12, decimal_places=2, default=0)
    tipo_chamado = models.CharField(
        "Tipo chamado",
        max_length=30,
        choices=[("ganho", "Ganho de saldo"), ("quebra", "Quebra de saldo"), ("ajuste", "Ajuste"), ("outro", "Outro")],
        default="ajuste",
    )
    prioridade = models.CharField("Prioridade", max_length=20, default="normal")
    numero_glpi = models.CharField("Número GLPI", max_length=60, blank=True)
    status = models.CharField("Status", max_length=20, choices=STATUS_CHOICES, default="aberto")
    responsavel = models.CharField("Responsável", max_length=120, blank=True)

    def save(self, *args, **kwargs):
        self.diferenca = self.saldo_fisico - self.saldo_sistema
        super().save(*args, **kwargs)

    def __str__(self):
        return self.produto


class Unitizador(BaseOperacional):
    loja = models.CharField("Loja", max_length=80)
    quantidade = models.PositiveIntegerField("Quantidade", default=0)
    tipo_unitizador = models.CharField("Tipo", max_length=60, default="Palete")
    status = models.CharField("Status", max_length=20, choices=STATUS_CHOICES, default="aberto")
    data_retorno = models.DateField("Data retorno", null=True, blank=True)

    def __str__(self):
        return f"{self.loja} - {self.quantidade}"


class ControlePaleteVasilhame(BaseOperacional):
    turno = models.CharField(
        "Turno",
        max_length=20,
        choices=[("manha", "Manha"), ("tarde", "Tarde"), ("noite", "Noite"), ("dia", "Dia completo")],
        default="dia",
    )
    responsavel = models.CharField("Responsavel", max_length=120, blank=True)
    palete_pbr = models.PositiveIntegerField("Paletes PBR", default=0)
    palete_chep = models.PositiveIntegerField("Paletes CHEP", default=0)
    palete_chapatex = models.PositiveIntegerField("Paletes Chapatex", default=0)
    palete_descartavel = models.PositiveIntegerField("Paletes descartaveis", default=0)
    vasilhame_coca = models.PositiveIntegerField("Vasilhames Coca-Cola retornavel", default=0)
    vasilhame_cerveja = models.PositiveIntegerField("Vasilhames cerveja/engradado", default=0)
    palete_cheio = models.PositiveIntegerField("Paletes cheios", default=0)
    palete_vazio = models.PositiveIntegerField("Paletes vazios", default=0)

    @property
    def total_paletes(self):
        return self.palete_pbr + self.palete_chep + self.palete_chapatex + self.palete_descartavel

    @property
    def total_vasilhames(self):
        return self.vasilhame_coca + self.vasilhame_cerveja

    def __str__(self):
        return f"{self.data:%d/%m/%Y} - {self.responsavel or self.get_turno_display()}"


class PaleteRedeSaldo(BaseOperacional):
    LOCAL_TIPOS = [
        ("cd", "CD"),
        ("loja", "Loja"),
        ("outro", "Outro local"),
    ]
    TIPOS_PALETE = [
        ("pbr", "PBR"),
        ("pbr2", "PBR 2"),
        ("chep", "CHEP"),
        ("descartavel", "Descartável"),
    ]

    local_tipo = models.CharField("Tipo de local", max_length=20, choices=LOCAL_TIPOS, default="cd")
    local_codigo = models.CharField("Código do local", max_length=20, blank=True)
    local_nome = models.CharField("Local", max_length=140)
    tipo_palete = models.CharField("Tipo de pallet", max_length=20, choices=TIPOS_PALETE, default="pbr")
    quantidade = models.PositiveIntegerField("Saldo total", default=0)
    quebrados = models.PositiveIntegerField("Quebrados", default=0)
    reservados = models.PositiveIntegerField("Reservados", default=0)
    responsavel = models.CharField("Responsável", max_length=120, blank=True)

    class Meta(BaseOperacional.Meta):
        constraints = [
            models.UniqueConstraint(
                fields=["local_tipo", "local_codigo", "local_nome", "tipo_palete"],
                name="palete_rede_saldo_unico",
            )
        ]

    @property
    def disponivel(self):
        return max(self.quantidade - self.quebrados - self.reservados, 0)

    @property
    def local_label(self):
        if self.local_tipo == "cd":
            return f"CD {self.local_codigo or self.local_nome}"
        return self.local_nome

    def __str__(self):
        return f"{self.local_label} - {self.get_tipo_palete_display()}: {self.quantidade}"


class PaleteRedeMovimentacao(BaseOperacional):
    OPERACOES = [
        ("reserva", "Reserva"),
        ("transferencia", "Transferência"),
        ("retirada", "Retirada/entrega"),
        ("ajuste", "Ajuste"),
    ]
    STATUS = [
        ("aberto", "Aberto"),
        ("concluido", "Concluído"),
        ("cancelado", "Cancelado"),
    ]

    operacao = models.CharField("Operação", max_length=20, choices=OPERACOES, default="reserva")
    tipo_palete = models.CharField("Tipo de pallet", max_length=20, choices=PaleteRedeSaldo.TIPOS_PALETE, default="pbr")
    quantidade = models.PositiveIntegerField("Quantidade", default=0)
    origem_resumo = models.CharField("Origem", max_length=240, blank=True)
    destino_tipo = models.CharField("Tipo de destino", max_length=20, choices=PaleteRedeSaldo.LOCAL_TIPOS, default="outro")
    destino_codigo = models.CharField("Código destino", max_length=20, blank=True)
    destino_nome = models.CharField("Destino", max_length=140, blank=True)
    solicitante = models.CharField("Solicitante", max_length=120, blank=True)
    status = models.CharField("Status", max_length=20, choices=STATUS, default="concluido")
    fontes = models.JSONField("Fontes", default=list, blank=True)

    def __str__(self):
        return f"{self.get_operacao_display()} - {self.get_tipo_palete_display()} - {self.quantidade}"

class Separacao(BaseOperacional):
    loja = models.CharField("Loja", max_length=80)
    setor = models.CharField("Setor", max_length=80, blank=True)
    categoria = models.CharField(
        "Categoria",
        max_length=120,
        default="",
        blank=True,
    )
    unidades = models.DecimalField("Unidades separadas", max_digits=12, decimal_places=2, default=0)
    paletes = models.PositiveIntegerField("Paletes", default=0)
    onda_gerada = models.PositiveIntegerField("Onda gerada", default=0)
    total_a_produzir = models.DecimalField("Total a produzir", max_digits=14, decimal_places=2, default=0)
    crossdocking_produzido = models.DecimalField("Crossdocking produzido", max_digits=14, decimal_places=2, default=0)
    separacao_pulmao_produzido = models.DecimalField("Separação pulmão produzida", max_digits=14, decimal_places=2, default=0)
    separacao_picking_produzido = models.DecimalField("Separação picking produzida", max_digits=14, decimal_places=2, default=0)
    recursos_picking = models.PositiveIntegerField("Recursos picking", default=0)
    horas_picking = models.DecimalField("Horas picking", max_digits=8, decimal_places=2, default=0)
    recursos_pulmao = models.PositiveIntegerField("Recursos pulmão", default=0)
    horas_pulmao = models.DecimalField("Horas pulmão", max_digits=8, decimal_places=2, default=0)
    responsavel = models.CharField("Responsável", max_length=120, blank=True)
    status = models.CharField("Status", max_length=20, choices=STATUS_CHOICES, default="concluido")

    @property
    def produtividade_picking(self):
        base = self.recursos_picking * self.horas_picking
        if not base:
            return 0
        return round(self.separacao_picking_produzido / base, 2)

    @property
    def produtividade_pulmao(self):
        base = self.recursos_pulmao * self.horas_pulmao
        if not base:
            return 0
        return round(self.separacao_pulmao_produzido / base, 2)

    def __str__(self):
        return f"{self.loja} - {self.unidades}"


class Ressuprimento(BaseOperacional):
    gtin = models.CharField("GTIN", max_length=40, blank=True)
    produto = models.CharField("Produto", max_length=180)
    endereco_origem = models.CharField("Endereço origem", max_length=80, blank=True)
    endereco_destino = models.CharField("Endereço destino", max_length=80, blank=True)
    quantidade = models.DecimalField("Quantidade", max_digits=12, decimal_places=2, default=0)
    responsavel = models.CharField("Responsável", max_length=120, blank=True)
    status = models.CharField("Status", max_length=20, choices=STATUS_CHOICES, default="concluido")

    def __str__(self):
        return f"{self.gtin} - {self.produto}"


class RessuprimentoPainel(BaseOperacional):
    TIPO = [
        ("demanda", "Demanda"),
        ("ocupacao", "Ocupação"),
        ("producao", "Produção"),
    ]

    tipo_painel = models.CharField("Tipo de painel", max_length=20, choices=TIPO, default="demanda")
    setor = models.CharField("Setor", max_length=80)
    area = models.CharField("Área", max_length=80, blank=True)
    rua = models.CharField("Rua", max_length=80, blank=True)
    local_estoque = models.CharField("Local de estoque", max_length=120, blank=True)
    atividade = models.CharField("Atividade", max_length=160, blank=True)
    qtd_itens = models.PositiveIntegerField("Qtd. itens", default=0)
    qtd_volume = models.DecimalField("Qtd. volume", max_digits=14, decimal_places=2, default=0)
    pendencia_itens = models.PositiveIntegerField("Pendência itens", default=0)
    pendencia_volume = models.DecimalField("Pendência volume", max_digits=14, decimal_places=2, default=0)
    produzido_itens = models.PositiveIntegerField("Produzido itens", default=0)
    produzido_volume = models.DecimalField("Produzido volume", max_digits=14, decimal_places=2, default=0)
    picking = models.DecimalField("Picking", max_digits=14, decimal_places=2, default=0)
    pulmao = models.DecimalField("Pulmão", max_digits=14, decimal_places=2, default=0)

    @property
    def percentual_produzido(self):
        if not self.qtd_volume:
            return 0
        return round((self.produzido_volume / self.qtd_volume) * 100, 2)

    def __str__(self):
        return f"{self.get_tipo_painel_display()} - {self.setor or self.rua}"


class SeparacaoProdutividade(BaseOperacional):
    setor = models.CharField("Setor", max_length=80, blank=True)
    onda_gerada = models.PositiveIntegerField("Onda gerada", default=0)
    total_a_produzir = models.DecimalField("Total a produzir", max_digits=14, decimal_places=2, default=0)
    crossdocking_produzido = models.DecimalField("Crossdocking produzido", max_digits=14, decimal_places=2, default=0)
    separacao_pulmao_produzido = models.DecimalField("Separação pulmão produzida", max_digits=14, decimal_places=2, default=0)
    separacao_picking_produzido = models.DecimalField("Separação picking produzida", max_digits=14, decimal_places=2, default=0)
    recursos_picking = models.PositiveIntegerField("Recursos picking", default=0)
    horas_picking = models.DecimalField("Horas picking", max_digits=8, decimal_places=2, default=0)
    recursos_pulmao = models.PositiveIntegerField("Recursos pulmão", default=0)
    horas_pulmao = models.DecimalField("Horas pulmão", max_digits=8, decimal_places=2, default=0)

    @property
    def produtividade_picking(self):
        base = self.recursos_picking * self.horas_picking
        if not base:
            return 0
        return round(self.separacao_picking_produzido / base, 2)

    @property
    def produtividade_pulmao(self):
        base = self.recursos_pulmao * self.horas_pulmao
        if not base:
            return 0
        return round(self.separacao_pulmao_produzido / base, 2)

    def __str__(self):
        return f"{self.setor or 'Separação'} - {self.data}"


class Expedicao(BaseOperacional):
    loja = models.CharField("Loja", max_length=80)
    placa = models.CharField("Placa", max_length=20, blank=True)
    motorista = models.CharField("Motorista", max_length=120, blank=True)
    qtd_paletes = models.PositiveIntegerField("Qtd. paletes", default=0)
    periodo = models.CharField("Período", max_length=30, choices=[("manha", "Manhã"), ("tarde", "Tarde")], default="manha")
    status = models.CharField("Status", max_length=20, choices=STATUS_CHOICES, default="concluido")

    def __str__(self):
        return f"{self.loja} - {self.qtd_paletes} paletes"


class ExpedicaoPlanejamento(BaseOperacional):
    loja = models.CharField("Loja", max_length=80)
    qtd_paletes = models.PositiveIntegerField("Qtd. paletes", default=0)
    pode_remontar = models.BooleanField("Dá para remontar", default=False)
    qtd_remontavel = models.PositiveIntegerField("Qtd. que dá para remontar", default=0)
    carregado_em = models.DateTimeField("Carregado em", null=True, blank=True)
    status = models.CharField(
        "Status",
        max_length=20,
        choices=[("planejado", "Planejado"), ("distribuido", "Distribuido"), ("cancelado", "Cancelado")],
        default="planejado",
    )

    class Meta(BaseOperacional.Meta):
        constraints = [
            models.UniqueConstraint(fields=["cd_unidade", "data", "loja"], name="expedicao_planejamento_unico")
        ]

    def __str__(self):
        return f"{self.data:%d/%m/%Y} - CD {self.cd_unidade} - {self.loja}: {self.qtd_paletes}"


class ExpedicaoVinculo(BaseOperacional):
    STATUS = [
        ("vinculado", "Vinculado"),
        ("carregado", "Carregado"),
        ("cancelado", "Cancelado"),
    ]

    loja = models.CharField("Loja", max_length=80)
    placa = models.CharField("Placa", max_length=20, blank=True)
    motorista = models.CharField("Motorista", max_length=120, blank=True)
    qtd_paletes = models.PositiveIntegerField("Qtd. paletes", default=0)
    periodo = models.CharField("Período", max_length=30, choices=[("manha", "Manhã"), ("tarde", "Tarde")], default="manha")
    exige_plataforma = models.BooleanField("Exige plataforma", default=False)
    motivo_plataforma = models.CharField("Motivo plataforma", max_length=160, blank=True)
    ordem_entrega = models.PositiveIntegerField("Ordem sugerida de entrega", default=0)
    orientacao_frota = models.TextField("Orientacao da Frota", blank=True)
    status = models.CharField("Status", max_length=20, choices=STATUS, default="vinculado")
    carregado_em = models.DateTimeField("Carregado em", null=True, blank=True)
    expedicao = models.ForeignKey(Expedicao, null=True, blank=True, on_delete=models.SET_NULL, related_name="vinculos")

    def __str__(self):
        return f"CD {self.cd_unidade} - {self.loja} - {self.motorista or self.placa}: {self.qtd_paletes} pallets"


class SolicitacaoCarregamentoManual(BaseOperacional):
    STATUS = [
        ("pendente", "Pendente"),
        ("aprovado", "Aprovado"),
        ("recusado", "Recusado"),
        ("cancelado", "Cancelado"),
        ("concluido", "Concluido"),
    ]

    loja = models.CharField("Loja", max_length=80)
    placa = models.CharField("Placa", max_length=20, blank=True)
    motorista = models.CharField("Motorista", max_length=120, blank=True)
    qtd_paletes = models.PositiveIntegerField("Qtd. paletes", default=0)
    valor_carga = models.DecimalField("Valor da carga", max_digits=12, decimal_places=2, default=0)
    periodo = models.CharField("Periodo", max_length=30, choices=[("manha", "Manha"), ("tarde", "Tarde")], default="manha")
    exige_plataforma = models.BooleanField("Exige plataforma", default=False)
    motivo_plataforma = models.CharField("Motivo plataforma", max_length=160, blank=True)
    status = models.CharField("Status", max_length=20, choices=STATUS, default="pendente")
    motivo = models.TextField("Motivo da solicitação")
    resposta = models.TextField("Resposta da Frota", blank=True)
    aprovado_por = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        related_name="carregamentos_manuais_aprovados",
        on_delete=models.SET_NULL,
    )
    aprovado_em = models.DateTimeField("Aprovado/recusado em", null=True, blank=True)
    expedicao = models.ForeignKey(Expedicao, null=True, blank=True, on_delete=models.SET_NULL, related_name="solicitacoes_manuais")
    lido_pelo_solicitante_em = models.DateTimeField("Lido pelo solicitante em", null=True, blank=True)

    def __str__(self):
        return f"CD {self.cd_unidade} - {self.loja}: {self.qtd_paletes} pallet(s) - {self.get_status_display()}"


class SolicitacaoCargaPronta(BaseOperacional):
    STATUS = [
        ("pronta", "Pronta para carregar"),
        ("em_carregamento", "Em carregamento"),
        ("carregada", "Carregada"),
        ("cancelada", "Cancelada"),
    ]
    PERIODOS = [
        ("manha", "Manhã"),
        ("tarde", "Tarde"),
    ]

    loja = models.CharField("Loja", max_length=80)
    qtd_paletes = models.PositiveIntegerField("Qtd. paletes", default=0)
    periodo = models.CharField("Período", max_length=20, choices=PERIODOS, default="manha")
    status = models.CharField("Status", max_length=30, choices=STATUS, default="pronta")
    resposta = models.TextField("Resposta da Frota", blank=True)
    tratado_por = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        related_name="cargas_prontas_tratadas",
        on_delete=models.SET_NULL,
    )
    tratado_em = models.DateTimeField("Tratado em", null=True, blank=True)
    lido_pelo_solicitante_em = models.DateTimeField("Lido pelo solicitante em", null=True, blank=True)

    def __str__(self):
        return f"CD {self.cd_unidade} - {self.loja}: {self.qtd_paletes} pallet(s) - {self.get_status_display()}"


class SolicitacaoCaminhaoCD(BaseOperacional):
    STATUS = [
        ("pendente", "Pendente"),
        ("em_andamento", "Em andamento"),
        ("atendida", "Atendida"),
        ("recusada", "Recusada"),
        ("cancelada", "Cancelada"),
    ]
    PERIODOS = [
        ("manha", "Manhã"),
        ("tarde", "Tarde"),
        ("dia_todo", "Dia todo"),
    ]

    data_necessidade = models.DateField("Data de necessidade")
    cd_destino = models.CharField("CD que recebera o caminhao", max_length=3, choices=CD_CHOICES, default="806")
    qtd_caminhoes = models.PositiveIntegerField("Qtd. caminhões", default=1)
    precisa_plataforma = models.BooleanField("Precisa de plataforma", default=False)
    qtd_plataforma = models.PositiveIntegerField("Qtd. plataforma", default=0)
    periodo = models.CharField("Período", max_length=20, choices=PERIODOS, default="manha")
    motivo = models.CharField("Motivo", max_length=240, blank=True)
    status = models.CharField("Status", max_length=20, choices=STATUS, default="pendente")
    resposta = models.TextField("Resposta da Frota", blank=True)
    motoristas_enviados = models.TextField("Motoristas enviados", blank=True)
    atendido_por = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        related_name="solicitacoes_caminhao_atendidas",
        on_delete=models.SET_NULL,
    )
    atendido_em = models.DateTimeField("Atendido em", null=True, blank=True)
    lido_pelo_solicitante_em = models.DateTimeField("Lido pelo solicitante em", null=True, blank=True)

    def __str__(self):
        return f"CD {self.cd_unidade} pediu {self.qtd_caminhoes} caminhão(ões) para {self.data_necessidade:%d/%m/%Y}"


class ExpedicaoFaturamento(BaseOperacional):
    STATUS = [
        ("aguardando", "Aguardando faturamento"),
        ("erro_fiscal", "Erro fiscal"),
        ("nf_emitida", "NF emitida"),
        ("liberado_ronilo", "Liberado Ronilo/lacre"),
        ("cancelado", "Cancelado"),
    ]

    vinculo = models.ForeignKey(ExpedicaoVinculo, null=True, blank=True, on_delete=models.SET_NULL, related_name="faturamentos")
    loja = models.CharField("Loja", max_length=80)
    placa = models.CharField("Placa", max_length=20, blank=True)
    motorista = models.CharField("Motorista", max_length=120, blank=True)
    qtd_paletes = models.PositiveIntegerField("Qtd. paletes", default=0)
    valor_carga = models.DecimalField("Valor da carga", max_digits=12, decimal_places=2, default=0)
    periodo = models.CharField("Periodo", max_length=30, choices=[("manha", "Manha"), ("tarde", "Tarde")], default="manha")
    carga_bluesoft = models.CharField("Carga BlueSoft", max_length=60, blank=True)
    nota_fiscal = models.CharField("Nota fiscal", max_length=60, blank=True)
    chave_acesso = models.CharField("Chave de acesso", max_length=80, blank=True)
    status = models.CharField("Status", max_length=30, choices=STATUS, default="aguardando")
    motivo_fiscal = models.TextField("Motivo fiscal", blank=True)
    faturado_em = models.DateTimeField("Faturado em", null=True, blank=True)
    liberado_ronilo_em = models.DateTimeField("Liberado Ronilo/lacre em", null=True, blank=True)

    def __str__(self):
        return f"CD {self.cd_unidade} - {self.loja} - {self.get_status_display()}"


class SistemaNotificacao(models.Model):
    CATEGORIAS = [
        ("geral", "Geral"),
        ("frota", "Frota"),
        ("expedicao", "Expedicao"),
        ("faturamento", "Faturamento"),
        ("ferias", "Ferias"),
    ]

    usuario = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="notificacoes_sistema")
    titulo = models.CharField(max_length=120)
    mensagem = models.TextField()
    url = models.CharField(max_length=240, blank=True)
    categoria = models.CharField(max_length=30, choices=CATEGORIAS, default="geral")
    cd_unidade = models.CharField(max_length=3, choices=CD_CHOICES, blank=True)
    payload = models.JSONField(default=dict, blank=True)
    lida_em = models.DateTimeField(null=True, blank=True)
    enviada_push_em = models.DateTimeField(null=True, blank=True)
    criado_em = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-criado_em"]

    def __str__(self):
        return f"{self.usuario} - {self.titulo}"


class PushSubscription(models.Model):
    usuario = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="push_subscriptions")
    endpoint = models.TextField(unique=True)
    p256dh = models.TextField()
    auth = models.TextField()
    vapid_public_key = models.TextField(blank=True)
    user_agent = models.TextField(blank=True)
    ativo = models.BooleanField(default=True)
    ultimo_erro = models.TextField(blank=True)
    criado_em = models.DateTimeField(auto_now_add=True)
    atualizado_em = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-atualizado_em"]

    def __str__(self):
        return f"{self.usuario} - {'ativo' if self.ativo else 'inativo'}"


class VeiculoFrota(BaseOperacional):
    STATUS_OPERACIONAL = [
        ("disponivel", "Disponivel"),
        ("em_rota", "Em rota"),
        ("ocupado", "Ocupado"),
        ("manutencao", "Manutencao"),
        ("limpeza", "Limpeza"),
        ("indisponivel", "Indisponivel"),
    ]
    TACOGRAFO_FREQUENCIAS = [
        ("diario", "Diario"),
        ("semanal", "Semanal"),
    ]

    motorista = models.CharField("Motorista", max_length=120)
    placa = models.CharField("Placa", max_length=20)
    tipo_caminhao = models.CharField("Tipo de caminhão", max_length=80, blank=True)
    plataforma_operacional = models.BooleanField("Plataforma operacional", default=True)
    status_operacional = models.CharField("Status operacional", max_length=20, choices=STATUS_OPERACIONAL, default="disponivel")
    local_atual = models.CharField("Local atual", max_length=120, blank=True)
    orientacao_frota = models.TextField("Orientacao da frota", blank=True)
    tacografo_frequencia = models.CharField("Frequencia do tacografo", max_length=20, choices=TACOGRAFO_FREQUENCIAS, default="diario")
    usuario_motorista = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name="Usuário do motorista",
        null=True,
        blank=True,
        related_name="veiculos_frota",
        on_delete=models.SET_NULL,
    )
    ativo = models.BooleanField("Ativo", default=True)

    class Meta(BaseOperacional.Meta):
        constraints = [
            models.UniqueConstraint(fields=["cd_unidade", "placa", "motorista"], name="veiculo_frota_unico")
        ]

    def __str__(self):
        return f"{self.motorista} - {self.placa}"

    @property
    def eh_plataforma(self):
        return "plataforma" in (self.tipo_caminhao or "").casefold()

    @property
    def plataforma_disponivel(self):
        return self.eh_plataforma and self.plataforma_operacional


class EscalaVeiculoFrota(BaseOperacional):
    MOTIVOS = [
        ("ferias", "Férias"),
        ("manutencao", "Manutenção"),
        ("troca_operacional", "Troca operacional"),
        ("apoio", "Apoio"),
        ("outro", "Outro"),
    ]

    placa = models.CharField("Placa", max_length=20)
    motorista_titular = models.CharField("Motorista titular", max_length=120, blank=True)
    motorista_responsavel = models.CharField("Motorista responsável no período", max_length=120)
    usuario_responsavel = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name="Login do responsável",
        null=True,
        blank=True,
        related_name="escalas_veiculos_frota",
        on_delete=models.SET_NULL,
    )
    inicio = models.DateField("Início da troca")
    fim = models.DateField("Fim da troca")
    motivo = models.CharField("Motivo", max_length=30, choices=MOTIVOS, default="ferias")
    ativo = models.BooleanField("Ativo", default=True)

    class Meta(BaseOperacional.Meta):
        ordering = ["-inicio", "placa"]

    def __str__(self):
        return f"{self.placa} - {self.motorista_responsavel} ({self.inicio:%d/%m} a {self.fim:%d/%m})"


CHECKLIST_TIPO_CHOICES = [
    ("saida", "Saída do CD"),
    ("chegada_loja", "Chegada na loja"),
    ("saida_loja", "Saída da loja"),
    ("retorno", "Retorno ao CD"),
    ("ambos", "Todas as etapas"),
]


class ChecklistFrota(BaseOperacional):
    TIPO_CHECKLIST = [choice for choice in CHECKLIST_TIPO_CHOICES if choice[0] != "ambos"]

    tipo_checklist = models.CharField("Tipo de checklist", max_length=12, choices=TIPO_CHECKLIST, default="saida")
    acompanhar_retorno = models.BooleanField("Acompanhar retorno", default=False)
    saida_referencia = models.OneToOneField(
        "self",
        verbose_name="Saída de referência",
        null=True,
        blank=True,
        related_name="retorno_registrado",
        on_delete=models.SET_NULL,
    )
    retorno_dispensado = models.BooleanField("Retorno dispensado", default=False)
    retorno_dispensado_motivo = models.CharField("Motivo da dispensa do retorno", max_length=240, blank=True)
    retorno_dispensado_em = models.DateTimeField("Retorno dispensado em", null=True, blank=True)
    retorno_dispensado_por = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name="Retorno dispensado por",
        null=True,
        blank=True,
        related_name="checklists_retorno_dispensado",
        on_delete=models.SET_NULL,
    )
    motorista = models.CharField("Motorista", max_length=120)
    veiculo = models.CharField("Veiculo", max_length=120)
    placa = models.CharField("Placa", max_length=20)
    loja_destino = models.CharField("Loja destino", max_length=120, blank=True)
    intercala_cd = models.BooleanField("Vai intercalar em outro CD", default=False)
    cd_intercalacao = models.CharField("CD de intercalacao", max_length=3, choices=CD_CHOICES, blank=True)
    intercalacao_concluida = models.BooleanField("Intercalacao concluida", default=False)
    observacao_intercalacao = models.TextField("Observacao da intercalacao", blank=True)
    km_inicial = models.PositiveIntegerField("KM inicial", default=0)
    km_final = models.PositiveIntegerField("KM final", default=0)
    horario_saida = models.TimeField("Horario de saida", null=True, blank=True)
    horario_chegada_loja = models.TimeField("Chegada na loja", null=True, blank=True)
    horario_saida_loja = models.TimeField("Saida da loja", null=True, blank=True)
    horario_retorno = models.TimeField("Horario de retorno", null=True, blank=True)
    carga_retorno = models.BooleanField("Saiu da loja com carga/transferencia", default=False)
    carga_retorno_descricao = models.TextField("O que esta trazendo no caminhao", blank=True)

    documentacao_veiculo_ok = models.BooleanField("Documentacao do veiculo OK", default=True)
    documentacao_veiculo_obs = models.CharField("Obs. documentacao", max_length=180, blank=True)
    cnh_motorista_ok = models.BooleanField("CNH do motorista OK", default=True)
    cnh_motorista_obs = models.CharField("Obs. CNH", max_length=180, blank=True)
    documento_motorista_ok = models.BooleanField("Documento do motorista OK", default=True)
    documento_motorista_obs = models.CharField("Obs. documento motorista", max_length=180, blank=True)
    tacografo_ok = models.BooleanField("Tacografo OK", default=True)
    tacografo_obs = models.CharField("Obs. tacografo", max_length=180, blank=True)
    rastreador_ok = models.BooleanField("Rastreador OK", default=True)
    rastreador_obs = models.CharField("Obs. rastreador", max_length=180, blank=True)
    pneus_ok = models.BooleanField("Pneus OK", default=True)
    pneus_obs = models.CharField("Obs. pneus", max_length=180, blank=True)
    calibragem_pneus_ok = models.BooleanField("Calibragem dos pneus OK", default=True)
    calibragem_pneus_obs = models.CharField("Obs. calibragem", max_length=180, blank=True)
    estepe_ok = models.BooleanField("Estepe OK", default=True)
    estepe_obs = models.CharField("Obs. estepe", max_length=180, blank=True)
    macaco_chave_ok = models.BooleanField("Macaco e chave de roda OK", default=True)
    macaco_chave_obs = models.CharField("Obs. macaco/chave", max_length=180, blank=True)
    triangulo_ok = models.BooleanField("Triangulo OK", default=True)
    triangulo_obs = models.CharField("Obs. triangulo", max_length=180, blank=True)
    extintor_ok = models.BooleanField("Extintor OK", default=True)
    extintor_obs = models.CharField("Obs. extintor", max_length=180, blank=True)
    cintos_ok = models.BooleanField("Cintos de seguranca OK", default=True)
    cintos_obs = models.CharField("Obs. cintos", max_length=180, blank=True)
    farois_ok = models.BooleanField("Farois OK", default=True)
    farois_obs = models.CharField("Obs. farois", max_length=180, blank=True)
    lanternas_ok = models.BooleanField("Lanternas OK", default=True)
    lanternas_obs = models.CharField("Obs. lanternas", max_length=180, blank=True)
    luz_freio_ok = models.BooleanField("Luz de freio OK", default=True)
    luz_freio_obs = models.CharField("Obs. luz de freio", max_length=180, blank=True)
    setas_ok = models.BooleanField("Setas OK", default=True)
    setas_obs = models.CharField("Obs. setas", max_length=180, blank=True)
    luz_re_ok = models.BooleanField("Luz de re OK", default=True)
    luz_re_obs = models.CharField("Obs. luz de re", max_length=180, blank=True)
    limpador_para_brisa_ok = models.BooleanField("Limpador de para-brisa OK", default=True)
    limpador_para_brisa_obs = models.CharField("Obs. limpador", max_length=180, blank=True)
    agua_limpador_ok = models.BooleanField("Agua do limpador OK", default=True)
    agua_limpador_obs = models.CharField("Obs. agua do limpador", max_length=180, blank=True)
    buzina_ok = models.BooleanField("Buzina OK", default=True)
    buzina_obs = models.CharField("Obs. buzina", max_length=180, blank=True)
    retrovisores_ok = models.BooleanField("Retrovisores OK", default=True)
    retrovisores_obs = models.CharField("Obs. retrovisores", max_length=180, blank=True)
    lataria_ok = models.BooleanField("Lataria OK", default=True)
    lataria_obs = models.CharField("Obs. lataria", max_length=180, blank=True)
    freios_ok = models.BooleanField("Freios OK", default=True)
    freios_obs = models.CharField("Obs. freios", max_length=180, blank=True)
    embreagem_ok = models.BooleanField("Embreagem OK", default=True)
    embreagem_obs = models.CharField("Obs. embreagem", max_length=180, blank=True)
    direcao_ok = models.BooleanField("Direcao OK", default=True)
    direcao_obs = models.CharField("Obs. direcao", max_length=180, blank=True)
    suspensao_ok = models.BooleanField("Suspensao OK", default=True)
    suspensao_obs = models.CharField("Obs. suspensao", max_length=180, blank=True)
    bateria_ok = models.BooleanField("Bateria OK", default=True)
    bateria_obs = models.CharField("Obs. bateria", max_length=180, blank=True)
    vazamentos_ok = models.BooleanField("Sem vazamentos aparentes", default=True)
    vazamentos_obs = models.CharField("Obs. vazamentos", max_length=180, blank=True)
    combustivel_ok = models.BooleanField("Combustivel OK", default=True)
    combustivel_obs = models.CharField("Obs. combustivel", max_length=180, blank=True)
    arla_ok = models.BooleanField("ARLA OK", default=True)
    arla_obs = models.CharField("Obs. ARLA", max_length=180, blank=True)
    oleo_motor_ok = models.BooleanField("Oleo do motor OK", default=True)
    oleo_motor_obs = models.CharField("Obs. oleo do motor", max_length=180, blank=True)
    agua_radiador_ok = models.BooleanField("Agua do radiador OK", default=True)
    agua_radiador_obs = models.CharField("Obs. agua do radiador", max_length=180, blank=True)
    cabine_limpa_saida_ok = models.BooleanField("Cabine limpa na saida", default=True)
    cabine_limpa_saida_obs = models.CharField("Obs. cabine saida", max_length=180, blank=True)
    bau_limpo_saida_ok = models.BooleanField("Bau limpo na saida", default=True)
    bau_limpo_saida_obs = models.CharField("Obs. bau saida", max_length=180, blank=True)
    vedacao_bau_ok = models.BooleanField("Vedacao do bau OK", default=True)
    vedacao_bau_obs = models.CharField("Obs. vedacao bau", max_length=180, blank=True)
    portas_travas_ok = models.BooleanField("Portas e travas OK", default=True)
    portas_travas_obs = models.CharField("Obs. portas/travas", max_length=180, blank=True)

    pane_mecanica = models.BooleanField("Pane mecanica", default=False)
    furo_pneu = models.BooleanField("Furo de pneu", default=False)
    acidente = models.BooleanField("Acidente", default=False)
    multa = models.BooleanField("Multa", default=False)
    avaria_carga = models.BooleanField("Avaria na carga", default=False)
    vazamento_viagem = models.BooleanField("Vazamento durante viagem", default=False)
    outra_ocorrencia = models.BooleanField("Outra ocorrencia", default=False)
    descricao_ocorrencia = models.TextField("Descricao da ocorrencia", blank=True)
    ocorrencia_rota_loja = models.TextField("Ocorrencia no caminho ou na loja", blank=True)

    veiculo_limpo_retorno_ok = models.BooleanField("Veiculo limpo no retorno", default=True)
    veiculo_limpo_retorno_obs = models.CharField("Obs. veiculo retorno", max_length=180, blank=True)
    bau_limpo_retorno_ok = models.BooleanField("Bau limpo no retorno", default=True)
    bau_limpo_retorno_obs = models.CharField("Obs. bau retorno", max_length=180, blank=True)
    combustivel_informado_ok = models.BooleanField("Combustivel informado", default=True)
    combustivel_informado_obs = models.CharField("Obs. combustivel retorno", max_length=180, blank=True)
    novas_avarias_ok = models.BooleanField("Sem novas avarias", default=True)
    novas_avarias_obs = models.CharField("Obs. novas avarias", max_length=180, blank=True)
    pneus_sem_danos_ok = models.BooleanField("Pneus sem danos no retorno", default=True)
    pneus_sem_danos_obs = models.CharField("Obs. pneus retorno", max_length=180, blank=True)
    documentacao_entregue_ok = models.BooleanField("Documentacao entregue", default=True)
    documentacao_entregue_obs = models.CharField("Obs. documentacao retorno", max_length=180, blank=True)
    chaves_devolvidas_ok = models.BooleanField("Chaves devolvidas", default=True)
    chaves_devolvidas_obs = models.CharField("Obs. chaves", max_length=180, blank=True)

    necessita_manutencao = models.BooleanField("Necessita manutencao", default=False)
    descricao_manutencao = models.TextField("Descricao da manutencao", blank=True)
    responsavel_frota = models.CharField("Responsavel pela frota", max_length=120, blank=True)
    itens_personalizados = models.JSONField("Itens personalizados", default=dict, blank=True)

    @property
    def km_rodado(self):
        if self.km_final and self.km_final >= self.km_inicial:
            return self.km_final - self.km_inicial
        return 0

    @staticmethod
    def _tempo_entre(inicio, fim):
        if not inicio or not fim:
            return None
        base = timezone.localdate()
        inicio_dt = timezone.datetime.combine(base, inicio)
        fim_dt = timezone.datetime.combine(base, fim)
        if fim_dt < inicio_dt:
            fim_dt += timezone.timedelta(days=1)
        return int((fim_dt - inicio_dt).total_seconds() // 60)

    @staticmethod
    def _formatar_tempo(minutos):
        if minutos is None:
            return ""
        horas, restante = divmod(minutos, 60)
        if horas:
            return f"{horas}h {restante:02d}min"
        return f"{restante}min"

    @property
    def tempo_cd_ate_loja_min(self):
        saida = self.horario_saida
        if self.tipo_checklist == "retorno" and self.saida_referencia_id:
            saida = self.saida_referencia.horario_saida
        return self._tempo_entre(saida, self.horario_chegada_loja)

    @property
    def tempo_cd_ate_loja(self):
        return self._formatar_tempo(self.tempo_cd_ate_loja_min)

    @property
    def tempo_na_loja_min(self):
        return self._tempo_entre(self.horario_chegada_loja, self.horario_saida_loja)

    @property
    def tempo_na_loja(self):
        return self._formatar_tempo(self.tempo_na_loja_min)

    @property
    def tempo_loja_ate_cd_min(self):
        return self._tempo_entre(self.horario_saida_loja, self.horario_retorno)

    @property
    def tempo_loja_ate_cd(self):
        return self._formatar_tempo(self.tempo_loja_ate_cd_min)

    @property
    def tempo_total_viagem_min(self):
        saida = self.horario_saida
        if self.tipo_checklist == "retorno" and self.saida_referencia_id:
            saida = self.saida_referencia.horario_saida
        return self._tempo_entre(saida, self.horario_retorno)

    @property
    def tempo_total_viagem(self):
        return self._formatar_tempo(self.tempo_total_viagem_min)

    @property
    def status_intercalacao(self):
        if not self.intercala_cd:
            return "Sem passagem por outro CD"
        if self.intercalacao_concluida:
            return "Passagem concluida"
        return "Pendente de passagem por outro CD"

    def clean(self):
        super().clean()

    def __str__(self):
        return f"{self.data:%d/%m/%Y} - {self.placa} - {self.motorista}"


class ChecklistFrotaItem(BaseOperacional):
    TIPO_CHECKLIST = CHECKLIST_TIPO_CHOICES
    FREQUENCIAS = [
        ("sempre", "Todo checklist"),
        ("diario", "Diario"),
        ("semanal", "Semanal"),
        ("mensal", "Mensal"),
    ]

    grupo_config = models.ForeignKey(
        "ChecklistFrotaGrupo",
        verbose_name="Grupo do checklist",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="itens",
    )
    grupo = models.CharField("Grupo", max_length=120, default="Itens adicionais")
    titulo = models.CharField("Item do checklist", max_length=180)
    campo_sistema = models.CharField("Campo do sistema", max_length=80, blank=True)
    tipo_checklist = models.CharField("Tipo de checklist", max_length=12, choices=TIPO_CHECKLIST, default="ambos")
    ordem = models.PositiveIntegerField("Ordem", default=0)
    frequencia = models.CharField("Frequencia", max_length=20, choices=FREQUENCIAS, default="sempre")
    ativo = models.BooleanField("Ativo", default=True)
    obrigatorio = models.BooleanField("Obrigatório", default=False)

    class Meta(BaseOperacional.Meta):
        ordering = ["ordem", "grupo", "titulo"]

    def __str__(self):
        return self.titulo


class ChecklistFrotaGrupo(BaseOperacional):
    TIPO_CHECKLIST = ChecklistFrotaItem.TIPO_CHECKLIST

    codigo = models.CharField("Código interno", max_length=80)
    titulo = models.CharField("Grupo do checklist", max_length=160)
    dica = models.CharField("Orientação do grupo", max_length=240, blank=True)
    tipo_checklist = models.CharField("Tipo de checklist", max_length=12, choices=TIPO_CHECKLIST, default="ambos")
    ordem = models.PositiveIntegerField("Ordem", default=0)
    ativo = models.BooleanField("Ativo", default=True)
    mostrar_motorista = models.BooleanField("Mostrar para motorista", default=True)

    class Meta(BaseOperacional.Meta):
        ordering = ["ordem", "titulo"]
        constraints = [
            models.UniqueConstraint(fields=["cd_unidade", "codigo"], name="checklist_grupo_unico_por_cd"),
        ]

    def __str__(self):
        return self.titulo


class MaterialFrota(BaseOperacional):
    CONDICOES = [
        ("boa", "Boa para uso"),
        ("quebrada", "Quebrada"),
        ("reparo", "Aguardando reparo"),
        ("descartada", "Descartada"),
    ]
    TIPOS = [
        ("peca", "Peca"),
        ("ferramenta", "Ferramenta"),
        ("acessorio", "Acessorio"),
        ("documento", "Documento/kit"),
        ("outro", "Outro"),
    ]
    MOVIMENTOS = [
        ("entrada", "Entrada"),
        ("saida", "Saida"),
        ("inventario", "Inventario"),
        ("baixa", "Baixa"),
    ]

    material = models.CharField("Material/peca", max_length=140)
    tipo_material = models.CharField("Tipo", max_length=20, choices=TIPOS, default="peca")
    condicao = models.CharField("Condicao", max_length=20, choices=CONDICOES, default="boa")
    movimento = models.CharField("Movimento", max_length=20, choices=MOVIMENTOS, default="inventario")
    quantidade = models.PositiveIntegerField("Quantidade", default=1)
    placa = models.CharField("Placa/veiculo", max_length=20, blank=True)
    patrimonio = models.CharField("Patrimonio", max_length=80, blank=True)
    localizacao = models.CharField("Localizacao", max_length=120, blank=True)
    responsavel = models.CharField("Responsavel", max_length=120, blank=True)

    def __str__(self):
        return f"{self.material} - {self.quantidade} - {self.get_condicao_display()}"


class LacreFrota(BaseOperacional):
    CDS_LACRE = [
        ("801", "CD 801"),
        ("806", "CD 806"),
        ("ambos", "CD 801 e CD 806"),
    ]
    MOTIVOS = [
        ("lacre_nao_confere", "Lacre nao confere"),
        ("lacre_rompido", "Lacre rompido"),
        ("lacre_faltando", "Lacre faltando"),
        ("erro_lancamento", "Erro de lancamento"),
        ("troca_lacre", "Troca de lacre"),
        ("outro", "Outro"),
    ]
    STATUS = [
        ("aberto", "Aberto"),
        ("em_analise", "Em analise"),
        ("resolvido", "Resolvido"),
        ("cancelado", "Cancelado"),
    ]

    motorista = models.CharField("Motorista", max_length=120)
    placa = models.CharField("Placa", max_length=20)
    loja_destino = models.CharField("Loja destino", max_length=120)
    lacre = models.CharField("Lacre com erro", max_length=80)
    cd_lacre = models.CharField("CD do lacre", max_length=10, choices=CDS_LACRE, default="806")
    motivo = models.CharField("Motivo do erro", max_length=30, choices=MOTIVOS, default="lacre_nao_confere")
    descricao = models.TextField("Descricao da ocorrencia", blank=True)
    status = models.CharField("Status", max_length=20, choices=STATUS, default="aberto")
    tratado_por = models.CharField("Tratado por", max_length=120, blank=True)

    def __str__(self):
        return f"{self.data:%d/%m/%Y} - {self.placa} - {self.lacre}"


class OcorrenciaOperacional(BaseOperacional):
    TIPOS = [
        ("frota", "Frota"),
        ("avaria", "Avaria"),
        ("equipamento", "Equipamento"),
        ("glpi", "GLPI/Saldo"),
        ("pessoas", "Pessoas"),
        ("recebimento", "Recebimento"),
        ("separacao", "Separacao"),
        ("expedicao", "Expedicao"),
        ("outro", "Outro"),
    ]
    SEVERIDADES = [
        ("baixa", "Baixa"),
        ("media", "Media"),
        ("alta", "Alta"),
        ("critica", "Critica"),
    ]
    STATUS = [
        ("aberto", "Aberto"),
        ("analise", "Em analise"),
        ("resolvido", "Resolvido"),
        ("recorrente", "Recorrente"),
        ("cancelado", "Cancelado"),
    ]

    tipo = models.CharField("Tipo", max_length=30, choices=TIPOS, default="outro")
    setor = models.CharField("Setor", max_length=30, choices=SETOR_CHOICES, default="recebimento")
    titulo = models.CharField("Titulo", max_length=160)
    descricao = models.TextField("Descricao")
    origem = models.CharField("Origem", max_length=120, blank=True)
    referencia = models.CharField("Referencia", max_length=120, blank=True)
    severidade = models.CharField("Severidade", max_length=20, choices=SEVERIDADES, default="media")
    status = models.CharField("Status", max_length=20, choices=STATUS, default="aberto")
    recorrente = models.BooleanField("Recorrente", default=False)
    responsavel = models.CharField("Responsavel", max_length=120, blank=True)
    assumido_por = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        related_name="ocorrencias_assumidas",
        on_delete=models.SET_NULL,
    )
    assumido_em = models.DateTimeField("Assumido em", null=True, blank=True)
    resolvido_por = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        related_name="ocorrencias_resolvidas",
        on_delete=models.SET_NULL,
    )
    resolvido_em = models.DateTimeField("Resolvido em", null=True, blank=True)
    solucao = models.TextField("Solucao", blank=True)

    def __str__(self):
        return f"{self.get_tipo_display()} - {self.titulo}"


class AprovacaoOperacional(BaseOperacional):
    NIVEIS = [
        ("supervisor", "Supervisor"),
        ("gerente", "Gerente"),
    ]
    STATUS = [
        ("pendente", "Pendente"),
        ("validado", "Validado"),
        ("aprovado", "Aprovado"),
        ("reprovado", "Reprovado"),
        ("cancelado", "Cancelado"),
    ]

    setor = models.CharField("Setor", max_length=30, choices=SETOR_CHOICES, default="recebimento")
    titulo = models.CharField("Titulo", max_length=160)
    descricao = models.TextField("Descricao")
    referencia = models.CharField("Referencia", max_length=120, blank=True)
    solicitante = models.CharField("Solicitante", max_length=120, blank=True)
    nivel_necessario = models.CharField("Nivel necessario", max_length=20, choices=NIVEIS, default="supervisor")
    status = models.CharField("Status", max_length=20, choices=STATUS, default="pendente")
    aprovado_por = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        related_name="aprovacoes_operacionais",
        on_delete=models.SET_NULL,
    )
    aprovado_em = models.DateTimeField("Aprovado em", null=True, blank=True)
    justificativa = models.TextField("Justificativa/decisao", blank=True)

    def __str__(self):
        return f"{self.titulo} - {self.get_status_display()}"


class FechamentoDia(BaseOperacional):
    STATUS = [
        ("rascunho", "Rascunho"),
        ("em_revisao", "Em revisao"),
        ("fechado", "Fechado"),
        ("reaberto", "Reaberto"),
    ]

    turno = models.CharField("Turno", max_length=20, choices=TURNO_CHOICES, default="dia")
    responsavel = models.CharField("Responsavel", max_length=120, blank=True)
    recebimento_fechado = models.BooleanField("Recebimento fechado", default=False)
    expedicao_fechada = models.BooleanField("Expedicao fechada", default=False)
    frota_conferida = models.BooleanField("Frota conferida", default=False)
    pessoas_registradas = models.BooleanField("Pessoas do turno registradas", default=False)
    avarias_revisadas = models.BooleanField("Avarias revisadas", default=False)
    pendencias_amanha = models.BooleanField("Pendencias abertas para amanha", default=False)
    status = models.CharField("Status", max_length=20, choices=STATUS, default="rascunho")
    resumo_gerencial = models.TextField("Resumo gerencial", blank=True)
    finalizado_por = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        related_name="fechamentos_finalizados",
        on_delete=models.SET_NULL,
    )
    finalizado_em = models.DateTimeField("Finalizado em", null=True, blank=True)

    class Meta(BaseOperacional.Meta):
        constraints = [
            models.UniqueConstraint(fields=["cd_unidade", "data", "turno"], name="fechamento_unico_por_cd_data_turno")
        ]

    def __str__(self):
        return f"Fechamento CD {self.cd_unidade} - {self.data:%d/%m/%Y} - {self.get_turno_display()}"


class MelhoriaSistema(BaseOperacional):
    TIPOS_SOLICITACAO = [
        ("melhoria", "Melhoria"),
        ("implantacao", "Nova implantação"),
        ("correcao", "Correção de problema"),
        ("outro", "Outro pedido"),
    ]
    AREAS = [
        ("geral", "Geral"),
        ("recebimento", "Recebimento"),
        ("ressuprimento", "Ressuprimento"),
        ("separacao", "Separacao"),
        ("expedicao", "Expedicao"),
        ("frota", "Frota"),
        ("equipamentos", "Equipamentos"),
        ("relatorios", "Relatorios"),
        ("acesso", "Acesso/usuarios"),
    ]
    PRIORIDADES = [
        ("baixa", "Baixa"),
        ("media", "Media"),
        ("alta", "Alta"),
        ("urgente", "Urgente"),
    ]
    STATUS = [
        ("sugestao", "Sugestao"),
        ("avaliando", "Avaliando"),
        ("aprovada", "Aprovada"),
        ("em_andamento", "Em andamento"),
        ("concluida", "Concluida"),
        ("pausada", "Pausada"),
        ("descartada", "Descartada"),
    ]

    titulo = models.CharField("Titulo da melhoria", max_length=160)
    tipo_solicitacao = models.CharField("Tipo de solicitação", max_length=20, choices=TIPOS_SOLICITACAO, default="melhoria")
    area = models.CharField("Area", max_length=40, choices=AREAS, default="geral")
    solicitante = models.CharField("Quem sugeriu", max_length=120, blank=True)
    descricao = models.TextField("Descricao da melhoria")
    impacto = models.TextField("Impacto esperado", blank=True)
    prioridade = models.CharField("Prioridade", max_length=20, choices=PRIORIDADES, default="media")
    status = models.CharField("Status", max_length=30, choices=STATUS, default="sugestao")
    responsavel = models.CharField("Responsavel", max_length=120, blank=True)
    previsao = models.DateField("Previsao", null=True, blank=True)
    decisao = models.TextField("Decisao/retorno", blank=True)
    lido_pelo_solicitante_em = models.DateTimeField(null=True, blank=True)

    def __str__(self):
        return f"{self.get_area_display()} - {self.titulo}"


class TmsVeiculo(models.Model):
    """Capacidade física do veículo — espelho de tms_veiculos do Worker."""

    id = models.CharField(max_length=80, primary_key=True)
    placa = models.CharField(max_length=20)
    capacidade_max_kg = models.FloatField(default=0)
    capacidade_max_pallets = models.PositiveIntegerField(default=0)
    capacidade_max_m3 = models.FloatField(default=0)
    ativo = models.BooleanField(default=True)

    class Meta:
        db_table = "tms_veiculos"

    def __str__(self):
        return self.placa


class TmsViagem(models.Model):
    """Viagem TMS — espelho reduzido de tms_viagens usado pela Central de Expedição."""

    STATUS_LOGISTICO = [
        ("em_patio", "Em pátio"),
        ("em_transito", "Em trânsito"),
        ("em_transporte", "Em transporte"),
        ("em_descarregamento", "Em descarregamento"),
        ("em_transito_retorno", "Em trânsito de retorno"),
        ("entregue", "Entregue"),
        ("retornou_base", "Retornou à base"),
    ]

    motorista_nome = models.CharField(max_length=160, blank=True, default="")
    veiculo_id = models.CharField(max_length=80, blank=True, default="")
    loja_codigo = models.CharField(max_length=40, blank=True, default="")
    loja_nome = models.CharField(max_length=160, blank=True, default="")
    origem_cd = models.CharField(max_length=7, blank=True, default="")
    cd_atual = models.CharField(max_length=7, blank=True, default="")
    status = models.CharField(max_length=30, default="atribuida")
    status_logistico = models.CharField(max_length=40, choices=STATUS_LOGISTICO, default="em_patio")
    peso_total_kg = models.FloatField(default=0)
    volume_total_m3 = models.FloatField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "tms_viagens"
        ordering = ["created_at", "id"]

    def __str__(self):
        return f"Viagem #{self.pk} · {self.loja_codigo}"


class TmsViagemParada(models.Model):
    id = models.CharField(max_length=80, primary_key=True)
    viagem = models.ForeignKey(TmsViagem, related_name="paradas", on_delete=models.CASCADE)
    ordem = models.PositiveIntegerField(default=1)
    romaneio_id = models.IntegerField(null=True, blank=True)
    paletes = models.PositiveIntegerField(default=0)
    status = models.CharField(max_length=40, default="pendente")

    class Meta:
        db_table = "tms_viagem_paradas"
        ordering = ["ordem", "id"]


class TmsGeofenceEvento(models.Model):
    viagem = models.ForeignKey(TmsViagem, related_name="eventos_geofence", on_delete=models.CASCADE)
    geofence_tipo = models.CharField(max_length=20, default="LOJA")
    loja_codigo = models.CharField(max_length=40, blank=True, default="")
    acao = models.CharField(max_length=10)
    ocorreu_em = models.DateTimeField()

    class Meta:
        db_table = "logistica_geofence_eventos"
        ordering = ["-ocorreu_em", "-id"]


class TmsRomaneio(models.Model):
    """Romaneio TMS — tradução de tms_romaneios (Worker)."""

    numero_romaneio = models.CharField(max_length=40, unique=True)
    data = models.DateField()
    hora = models.CharField(max_length=8, blank=True, default="")
    cd_origem = models.CharField(max_length=7, default="806")
    loja_destino = models.CharField(max_length=160, blank=True, default="")
    endereco_loja = models.CharField(max_length=240, blank=True, default="")
    motorista = models.CharField(max_length=160, blank=True, default="")
    placa = models.CharField(max_length=20, blank=True, default="")
    tipo_veiculo = models.CharField(max_length=80, blank=True, default="")
    celular_motorista = models.CharField(max_length=30, blank=True, default="")
    quantidade_nfes = models.PositiveIntegerField(default=0)
    valor_total_carga = models.FloatField(default=0)
    valor_acumulado_bluesoft = models.FloatField(default=0)
    paletes_pbr = models.PositiveIntegerField(default=0)
    paletes_chep = models.PositiveIntegerField(default=0)
    paletes_descartavel = models.PositiveIntegerField(default=0)
    total_paletes = models.PositiveIntegerField(default=0)
    responsavel = models.CharField(max_length=160, blank=True, default="")
    lacres = models.CharField(max_length=240, blank=True, default="")
    observacoes = models.TextField(blank=True, default="")
    status = models.CharField(max_length=40, default="rascunho")
    km_saida = models.FloatField(default=0)
    km_chegada = models.FloatField(default=0)
    data_saida = models.DateTimeField(null=True, blank=True)
    data_chegada_loja = models.DateTimeField(null=True, blank=True)
    data_retorno_cd = models.DateTimeField(null=True, blank=True)
    recebido_por = models.CharField(max_length=160, blank=True, default="")
    criado_por = models.CharField(max_length=160, blank=True, default="")
    atualizado_por = models.CharField(max_length=160, blank=True, default="")
    ordem_entrega = models.PositiveIntegerField(default=0)
    distancia_rota_km = models.FloatField(default=0)
    tempo_rota_minutos = models.PositiveIntegerField(default=0)
    comprovante_entrega = models.CharField(max_length=240, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "tms_romaneios"
        ordering = ["-data", "-id"]

    def __str__(self):
        return self.numero_romaneio

    @property
    def km_rodado(self):
        if self.km_chegada > self.km_saida > 0:
            return self.km_chegada - self.km_saida
        return 0


class TmsDivergencia(models.Model):
    romaneio = models.ForeignKey(TmsRomaneio, null=True, blank=True, related_name="divergencias", on_delete=models.SET_NULL)
    data = models.DateField()
    tipo = models.CharField(max_length=30, default="operacional")
    severidade = models.CharField(max_length=20, default="normal")
    descricao = models.TextField(blank=True, default="")
    responsavel = models.CharField(max_length=160, blank=True, default="")
    status = models.CharField(max_length=20, default="aberta")
    solucao = models.TextField(blank=True, default="")
    criado_por = models.CharField(max_length=160, blank=True, default="")
    atualizado_por = models.CharField(max_length=160, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "tms_divergencias"
        ordering = ["-data", "-id"]


class TmsDevolucao(models.Model):
    romaneio = models.ForeignKey(TmsRomaneio, related_name="devolucoes", on_delete=models.CASCADE)
    data = models.DateField()
    tipo = models.CharField(max_length=30, default="parcial")
    motivo = models.CharField(max_length=240, blank=True, default="")
    quantidade_nfes = models.PositiveIntegerField(default=0)
    valor_devolvido = models.FloatField(default=0)
    paletes_pbr = models.PositiveIntegerField(default=0)
    paletes_chep = models.PositiveIntegerField(default=0)
    paletes_descartavel = models.PositiveIntegerField(default=0)
    descricao = models.TextField(blank=True, default="")
    responsavel = models.CharField(max_length=160, blank=True, default="")
    status = models.CharField(max_length=20, default="aberta")
    criado_por = models.CharField(max_length=160, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "tms_devolucoes"
        ordering = ["-data", "-id"]


class TmsRota(models.Model):
    cd_origem = models.CharField(max_length=7, default="806")
    loja_codigo = models.CharField(max_length=40, blank=True, default="")
    loja_nome = models.CharField(max_length=160, blank=True, default="")
    endereco = models.CharField(max_length=240, blank=True, default="")
    cep_origem = models.CharField(max_length=12, blank=True, default="")
    cep_destino = models.CharField(max_length=12, blank=True, default="")
    distancia_km = models.FloatField(default=0)
    tempo_previsto_minutos = models.PositiveIntegerField(default=0)
    ativa = models.BooleanField(default=True)
    observacao = models.TextField(blank=True, default="")
    latitude = models.FloatField(null=True, blank=True)
    longitude = models.FloatField(null=True, blank=True)
    criado_por = models.CharField(max_length=160, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "tms_rotas"
        ordering = ["cd_origem", "loja_nome", "id"]


class TmsGeofence(models.Model):
    id = models.CharField(max_length=80, primary_key=True)
    nome = models.CharField(max_length=160)
    tipo = models.CharField(max_length=20, default="LOJA")
    latitude = models.FloatField()
    longitude = models.FloatField()
    raio_metros = models.PositiveIntegerField(default=300)
    ativo = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "tms_geofences"
        ordering = ["tipo", "id"]


class TmsTelemetria(models.Model):
    viagem = models.ForeignKey(TmsViagem, related_name="telemetrias", on_delete=models.CASCADE)
    placa = models.CharField(max_length=20, blank=True, default="")
    latitude = models.FloatField()
    longitude = models.FloatField()
    ocorreu_em = models.DateTimeField()

    class Meta:
        db_table = "tms_telemetria"
        ordering = ["-ocorreu_em", "-id"]


class TmsProdutoCapacidade(models.Model):
    """Base interna de peso/cubagem por fardo — motor de /tms/viagens/capacidade/."""

    ean = models.CharField(max_length=14, unique=True)
    descricao = models.CharField(max_length=180)
    peso_kg_fardo = models.FloatField(default=0)
    volume_m3_fardo = models.FloatField(default=0)
    paletes_por_fardo = models.FloatField(default=0.05)

    class Meta:
        db_table = "tms_produto_capacidade"


class WmsPosicao(models.Model):
    cd_codigo = models.CharField(max_length=7, default="806")
    rua = models.CharField(max_length=10)
    nivel = models.PositiveIntegerField(default=1)
    codigo = models.CharField(max_length=20)
    status = models.CharField(max_length=20, default="LIVRE")
    produto_ean = models.CharField(max_length=20, blank=True, default="")
    produto_nome = models.CharField(max_length=180, blank=True, default="")
    quantidade_paletes = models.FloatField(default=0)

    class Meta:
        db_table = "wms_posicoes"
        ordering = ["rua", "nivel", "codigo"]


class YmsDoca(models.Model):
    cd_codigo = models.CharField(max_length=7, default="806")
    codigo = models.CharField(max_length=20)
    nome = models.CharField(max_length=80, blank=True, default="")
    status = models.CharField(max_length=40, default="livre")
    ativa = models.BooleanField(default=True)

    class Meta:
        db_table = "yms_docas"
        ordering = ["codigo"]


class YmsMovimentacao(models.Model):
    cd_codigo = models.CharField(max_length=7, default="806")
    doca = models.ForeignKey(YmsDoca, null=True, blank=True, related_name="movimentacoes", on_delete=models.SET_NULL)
    placa = models.CharField(max_length=20)
    transportadora = models.CharField(max_length=160, blank=True, default="")
    motorista = models.CharField(max_length=160, blank=True, default="")
    nota_fiscal = models.CharField(max_length=40, blank=True, default="")
    tipo_operacao = models.CharField(max_length=30, default="descarregamento")
    fluxo = models.CharField(max_length=30, default="expedicao")
    status = models.CharField(max_length=40, default="aguardando_doca")
    chegada_em = models.DateTimeField()
    doca_atribuida_em = models.DateTimeField(null=True, blank=True)
    saida_em = models.DateTimeField(null=True, blank=True)
    criado_por = models.CharField(max_length=160, blank=True, default="")

    class Meta:
        db_table = "yms_movimentacoes"
        ordering = ["chegada_em", "id"]


class PageBuilderTela(models.Model):
    nome = models.CharField(max_length=120)
    fonte = models.CharField(max_length=60)
    criado_por = models.CharField(max_length=160, blank=True, default="")
    criado_em = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "page_builder_telas"
        ordering = ["-criado_em"]


class TmsRomaneioNfe(models.Model):
    """NF-e do embarque — tradução de tms_romaneio_nfes."""

    romaneio = models.ForeignKey(TmsRomaneio, related_name="nfes", on_delete=models.CASCADE)
    chave_acesso = models.CharField(max_length=44, db_index=True)
    numero = models.CharField(max_length=20, blank=True, default="")
    serie = models.CharField(max_length=8, blank=True, default="1")
    valor_total = models.FloatField(default=0)
    status_conferencia = models.CharField(max_length=20, default="pendente")

    class Meta:
        db_table = "tms_romaneio_nfes"
        ordering = ["id"]


class TmsRascunho(models.Model):
    """Pré-romaneio ainda sem carga fechada — tradução de romaneios_rascunhos."""

    status = models.CharField(max_length=40, default="pendente_conferencia")
    cd_origem = models.CharField(max_length=7, default="806")
    loja_destino = models.CharField(max_length=160, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "romaneios_rascunhos"
        ordering = ["-id"]


class TmsRascunhoNfe(models.Model):
    rascunho = models.ForeignKey(TmsRascunho, related_name="nfes", on_delete=models.CASCADE)
    chave_acesso = models.CharField(max_length=44, db_index=True)

    class Meta:
        db_table = "tms_romaneio_email_nfes"


class TmsMdfeManifesto(models.Model):
    numero = models.CharField(max_length=40)
    serie = models.CharField(max_length=8, default="1")
    status = models.CharField(max_length=20, default="rascunho")
    data_emissao = models.DateField()
    emitente_razao_social = models.CharField(max_length=180, blank=True, default="")
    emitente_cnpj = models.CharField(max_length=14, blank=True, default="")
    emitente_ie = models.CharField(max_length=20, blank=True, default="")
    uf_carregamento = models.CharField(max_length=2, blank=True, default="")
    uf_descarregamento = models.CharField(max_length=2, blank=True, default="")
    motorista_nome = models.CharField(max_length=160, blank=True, default="")
    motorista_cpf = models.CharField(max_length=11, blank=True, default="")
    placa = models.CharField(max_length=20, blank=True, default="")
    rntrc = models.CharField(max_length=20, blank=True, default="")
    lacres = models.CharField(max_length=240, blank=True, default="")
    observacoes = models.TextField(blank=True, default="")
    payload_json = models.JSONField(default=dict, blank=True)
    criado_por = models.CharField(max_length=160, blank=True, default="")
    atualizado_por = models.CharField(max_length=160, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "tms_mdfe_manifestos"
        ordering = ["-id"]


class TmsMdfeRomaneio(models.Model):
    manifesto = models.ForeignKey(TmsMdfeManifesto, related_name="vinculos", on_delete=models.CASCADE)
    romaneio = models.ForeignKey(TmsRomaneio, related_name="mdfes", on_delete=models.CASCADE)

    class Meta:
        db_table = "tms_mdfe_romaneios"
        constraints = [
            models.UniqueConstraint(fields=["manifesto", "romaneio"], name="mdfe_romaneio_unico"),
        ]


class TmsExcecaoOperacional(models.Model):
    romaneio = models.ForeignKey(TmsRomaneio, related_name="excecoes", on_delete=models.CASCADE)
    acao = models.CharField(max_length=40)
    justificativa = models.TextField()
    usuario = models.CharField(max_length=160, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "tms_excecoes_operacionais"
        ordering = ["-created_at"]


class TmsContaPalete(models.Model):
    romaneio = models.OneToOneField(TmsRomaneio, related_name="conta_palete", on_delete=models.CASCADE)
    cd_origem = models.CharField(max_length=7, blank=True, default="")
    filial = models.CharField(max_length=160, blank=True, default="")
    paletes_pbr_enviados = models.PositiveIntegerField(default=0)
    paletes_descartaveis_enviados = models.PositiveIntegerField(default=0)
    paletes_pbr_devolvidos = models.PositiveIntegerField(default=0)
    paletes_descartaveis_devolvidos = models.PositiveIntegerField(default=0)
    criado_por = models.CharField(max_length=160, blank=True, default="")
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "conta_corrente_paletes_rede"


class LogisticaDispositivo(models.Model):
    identificador = models.CharField(max_length=80, unique=True)
    nome = models.CharField(max_length=160, blank=True, default="")
    tipo = models.CharField(max_length=40, default="celular")
    motorista = models.CharField(max_length=160, blank=True, default="")
    ativo = models.BooleanField(default=True)
    latitude = models.FloatField(null=True, blank=True)
    longitude = models.FloatField(null=True, blank=True)
    ultimo_ping = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "logistica_dispositivos"
        ordering = ["nome", "identificador"]
