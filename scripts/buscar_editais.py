#!/usr/bin/env python3
"""Busca diária de editais no PNCP (Portal Nacional de Contratações Públicas).

Consulta as contratações publicadas nos últimos dias, filtra as que tratam de
licenças/assinaturas de plataformas de software (ChatGPT, Claude, Gemini,
Google Workspace, Zoom etc.), descarta as que já foram informadas em
relatórios anteriores e gera um relatório em Markdown e HTML.

Usa apenas a biblioteca padrão do Python.
"""

from __future__ import annotations

import argparse
import html
import json
import os
import re
import smtplib
import sys
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
CONFIG = RAIZ / "config"
DADOS = RAIZ / "dados"
RELATORIOS = RAIZ / "relatorios"
ARQUIVO_VISTOS = DADOS / "editais_vistos.json"

API_PNCP = "https://pncp.gov.br/api/consulta/v1/contratacoes/publicacao"
TAMANHO_PAGINA = 50
DIAS_MEMORIA = 90  # por quanto tempo lembrar de um edital já informado
FUSO_BRASILIA = timezone(timedelta(hours=-3))
LIMITE_CORPO_ISSUE = 60000


# ---------------------------------------------------------------------------
# Configuração e filtros
# ---------------------------------------------------------------------------

def normalizar(texto: str) -> str:
    """Minúsculas, sem acentos e com espaços simples."""
    texto = unicodedata.normalize("NFKD", texto or "")
    texto = "".join(c for c in texto if not unicodedata.combining(c))
    return re.sub(r"\s+", " ", texto.lower()).strip()


def ler_termos(nome_arquivo: str) -> list[str]:
    caminho = CONFIG / nome_arquivo
    if not caminho.exists():
        return []
    termos = []
    for linha in caminho.read_text(encoding="utf-8").splitlines():
        linha = linha.strip()
        if linha and not linha.startswith("#"):
            termos.append(normalizar(linha))
    return termos


def compilar(termos: list[str]) -> list[tuple[str, re.Pattern]]:
    padroes = []
    for termo in termos:
        corpo = r"\s+".join(re.escape(p) for p in termo.split(" "))
        padroes.append((termo, re.compile(rf"(?<![a-z0-9]){corpo}(?![a-z0-9])")))
    return padroes


@dataclass
class Filtro:
    principais: list[tuple[str, re.Pattern]]
    gerais: list[tuple[str, re.Pattern]]
    exclusao: list[tuple[str, re.Pattern]]
    incluir_media: bool = True

    @classmethod
    def carregar(cls, incluir_media: bool = True) -> "Filtro":
        return cls(
            compilar(ler_termos("termos_principais.txt")),
            compilar(ler_termos("termos_gerais.txt")),
            compilar(ler_termos("termos_exclusao.txt")),
            incluir_media,
        )

    def avaliar(self, texto: str) -> tuple[str | None, list[str]]:
        """Retorna (prioridade, termos encontrados). Prioridade None = descartar."""
        texto = normalizar(texto)
        if any(p.search(texto) for _, p in self.exclusao):
            return None, []
        achados = [t for t, p in self.principais if p.search(texto)]
        if achados:
            return "ALTA", achados
        if self.incluir_media:
            achados = [t for t, p in self.gerais if p.search(texto)]
            if achados:
                return "MÉDIA", achados
        return None, []


# ---------------------------------------------------------------------------
# Consulta ao PNCP
# ---------------------------------------------------------------------------

def requisitar(params: dict, tentativas: int = 6) -> dict:
    url = f"{API_PNCP}?{urllib.parse.urlencode(params)}"
    espera = 3
    for tentativa in range(1, tentativas + 1):
        try:
            req = urllib.request.Request(
                url,
                headers={"Accept": "application/json", "User-Agent": "busca-de-editais/1.0"},
            )
            with urllib.request.urlopen(req, timeout=60) as resp:
                if resp.status == 204:
                    return {"data": [], "totalPaginas": 0}
                corpo = resp.read()
                return json.loads(corpo) if corpo.strip() else {"data": [], "totalPaginas": 0}
        except urllib.error.HTTPError as erro:
            if erro.code == 204:
                return {"data": [], "totalPaginas": 0}
            if erro.code not in (429, 500, 502, 503, 504) or tentativa == tentativas:
                raise
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, ConnectionError):
            if tentativa == tentativas:
                raise
        time.sleep(espera)
        espera = min(espera * 2, 60)
    raise RuntimeError("inalcançável")


def buscar_modalidade(codigo: str, data_inicial: str, data_final: str) -> list[dict]:
    itens: list[dict] = []
    pagina = 1
    while True:
        resposta = requisitar({
            "dataInicial": data_inicial,
            "dataFinal": data_final,
            "codigoModalidadeContratacao": codigo,
            "pagina": pagina,
            "tamanhoPagina": TAMANHO_PAGINA,
        })
        itens.extend(resposta.get("data") or [])
        total_paginas = resposta.get("totalPaginas") or 0
        if pagina >= total_paginas:
            return itens
        pagina += 1
        time.sleep(0.3)


# ---------------------------------------------------------------------------
# Montagem dos resultados
# ---------------------------------------------------------------------------

@dataclass
class Edital:
    id: str
    prioridade: str
    termos: list[str]
    orgao: str
    unidade: str
    uf: str
    municipio: str
    modalidade: str
    objeto: str
    informacao: str
    valor: float | None
    publicacao: str
    abertura: str
    encerramento: str
    link_pncp: str
    link_origem: str
    extras: dict = field(default_factory=dict)


def extrair(item: dict, prioridade: str, termos: list[str]) -> Edital:
    orgao = item.get("orgaoEntidade") or {}
    unidade = item.get("unidadeOrgao") or {}
    cnpj = orgao.get("cnpj", "")
    ano = item.get("anoCompra", "")
    seq = item.get("sequencialCompra", "")
    return Edital(
        id=item.get("numeroControlePNCP") or f"{cnpj}-{ano}-{seq}",
        prioridade=prioridade,
        termos=termos,
        orgao=orgao.get("razaoSocial", "").strip(),
        unidade=(unidade.get("nomeUnidade") or "").strip(),
        uf=unidade.get("ufSigla", ""),
        municipio=unidade.get("municipioNome", ""),
        modalidade=item.get("modalidadeNome", ""),
        objeto=(item.get("objetoCompra") or "").strip(),
        informacao=(item.get("informacaoComplementar") or "").strip(),
        valor=item.get("valorTotalEstimado"),
        publicacao=item.get("dataPublicacaoPncp") or "",
        abertura=item.get("dataAberturaProposta") or "",
        encerramento=item.get("dataEncerramentoProposta") or "",
        link_pncp=f"https://pncp.gov.br/app/editais/{cnpj}/{ano}/{seq}" if cnpj else "",
        link_origem=item.get("linkSistemaOrigem") or "",
    )


def fmt_data(valor: str) -> str:
    if not valor:
        return "—"
    try:
        return datetime.fromisoformat(valor.replace("Z", "")).strftime("%d/%m/%Y %H:%M")
    except ValueError:
        return valor


def fmt_valor(valor) -> str:
    if valor in (None, "", 0):
        return "não informado / sigiloso"
    texto = f"{float(valor):,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
    return f"R$ {texto}"


def ordenar(editais: list[Edital]) -> list[Edital]:
    return sorted(editais, key=lambda e: (e.prioridade != "ALTA", e.encerramento or "9999"))


# ---------------------------------------------------------------------------
# Relatórios
# ---------------------------------------------------------------------------

def relatorio_markdown(editais, hoje, periodo, erros, total_analisado) -> str:
    alta = [e for e in editais if e.prioridade == "ALTA"]
    media = [e for e in editais if e.prioridade == "MÉDIA"]
    linhas = [
        f"# Novos editais — {hoje:%d/%m/%Y}",
        "",
        f"Período de publicação consultado no PNCP: **{periodo}**  ",
        f"Contratações analisadas: **{total_analisado}** · "
        f"Novos editais relevantes: **{len(editais)}** "
        f"({len(alta)} prioridade alta, {len(media)} prioridade média)",
        "",
    ]
    if erros:
        linhas += ["> ⚠️ **Atenção:** algumas consultas falharam e o relatório pode estar incompleto:"]
        linhas += [f"> - {erro}" for erro in erros]
        linhas.append("")
    if not editais:
        linhas.append("Nenhum edital novo na área de licenças de software foi encontrado hoje.")
    for titulo, grupo in (("🔴 Prioridade alta — plataformas que fornecemos", alta),
                          ("🟡 Prioridade média — licenças/assinaturas de software em geral", media)):
        if not grupo:
            continue
        linhas += [f"## {titulo}", ""]
        for n, e in enumerate(grupo, 1):
            local = " / ".join(p for p in (e.municipio, e.uf) if p)
            linhas += [
                f"### {n}. {e.orgao} ({local})",
                "",
                f"**Objeto:** {e.objeto}",
                "",
                f"- **Modalidade:** {e.modalidade}",
                f"- **Unidade:** {e.unidade or '—'}",
                f"- **Valor estimado:** {fmt_valor(e.valor)}",
                f"- **Publicado em:** {fmt_data(e.publicacao)}",
                f"- **Propostas:** de {fmt_data(e.abertura)} até **{fmt_data(e.encerramento)}**",
                f"- **Termos encontrados:** {', '.join(e.termos)}",
                f"- **Nº PNCP:** `{e.id}`",
            ]
            if e.link_pncp:
                linhas.append(f"- **Edital no PNCP:** {e.link_pncp}")
            if e.link_origem:
                linhas.append(f"- **Portal de origem (onde se envia a proposta):** {e.link_origem}")
            linhas.append("")
    return "\n".join(linhas).rstrip() + "\n"


def relatorio_html(editais, hoje, periodo, erros, total_analisado) -> str:
    esc = html.escape
    alta = sum(e.prioridade == "ALTA" for e in editais)
    partes = [
        "<html><body style=\"font-family:Arial,Helvetica,sans-serif;color:#222;max-width:820px\">",
        f"<h2>Novos editais — {hoje:%d/%m/%Y}</h2>",
        f"<p>Período consultado no PNCP: <b>{esc(periodo)}</b><br>"
        f"Contratações analisadas: <b>{total_analisado}</b> · Novos editais relevantes: "
        f"<b>{len(editais)}</b> ({alta} prioridade alta, {len(editais) - alta} prioridade média)</p>",
    ]
    if erros:
        partes.append("<p style=\"background:#fff4e5;padding:8px;border-left:4px solid #f0a020\">"
                      "<b>Atenção:</b> algumas consultas falharam e o relatório pode estar incompleto:<br>"
                      + "<br>".join(esc(e) for e in erros) + "</p>")
    if not editais:
        partes.append("<p>Nenhum edital novo na área de licenças de software foi encontrado hoje.</p>")
    for e in editais:
        cor = "#c0392b" if e.prioridade == "ALTA" else "#d4a017"
        local = " / ".join(p for p in (e.municipio, e.uf) if p)
        links = []
        if e.link_pncp:
            links.append(f"<a href=\"{esc(e.link_pncp)}\">Ver no PNCP</a>")
        if e.link_origem:
            links.append(f"<a href=\"{esc(e.link_origem)}\">Portal de origem</a>")
        partes.append(
            f"<div style=\"border:1px solid #ddd;border-left:5px solid {cor};padding:10px 14px;margin:14px 0\">"
            f"<div style=\"font-size:12px;color:{cor};font-weight:bold\">PRIORIDADE {e.prioridade}</div>"
            f"<div style=\"font-size:16px;font-weight:bold;margin:4px 0\">{esc(e.orgao)} ({esc(local)})</div>"
            f"<p style=\"margin:6px 0\"><b>Objeto:</b> {esc(e.objeto)}</p>"
            f"<table style=\"font-size:14px\">"
            f"<tr><td><b>Modalidade</b></td><td>{esc(e.modalidade)}</td></tr>"
            f"<tr><td><b>Valor estimado</b></td><td>{esc(fmt_valor(e.valor))}</td></tr>"
            f"<tr><td><b>Publicado em</b></td><td>{fmt_data(e.publicacao)}</td></tr>"
            f"<tr><td><b>Propostas até</b></td><td><b>{fmt_data(e.encerramento)}</b></td></tr>"
            f"<tr><td><b>Termos</b></td><td>{esc(', '.join(e.termos))}</td></tr>"
            f"<tr><td><b>Nº PNCP</b></td><td>{esc(e.id)}</td></tr>"
            f"</table><p style=\"margin:8px 0 0\">{' · '.join(links)}</p></div>"
        )
    partes.append("<p style=\"font-size:12px;color:#777\">Relatório gerado automaticamente a partir do "
                  "PNCP — Portal Nacional de Contratações Públicas.</p></body></html>")
    return "\n".join(partes)


def enviar_email(assunto: str, corpo_html: str, corpo_texto: str) -> bool:
    host = os.environ.get("SMTP_HOST")
    usuario = os.environ.get("SMTP_USUARIO")
    senha = os.environ.get("SMTP_SENHA")
    destino = os.environ.get("EMAIL_DESTINO")
    if not all((host, usuario, senha, destino)):
        print("E-mail não configurado (segredos SMTP ausentes) — envio ignorado.")
        return False
    porta = int(os.environ.get("SMTP_PORTA") or 465)
    msg = MIMEMultipart("alternative")
    msg["Subject"] = assunto
    msg["From"] = usuario
    destinatarios = [d.strip() for d in destino.split(",") if d.strip()]
    msg["To"] = ", ".join(destinatarios)
    msg.attach(MIMEText(corpo_texto, "plain", "utf-8"))
    msg.attach(MIMEText(corpo_html, "html", "utf-8"))
    if porta == 465:
        with smtplib.SMTP_SSL(host, porta, timeout=60) as smtp:
            smtp.login(usuario, senha)
            smtp.sendmail(usuario, destinatarios, msg.as_string())
    else:
        with smtplib.SMTP(host, porta, timeout=60) as smtp:
            smtp.starttls()
            smtp.login(usuario, senha)
            smtp.sendmail(usuario, destinatarios, msg.as_string())
    print(f"E-mail enviado para {msg['To']}.")
    return True


# ---------------------------------------------------------------------------
# Execução
# ---------------------------------------------------------------------------

def carregar_vistos() -> dict:
    if ARQUIVO_VISTOS.exists():
        return json.loads(ARQUIVO_VISTOS.read_text(encoding="utf-8"))
    return {}


def salvar_vistos(vistos: dict, hoje: datetime) -> None:
    limite = (hoje - timedelta(days=DIAS_MEMORIA)).strftime("%Y-%m-%d")
    vistos = {k: v for k, v in vistos.items() if v >= limite}
    DADOS.mkdir(exist_ok=True)
    ARQUIVO_VISTOS.write_text(json.dumps(vistos, indent=1, sort_keys=True, ensure_ascii=False) + "\n",
                              encoding="utf-8")


def escrever_saida_github(**valores) -> None:
    arquivo = os.environ.get("GITHUB_OUTPUT")
    if not arquivo:
        return
    with open(arquivo, "a", encoding="utf-8") as f:
        for chave, valor in valores.items():
            f.write(f"{chave}={valor}\n")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dias", type=int, help="quantos dias para trás consultar")
    ap.add_argument("--sem-email", action="store_true", help="não enviar e-mail")
    ap.add_argument("--incluir-vistos", action="store_true",
                    help="incluir editais já informados em relatórios anteriores")
    args = ap.parse_args()

    conf = json.loads((CONFIG / "configuracoes.json").read_text(encoding="utf-8"))
    filtro = Filtro.carregar(conf.get("incluir_prioridade_media", True))
    hoje = datetime.now(FUSO_BRASILIA)
    dias = args.dias if args.dias is not None else conf.get("dias_retroativos", 2)
    inicio = hoje - timedelta(days=dias)
    data_inicial, data_final = inicio.strftime("%Y%m%d"), hoje.strftime("%Y%m%d")
    periodo = f"{inicio:%d/%m/%Y} a {hoje:%d/%m/%Y}"

    vistos = carregar_vistos()
    encontrados: dict[str, Edital] = {}
    erros: list[str] = []
    total = 0
    for codigo, nome in conf["modalidades"].items():
        try:
            itens = buscar_modalidade(codigo, data_inicial, data_final)
        except Exception as erro:  # noqa: BLE001 - registrar e seguir com as demais
            erros.append(f"{nome}: {erro}")
            print(f"[ERRO] {nome}: {erro}", file=sys.stderr)
            continue
        total += len(itens)
        print(f"{nome}: {len(itens)} contratações publicadas")
        for item in itens:
            texto = f"{item.get('objetoCompra') or ''} {item.get('informacaoComplementar') or ''}"
            prioridade, termos = filtro.avaliar(texto)
            if not prioridade:
                continue
            edital = extrair(item, prioridade, termos)
            if edital.id in vistos and not args.incluir_vistos:
                continue
            encontrados[edital.id] = edital

    editais = ordenar(list(encontrados.values()))
    md = relatorio_markdown(editais, hoje, periodo, erros, total)
    htm = relatorio_html(editais, hoje, periodo, erros, total)

    RELATORIOS.mkdir(exist_ok=True)
    (RELATORIOS / f"{hoje:%Y-%m-%d}.md").write_text(md, encoding="utf-8")
    corpo_issue = md if len(md) <= LIMITE_CORPO_ISSUE else (
        md[:LIMITE_CORPO_ISSUE] + f"\n\n… relatório truncado. Veja o arquivo completo em "
                                  f"`relatorios/{hoje:%Y-%m-%d}.md`.\n")
    (DADOS / "corpo_issue.md").parent.mkdir(exist_ok=True)
    (DADOS / "corpo_issue.md").write_text(corpo_issue, encoding="utf-8")

    alta = sum(e.prioridade == "ALTA" for e in editais)
    assunto = f"Editais {hoje:%d/%m/%Y}: {len(editais)} novo(s) ({alta} prioridade alta)"
    if erros and len(erros) == len(conf["modalidades"]):
        assunto = f"Editais {hoje:%d/%m/%Y}: FALHA ao consultar o PNCP"

    if not args.sem_email:
        try:
            enviar_email(assunto, htm, md)
        except Exception as erro:  # noqa: BLE001
            print(f"[ERRO] envio de e-mail: {erro}", file=sys.stderr)

    for e in editais:
        vistos.setdefault(e.id, hoje.strftime("%Y-%m-%d"))
    salvar_vistos(vistos, hoje)

    escrever_saida_github(assunto=assunto, quantidade=len(editais))
    print(assunto)
    # Falha total na consulta: sinaliza erro para o GitHub avisar.
    return 1 if erros and len(erros) == len(conf["modalidades"]) else 0


if __name__ == "__main__":
    sys.exit(main())
