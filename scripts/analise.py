"""Análise de cada edital candidato: lê o edital no PNCP e, se houver chave da
API da Anthropic (ANTHROPIC_API_KEY), usa o Claude para identificar o produto,
dizer se a empresa pode fornecê-lo e se o edital exige fornecedor autorizado.

Sem a chave, a análise se limita a procurar no texto do edital exigências de
revenda/parceiro autorizado.
"""

from __future__ import annotations

import io
import json
import logging
import os
import re
import urllib.request
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

API_ARQUIVOS = "https://pncp.gov.br/api/pncp/v1/orgaos/{cnpj}/compras/{ano}/{seq}/arquivos"
LIMITE_DOWNLOAD = 15 * 1024 * 1024
LIMITE_PAGINAS = 60
LIMITE_LOTE_TRIAGEM = 40

# PDFs com fontes incomuns geram muitos avisos inofensivos do pypdf no log.
logging.getLogger("pypdf").setLevel(logging.ERROR)

# Trechos de edital que indicam exigência de fornecedor autorizado pelo fabricante.
RE_AUTORIZADO = re.compile(
    r"(revend\w*|distribuidor\w*|parceir\w*|credenciad\w*|representante\w*|canal)"
    r"\W+(?:\w+\W+){0,3}?(autorizad\w*|oficia\w*|certificad\w*|do fabricante|homologad\w*)"
    r"|declara[cç][aã]o\W+(?:\w+\W+){0,4}?fabricante"
    r"|carta\W+(?:de\W+|do\W+)?(solidariedade|fabricante)"
    r"|autoriza[cç][aã]o\W+(?:\w+\W+){0,2}?fabricante"
    r"|comprova\w*\W+(?:\w+\W+){0,3}?(parceria|v[ií]nculo)\W+(?:\w+\W+){0,3}?fabricante",
    re.IGNORECASE,
)
RE_RELEVANTE = re.compile(
    r"objeto|especifica[cç]|licen[cç]a|assinatura|subscri[cç]|plataforma|software|"
    r"whatsapp|usu[aá]rio|fabricante|marca|modelo|vers[aã]o|plano",
    re.IGNORECASE,
)

# Modelos que aceitam o reenvio automático para outro modelo em caso de recusa.
MODELOS_COM_FALLBACK = {"claude-opus-5-5", "claude-opus-5", "claude-fable-5-1", "claude-sonnet-5-5"}

PERFIL = Path(__file__).resolve().parent.parent / "config" / "perfil_empresa.txt"


# ---------------------------------------------------------------------------
# Leitura do edital no PNCP
# ---------------------------------------------------------------------------

def _baixar(url: str, limite: int = LIMITE_DOWNLOAD) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "busca-de-editais/1.0"})
    with urllib.request.urlopen(req, timeout=60) as resp:
        dados = resp.read(limite + 1)
    if len(dados) > limite:
        raise ValueError("arquivo grande demais")
    return dados


def _texto_pdf(dados: bytes) -> str:
    from pypdf import PdfReader

    leitor = PdfReader(io.BytesIO(dados))
    partes = []
    for pagina in leitor.pages[:LIMITE_PAGINAS]:
        try:
            partes.append(pagina.extract_text() or "")
        except Exception:  # noqa: BLE001 - página ilegível não derruba o resto
            continue
    return "\n".join(partes)


def _texto_arquivo(dados: bytes) -> str:
    if dados[:4] == b"%PDF":
        return _texto_pdf(dados)
    if dados[:2] == b"PK":
        with zipfile.ZipFile(io.BytesIO(dados)) as zf:
            textos = []
            for nome in zf.namelist():
                if nome.lower().endswith(".pdf"):
                    textos.append(_texto_pdf(zf.read(nome)))
                    if sum(map(len, textos)) > 400_000:
                        break
            return "\n".join(textos)
    return ""


def texto_do_edital(cnpj: str, ano, seq) -> tuple[str, str]:
    """Baixa os documentos do edital no PNCP. Retorna (texto, observação)."""
    try:
        lista = json.loads(_baixar(API_ARQUIVOS.format(cnpj=cnpj, ano=ano, seq=seq), 2_000_000))
    except Exception as erro:  # noqa: BLE001
        return "", f"lista de arquivos indisponível ({erro})"
    if isinstance(lista, dict):
        lista = lista.get("data") or []

    def prioridade(arq):
        titulo = f"{arq.get('titulo', '')} {arq.get('tipoDocumentoNome', '')}".lower()
        if "termo de refer" in titulo:
            return 0
        if "edital" in titulo or "aviso" in titulo:
            return 1
        return 2

    textos, lidos = [], []
    for arq in sorted(lista, key=prioridade)[:3]:
        url = arq.get("url") or arq.get("uri")
        if not url and arq.get("sequencialDocumento") is not None:
            url = (API_ARQUIVOS.format(cnpj=cnpj, ano=ano, seq=seq)
                   + f"/{arq['sequencialDocumento']}")
        if not url:
            continue
        try:
            texto = _texto_arquivo(_baixar(url))
        except Exception:  # noqa: BLE001
            continue
        if texto.strip():
            textos.append(texto)
            lidos.append(arq.get("titulo") or "documento")
    if not textos:
        return "", "edital sem texto legível no PNCP"
    return "\n\n".join(textos), "lidos: " + ", ".join(lidos)


def trechos_autorizacao(texto: str, maximo: int = 3) -> list[str]:
    trechos = []
    for m in RE_AUTORIZADO.finditer(texto):
        ini, fim = max(0, m.start() - 220), min(len(texto), m.end() + 220)
        trecho = re.sub(r"\s+", " ", texto[ini:fim]).strip()
        if not any(trecho[:80] in t for t in trechos):
            trechos.append(trecho)
        if len(trechos) >= maximo:
            break
    return trechos


def resumo_para_ia(texto: str, limite: int = 14_000) -> str:
    """Início do documento + parágrafos com termos relevantes, até o limite."""
    texto = re.sub(r"[ \t]+", " ", texto)
    paragrafos = [p.strip() for p in re.split(r"\n\s*\n|\n(?=\d+(?:\.\d+)*\s)", texto) if p.strip()]
    saida = texto[:4000]
    for p in paragrafos:
        if len(saida) >= limite:
            break
        if RE_RELEVANTE.search(p) and p[:200] not in saida:
            saida += "\n…\n" + p[:1500]
    return saida[:limite]


# ---------------------------------------------------------------------------
# IA (Claude)
# ---------------------------------------------------------------------------

def _cliente():
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return None
    try:
        import anthropic
    except ImportError:
        return None
    return anthropic.Anthropic()


def _perfil() -> str:
    linhas = PERFIL.read_text(encoding="utf-8").splitlines() if PERFIL.exists() else []
    return "\n".join(l for l in linhas if not l.startswith("#")).strip()


def _chamar(cliente, modelo: str, sistema: str, conteudo: str, schema: dict, max_tokens: int):
    """Uma chamada com saída JSON validada. Retorna o dict ou levanta erro."""
    import anthropic

    params = dict(
        model=modelo,
        max_tokens=max_tokens,
        system=sistema,
        cache_control={"type": "ephemeral"},
        messages=[{"role": "user", "content": conteudo}],
        output_config={"effort": "low", "format": {"type": "json_schema", "schema": schema}},
    )
    resp = None
    if modelo in MODELOS_COM_FALLBACK:
        try:
            resp = cliente.beta.messages.create(
                betas=["server-side-fallback-2026-07-01"], fallbacks="default", **params)
        except anthropic.BadRequestError:
            resp = None  # sem suporte a fallback: tenta a chamada simples
    if resp is None:
        resp = cliente.messages.create(**params)
    if resp.stop_reason == "refusal":
        raise RuntimeError("a IA recusou a análise")
    texto = next((b.text for b in resp.content if b.type == "text"), "")
    return json.loads(texto)


SCHEMA_TRIAGEM = {
    "type": "object",
    "properties": {
        "itens": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "relevante": {"type": "boolean"},
                    "motivo": {"type": "string"},
                },
                "required": ["id", "relevante", "motivo"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["itens"],
    "additionalProperties": False,
}

SCHEMA_ANALISE = {
    "type": "object",
    "properties": {
        "produto": {"type": "string"},
        "fabricante": {"type": "string"},
        "modo_aquisicao": {"type": "string", "enum": [
            "compra_direta_online", "exige_parceiro_ou_revenda", "sob_medida_ou_implantacao",
            "indefinido"]},
        "exige_fornecedor_autorizado": {"type": "string", "enum": [
            "sim", "nao_mencionado", "nao_foi_possivel_ler"]},
        "trecho_exigencia": {"type": "string"},
        "servicos_exigidos": {"type": "string"},
        "recomendacao": {"type": "string", "enum": ["disputar", "avaliar", "descartar"]},
        "justificativa": {"type": "string"},
    },
    "required": ["produto", "fabricante", "modo_aquisicao", "exige_fornecedor_autorizado",
                 "trecho_exigencia", "servicos_exigidos", "recomendacao", "justificativa"],
    "additionalProperties": False,
}


def triagem(candidatos: list[dict], modelo: str, avisos: list[str]) -> set[str] | None:
    """Recebe [{id, objeto}] e devolve os ids que merecem análise detalhada.
    None = IA indisponível (sem chave ou erro)."""
    cliente = _cliente()
    if cliente is None:
        return None
    sistema = (
        "Você faz a triagem de contratações públicas brasileiras para uma empresa. "
        "Perfil da empresa:\n\n" + _perfil() + "\n\n"
        "Para cada item, marque relevante=true SOMENTE se o objeto for (ou incluir) "
        "licença/assinatura de uma plataforma ou software PRONTO, de prateleira, do tipo que "
        "se compra no site do fabricante: ferramentas de IA, design/mídia, escritório e "
        "colaboração (Office, Workspace, videoconferência), armazenamento em nuvem, redes "
        "sociais/marketing, bancos de imagens, ou automação/robô/chatbot/IA para WhatsApp. "
        "Também true quando o objeto é genérico ('licenças de software', 'softwares e "
        "plataformas digitais') e pode incluir esses produtos.\n"
        "Marque false para: sistemas de gestão de qualquer área (gestão pública, contábil, "
        "tributária, folha/RH, ponto eletrônico, saúde/hospitalar/PACS, escolar, SUAS, frota, "
        "previdência, licitações/cesta de preços, protocolo/processos, gestão documental, "
        "diário oficial, portal/site/transparência, ouvidoria, BI municipal, licenciamento "
        "ambiental, georreferenciamento); soluções que exigem implantação, migração ou "
        "operação; segurança corporativa (antivírus/endpoint, firewall, backup, proxy, CDN/WAF, "
        "Zscaler, Fortinet); Autodesk/Esri/SAP/Oracle/VMware; outsourcing de impressão; "
        "hardware, monitoramento, rastreamento, videomonitoramento, ponto biométrico; "
        "serviços de atendimento/call center com operadores; cursos, eventos, obras e "
        "serviços comuns. Motivo: no máximo 12 palavras."
    )
    relevantes: set[str] = set()
    for i in range(0, len(candidatos), LIMITE_LOTE_TRIAGEM):
        lote = candidatos[i:i + LIMITE_LOTE_TRIAGEM]
        conteudo = json.dumps([{"id": c["id"], "objeto": c["objeto"][:700]} for c in lote],
                              ensure_ascii=False)
        try:
            resposta = _chamar(cliente, modelo, sistema, conteudo, SCHEMA_TRIAGEM, 8000)
        except Exception as erro:  # noqa: BLE001
            avisos.append(f"triagem por IA falhou em um lote ({erro}); itens do lote mantidos")
            relevantes.update(c["id"] for c in lote)
            continue
        relevantes.update(r["id"] for r in resposta.get("itens", []) if r.get("relevante"))
    return relevantes


def analisar(edital, modelo: str) -> dict:
    """Análise detalhada de um edital (objeto `Edital`). Sempre retorna um dict."""
    partes = (edital.link_pncp or "").rstrip("/").split("/")
    texto, obs = ("", "sem link do PNCP")
    if len(partes) >= 3:
        texto, obs = texto_do_edital(partes[-3], partes[-2], partes[-1])
    trechos = trechos_autorizacao(texto) if texto else []
    resultado = {
        "produto": "",
        "fabricante": "",
        "modo_aquisicao": "indefinido",
        "exige_fornecedor_autorizado": ("sim" if trechos else
                                        "nao_mencionado" if texto else "nao_foi_possivel_ler"),
        "trecho_exigencia": trechos[0] if trechos else "",
        "servicos_exigidos": "",
        "recomendacao": "avaliar",
        "justificativa": "",
        "leitura": obs,
        "ia": False,
    }
    cliente = _cliente()
    if cliente is None:
        return resultado
    sistema = (
        "Você é analista de licitações de uma empresa brasileira. Perfil da empresa:\n\n"
        + _perfil() + "\n\n"
        "Analise o edital e responda em português: qual produto/plataforma é pedido (nome "
        "comercial e plano, se houver) e o fabricante; como ele é adquirido (compra direta "
        "online no site do fabricante, ou só via parceiro/revenda autorizada, ou é sistema "
        "sob medida/com implantação); se o edital exige fornecedor autorizado/parceiro/"
        "declaração do fabricante (copie o trecho exato em trecho_exigencia, ou deixe vazio); "
        "quais serviços além da licença são exigidos (suporte, treinamento, implantação…); "
        "e recomende 'disputar' (a empresa consegue fornecer), 'avaliar' (é plataforma pronta "
        "vendida por assinatura no site do fabricante, mas há algo a confirmar) ou 'descartar' "
        "(fora do perfil). Seja rigoroso: descarte sistemas de gestão de qualquer área, "
        "soluções de nicho de um fornecedor específico que exigem implantação, migração, "
        "hospedagem ou operação, outsourcing, hardware, segurança corporativa e produtos "
        "vendidos só por revenda autorizada. Na dúvida entre 'avaliar' e 'descartar' para "
        "sistemas sem nome comercial conhecido, descarte. Justificativa: no máximo 2 frases. "
        "Se o produto não estiver claro, use o objeto e a especificação para deduzi-lo."
    )
    conteudo = (
        f"Órgão: {edital.orgao} ({edital.municipio}/{edital.uf})\n"
        f"Modalidade: {edital.modalidade}\n"
        f"Objeto: {edital.objeto}\n"
        f"Informação complementar: {edital.informacao[:1500]}\n\n"
        f"Trechos encontrados sobre fornecedor autorizado: {json.dumps(trechos, ensure_ascii=False)}\n\n"
        f"Texto do edital ({obs}):\n{resumo_para_ia(texto) if texto else '(não disponível)'}"
    )
    try:
        resposta = _chamar(cliente, modelo, sistema, conteudo, SCHEMA_ANALISE, 4000)
    except Exception as erro:  # noqa: BLE001
        resultado["justificativa"] = f"análise por IA indisponível ({erro})"
        return resultado
    resultado.update(resposta)
    if trechos:
        # O padrão de texto achou exigência: não deixa passar despercebido.
        resultado["exige_fornecedor_autorizado"] = "sim"
        resultado["trecho_exigencia"] = resultado["trecho_exigencia"] or trechos[0]
    resultado["ia"] = True
    return resultado


def analisar_varios(editais: list, modelo: str, trabalhadores: int = 4) -> None:
    """Analisa em paralelo e guarda o resultado em edital.extras['analise']."""
    with ThreadPoolExecutor(max_workers=trabalhadores) as pool:
        for edital, res in zip(editais, pool.map(lambda e: analisar(e, modelo), editais)):
            edital.extras["analise"] = res
