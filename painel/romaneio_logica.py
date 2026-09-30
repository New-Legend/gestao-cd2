"""Lógica de romaneio traduzida do Worker (index.ts).

Cobre a leitura do XML da NF-e, a classificação de palete, a prévia,
o fechamento com placa, motorista, lacres e capacidade, a edição do
acumulado BlueSoft e o vínculo de veículo. O cálculo do delta e a
peneira de chaves continuam em bluesoft_valor.py.
"""

from __future__ import annotations

import re
import uuid
from datetime import date, timedelta
from xml.etree import ElementTree

from django.db import IntegrityError, transaction
from django.utils import timezone

from .bluesoft_valor import (
    calcular_grupo_valores,
    chaves_ja_vinculadas,
    grupo_rows,
    mesmo_grupo,
    round_money,
)
from .bluesoft_valor import _gravar
from .models import (
    ProdutoGtin,
    TmsLocal,
    TmsProdutoCapacidade,
    TmsRomaneio,
    TmsRomaneioLacreFoto,
    TmsRomaneioNfe,
    TmsRomaneioPrevia,
    TmsRomaneioProduto,
    TmsVeiculo,
    VeiculoFrota,
)

_LACRE = re.compile(r"^[0-9]{7}$")
_LACRE_CAMPOS = ("lacre_01", "lacre_02", "lacre_03")
_STATUS_PLACA = {
    "pendente_conferencia",
    "conferido",
    "aguardando_conferencia",
    "aguardando_complemento",
    "em_transporte",
    "entregue",
}
_STATUS_LACRE = {
    "em_transporte",
    "em_transito",
    "em_descarregamento",
    "em_transferencia",
    "aguardando_complemento",
}
_STATUS_VINCULO = {"rascunho", "pendente_conferencia"}
_FOTO_TIPOS = {"image/jpeg", "image/png", "image/webp", "image/gif"}


def formato_numero(value) -> str:
    numeric = float(value or 0)
    text = f"{numeric:,.2f}".rstrip("0").rstrip(".")
    return text.replace(",", "X").replace(".", ",").replace("X", ".")


def _digitos(value) -> str:
    return re.sub(r"\D", "", str(value or ""))


def _compacto(value) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _placa(value) -> str:
    return re.sub(r"[^A-Z0-9]", "", _compacto(value).upper())


def _numero_xml(value) -> float:
    normalized = re.sub(r"\s", "", str(value or ""))
    if not normalized:
        return 0
    if "," in normalized:
        normalized = normalized.replace(".", "").replace(",", ".")
    try:
        return float(normalized)
    except ValueError:
        return 0


def _numero_valido(value) -> bool:
    normalized = re.sub(r"\s", "", str(value or ""))
    return bool(re.fullmatch(r"\d+(?:[.,]\d+)?", normalized))


def _data_xml(value) -> str:
    match = re.search(r"\d{4}-\d{2}-\d{2}", str(value or ""))
    return match.group(0) if match else ""


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _filho(node, name: str):
    if node is None:
        return None
    for child in list(node):
        if _local_name(child.tag) == name:
            return child
    return None


def _texto(node, name: str) -> str:
    found = _filho(node, name)
    return _compacto(found.text if found is not None and found.text else "")


def _blocos(xml: str, tag: str) -> list[str]:
    pattern = rf"<(?:(?:[A-Za-z_][\w.-]*):)?{tag}\b[^>]*>[\s\S]*?</(?:(?:[A-Za-z_][\w.-]*):)?{tag}\s*>"
    return re.findall(pattern, xml or "", flags=re.IGNORECASE)


def _tag(xml: str, tag: str) -> str:
    values = _tags(xml, tag)
    return values[0] if values else ""


def _tags(xml: str, tag: str) -> list[str]:
    pattern = rf"<(?:(?:[A-Za-z_][\w.-]*):)?{tag}\b[^>]*>([\s\S]*?)</(?:(?:[A-Za-z_][\w.-]*):)?{tag}\s*>"
    return [re.sub(r"\s+", " ", match).strip() for match in re.findall(pattern, xml or "", flags=re.IGNORECASE)]


def _bloco(xml: str, tag: str) -> str:
    found = _blocos(xml, tag)
    return found[0] if found else ""


def produtos_xml(xml: str) -> list[dict]:
    produtos = []
    for det in _blocos(xml, "det"):
        prod = _bloco(det, "prod") or det
        descricao = _tag(prod, "xProd")
        if not descricao:
            continue
        gtin = _gtin(_tag(prod, "cEAN")) or _gtin(_tag(prod, "cEANTrib"))
        produtos.append({"gtin": gtin, "descricao": descricao[:180], "quantidade": _numero_xml(_tag(prod, "qCom") or _tag(prod, "qTrib"))})
    return produtos


def _gtin(value) -> str:
    digits = _digitos(value)
    if not digits or re.fullmatch(r"0+", digits):
        return ""
    return digits[:20]


def classificacao_por_quantidade(produtos: list[dict]) -> str:
    quantidades = [max(0, float(item.get("quantidade") or 0)) for item in produtos]
    quantidades = [item for item in quantidades if item > 0]
    if not quantidades:
        return ""
    fechadas = True
    for quantidade in quantidades:
        inteiro = round(quantidade)
        if abs(quantidade - inteiro) > 0.0001 or inteiro < 50 or inteiro % 50:
            fechadas = False
            break
    return "DESCARTAVEL" if fechadas else "PBR"


def classificar_nota(nf: dict, produtos: list[dict]) -> dict:
    nota = dict(nf)
    nota["produtos"] = produtos
    por_quantidade = classificacao_por_quantidade(produtos)
    if por_quantidade:
        nota["classificacaoPalete"] = por_quantidade
        nota["descartavelAutomatico"] = 1 if por_quantidade == "DESCARTAVEL" else 0
        return nota
    descartavel = 0
    for produto in produtos:
        cadastrado = None
        if produto.get("gtin"):
            cadastrado = ProdutoGtin.objects.filter(gtin=produto["gtin"], ativo=True).order_by("-id").first()
        if not cadastrado and produto.get("descricao"):
            nome = _compacto(produto["descricao"])[:30].casefold()
            cadastrado = ProdutoGtin.objects.filter(ativo=True, descricao__iexact=produto["descricao"][:180]).order_by("-id").first()
            if not cadastrado and nome:
                cadastrado = next((item for item in ProdutoGtin.objects.filter(ativo=True).order_by("id")[:400] if nome in item.descricao.casefold()), None)
        texto = f"{getattr(cadastrado, 'descricao', '')} {getattr(cadastrado, 'categoria', '')}".casefold()
        if cadastrado and ("descart" in texto or "retorn" in texto or ("coca" in texto and "retorn" in texto)):
            descartavel = 1
            break
    nota["classificacaoPalete"] = "DESCARTAVEL" if descartavel else "PBR"
    nota["descartavelAutomatico"] = descartavel
    return nota


def _nf_de_campos(file_name, chave, numero, serie, data_emissao, emitente, destinatario, cidade, valor, peso, volumes) -> dict:
    return {
        "chaveAcesso": chave,
        "numero": numero[:40],
        "serie": (serie or "1")[:20],
        "dataEmissao": data_emissao,
        "emitenteCnpj": emitente,
        "emitenteNome": "",
        "destinatarioCnpj": destinatario,
        "destinatarioNome": "",
        "cidadeDestino": cidade[:120],
        "valorTotal": round_money(valor),
        "pesoBrutoKg": round(peso, 3),
        "totalVolumes": round(volumes, 2),
        "arquivoNome": _compacto(file_name)[:180],
        "classificacaoPalete": "PBR",
        "descartavelAutomatico": 0,
        "produtos": [],
    }


def _validar_campos(file_name, emitente, destinatario, numero, data_emissao, valor_texto, chave) -> str:
    if len(emitente) != 14:
        return f"{file_name}: CNPJ do emitente não encontrado ou inválido."
    if len(destinatario) != 14:
        return f"{file_name}: CNPJ do destinatário não encontrado ou inválido."
    if not numero:
        return f"{file_name}: número da NF-e não encontrado."
    if not data_emissao:
        return f"{file_name}: data de emissão não encontrada."
    if not valor_texto or not _numero_valido(valor_texto):
        return f"{file_name}: valor total da NF-e não encontrado ou inválido."
    if len(chave) != 44:
        return f"{file_name}: chave de acesso da NF-e não encontrada."
    return ""


def _parse_estruturado(xml: str, file_name: str) -> dict:
    if not xml or not re.search(r"<(?:(?:[A-Za-z_][\w.-]*):)?NFe\b", xml, re.IGNORECASE):
        return {"ok": False, "error": f"{file_name}: arquivo não parece ser um XML de NF-e."}
    try:
        root = ElementTree.fromstring(xml)
    except ElementTree.ParseError:
        return {"ok": False, "error": f"{file_name}: não foi possível ler o XML."}
    inf = None
    for node in root.iter():
        if _local_name(node.tag) == "infNFe":
            inf = node
            break
    if inf is None:
        return {"ok": False, "error": f"{file_name}: arquivo não parece ser um XML de NF-e."}
    ide = _filho(inf, "ide")
    emit = _filho(inf, "emit")
    dest = _filho(inf, "dest")
    total = _filho(_filho(inf, "total"), "ICMSTot")
    transp = _filho(inf, "transp")
    protocolo = None
    for node in root.iter():
        if _local_name(node.tag) == "infProt":
            protocolo = node
            break
    emitente = _digitos(_texto(emit, "CNPJ"))
    destinatario = _digitos(_texto(dest, "CNPJ"))
    numero = _texto(ide, "nNF")
    serie = _texto(ide, "serie")
    data_emissao = _data_xml(_texto(ide, "dhEmi") or _texto(ide, "dEmi") or _texto(ide, "dhSaiEnt") or _texto(ide, "dSaiEnt"))
    valor_texto = _texto(total, "vNF") or _texto(inf, "vNF")
    chave = _digitos(_texto(protocolo, "chNFe") or (inf.attrib.get("Id") or ""))
    erro = _validar_campos(file_name, emitente, destinatario, numero, data_emissao, valor_texto, chave)
    if erro:
        return {"ok": False, "error": erro}
    volumes = 0
    peso = _numero_xml(_texto(transp, "pesoB"))
    if transp is not None:
        for vol in list(transp):
            if _local_name(vol.tag) != "vol":
                continue
            volumes += _numero_xml(_texto(vol, "qVol"))
            if not peso:
                peso = _numero_xml(_texto(vol, "pesoB"))
    nota = _nf_de_campos(file_name, chave, numero, serie, data_emissao, emitente, destinatario, _texto(_filho(dest, "enderDest"), "xMun") or _texto(dest, "xMun"), _numero_xml(valor_texto), peso, volumes)
    nota["emitenteNome"] = _texto(emit, "xNome")[:180]
    nota["destinatarioNome"] = _texto(dest, "xNome")[:180]
    return {"ok": True, "nf": nota}


def _parse_legado(xml: str, file_name: str) -> dict:
    source = str(xml or "").strip()
    if not source or not re.search(r"<(?:(?:[A-Za-z_][\w.-]*):)?NFe\b", source, re.IGNORECASE):
        return {"ok": False, "error": f"{file_name}: arquivo não parece ser um XML de NF-e."}
    inf = _bloco(source, "infNFe") or source
    emit = _bloco(source, "emit")
    dest = _bloco(source, "dest")
    endereco = _bloco(dest, "enderDest")
    emitente = _digitos(_tag(emit, "CNPJ"))
    destinatario = _digitos(_tag(dest, "CNPJ"))
    numero = _tag(inf, "nNF") or _tag(source, "nNF")
    serie = _tag(inf, "serie") or _tag(source, "serie")
    data_emissao = _data_xml(_tag(inf, "dhEmi") or _tag(inf, "dEmi") or _tag(source, "dhEmi") or _tag(inf, "dhSaiEnt") or _tag(source, "dhSaiEnt"))
    valor_texto = _tag(inf, "vNF") or _tag(source, "vNF")
    chave = _digitos(_tag(source, "chNFe"))
    if len(chave) != 44:
        match = re.search(r"\bNFe(\d{44})\b", source, re.IGNORECASE)
        chave = match.group(1) if match else ""
    erro = _validar_campos(file_name, emitente, destinatario, numero, data_emissao, valor_texto, chave)
    if erro:
        return {"ok": False, "error": erro}
    pesos = _tags(source, "pesoB") or _tags(source, "pesoL")
    volumes = sum(_numero_xml(item) for item in _tags(source, "qVol"))
    nota = _nf_de_campos(file_name, chave, numero, serie, data_emissao, emitente, destinatario, _compacto(_tag(endereco, "xMun") or _tag(dest, "xMun"))[:120], _numero_xml(valor_texto), sum(_numero_xml(item) for item in pesos), volumes)
    nota["emitenteNome"] = _tag(emit, "xNome")[:180]
    nota["destinatarioNome"] = _tag(dest, "xNome")[:180]
    return {"ok": True, "nf": nota}


def parse_nfe_xml(xml: str, file_name: str) -> dict:
    estruturado = _parse_estruturado(xml, file_name)
    if estruturado.get("ok"):
        return estruturado
    legado = _parse_legado(xml, file_name)
    return legado if legado.get("ok") else estruturado


def ler_xmls(arquivos) -> dict:
    notas = []
    erros = []
    for arquivo in arquivos:
        nome = getattr(arquivo, "name", None) or "NF-e"
        tamanho = getattr(arquivo, "size", 0) or 0
        if tamanho > 10_000_000:
            erros.append(f"{nome}: o arquivo excede o limite de 10 MB.")
            continue
        try:
            xml = arquivo.read()
            if isinstance(xml, bytes):
                xml = xml.decode("utf-8", errors="replace")
            parsed = parse_nfe_xml(xml, nome)
            if not parsed.get("ok"):
                erros.append(parsed["error"])
                continue
            notas.append(classificar_nota(parsed["nf"], produtos_xml(xml)))
        except Exception as error:
            erros.append(f"{nome}: {error}")
    return {"notas": notas, "erros": erros}


def mesclar_notas(existentes: list[dict], novas: list[dict], duplicadas=0, grupo_homogeneo=True) -> dict:
    unicas = {nota["chaveAcesso"]: nota for nota in existentes}
    erros = []
    repetidas = duplicadas
    for nota in novas:
        anterior = unicas.get(nota["chaveAcesso"])
        if not anterior:
            unicas[nota["chaveAcesso"]] = nota
            continue
        igual = (
            anterior.get("numero") == nota.get("numero")
            and anterior.get("dataEmissao") == nota.get("dataEmissao")
            and anterior.get("destinatarioCnpj") == nota.get("destinatarioCnpj")
            and abs(float(anterior.get("valorTotal") or 0) - float(nota.get("valorTotal") or 0)) < 0.01
        )
        if not igual:
            erros.append(f"{nota.get('arquivoNome')}: a chave de acesso apareceu com dados diferentes.")
        else:
            repetidas += 1
    notas = list(unicas.values())
    base = notas[0] if notas else None
    if grupo_homogeneo and base and not all(
        nota.get("destinatarioCnpj") == base.get("destinatarioCnpj")
        and nota.get("emitenteCnpj") == base.get("emitenteCnpj")
        and nota.get("dataEmissao") == base.get("dataEmissao")
        for nota in notas
    ):
        erros.append("Os XMLs precisam ser do mesmo CD emitente, da mesma loja destinatária e da mesma data de emissão.")
    return {"notas": notas, "duplicadas": repetidas, "erros": erros}


def local_por_cnpj(cnpj: str, tipo: str):
    digits = _digitos(cnpj)
    if len(digits) != 14:
        return None
    for row in TmsLocal.objects.filter(cnpj=digits, ativo=True).order_by("id"):
        kind = (row.tipo or "").upper()
        if tipo == "CD" and kind != "CD":
            continue
        if tipo == "LOJA" and kind == "CD":
            continue
        code_digits = _digitos(row.codigo)
        nome = _compacto(row.nome)
        if not code_digits or not nome:
            continue
        if tipo == "CD":
            codigo = f"CD{int(code_digits)}"
        else:
            codigo = f"LJ{int(code_digits):02d}"
        return {"codigo": codigo, "nome": nome, "endereco": _compacto(row.endereco), "cep": _compacto(row.cep), "cnpj": digits, "cd": str(int(code_digits)) if tipo == "CD" else ""}
    return None


def _totais(notas: list[dict]) -> dict:
    return {
        "totalValor": round_money(sum(float(nota.get("valorTotal") or 0) for nota in notas)),
        "totalPesoBruto": round(sum(float(nota.get("pesoBrutoKg") or 0) for nota in notas), 3),
        "totalVolumes": round(sum(float(nota.get("totalVolumes") or 0) for nota in notas), 2),
    }


def montar_payload(notas: list[dict], duplicadas=0, ignore_rascunho_id=0) -> dict:
    base = notas[0] if notas else None
    if not base:
        return {"ok": False, "error": "Não encontrei uma NF-e válida nos arquivos enviados."}
    origem = local_por_cnpj(base.get("emitenteCnpj"), "CD")
    if not origem:
        return {"ok": False, "error": f"Não encontrei no cadastro um CD ativo com o CNPJ {base.get('emitenteCnpj')}."}
    loja = local_por_cnpj(base.get("destinatarioCnpj"), "LOJA")
    if not loja:
        return {"ok": False, "error": f"Não encontrei no cadastro uma loja ativa com o CNPJ {base.get('destinatarioCnpj')}."}
    existentes = chaves_ja_vinculadas([nota["chaveAcesso"] for nota in notas], ignore_rascunho_id)
    frescas = [nota for nota in notas if nota["chaveAcesso"] not in existentes]
    descartadas = duplicadas + len(existentes)
    if not frescas:
        return {"ok": False, "error": "Todas as NF-e enviadas já estão vinculadas a romaneios. Nenhum lançamento duplicado foi criado."}
    totais = _totais(frescas)
    return {
        "ok": True,
        "duplicateCount": descartadas,
        "payload": {
            "data": base["dataEmissao"],
            "cdOrigem": origem["cd"],
            "origemNome": origem["nome"],
            "emitenteCnpj": base["emitenteCnpj"],
            "lojaDestino": f"{loja['codigo']} - {loja['nome']}".strip(),
            "enderecoLoja": loja["endereco"],
            "cidadeDestino": _compacto(base.get("cidadeDestino"))[:120],
            "cepDestino": loja["cep"],
            "destinatarioCnpj": base["destinatarioCnpj"],
            "notas": frescas,
            "duplicadas": descartadas,
            **totais,
        },
    }


def preparar_previa(arquivos, username: str) -> dict:
    if not arquivos:
        return {"ok": False, "error": "Selecione pelo menos um XML de NF-e para continuar."}
    lidos = ler_xmls(arquivos)
    if lidos["erros"]:
        return {"ok": False, "error": " ".join(lidos["erros"])}
    if not lidos["notas"]:
        return {"ok": False, "error": "Não encontrei uma NF-e válida nos arquivos enviados."}
    merged = mesclar_notas([], lidos["notas"])
    if merged["erros"]:
        return {"ok": False, "error": " ".join(merged["erros"])}
    built = montar_payload(merged["notas"], merged["duplicadas"])
    if not built.get("ok"):
        return built
    token = uuid.uuid4().hex
    limite = timezone.now() - timedelta(days=1)
    TmsRomaneioPrevia.objects.filter(created_at__lt=limite).delete()
    TmsRomaneioPrevia.objects.create(token=token, criado_por=username[:160], payload_json=built["payload"])
    return {"ok": True, "token": token, "payload": built["payload"], "duplicateCount": built["duplicateCount"]}


def carregar_previa(token: str, username: str):
    if not token:
        return None
    limite = timezone.now() - timedelta(days=1)
    return TmsRomaneioPrevia.objects.filter(token=token, criado_por=username, created_at__gte=limite).first()


def total_descartaveis(notas: list[dict]) -> int:
    return sum(1 for nota in notas if int(nota.get("descartavelAutomatico") or 0) == 1)


def calcular_pbr(total_nfs: int, automatico: int, chep=0, manual=0) -> int:
    return max(0, int(total_nfs) - int(automatico) - int(chep) - int(manual))


def separar_lacres(texto: str) -> list[str]:
    return [item for item in re.split(r"[\s,;|/]+", str(texto or "")) if item][:3]


def lacres_em_uso(lacres: list[str], exclude_id=0) -> set[str]:
    wanted = {item for item in lacres if _LACRE.fullmatch(item or "")}
    if not wanted:
        return set()
    found = set()
    rows = TmsRomaneio.objects.filter(status__in=_STATUS_LACRE)
    if exclude_id:
        rows = rows.exclude(pk=exclude_id)
    for row in rows.only("lacres"):
        for item in re.split(r"[^0-9A-Za-z]+", row.lacres or ""):
            if item in wanted:
                found.add(item)
    return found


def conferir_lacres(valores: dict, exclude_id=0, opcional=False) -> dict:
    preenchidos = [key for key in _LACRE_CAMPOS if valores.get(key)]
    invalidos = [key for key in _LACRE_CAMPOS if (valores.get(key) or not opcional) and not _LACRE.fullmatch(valores.get(key) or "")]
    if invalidos:
        return {"ok": False, "message": "Cada lacre deve ter exatamente 7 dígitos numéricos.", "campos": {key: "O lacre deve ter 7 dígitos" for key in invalidos}}
    repetidos = [key for index, key in enumerate(preenchidos) if any(valores[other] == valores[key] for other in preenchidos[:index])]
    if repetidos:
        return {"ok": False, "message": f"Lacre {valores[repetidos[0]]} repetido nesta carga.", "campos": {key: "Lacre repetido nesta carga. Insira um novo número." for key in repetidos}}
    usados = lacres_em_uso([valores[key] for key in preenchidos], exclude_id)
    duplicados = [key for key in preenchidos if valores[key] in usados]
    if duplicados:
        numeros = ", ".join(valores[key] for key in duplicados)
        return {"ok": False, "message": f"Lacre {numeros} já utilizado em outra carga. Insira um novo número.", "campos": {key: "Lacre já utilizado em outra carga. Insira um novo número." for key in duplicados}}
    return {"ok": True}


def placa_em_conflito(placa: str, allowed_id=0):
    normalizada = _placa(placa)
    if not normalizada:
        return None
    for row in TmsRomaneio.objects.filter(status__in=_STATUS_PLACA).exclude(pk=allowed_id).order_by("-id"):
        if _placa(row.placa) == normalizada:
            return row
    return None


def motorista_da_frota(placa: str, informado: str) -> str:
    plate = _compacto(placa).upper()
    entrada = _compacto(informado)
    frota = VeiculoFrota.objects.filter(placa__iexact=plate, ativo=True).order_by("-id").first() if plate else None
    nome = _compacto(frota.motorista) if frota else ""
    if nome and (not entrada or entrada.isdigit() or entrada.casefold() == nome.casefold()):
        return nome[:160]
    return entrada[:160]


def tipo_do_veiculo(placa: str, atual="") -> str:
    frota = VeiculoFrota.objects.filter(placa__iexact=_compacto(placa), ativo=True).order_by("-id").first() if placa else None
    if frota and frota.tipo_caminhao:
        return frota.tipo_caminhao[:80]
    if "EGG" in (placa or "").upper():
        return "Carreta"
    return atual or ""


def veiculo_ativo(placa: str):
    normalizada = _placa(placa)
    for row in TmsVeiculo.objects.filter(ativo=True):
        if _placa(row.placa) == normalizada:
            return row
    return None


def peso_da_carga(notas: list[dict]) -> float:
    total = 0
    for nota in notas:
        for produto in nota.get("produtos") or []:
            ean = _gtin(produto.get("gtin"))
            if not ean:
                continue
            base = TmsProdutoCapacidade.objects.filter(ean=ean).first()
            if base:
                total += float(base.peso_kg_fardo or 0) * float(produto.get("quantidade") or 0)
    return round(total, 3)


def proximo_numero(data_iso: str) -> str:
    year = (data_iso or "")[:4] or str(timezone.localdate().year)
    prefix = f"ROM-{year}-"
    ultimo = TmsRomaneio.objects.filter(numero_romaneio__startswith=prefix).order_by("-numero_romaneio").values_list("numero_romaneio", flat=True).first()
    atual = 0
    if ultimo and str(ultimo).startswith(prefix):
        try:
            atual = int(str(ultimo)[len(prefix):])
        except ValueError:
            atual = 0
    return f"{prefix}{atual + 1:04d}"


def _data_de(texto: str):
    try:
        return date.fromisoformat(texto)
    except ValueError:
        return timezone.localdate()


def salvar_foto(romaneio_id: int, arquivo, username: str) -> str:
    if not arquivo:
        return ""
    mime = getattr(arquivo, "content_type", "") or ""
    if mime not in _FOTO_TIPOS:
        return "A foto dos lacres precisa estar em JPG, PNG, WEBP ou GIF."
    conteudo = arquivo.read()
    if len(conteudo) > 900_000:
        return "A foto dos lacres deve ter no máximo 900 KB após a otimização."
    TmsRomaneioLacreFoto.objects.update_or_create(
        romaneio_id=romaneio_id,
        defaults={
            "arquivo_nome": _compacto(getattr(arquivo, "name", "foto-lacres"))[:180],
            "mime_type": mime,
            "tamanho_bytes": len(conteudo),
            "conteudo": conteudo,
            "criado_por": username[:160],
        },
    )
    return ""


def _gravar_notas(row: TmsRomaneio, notas: list[dict]) -> None:
    TmsRomaneioNfe.objects.bulk_create(
        [
            TmsRomaneioNfe(
                romaneio=row,
                chave_acesso=nota["chaveAcesso"],
                numero=str(nota.get("numero") or "")[:20],
                serie=str(nota.get("serie") or "1")[:8],
                valor_total=round_money(nota.get("valorTotal")),
                data_emissao=_data_de(nota.get("dataEmissao") or ""),
                emitente_cnpj=nota.get("emitenteCnpj") or "",
                destinatario_cnpj=nota.get("destinatarioCnpj") or "",
                peso_bruto_kg=float(nota.get("pesoBrutoKg") or 0),
                total_volumes=float(nota.get("totalVolumes") or 0),
                arquivo_nome=str(nota.get("arquivoNome") or "")[:180],
                classificacao_palete=nota.get("classificacaoPalete") or "PBR",
                descartavel_automatico=int(nota.get("descartavelAutomatico") or 0),
                status_conferencia="pendente",
            )
            for nota in notas
        ]
    )
    produtos = []
    agrupados: dict[tuple, dict] = {}
    for nota in notas:
        for produto in nota.get("produtos") or []:
            ean = _gtin(produto.get("gtin"))
            quantidade = max(0, float(produto.get("quantidade") or 0))
            if not ean or quantidade <= 0:
                continue
            chave = (nota["chaveAcesso"], ean)
            atual = agrupados.get(chave)
            if atual:
                atual["quantidade"] += quantidade
            else:
                agrupados[chave] = {"descricao": str(produto.get("descricao") or "")[:180], "quantidade": quantidade}
    for (chave, ean), produto in agrupados.items():
        produtos.append(TmsRomaneioProduto(romaneio=row, chave_acesso=chave, ean=ean, descricao=produto["descricao"], quantidade=produto["quantidade"]))
    if produtos:
        TmsRomaneioProduto.objects.bulk_create(produtos)


def finalizar_xml(payload: dict, operacional: dict, username: str, ignore_rascunho_id=0) -> dict:
    """tmsFinalizeRomaneioXml: peneira de novo, valida transporte e grava só o delta."""
    notas = list(payload.get("notas") or [])
    ja = chaves_ja_vinculadas([nota["chaveAcesso"] for nota in notas], ignore_rascunho_id)
    if ja:
        frescas = [nota for nota in notas if nota["chaveAcesso"] not in ja]
        if not frescas:
            return {"sucesso": False, "erro": "Todas as NF-e desta prévia já constam em romaneios da mesma loja. Nenhuma duplicidade foi criada."}
        payload = {**payload, "notas": frescas, "duplicadas": int(payload.get("duplicadas") or 0) + len(ja), **_totais(frescas)}
        notas = frescas
    placa = _compacto(operacional.get("placa")).upper()[:20]
    motorista = motorista_da_frota(placa, operacional.get("motorista") or "")
    lacres = " ".join(_compacto(operacional.get(key)) for key in _LACRE_CAMPOS if _compacto(operacional.get(key)))
    paletes_texto = str(operacional.get("total_paletes") or "").strip()
    if not placa or not motorista or len(separar_lacres(lacres)) != 3 or not paletes_texto:
        return {"sucesso": False, "erro": "Preencha placa, motorista, os três lacres e o total de paletes físicos antes de salvar."}
    lacre = conferir_lacres({key: _compacto(operacional.get(key))[:20] for key in _LACRE_CAMPOS})
    if not lacre.get("ok"):
        return {"sucesso": False, "erro": lacre["message"], "campos": lacre.get("campos") or {}}
    try:
        total_paletes = int(paletes_texto)
    except ValueError:
        total_paletes = -1
    if total_paletes < 0:
        return {"sucesso": False, "erro": "O total de paletes físicos deve ser um número inteiro igual ou maior que zero."}
    veiculo = veiculo_ativo(placa)
    if veiculo and int(veiculo.capacidade_max_pallets or 0) and total_paletes > int(veiculo.capacidade_max_pallets):
        return {"sucesso": False, "erro": f"A ocupação informada ({formato_numero(total_paletes)} paletes) ultrapassa a capacidade cadastrada da placa {placa} ({formato_numero(veiculo.capacidade_max_pallets)} paletes)."}
    if veiculo:
        peso = peso_da_carga(notas)
        if peso > float(veiculo.capacidade_max_kg or 0):
            return {"sucesso": False, "erro": f"A carga calculada pela base interna ({formato_numero(peso)} kg) ultrapassa a capacidade da placa {placa} ({formato_numero(veiculo.capacidade_max_kg)} kg)."}
    conflito = placa_em_conflito(placa)
    if conflito:
        rotulo = {
            "rascunho": "Pendente",
            "pendente_conferencia": "Pendente",
            "conferido": "Conferido / romaneio pronto",
            "em_transporte": "Em transporte",
            "entregue": "Entregue na loja",
        }.get(conflito.status, conflito.status)
        return {"sucesso": False, "erro": f"A placa {placa} já está vinculada ao romaneio {conflito.numero_romaneio} ({rotulo}). Encerre a operação anterior antes de iniciar outra."}
    chep = max(0, int(operacional.get("paletes_chep") or 0)) if operacional.get("ajuste_manual") else 0
    manual = max(0, int(operacional.get("paletes_descartavel") or 0)) if operacional.get("ajuste_manual") else 0
    automaticos = total_descartaveis(notas)
    if chep + manual + automaticos > len(notas):
        return {"sucesso": False, "erro": "Os abatimentos de paletes não podem ultrapassar a quantidade de NF-e informadas."}
    data_op = _data_de(payload.get("data") or "")
    grupo = calcular_grupo_valores(grupo_rows(data_op, payload.get("cdOrigem"), payload.get("lojaDestino")))
    if not grupo.ok:
        return {"sucesso": False, "erro": grupo.error or "A sequência de valores desta loja precisa ser revisada."}
    valor = round_money(payload.get("totalValor"))
    acumulado = round_money(grupo.total + valor)
    numero = proximo_numero(payload.get("data") or "")
    try:
        with transaction.atomic():
            _gravar(grupo.updates, username)
            row = TmsRomaneio.objects.create(
                numero_romaneio=numero,
                data=data_op,
                hora=timezone.localtime().strftime("%H:%M"),
                cd_origem=str(payload.get("cdOrigem") or "")[:7],
                loja_destino=str(payload.get("lojaDestino") or "")[:160],
                endereco_loja=str(payload.get("enderecoLoja") or "")[:240],
                cidade_loja=str(payload.get("cidadeDestino") or "")[:120],
                cep_loja=str(payload.get("cepDestino") or "")[:12],
                destinatario_cnpj=str(payload.get("destinatarioCnpj") or "")[:14],
                motorista=motorista,
                placa=placa,
                tipo_veiculo=tipo_do_veiculo(placa),
                quantidade_nfes=len(notas),
                valor_total_carga=valor,
                valor_acumulado_bluesoft=acumulado,
                peso_bruto_kg=float(payload.get("totalPesoBruto") or 0),
                total_volumes=float(payload.get("totalVolumes") or 0),
                paletes_pbr=calcular_pbr(len(notas), automaticos, chep, manual),
                paletes_chep=chep,
                paletes_descartavel=automaticos + manual,
                total_paletes=total_paletes,
                responsavel=username[:160],
                lacres=lacres[:240],
                status="pendente_conferencia",
                criado_por=username[:160],
                atualizado_por=username[:160],
            )
            _gravar_notas(row, notas)
    except IntegrityError:
        return {"sucesso": False, "erro": "Uma das NF-e já foi vinculada enquanto esta prévia estava aberta. Leia os XMLs novamente."}
    return {"sucesso": True, "romaneio": row.pk, "numero": row.numero_romaneio, "valor_faturado": valor, "valor_acumulado_bluesoft": acumulado}


def data_edicao(existing: date, requested: date, status: str) -> date:
    current = timezone.localdate()
    if existing and existing < current and requested == existing and status not in {"finalizado", "cancelado"}:
        return current
    return requested


def editar_romaneio(row: TmsRomaneio, dados: dict, username: str) -> dict:
    """tmsRomaneioEdit: o número digitado é o acumulado BlueSoft, não o valor da nota."""
    if row.status == "cancelado":
        return {"sucesso": False, "erro": "Este romaneio não pode ser editado."}
    pedida = dados.get("data") or row.data
    if isinstance(pedida, str):
        pedida = _data_de(pedida)
    nova_data = data_edicao(row.data, pedida, row.status)
    novo_cd = _compacto(dados.get("cd_origem") or row.cd_origem)[:7]
    nova_loja = _compacto(dados.get("loja_destino") or row.loja_destino)[:160]
    valores = {key: _compacto(dados.get(key))[:20] for key in _LACRE_CAMPOS}
    if not any(valores.values()) and row.lacres:
        partes = separar_lacres(row.lacres)
        for index, key in enumerate(_LACRE_CAMPOS):
            valores[key] = partes[index] if index < len(partes) else ""
    lacre = conferir_lacres(valores, row.pk, True)
    if not lacre.get("ok"):
        return {"sucesso": False, "erro": lacre["message"], "campos": lacre.get("campos") or {}}
    snapshot = round_money(dados.get("snapshot"))
    placa = _compacto(dados.get("placa") or row.placa).upper()[:20]
    motorista = motorista_da_frota(placa, dados.get("motorista") if dados.get("motorista") is not None else row.motorista)
    antigo = calcular_grupo_valores(grupo_rows(row.data, row.cd_origem, row.loja_destino, exclude_id=row.pk))
    if not antigo.ok:
        return {"sucesso": False, "erro": antigo.error or "A sequência atual desta loja precisa ser revisada antes da edição."}
    igual = mesmo_grupo(row.data, row.cd_origem, row.loja_destino, nova_data, novo_cd, nova_loja)
    base = grupo_rows(row.data, row.cd_origem, row.loja_destino, exclude_id=row.pk) if igual else grupo_rows(nova_data, novo_cd, nova_loja, exclude_id=row.pk)
    from .bluesoft_valor import _Linha

    projetada = _Linha(id=row.pk, created_at=row.created_at, numero_romaneio=row.numero_romaneio, valor_total_carga=0, valor_acumulado_bluesoft=snapshot)
    calculo = calcular_grupo_valores([*base, projetada])
    if not calculo.ok:
        return {"sucesso": False, "erro": calculo.error or "O acumulado informado é menor que um lançamento posterior desta loja."}
    with transaction.atomic():
        if not igual:
            _gravar(antigo.updates, username)
        _gravar(calculo.updates, username, skip_id=row.pk)
        row.data = nova_data
        if nova_data != pedida:
            row.hora = timezone.localtime().strftime("%H:%M")
        row.cd_origem = novo_cd
        row.loja_destino = nova_loja
        row.motorista = motorista
        row.placa = placa
        row.tipo_veiculo = tipo_do_veiculo(placa, row.tipo_veiculo)
        row.lacres = " ".join(valores[key] for key in _LACRE_CAMPOS if valores[key])[:240]
        row.paletes_pbr = max(0, int(dados.get("paletes_pbr") or 0))
        row.paletes_chep = max(0, int(dados.get("paletes_chep") or 0))
        row.paletes_descartavel = max(0, int(dados.get("paletes_descartavel") or 0))
        row.total_paletes = max(0, int(dados.get("total_paletes") or 0))
        row.observacoes = str(dados.get("observacoes") if dados.get("observacoes") is not None else row.observacoes)
        row.valor_total_carga = calculo.deltas.get(row.pk, 0)
        row.valor_acumulado_bluesoft = calculo.snapshots.get(row.pk, snapshot)
        row.atualizado_por = username[:160]
        row.save()
    return {"sucesso": True, "valor_faturado": row.valor_total_carga, "valor_acumulado_bluesoft": row.valor_acumulado_bluesoft}


def vincular_veiculo(row: TmsRomaneio, dados: dict, username: str) -> dict:
    if row.status not in _STATUS_VINCULO:
        return {"ok": False, "error": "Placa, motorista e lacre só podem ser alterados antes do fechamento fiscal do romaneio.", "status": 409}
    placa = re.sub(r"[^A-Z0-9-]", "", _compacto(dados.get("placa")).upper())[:10]
    motorista = _compacto(dados.get("motorista"))[:120]
    legado = separar_lacres(str(dados.get("lacre") or ""))
    valores = {}
    for index, key in enumerate(_LACRE_CAMPOS):
        valores[key] = _compacto(dados.get(key) if dados.get(key) is not None else (legado[index] if index < len(legado) else ""))[:20]
    lacres = " ".join(valores[key] for key in _LACRE_CAMPOS if valores[key])
    if not placa and not motorista and not lacres:
        return {"ok": False, "error": "Informe a placa, o motorista ou o lacre.", "status": 422}
    lacre = conferir_lacres(valores, row.pk, True)
    if not lacre.get("ok"):
        return {"ok": False, "error": lacre["message"], "campos": lacre.get("campos") or {}, "status": 400}
    resolvido = motorista_da_frota(placa, motorista)
    tipo = tipo_do_veiculo(placa, row.tipo_veiculo if placa == (row.placa or "").upper() else "")
    row.placa = placa
    row.motorista = resolvido
    row.tipo_veiculo = tipo
    row.lacres = lacres[:240]
    row.atualizado_por = username[:160]
    row.save(update_fields=["placa", "motorista", "tipo_veiculo", "lacres", "atualizado_por", "updated_at"])
    return {"ok": True, "id": row.pk, "placa": placa, "motorista": resolvido, "lacres": row.lacres, "tipo_veiculo": tipo, "notice": f"Placa, motorista e lacre salvos no romaneio {row.numero_romaneio}."}
