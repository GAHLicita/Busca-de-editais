"""Fontes de editais além do PNCP.

- Sebrae (Sistema S): portal "Canal do Fornecedor", que reúne as licitações de
  todas as unidades estaduais e do Sebrae Nacional. O Sebrae segue regulamento
  próprio e publica poucas licitações no PNCP.
- Querido Diário: busca textual nos diários oficiais municipais. Traz avisos de
  licitação publicados no diário da prefeitura (às vezes antes de chegarem ao
  PNCP). São trechos de texto, sem prazo e valor estruturados.

Cada função devolve (itens, erro). Uma fonte fora do ar não interrompe a busca:
o erro aparece no relatório e as demais fontes seguem normalmente.
"""
from __future__ import annotations

import html
import http.cookiejar
import json
import re
import time
import urllib.parse
import urllib.request
from datetime import datetime

AGENTE = "Mozilla/5.0 (compatible; busca-editais/1.0)"

# ---------------------------------------------------------------------------
# Sebrae — Canal do Fornecedor
# ---------------------------------------------------------------------------

SEBRAE_BASE = "https://www.scf3.sebrae.com.br/PortalCf"
SEBRAE_EM_ANDAMENTO = "5"


def _sessao_sebrae():
    """Abre a página de licitações (cookies + token antifalsificação)."""
    abridor = urllib.request.build_opener(
        urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
    abridor.addheaders = [("User-Agent", AGENTE)]
    pagina = abridor.open(f"{SEBRAE_BASE}/Licitacoes", timeout=60).read().decode("utf-8", "replace")
    m = re.search(r'name="__RequestVerificationToken" type="hidden" value="([^"]+)"', pagina)
    if not m:
        raise RuntimeError("token do portal do Sebrae não encontrado (o site pode ter mudado)")
    return abridor, m.group(1)


def _data_sebrae(texto: str) -> str:
    """'21/10/2026 09:00' (horário de Brasília) -> '2026-10-21T09:00:00'."""
    try:
        return datetime.strptime((texto or "").strip(), "%d/%m/%Y %H:%M").isoformat()
    except ValueError:
        return ""


def buscar_sebrae(tentativas: int = 3) -> tuple[list[dict], str | None]:
    """Licitações do Sebrae com situação 'Em Andamento', já no formato dos campos
    de `Edital` (sem prioridade/termos). Itens com data de abertura vencida ou sem
    data ficam de fora (o portal às vezes não atualiza a situação)."""
    ultimo_erro = ""
    for tentativa in range(tentativas):
        try:
            abridor, token = _sessao_sebrae()
            corpo = urllib.parse.urlencode({
                "__RequestVerificationToken": token, "SomenteAtivos": "true",
                "StatusId": SEBRAE_EM_ANDAMENTO, "SebraeId": "", "ModalidadeId": "",
                "Ano": "", "NumeroLicitacao": "", "PalavraChave": "",
            }).encode()
            req = urllib.request.Request(f"{SEBRAE_BASE}/Licitacoes/Filter", data=corpo,
                                         headers={"X-Requested-With": "XMLHttpRequest"})
            dados = json.loads(abridor.open(req, timeout=120).read())
            break
        except Exception as erro:  # noqa: BLE001
            ultimo_erro = str(erro)
            time.sleep(5 * (tentativa + 1))
    else:
        return [], f"Sebrae (Canal do Fornecedor): {ultimo_erro}"

    agora = datetime.now()
    itens = []
    for x in dados.get("aData") or []:
        abertura = _data_sebrae(x.get("DataHoraAberturaNotAsString") or "")
        if not abertura or datetime.fromisoformat(abertura) < agora:
            continue
        unidade = x.get("ZSebrae") or {}
        modalidade = (x.get("LicitacaoModalidade") or {}).get("Descricao", "")
        itens.append({
            "id": f"sebrae-{x['Id']}",
            "orgao": unidade.get("Descricao") or "SEBRAE",
            "unidade": f"{x.get('Numero', '')} · processo {x.get('NumeroProcesso') or '—'}",
            "uf": unidade.get("Uf") or "",
            "municipio": unidade.get("Cidade") or "",
            "modalidade": modalidade,
            "objeto": (x.get("Objeto") or "").strip(),
            "informacao": f"Local da disputa: {x.get('Local') or '—'}",
            "valor": None,
            "publicacao": _data_sebrae(x.get("DataPublicacaoString") or ""),
            "abertura": abertura,
            # No Sebrae a data informada é a da sessão pública (limite das propostas).
            "encerramento": abertura,
            "link_pncp": "",
            "link_origem": f"{SEBRAE_BASE}/Licitacoes/Detalhe?id={x['Id']}",
            "extras": {"fonte": "sebrae", "sebrae_id": x["Id"],
                       "local_disputa": x.get("Local") or ""},
        })
    return itens, None


def arquivos_sebrae(sebrae_id) -> list[tuple[str, bytes]]:
    """Baixa até 3 documentos (termo de referência e edital primeiro) de uma
    licitação do Sebrae. Retorna [(nome, conteúdo)]."""
    abridor, _ = _sessao_sebrae()
    pagina = abridor.open(f"{SEBRAE_BASE}/Licitacoes/Detalhe?id={sebrae_id}",
                          timeout=60).read().decode("utf-8", "replace")
    links = re.findall(r'data-url-predownloadarq="([^"]+)"\s+title="Download do arquivo: ([^"]+)"',
                       pagina)

    def prioridade(item):
        nome = item[1].lower()
        if "termo de refer" in nome or nome.startswith("tr"):
            return 0
        if "edital" in nome:
            return 1
        return 2

    saida = []
    links = [(caminho, html.unescape(nome)) for caminho, nome in links]
    for caminho, nome in sorted(links, key=prioridade)[:3]:
        if prioridade((caminho, nome)) == 2 and saida:
            break  # propostas e documentos de habilitação de licitantes não interessam
        # O portal registra o arquivo escolhido na sessão e depois entrega o download.
        resposta = json.loads(abridor.open(f"https://www.scf3.sebrae.com.br{caminho}",
                                           timeout=60).read())
        url = resposta.get("urlDowload") or resposta.get("urlDownload")
        if not url:
            continue
        dados = abridor.open(f"https://www.scf3.sebrae.com.br{url}", timeout=120).read(15 * 1024 * 1024)
        saida.append((nome, dados))
    return saida


# ---------------------------------------------------------------------------
# Querido Diário — diários oficiais municipais
# ---------------------------------------------------------------------------

QD_APIS = ["https://api.queridodiario.org.br/gazettes",
           "https://api.queridodiario.ok.org.br/gazettes"]
RE_AVISO_LICITACAO = re.compile(
    r"licita[cç][aã]o|preg[aã]o|dispensa|inexigibilidade|chamamento|cota[cç][aã]o|"
    r"aviso de|edital|credenciamento|concorr[eê]ncia|registro de pre[cç]os",
    re.IGNORECASE,
)


def _querystring(termos: list[str]) -> str:
    """Termos entre aspas unidos por OU (sintaxe 'simple query string')."""
    return " | ".join(f'"{t}"' for t in termos)


def buscar_querido_diario(termos: list[str], desde: str, ate: str,
                          max_diarios: int = 500) -> tuple[list[dict], str | None]:
    """Trechos de diários oficiais municipais que citam os termos, publicados entre
    `desde` e `ate` (AAAA-MM-DD). Cada item: {id, municipio, uf, data, trecho, url}."""
    ultimo_erro = ""
    for base in QD_APIS:
        itens, offset = [], 0
        try:
            while offset < max_diarios:
                params = urllib.parse.urlencode({
                    "querystring": _querystring(termos), "published_since": desde,
                    "published_until": ate, "excerpt_size": 600, "number_of_excerpts": 3,
                    "pre_tags": "", "post_tags": "", "size": 100, "offset": offset,
                    "sort_by": "descending_date",
                })
                req = urllib.request.Request(f"{base}?{params}", headers={"User-Agent": AGENTE})
                dados = json.loads(urllib.request.urlopen(req, timeout=90).read())
                diarios = dados.get("gazettes") or []
                for d in diarios:
                    for n, trecho in enumerate(d.get("excerpts") or []):
                        trecho = re.sub(r"\s+", " ", trecho).strip()
                        if not RE_AVISO_LICITACAO.search(trecho):
                            continue
                        itens.append({
                            "id": f"qd-{d.get('territory_id')}-{d.get('date')}-{d.get('edition') or ''}-{n}",
                            "municipio": d.get("territory_name") or "",
                            "uf": d.get("state_code") or "",
                            "data": d.get("date") or "",
                            "trecho": trecho,
                            "url": d.get("url") or d.get("txt_url") or "",
                        })
                offset += len(diarios)
                if len(diarios) < 100 or offset >= (dados.get("total_gazettes") or 0):
                    break
            return itens, None
        except Exception as erro:  # noqa: BLE001
            ultimo_erro = f"{base}: {erro}"
    return [], f"Querido Diário (diários municipais) indisponível — {ultimo_erro}"
