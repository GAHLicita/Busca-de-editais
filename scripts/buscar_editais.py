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
from concurrent.futures import ThreadPoolExecutor, as_completed
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
# Contratações com período de recebimento de propostas em aberto
API_PNCP_ABERTOS = "https://pncp.gov.br/api/consulta/v1/contratacoes/proposta"
ABERTOS = "abertos"
TAMANHO_PAGINA = 50
DIAS_MEMORIA = 90  # por quanto tempo lembrar de um edital já informado
FUSO_BRASILIA = timezone(timedelta(hours=-3))
LIMITE_CORPO_ISSUE = 60000


# ---------------------------------------------------------------------------
# Configuração e filtros
# ---------------------------------------------------------------------------

def normalizar(texto: str) -> str:
    """Minúsculas, sem acentos, sem pontuação e com espaços simples."""
    texto = unicodedata.normalize("NFKD", texto or "")
    texto = "".join(c for c in texto if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9]+", " ", texto.lower()).strip()


def ler_termos(nome_arquivo: str) -> list[str]:
    caminho = CONFIG / nome_arquivo
    if not caminho.exists():
        return []
    termos = []
    for linha in caminho.read_text(encoding="utf-8").splitlines():
        linha = linha.strip()
        if linha and not linha.startswith("#") and normalizar(linha):
            termos.append(normalizar(linha))
    return termos


def compilar(termos: list[str]) -> list[tuple[str, re.Pattern]]:
    return [(t, re.compile(rf"(?<![a-z0-9]){re.escape(t)}(?![a-z0-9])")) for t in termos]


# Para editais que citam uma plataforma da lista principal, além das palavras
# de licenciamento também valem estas, pois o objeto costuma descrever a
# contratação como "solução", "plataforma", "renovação" etc.
CONTEXTO_EXTRA_MARCAS = [
    "software", "softwares", "plataforma", "solucao", "ferramenta", "saas", "nuvem",
    "renovacao", "ambiente", "business", "enterprise", "workplace", "usuarios",
]


@dataclass
class Filtro:
    principais: list[tuple[str, re.Pattern]]
    gerais: list[tuple[str, re.Pattern]]
    contexto: list[tuple[str, re.Pattern]]
    contexto_marcas: list[tuple[str, re.Pattern]]
    exclusao: list[tuple[str, re.Pattern]]
    exclusao_media: list[tuple[str, re.Pattern]]
    incluir_media: bool = True

    @classmethod
    def carregar(cls, incluir_media: bool = True) -> "Filtro":
        contexto = ler_termos("termos_contexto.txt")
        return cls(
            compilar(ler_termos("termos_principais.txt")),
            compilar(ler_termos("termos_gerais.txt")),
            compilar(contexto),
            compilar(contexto + CONTEXTO_EXTRA_MARCAS),
            compilar(ler_termos("termos_exclusao.txt")),
            compilar(ler_termos("termos_exclusao_media.txt")),
            incluir_media,
        )

    @staticmethod
    def _tem(padroes, texto: str) -> bool:
        return any(p.search(texto) for _, p in padroes)

    def avaliar(self, texto: str) -> tuple[str | None, list[str]]:
        """Retorna (prioridade, termos encontrados). Prioridade None = descartar."""
        texto = normalizar(texto)
        if self._tem(self.exclusao, texto):
            return None, []
        achados = [t for t, p in self.principais if p.search(texto)]
        if achados and self._tem(self.contexto_marcas, texto):
            return "ALTA", achados
        if not self.incluir_media or self._tem(self.exclusao_media, texto):
            return None, []
        achados = [t for t, p in self.gerais if p.search(texto)]
        if achados and self._tem(self.contexto, texto):
            return "MÉDIA", achados
        return None, []


# ---------------------------------------------------------------------------
# Consulta ao PNCP
# ---------------------------------------------------------------------------

def requisitar(params: dict, tentativas: int = 6, api: str = API_PNCP) -> dict:
    url = f"{api}?{urllib.parse.urlencode(params)}"
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
            if erro.code == 429 and (erro.headers.get("Retry-After") or "").isdigit():
                espera = max(espera, int(erro.headers["Retry-After"]))
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, ConnectionError):
            if tentativa == tentativas:
                raise
        time.sleep(espera)
        espera = min(espera * 2, 60)
    raise RuntimeError("inalcançável")


def _consulta(codigo: str, chave: str, pagina: int) -> tuple[dict, str]:
    """Parâmetros e endereço da consulta. chave = dia (AAAAMMDD) ou ABERTOS."""
    if chave == ABERTOS:
        limite = (datetime.now(FUSO_BRASILIA) + timedelta(days=365)).strftime("%Y%m%d")
        params = {"dataFinal": limite, "codigoModalidadeContratacao": codigo}
        api = API_PNCP_ABERTOS
    else:
        params = {"dataInicial": chave, "dataFinal": chave, "codigoModalidadeContratacao": codigo}
        api = API_PNCP
    params.update(pagina=pagina, tamanhoPagina=TAMANHO_PAGINA)
    return params, api


def buscar_todas(modalidades: dict, dias: list[str], prazo: float,
                 trabalhadores: int = 4) -> tuple[dict[str, list[dict]], list[str]]:
    """Baixa, em paralelo, todas as páginas de cada modalidade em cada dia
    (ou, com dias=[ABERTOS], todas as contratações com proposta em aberto).

    Divide a consulta por dia para que cada busca tenha poucas páginas e para
    que as páginas possam ser baixadas simultaneamente. Para quando o tempo
    limite (prazo, em time.monotonic) é atingido, devolvendo o que já obteve.
    Retorna (itens por modalidade, lista de avisos de erro).
    """
    itens: dict[str, list[dict]] = {c: [] for c in modalidades}
    falhas: list[tuple[str, str, int]] = []
    puladas: dict[str, int] = {c: 0 for c in modalidades}

    def baixar(codigo: str, dia: str, pagina: int):
        if time.monotonic() > prazo:
            return codigo, dia, pagina, None, "tempo"
        try:
            params, api = _consulta(codigo, dia, pagina)
            return codigo, dia, pagina, requisitar(params, api=api), None
        except Exception as erro:  # noqa: BLE001
            return codigo, dia, pagina, None, str(erro)

    def registrar(resultado) -> None:
        codigo, dia, pagina, resposta, erro = resultado
        if erro == "tempo":
            puladas[codigo] += 1
        elif erro:
            falhas.append((codigo, dia, pagina))
            print(f"  [falha] {modalidades[codigo]} {dia} pág. {pagina}: {erro}", flush=True)
        else:
            itens[codigo].extend(resposta.get("data") or [])

    with ThreadPoolExecutor(max_workers=trabalhadores) as pool:
        # 1ª fase: primeira página de cada (modalidade, dia) — revela o total de páginas.
        restantes = []
        futuros = [pool.submit(baixar, c, d, 1) for c in modalidades for d in dias]
        for futuro in as_completed(futuros):
            resultado = futuro.result()
            registrar(resultado)
            codigo, dia, _, resposta, _ = resultado
            if resposta:
                total = resposta.get("totalPaginas") or 0
                restantes += [(codigo, dia, p) for p in range(2, total + 1)]
        print(f"Primeiras páginas concluídas; faltam {len(restantes)} páginas.", flush=True)

        # 2ª fase: demais páginas.
        futuros = [pool.submit(baixar, *t) for t in restantes]
        for n, futuro in enumerate(as_completed(futuros), 1):
            registrar(futuro.result())
            if n % 200 == 0:
                print(f"  {n}/{len(futuros)} páginas baixadas", flush=True)

    # 3ª fase: nova tentativa, mais devagar, das páginas que falharam (ex.: limite de
    # requisições do PNCP). Páginas 1 que falharem aqui não revelam páginas seguintes.
    if falhas:
        pendentes, falhas[:] = list(falhas), []
        print(f"Tentando novamente {len(pendentes)} página(s) que falharam...", flush=True)
        time.sleep(30)
        with ThreadPoolExecutor(max_workers=2) as pool:
            for resultado in pool.map(lambda t: baixar(*t), pendentes):
                registrar(resultado)
                codigo, dia, pagina, resposta, _ = resultado
                if resposta and pagina == 1:
                    total = resposta.get("totalPaginas") or 0
                    for extra in pool.map(lambda p: baixar(codigo, dia, p), range(2, total + 1)):
                        registrar(extra)

    avisos = []
    for codigo, nome in modalidades.items():
        n_falhas = sum(1 for c, _, _ in falhas if c == codigo)
        if n_falhas:
            avisos.append(f"{nome}: {n_falhas} página(s) não puderam ser baixadas")
        if puladas[codigo]:
            avisos.append(f"{nome}: {puladas[codigo]} página(s) não consultadas por limite de tempo "
                          f"(consulte um período menor)")
    return itens, avisos


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


def encerrado(edital: Edital, agora: datetime) -> bool:
    """True se o prazo de propostas já passou (datas do PNCP são de Brasília)."""
    if not edital.encerramento:
        return False
    try:
        fim = datetime.fromisoformat(edital.encerramento.replace("Z", ""))
    except ValueError:
        return False
    return fim < agora.replace(tzinfo=None)


def agrupar(editais: list[Edital]):
    """Separa em (alta com prazo aberto, média com prazo aberto, sem prazo de proposta)."""
    editais = ordenar(editais)
    com_prazo = [e for e in editais if e.encerramento]
    return ([e for e in com_prazo if e.prioridade == "ALTA"],
            [e for e in com_prazo if e.prioridade == "MÉDIA"],
            [e for e in editais if not e.encerramento])


TITULO_ALTA = "🔴 Prioridade alta — plataformas que fornecemos"
TITULO_MEDIA = "🟡 Prioridade média — licenças de ferramentas da nossa área"
TITULO_DIRETAS = "⚪ Contratações diretas, sem prazo de proposta (acompanhamento de mercado)"
EXPLICA_DIRETAS = ("Dispensas/inexigibilidades publicadas sem disputa aberta: normalmente o órgão já "
                   "escolheu o fornecedor. Servem para saber quem compra o quê e a que preço.")


def resumo(editais, alta, media, diretas, total_analisado) -> str:
    return (f"Contratações analisadas: **{total_analisado}** · Editais novos com proposta aberta: "
            f"**{len(alta) + len(media)}** ({len(alta)} prioridade alta, {len(media)} prioridade média)"
            f" · Contratações diretas: **{len(diretas)}**")


def relatorio_markdown(editais, hoje, periodo, erros, total_analisado) -> str:
    alta, media, diretas = agrupar(editais)
    linhas = [
        f"# Novos editais — {hoje:%d/%m/%Y}",
        "",
        f"Consulta no PNCP: **{periodo}**  ",
        resumo(editais, alta, media, diretas, total_analisado),
        "",
    ]
    if erros:
        linhas += ["> ⚠️ **Atenção:** algumas consultas falharam e o relatório pode estar incompleto:"]
        linhas += [f"> - {erro}" for erro in erros]
        linhas.append("")
    if not alta and not media:
        linhas += ["Nenhum edital novo com proposta aberta na nossa área foi encontrado hoje.", ""]
    for titulo, grupo in ((TITULO_ALTA, alta), (TITULO_MEDIA, media)):
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
                f"- **Propostas até:** **{fmt_data(e.encerramento)}** (abertura {fmt_data(e.abertura)})",
                f"- **Modalidade:** {e.modalidade}",
                f"- **Unidade:** {e.unidade or '—'}",
                f"- **Valor estimado:** {fmt_valor(e.valor)}",
                f"- **Publicado em:** {fmt_data(e.publicacao)}",
                f"- **Termos encontrados:** {', '.join(e.termos)}",
                f"- **Nº PNCP:** `{e.id}`",
            ]
            if e.link_pncp:
                linhas.append(f"- **Edital no PNCP:** {e.link_pncp}")
            if e.link_origem:
                linhas.append(f"- **Portal de origem (onde se envia a proposta):** {e.link_origem}")
            linhas.append("")
    if diretas:
        linhas += [f"## {TITULO_DIRETAS}", "", EXPLICA_DIRETAS, ""]
        for e in diretas:
            local = " / ".join(p for p in (e.municipio, e.uf) if p)
            link = f" — [PNCP]({e.link_pncp})" if e.link_pncp else ""
            linhas.append(f"- **{e.orgao}** ({local}) · {e.modalidade} · {fmt_valor(e.valor)} · "
                          f"{e.objeto[:220]}{'…' if len(e.objeto) > 220 else ''}{link}")
        linhas.append("")
    return "\n".join(linhas).rstrip() + "\n"


def relatorio_html(editais, hoje, periodo, erros, total_analisado) -> str:
    esc = html.escape
    alta, media, diretas = agrupar(editais)
    partes = [
        "<html><body style=\"font-family:Arial,Helvetica,sans-serif;color:#222;max-width:820px\">",
        f"<h2>Novos editais — {hoje:%d/%m/%Y}</h2>",
        f"<p>Consulta no PNCP: <b>{esc(periodo)}</b><br>"
        f"Contratações analisadas: <b>{total_analisado}</b> · Editais novos com proposta aberta: "
        f"<b>{len(alta) + len(media)}</b> ({len(alta)} prioridade alta, {len(media)} prioridade média)"
        f" · Contratações diretas: <b>{len(diretas)}</b></p>",
    ]
    if erros:
        partes.append("<p style=\"background:#fff4e5;padding:8px;border-left:4px solid #f0a020\">"
                      "<b>Atenção:</b> algumas consultas falharam e o relatório pode estar incompleto:<br>"
                      + "<br>".join(esc(e) for e in erros) + "</p>")
    if not alta and not media:
        partes.append("<p>Nenhum edital novo com proposta aberta na nossa área foi encontrado hoje.</p>")
    for titulo, grupo, cor in ((TITULO_ALTA, alta, "#c0392b"), (TITULO_MEDIA, media, "#d4a017")):
        if not grupo:
            continue
        partes.append(f"<h3 style=\"margin-top:28px\">{esc(titulo)}</h3>")
        for e in grupo:
            local = " / ".join(p for p in (e.municipio, e.uf) if p)
            links = []
            if e.link_pncp:
                links.append(f"<a href=\"{esc(e.link_pncp)}\">Ver no PNCP</a>")
            if e.link_origem:
                links.append(f"<a href=\"{esc(e.link_origem)}\">Portal de origem (enviar proposta)</a>")
            partes.append(
                f"<div style=\"border:1px solid #ddd;border-left:5px solid {cor};padding:10px 14px;margin:14px 0\">"
                f"<div style=\"font-size:16px;font-weight:bold;margin:4px 0\">{esc(e.orgao)} ({esc(local)})</div>"
                f"<p style=\"margin:6px 0\"><b>Objeto:</b> {esc(e.objeto)}</p>"
                f"<table style=\"font-size:14px\">"
                f"<tr><td><b>Propostas até</b></td><td><b style=\"color:{cor}\">{fmt_data(e.encerramento)}</b></td></tr>"
                f"<tr><td><b>Modalidade</b></td><td>{esc(e.modalidade)}</td></tr>"
                f"<tr><td><b>Valor estimado</b></td><td>{esc(fmt_valor(e.valor))}</td></tr>"
                f"<tr><td><b>Publicado em</b></td><td>{fmt_data(e.publicacao)}</td></tr>"
                f"<tr><td><b>Termos</b></td><td>{esc(', '.join(e.termos))}</td></tr>"
                f"<tr><td><b>Nº PNCP</b></td><td>{esc(e.id)}</td></tr>"
                f"</table><p style=\"margin:8px 0 0\">{' · '.join(links)}</p></div>"
            )
    if diretas:
        partes.append(f"<h3 style=\"margin-top:28px\">{esc(TITULO_DIRETAS)}</h3>"
                      f"<p style=\"font-size:13px;color:#555\">{esc(EXPLICA_DIRETAS)}</p><ul style=\"font-size:13px\">")
        for e in diretas:
            local = " / ".join(p for p in (e.municipio, e.uf) if p)
            link = f" — <a href=\"{esc(e.link_pncp)}\">PNCP</a>" if e.link_pncp else ""
            objeto = e.objeto[:220] + ("…" if len(e.objeto) > 220 else "")
            partes.append(f"<li><b>{esc(e.orgao)}</b> ({esc(local)}) · {esc(e.modalidade)} · "
                          f"{esc(fmt_valor(e.valor))} · {esc(objeto)}{link}</li>")
        partes.append("</ul>")
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
    ap.add_argument("--abertos", action="store_true",
                    help="buscar todos os editais com prazo de proposta ainda aberto, "
                         "independentemente da data de publicação (inclui já informados)")
    ap.add_argument("--minutos", type=float,
                    help="tempo máximo de consulta ao PNCP, em minutos "
                         "(padrão: 40; 100 com --abertos)")
    args = ap.parse_args()

    conf = json.loads((CONFIG / "configuracoes.json").read_text(encoding="utf-8"))
    filtro = Filtro.carregar(conf.get("incluir_prioridade_media", True))
    hoje = datetime.now(FUSO_BRASILIA)
    dias = args.dias if args.dias is not None else conf.get("dias_retroativos", 2)
    inicio = hoje - timedelta(days=dias)
    datas = [(inicio + timedelta(days=n)).strftime("%Y%m%d") for n in range(dias + 1)]
    periodo = f"publicados de {inicio:%d/%m/%Y} a {hoje:%d/%m/%Y}"
    if args.abertos:
        datas = [ABERTOS]
        periodo = f"todos os editais com proposta aberta em {hoje:%d/%m/%Y}"
        args.incluir_vistos = True
    print(f"Consultando PNCP: {periodo}...", flush=True)

    vistos = carregar_vistos()
    encontrados: dict[str, Edital] = {}
    minutos = args.minutos or (100 if args.abertos else 40)
    prazo = time.monotonic() + minutos * 60
    por_modalidade, erros = buscar_todas(conf["modalidades"], datas, prazo)
    total = 0
    for codigo, itens in por_modalidade.items():
        total += len(itens)
        print(f"{conf['modalidades'][codigo]}: {len(itens)} contratações publicadas", flush=True)
        for item in itens:
            texto = f"{item.get('objetoCompra') or ''} {item.get('informacaoComplementar') or ''}"
            prioridade, termos = filtro.avaliar(texto)
            if not prioridade:
                continue
            edital = extrair(item, prioridade, termos)
            if encerrado(edital, hoje):
                continue
            if edital.id in vistos and not args.incluir_vistos:
                continue
            encontrados[edital.id] = edital
    falha_total = bool(erros) and total == 0

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

    alta, media, _ = agrupar(editais)
    assunto = (f"Editais {hoje:%d/%m/%Y}: {len(alta) + len(media)} novo(s) com proposta aberta "
               f"({len(alta)} prioridade alta)")
    if args.abertos:
        assunto = (f"Editais em aberto {hoje:%d/%m/%Y}: {len(alta) + len(media)} com proposta aberta "
                   f"({len(alta)} prioridade alta)")
    if falha_total:
        assunto = f"Editais {hoje:%d/%m/%Y}: FALHA ao consultar o PNCP"

    tem_novidade = bool(alta or media)
    if not tem_novidade and not falha_total:
        print("Nenhum edital novo com proposta aberta — e-mail não enviado.")
    elif not args.sem_email:
        try:
            enviar_email(assunto, htm, md)
        except Exception as erro:  # noqa: BLE001
            print(f"[ERRO] envio de e-mail: {erro}", file=sys.stderr)

    for e in editais:
        vistos.setdefault(e.id, hoje.strftime("%Y-%m-%d"))
    salvar_vistos(vistos, hoje)

    escrever_saida_github(assunto=assunto, quantidade=len(alta) + len(media),
                          publicar="true" if tem_novidade or falha_total else "false")
    print(assunto)
    # Falha total na consulta: sinaliza erro para o GitHub avisar.
    return 1 if falha_total else 0


if __name__ == "__main__":
    sys.exit(main())
