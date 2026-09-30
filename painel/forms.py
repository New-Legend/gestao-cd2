from django import forms
from django.contrib.auth.models import User
from django.db.models import Q
import re

from .models import (
    ChecklistFrota,
    ChecklistFrotaGrupo,
    ChecklistFrotaItem,
    Equipamento,
    EquipamentoManutencao,
    EscalaVeiculoFrota,
    FuncaoTurno,
    Loja,
    ParametroCD,
    PerfilAcesso,
    PessoaTurno,
    RessuprimentoPainel,
    SEPARACAO_CATEGORIAS,
    Separacao,
    SETOR_CHOICES,
    VeiculoFrota,
)
from .registry import ALL_PERMISSIONS, FIELD_LABELS


class EquipmentChoiceField(forms.ModelChoiceField):
    def label_from_instance(self, obj):
        details = [obj.patrimonio]
        if obj.equipamento:
            details.append(obj.equipamento)
        if obj.colaborador:
            details.append(obj.colaborador)
        return " - ".join(details)


def loja_codigo(valor):
    texto = str(valor or "").strip()
    match = re.match(r"^\s*0*(\d+)", texto)
    return match.group(1).zfill(2) if match else texto.upper()


def placa_key(valor):
    return re.sub(r"[^A-Z0-9]", "", str(valor or "").upper())


def cd_scope_values(cd_unidade):
    cd = str(cd_unidade or "").strip()
    if cd in {"801+806", "801-806"}:
        return ["801", "806"]
    if cd == "801":
        return ["801"]
    if cd == "806":
        return ["806"]
    return []


def dedupe_veiculos_frota(veiculos):
    vistos = set()
    unicos = []
    for veiculo in veiculos:
        key = placa_key(veiculo.placa) or f"id:{veiculo.pk}"
        if key in vistos:
            continue
        vistos.add(key)
        unicos.append(veiculo)
    return unicos


class StyledModelForm(forms.ModelForm):
    def __init__(self, *args, **kwargs):
        cd_unidade = kwargs.pop("cd_unidade", None)
        allowed_sectors = kwargs.pop("allowed_sectors", None)
        super().__init__(*args, **kwargs)
        self._apply_loja_selector(cd_unidade)
        if self._meta.model is Separacao and "categoria" in self.fields:
            parametros = list(
                ParametroCD.objects.filter(cd_unidade=cd_unidade, tipo="categoria_separacao", ativo=True).order_by("ordem", "nome")
                if cd_unidade
                else ParametroCD.objects.filter(tipo="categoria_separacao", ativo=True).order_by("cd_unidade", "ordem", "nome")
            )
            if parametros:
                self.fields["categoria"] = forms.ChoiceField(
                    label=FIELD_LABELS.get("categoria", "Categoria"),
                    choices=[("", "Selecione a categoria")] + [(item.nome, item.nome) for item in parametros],
                    required=True,
                )
            else:
                prefix = "801_" if str(cd_unidade) == "801" else "806_"
                choices = [(label, label) for value, label in SEPARACAO_CATEGORIAS if str(value).startswith(prefix)]
                if choices:
                    self.fields["categoria"] = forms.ChoiceField(
                        label=FIELD_LABELS.get("categoria", "Categoria"),
                        choices=[("", "Selecione a categoria")] + choices,
                        required=True,
                    )
        if self._meta.model is Separacao and "setor" in self.fields:
            setores = list(
                ParametroCD.objects.filter(cd_unidade=cd_unidade, tipo="setor_armazenagem", ativo=True).order_by("ordem", "nome")
                if cd_unidade
                else ParametroCD.objects.filter(tipo="setor_armazenagem", ativo=True).order_by("cd_unidade", "ordem", "nome")
            )
            if setores:
                self.fields["setor"] = forms.ChoiceField(
                    label=FIELD_LABELS.get("setor", "Setor"),
                    choices=[("", "Selecione o setor")] + [(item.nome, item.nome) for item in setores],
                    required=False,
                )
        if self._meta.model is RessuprimentoPainel:
            for field_name, tipo, placeholder in [
                ("area", "area", "Selecione a área"),
                ("rua", "rua", "Selecione a rua"),
                ("local_estoque", "local_estoque", "Selecione o local de estoque"),
                ("atividade", "atividade", "Selecione a atividade"),
            ]:
                if field_name not in self.fields:
                    continue
                parametros = list(
                    ParametroCD.objects.filter(cd_unidade=cd_unidade, tipo=tipo, ativo=True).order_by("ordem", "nome")
                    if cd_unidade
                    else ParametroCD.objects.filter(tipo=tipo, ativo=True).order_by("cd_unidade", "ordem", "nome")
                )
                if parametros:
                    self.fields[field_name] = forms.ChoiceField(
                        label=FIELD_LABELS.get(field_name, self.fields[field_name].label),
                        choices=[("", placeholder)] + [(item.nome, item.nome) for item in parametros],
                        required=False,
                    )
        if self._meta.model is EquipamentoManutencao and "equipamento_cadastrado" in self.fields:
            equipamentos = Equipamento.objects.exclude(patrimonio="").order_by("patrimonio", "equipamento")
            if cd_unidade:
                equipamentos = equipamentos.filter(cd_unidade=cd_unidade)
            self.fields["equipamento_cadastrado"] = EquipmentChoiceField(
                label="Equipamento/patrimônio",
                queryset=equipamentos,
                empty_label="Selecione o patrimônio",
                required=not bool(self.instance and self.instance.pk),
            )
        if self._meta.model in {FuncaoTurno, PessoaTurno} and "setor" in self.fields and allowed_sectors is not None:
            choices = [(value, label) for value, label in SETOR_CHOICES if value in allowed_sectors]
            current_sector = getattr(self.instance, "setor", "") if self.instance and self.instance.pk else ""
            if current_sector and current_sector not in {value for value, _label in choices}:
                choices.append(next((choice for choice in SETOR_CHOICES if choice[0] == current_sector), (current_sector, current_sector)))
            self.fields["setor"].choices = choices
        if self._meta.model is PessoaTurno and "funcao" in self.fields:
            funcoes_qs = FuncaoTurno.objects.filter(ativa=True)
            if cd_unidade:
                funcoes_qs = funcoes_qs.filter(cd_unidade=cd_unidade)
            if allowed_sectors is not None:
                funcoes_qs = funcoes_qs.filter(setor__in=allowed_sectors)
            funcoes = list(funcoes_qs.order_by("setor", "turno", "nome"))
            if funcoes:
                self.fields["funcao"] = forms.ChoiceField(
                    label=FIELD_LABELS.get("funcao", "Função"),
                    choices=[("", "Selecione a função")] + [(item.nome, str(item)) for item in funcoes],
                    required=True,
                )
        if self._meta.model is EscalaVeiculoFrota:
            veiculos_qs = VeiculoFrota.objects.filter(ativo=True)
            cd_scope = cd_scope_values(cd_unidade)
            if cd_scope:
                veiculos_qs = veiculos_qs.filter(cd_unidade__in=cd_scope)
            veiculos = dedupe_veiculos_frota(veiculos_qs.order_by("placa", "cd_unidade", "motorista", "id"))
            placas = [(veiculo.placa, f"{veiculo.motorista} - {veiculo.placa}") for veiculo in veiculos]
            motoristas_users = User.objects.filter(
                Q(is_active=True),
                Q(perfil_krill__cargo="motorista") | Q(username__in=["marlusa", "marluza"]),
            ).distinct()
            motoristas_users = motoristas_users.order_by("username")
            motoristas_from_users = {
                (user.get_full_name() or user.username).strip().upper()
                for user in motoristas_users
                if (user.get_full_name() or user.username).strip()
            }
            motoristas = sorted({veiculo.motorista for veiculo in veiculos if veiculo.motorista} | motoristas_from_users)

            def with_current(choices, current):
                current = str(current or "").strip()
                if current and current not in {value for value, _label in choices}:
                    choices.append((current, current))
                return choices

            if "placa" in self.fields:
                current = getattr(self.instance, "placa", "") if self.instance else ""
                self.fields["placa"] = forms.ChoiceField(
                    label=FIELD_LABELS.get("placa", "Placa"),
                    choices=with_current([("", "Selecione a placa/caminhao")] + placas, current),
                    required=self.fields["placa"].required,
                )
            for field_name, placeholder in [
                ("motorista_titular", "Selecione o motorista titular"),
                ("motorista_responsavel", "Selecione quem vai cobrir"),
            ]:
                if field_name not in self.fields:
                    continue
                current = getattr(self.instance, field_name, "") if self.instance else ""
                self.fields[field_name] = forms.ChoiceField(
                    label=FIELD_LABELS.get(field_name, self.fields[field_name].label),
                    choices=with_current([("", placeholder)] + [(nome, nome) for nome in motoristas], current),
                    required=self.fields[field_name].required,
                )
            if "usuario_responsavel" in self.fields:
                current = getattr(self.instance, "usuario_responsavel_id", None) if self.instance else None
                self.fields["usuario_responsavel"].queryset = motoristas_users
                self.fields["usuario_responsavel"].empty_label = "Selecione o login do motorista"
                if current and not motoristas_users.filter(pk=current).exists():
                    self.fields["usuario_responsavel"].queryset = User.objects.filter(pk=current) | motoristas_users
        if self._meta.model is ChecklistFrota and not self.is_bound:
            for name, field in self.fields.items():
                if isinstance(field.widget, forms.CheckboxInput):
                    field.initial = False
                    self.initial[name] = False
        if self._meta.model is ChecklistFrotaItem:
            if "data" in self.fields:
                self.fields.pop("data")
            if "grupo_config" in self.fields:
                grupos = (
                    ChecklistFrotaGrupo.objects.filter(cd_unidade=cd_unidade).order_by("ordem", "titulo")
                    if cd_unidade
                    else ChecklistFrotaGrupo.objects.all().order_by("cd_unidade", "ordem", "titulo")
                )
                self.fields["grupo_config"].queryset = grupos
                self.fields["grupo_config"].required = False
                self.fields["grupo_config"].empty_label = "Selecione um grupo existente"
            placeholders = {
                "grupo": "Ex.: Documentos, veiculo, retorno",
                "titulo": "Ex.: Faróis e lanternas funcionando",
                "ordem": "Ex.: 10. Menor numero aparece primeiro.",
                "observacao": "Use para orientar o supervisor ou explicar quando este item deve aparecer.",
            }
            for field_name, placeholder in placeholders.items():
                if field_name in self.fields:
                    self.fields[field_name].widget.attrs.setdefault("placeholder", placeholder)
        for name, field in self.fields.items():
            field.label = FIELD_LABELS.get(name, field.label)
            css_class = "field-check" if isinstance(field.widget, forms.CheckboxInput) else "field"
            field.widget.attrs["class"] = css_class
            if isinstance(field.widget, forms.DateInput):
                field.widget.attrs["type"] = "date"
            if isinstance(field.widget, forms.TimeInput):
                field.widget.attrs["type"] = "time"
            if isinstance(field, (forms.IntegerField, forms.DecimalField)):
                field.widget.attrs.setdefault("inputmode", "decimal")
                field.widget.attrs.setdefault("min", "0")

    def _apply_loja_selector(self, cd_unidade):
        if self._meta.model is Loja:
            return
        loja_fields = [name for name in ("loja", "loja_destino") if name in self.fields]
        if not loja_fields:
            return
        lojas_qs = (
            Loja.objects.filter(cd_unidade__in=cd_scope_values(cd_unidade), ativa=True).order_by("cd_unidade", "codigo", "nome")
            if cd_unidade
            else Loja.objects.filter(ativa=True).order_by("cd_unidade", "codigo", "nome")
        )
        lojas = {}
        for loja in lojas_qs:
            lojas.setdefault(loja_codigo(loja.codigo), str(loja))
        if not lojas:
            return
        for field_name in loja_fields:
            field = self.fields[field_name]
            placeholder = "Selecione a loja" if field_name == "loja" else "Selecione a loja de destino"
            self.fields[field_name] = forms.CharField(
                label=FIELD_LABELS.get(field_name, field.label),
                widget=forms.Select(choices=[("", placeholder)] + [(label, label) for label in lojas.values()]),
                required=field.required,
            )


def build_model_form(model, fields):
    widgets = {}
    for name in fields:
        try:
            field = model._meta.get_field(name)
        except Exception:
            continue
        if field.get_internal_type() == "DateField":
            widgets[name] = forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d")
        elif field.get_internal_type() == "TimeField":
            widgets[name] = forms.TimeInput(attrs={"type": "time"}, format="%H:%M")
        elif field.get_internal_type() == "TextField":
            widgets[name] = forms.Textarea(attrs={"rows": 3})

    return forms.modelform_factory(
        model,
        form=StyledModelForm,
        fields=fields,
        widgets=widgets,
    )


class UsuarioForm(forms.Form):
    nome = forms.CharField(label="Nome", max_length=80)
    sobrenome = forms.CharField(label="Sobrenome", max_length=120, required=False)
    login = forms.CharField(label="Login", max_length=150)
    senha = forms.CharField(label="Senha", widget=forms.PasswordInput, min_length=6)
    cargo = forms.ChoiceField(
        label="Cargo",
        choices=PerfilAcesso.CARGOS,
    )
    cd_padrao = forms.ChoiceField(label="CD padrão", choices=[("806", "CD 806"), ("801", "CD 801"), ("801+806", "CD 801-806")])
    permissoes = forms.MultipleChoiceField(
        label="Permissões",
        choices=ALL_PERMISSIONS,
        widget=forms.CheckboxSelectMultiple,
        required=False,
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for name, field in self.fields.items():
            if not isinstance(field.widget, forms.CheckboxSelectMultiple):
                field.widget.attrs["class"] = "field"
                field.widget.attrs["autocomplete"] = "off"
                field.widget.attrs["id"] = f"id_create_{name}"


class PreferenciasForm(forms.Form):
    abas_ocultas = forms.MultipleChoiceField(
        label="Abas para ocultar da minha tela",
        choices=[],
        widget=forms.CheckboxSelectMultiple,
        required=False,
    )

    def __init__(self, modules, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["abas_ocultas"].choices = [(module.key, module.title) for module in modules]


class PerfilUsuarioForm(forms.Form):
    nome = forms.CharField(label="Nome", max_length=80, required=False)
    sobrenome = forms.CharField(label="Sobrenome", max_length=120, required=False)
    senha_atual = forms.CharField(label="Senha atual", widget=forms.PasswordInput, required=False)
    nova_senha = forms.CharField(label="Nova senha", widget=forms.PasswordInput, required=False, min_length=6)
    confirmar_senha = forms.CharField(label="Confirmar nova senha", widget=forms.PasswordInput, required=False)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for field in self.fields.values():
            field.widget.attrs["class"] = "field"

    def clean(self):
        cleaned = super().clean()
        nova = cleaned.get("nova_senha")
        confirmar = cleaned.get("confirmar_senha")
        atual = cleaned.get("senha_atual")
        if nova or confirmar or atual:
            if not atual:
                self.add_error("senha_atual", "Informe sua senha atual para trocar a senha.")
            if nova != confirmar:
                self.add_error("confirmar_senha", "A confirmação não confere com a nova senha.")
        return cleaned


class SolicitacaoSenhaForm(forms.Form):
    login = forms.CharField(label="Login", max_length=150)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["login"].widget.attrs.update({"class": "field", "autocomplete": "username", "autofocus": True})


class SolicitacaoAcessoForm(forms.Form):
    tipo = forms.ChoiceField(
        label="Tipo de pedido",
        choices=[
            ("novo_login", "Novo login"),
            ("alterar_acesso", "Alterar acesso/permissoes"),
            ("reativar_login", "Reativar login"),
            ("outro", "Outro"),
        ],
    )
    nome = forms.CharField(label="Nome completo", max_length=160)
    login_desejado = forms.CharField(label="Login desejado ou atual", max_length=150, required=False)
    cd_unidade = forms.ChoiceField(label="CD", choices=[("", "Selecione"), ("806", "CD 806"), ("801", "CD 801")], required=False)
    cargo = forms.CharField(label="Cargo/função", max_length=80, required=False)
    setor = forms.CharField(label="Setor", max_length=80, required=False)
    contato = forms.CharField(label="Contato", max_length=120, required=False)
    justificativa = forms.CharField(label="Justificativa", widget=forms.Textarea, required=False)

    def __init__(self, *args, **kwargs):
        user = kwargs.pop("user", None)
        super().__init__(*args, **kwargs)
        for field in self.fields.values():
            field.widget.attrs["class"] = "field"
            field.widget.attrs["autocomplete"] = "off"
        self.fields["justificativa"].widget.attrs.update({"rows": 4})
        if user and user.is_authenticated:
            self.fields["nome"].initial = user.get_full_name() or user.username
            self.fields["login_desejado"].initial = user.username
