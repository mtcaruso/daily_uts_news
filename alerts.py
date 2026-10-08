"""
Sistema de alertas. Roda a cada 30min via GitHub Actions, checa news/DOU
contra config (alerts_config.py) + ranking (nota do Top do Dia) + consultas
públicas do MME, e envia push via notify.py (ntfy + Telegram + WhatsApp).

State commitado em alerts_state.json (próximo run não realerta o mesmo item).

NOTA: a detecção de Fato Relevante/Comunicado em TEMPO REAL é do cvm_realtime.py
(scraper RAD direto). A antiga camada CVM via CSV anual foi REMOVIDA daqui —
tinha ~6 dias de atraso e era redundante com o realtime (mesmo state['cvm']).
"""
# digest.py lê env vars no top — define dummies antes de importar
import os
os.environ.setdefault("RESEND_API_KEY", "_unused_by_alerts")
os.environ.setdefault("DIGEST_TO", "_unused_by_alerts")

import hashlib
import html
import json
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

import notify
from alerts_config import CONFIG
from digest import fetch_source as fetch_news
from dou_mme import collect as dou_collect
from dou_mme import link_for as dou_link
from sources import SOURCES as NEWS_SOURCES

STATE_FILE = Path("alerts_state.json")

# Quantos IDs por categoria manter em state (evita arquivo crescer infinito)
STATE_CAP = 2000
# Limite de pushes por run (evita inundar o celular se config muda muito)
PUSH_CAP_PER_RUN = 25
# Nota mínima do ranking pra notificar uma notícia (faixa "verde" do Top do Dia)
SCORE_THRESHOLD = 60


# ============== STATE ==============
def load_state() -> dict:
    default = {"news": [], "dou": [], "cvm": [], "news_score": []}
    if STATE_FILE.exists():
        st = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        for k in default:
            st.setdefault(k, [])
        return st
    return default


def save_state(state: dict) -> None:
    for key in state:
        if isinstance(state[key], list):  # mme_cp é dict (por id de CP)
            state[key] = state[key][-STATE_CAP:]
    STATE_FILE.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")


def _load_json(path: str, default):
    p = Path(path)
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            return default
    return default


# ============== COLETA (uma vez, compartilhada) ==============
def collect_news() -> list:
    """Fetch de todas as fontes UMA vez. Retorna lista de (src_name, item)."""
    out = []
    for src in NEWS_SOURCES:
        try:
            for it in fetch_news(src):
                out.append((src["name"], it))
        except Exception as e:
            print(f"[news/{src['name']}] erro: {e}", file=sys.stderr)
    return out


# ============== MATCHING ==============
def text_matches(text: str, keywords: list) -> bool:
    """Match se QUALQUER keyword bater. Cada keyword: str (substring) ou
    list[str] (AND lógico de todos). Ex.: ["LRCAP", ["fato relevante","Eletrobras"]]."""
    if not text or not keywords:
        return False
    text_lower = text.lower()
    for k in keywords:
        if isinstance(k, str):
            if k.lower() in text_lower:
                return True
        elif isinstance(k, (list, tuple)):
            if all(kw.lower() in text_lower for kw in k):
                return True
    return False


# ============== NEWS (por keyword) ==============
def alert_news(news_items: list, config: dict, state: dict, dry: bool) -> int:
    """Notícias que casam keywords. State['news'] guarda só os já ALERTADOS."""
    keywords = config.get("news_keywords", [])
    if not keywords:
        return 0
    alerted = set(state.get("news", []))
    new_matches = []
    for src_name, item in news_items:
        link = item["link"]
        if link in alerted:
            continue
        if not text_matches(item["title"], keywords):
            continue
        new_matches.append((src_name, item))
        alerted.add(link)

    if not dry:
        for src_name, item in new_matches[:PUSH_CAP_PER_RUN]:
            notify.send(f"📰 {src_name}", item["title"], click=item["link"], tags=["newspaper"])

    state["news"] = list(alerted)[-STATE_CAP:]
    print(f"[news] {len(new_matches)} alertas novos")
    return len(new_matches)


# ============== NEWS (por NOTA / ranking) ==============
def alert_news_score(news_items: list, state: dict, dry: bool) -> int:
    """Notifica notícias cujo SCORE (mesma lógica do Top do Dia) supera
    SCORE_THRESHOLD. Dedup com state['news_score'] E state['news'] (não duplica
    com o alerta por keyword)."""
    try:
        from news_ranking import rank_news, extract_regulatory_entities
    except Exception as e:
        print(f"[news_score] news_ranking indisponível: {e}", file=sys.stderr)
        return 0

    # watch terms = watchlist.json (mesma lista da UI)
    wl = _load_json("watchlist.json", [])
    watch_terms = wl if isinstance(wl, list) else (wl.get("terms", []) if isinstance(wl, dict) else [])

    # entidades regulatórias do dia (DOU + ANEEL aux) — signal 'regulatory'
    dou_items = (_load_json("dou_history.json", {}) or {}).get("items", []) or []
    aneel_items = (_load_json("aneel_aux_history.json", {}) or {}).get("items", {}) or {}
    try:
        reg_entities = extract_regulatory_entities(aneel_items, dou_items)
    except Exception:
        reg_entities = set()

    # resumos + fonte do news_history pra enriquecer o score
    hist_items = (_load_json("news_history.json", {}) or {}).get("items", {}) or {}

    # dedup por link, enriquecendo com summary/source
    by_link = {}
    for src_name, it in news_items:
        link = it["link"]
        if link in by_link:
            continue
        h = hist_items.get(link, {})
        by_link[link] = {
            "title": it["title"], "link": link, "published": it["published"],
            "summary": h.get("summary") or "", "source": h.get("source") or src_name,
        }
    items = list(by_link.values())
    if not items:
        return 0

    ranked = rank_news(items, watch_terms, regulatory_entities=reg_entities,
                       top_n=200, min_score=SCORE_THRESHOLD)

    already = set(state.get("news_score", [])) | set(state.get("news", []))
    new_matches = [it for it in ranked if it.get("link") and it["link"] not in already]

    if not dry:
        for it in new_matches[:PUSH_CAP_PER_RUN]:
            score = round(it.get("_score", 0))
            notify.send(
                f"🔥 Top notícia · nota {score}",
                it.get("title", ""),
                click=it.get("link"),
                priority="high",
                tags=["fire"],
            )

    seen = set(state.get("news_score", []))
    seen.update(it["link"] for it in new_matches)
    state["news_score"] = list(seen)[-STATE_CAP:]
    print(f"[news_score] {len(new_matches)} alertas novos (nota>={SCORE_THRESHOLD})")
    return len(new_matches)


# ============== DOU ==============
def alert_dou(config: dict, state: dict, dry: bool) -> int:
    """State['dou'] guarda apenas items já alertados."""
    keywords = config.get("dou_keywords", [])
    if not keywords:
        return 0
    today = datetime.now().strftime("%d-%m-%Y")
    try:
        hits = dou_collect(today, with_summary=False)
    except Exception as e:
        print(f"[dou] erro: {e}", file=sys.stderr)
        return 0

    alerted = set(state.get("dou", []))
    new_matches = []
    for hit in hits:
        hit_id = hit.get("urlTitle") or hit.get("id") or hit.get("href", "")
        if not hit_id or hit_id in alerted:
            continue
        text = f"{hit.get('title', '')} {hit.get('content', '')}"
        if not text_matches(text, keywords):
            continue
        new_matches.append(hit)
        alerted.add(hit_id)

    if not dry:
        for hit in new_matches[:PUSH_CAP_PER_RUN]:
            notify.send(
                "🏛️ DOU",
                hit.get("title", "(sem título)"),
                click=dou_link(hit),
                tags=["classical_building"],
            )

    state["dou"] = list(alerted)[-STATE_CAP:]
    print(f"[dou] {len(new_matches)} alertas novos")
    return len(new_matches)


# ============== MME CONSULTAS PÚBLICAS ==============
# API JSON pública que o próprio site (Angular) usa; ordenada por id desc.
MME_CP_API = ("https://consultas-publicas.mme.gov.br/consulta-publica/v1/public/"
              "listagem-sem-filtros?pageNumber=0&pageSize=40&sortBy=id&sortDirection=desc")
MME_CP_SITE = "https://consultas-publicas.mme.gov.br/"
# Só energia elétrica (a pedido, 07/10/2026): fora petróleo/gás/biocombustíveis
# (SNPGB; SPG é o nome antigo) e mineração (SGM). Denylist DE PROPÓSITO: o campo
# área é bagunçado (SNEE, SE, "Secretaria Executiva", DPUE, LEGADO…) — área
# nova/renomeada alerta em vez de sumir calada.
MME_CP_EXCLUDED_AREAS = {"SNPGB", "SPG", "SGM"}


def alert_mme_cp(state: dict, dry: bool) -> int:
    """CP nova, prazo alterado/reaberta e documento novo em CP existente.

    State['mme_cp'] = {id: {"fim", "status", "docs": [ids de arquivo]}} de TODAS
    as CPs da janela (inclusive áreas excluídas). Sem ele (1ª run ou state
    perdido) só semeia, em silêncio. "Nova" exige id > maior id já visto —
    um retorno parcial da API nunca vira enxurrada de CPs antigas."""
    try:
        r = requests.post(MME_CP_API, json={}, timeout=30)
        r.raise_for_status()
        cps = r.json().get("content") or []
    except Exception as e:
        print(f"[mme_cp] erro: {e}", file=sys.stderr)
        return 0

    seen = state.get("mme_cp")
    seed = not isinstance(seen, dict) or not seen
    seen = {} if seed else dict(seen)  # cópia: exceção no meio não suja o state
    max_seen = max((int(k) for k in seen), default=0)

    msgs = []
    for cp in cps:
        if cp.get("isDeleted"):
            continue
        key = str(cp["id"])
        docs = {a["id"]: (a.get("titulo") or "").strip()
                for a in cp.get("arquivosConsultasPublicas") or [] if not a.get("deletar")}
        cur = {"fim": cp.get("dtFim"), "status": cp.get("status"), "docs": sorted(docs)}
        prev = seen.get(key)
        seen[key] = cur
        area = (cp.get("area") or "").split("/")[0].strip().upper()
        if seed or area in MME_CP_EXCLUDED_AREAS:
            continue

        num = f"CP MME nº {cp['id']}"
        titulo = (cp.get("titulo") or "").strip()[:250]
        if prev is None:
            if cp["id"] > max_seen:
                msgs.append((f"🗳️ {num} · nova",
                             f"{titulo}\nContribuições até {cp.get('dtFim')} · {cp.get('area')}"))
            continue
        if prev.get("fim") != cur["fim"]:
            reaberta = prev.get("status") != "ABERTA" and cur["status"] == "ABERTA"
            msgs.append((f"⏳ {num} · {'reaberta' if reaberta else 'prazo alterado'}",
                         f"{titulo}\nPrazo: {prev.get('fim')} → {cur['fim']}"))
        novos = [docs[d] for d in cur["docs"] if d not in set(prev.get("docs", []))]
        if novos:
            lista = "\n".join(f"• {t[:120]}" for t in novos[:5])
            msgs.append((f"📎 {num} · {len(novos)} documento{'s' if len(novos) > 1 else ''} novo{'s' if len(novos) > 1 else ''}",
                         f"{titulo}\n{lista}"))

    if not dry:
        for title, body in msgs[:PUSH_CAP_PER_RUN]:
            notify.send(title, body, click=MME_CP_SITE, tags=["ballot_box"])

    state["mme_cp"] = seen
    print(f"[mme_cp] {len(cps)} CPs lidas, {len(msgs)} alertas" + (" (seed silencioso)" if seed else ""))
    return len(msgs)


# ============== ARSESP (saneamento / Sabesp) ==============
# API REST do SharePoint do site (anônima). /Lists/ConsultasPublicas tem CPs E
# APs; documentos ficam em listas separadas, ligadas pelo ID do item.
ARSESP_API = "https://www.arsesp.sp.gov.br/_api/web/GetList('{lista}')/items"
ARSESP_SITE = "https://www.arsesp.sp.gov.br/SitePages/Consultas-Audiencias-Publicas.aspx"
ARSESP_DETALHE = "https://www.arsesp.sp.gov.br/SitePages/DetalhesACPublicas.aspx?{param}={id}"
# Só saneamento, sem item de outra concessionária nem de resíduos sólidos (a
# pedido, 07/10/2026), a não ser que também cite a Sabesp. Denylist de propósito:
# concessionária nova alerta em vez de sumir calada.
_ARSESP_OUTRAS = re.compile(
    r"brk|saneaqua|mairinque|gertrudes|\bsaeg\b|guaratinguet"
    r"|res[ií]duos\s+s[oó]lidos|smrsu|limpeza\s+urbana",
    re.IGNORECASE,
)


def _arsesp_get(lista: str, select: str, top: int, expand: str = None) -> list:
    # Sem $select a lista de documentos dá HTTP 500 (campo URL "Documento" quebra).
    params = {"$top": top, "$orderby": "ID desc", "$select": select}
    if expand:
        params["$expand"] = expand
    r = requests.get(ARSESP_API.format(lista=lista), params=params, timeout=30,
                     headers={"Accept": "application/json;odata=nometadata"})
    r.raise_for_status()
    return r.json()["value"]


def _arsesp_no_escopo(it: dict) -> bool:
    # Setor vem sujo: "Saneamento", "Saneamento Básico", "SANEAMENTO BÁSICO", "Sanemaneto Básico".
    if not re.match(r"\s*san", it.get("Setor") or "", re.IGNORECASE):
        return False
    texto = " ".join([it.get("Descricao") or "",
                      (it.get("Assunto") or {}).get("Title") or "",
                      (it.get("RevisaoTarifaria") or {}).get("Title") or ""])
    return "sabesp" in texto.lower() or not _ARSESP_OUTRAS.search(texto)


def _brt(iso: str, hora: bool = False) -> str:
    """'2026-09-28T21:00:00Z' (UTC) → '28/09/2026' (BRT)."""
    try:
        d = datetime.strptime(iso, "%Y-%m-%dT%H:%M:%SZ") - timedelta(hours=3)
    except (TypeError, ValueError):
        return "?"
    return d.strftime("%d/%m/%Y %Hh%M" if hora else "%d/%m/%Y")


def alert_arsesp(state: dict, dry: bool) -> int:
    """CP/AP nova de saneamento e documentos novos (deliberação, nota técnica,
    contribuições…) agrupados por CP/AP. Sem alerta de prazo (escolha do user).

    State['arsesp'] = marcas d'água por ID (as 3 listas têm ID crescente) +
    IDs já alertados. Item é reavaliado enquanto está na janela: um que nasce
    sem setor e é editado pra Saneamento depois ainda alerta. Sem state → só
    semeia, em silêncio."""
    try:
        itens = _arsesp_get(
            "/Lists/ConsultasPublicas",
            "ID,TipoItem,Setor,Numero,DataDeAbertura,DataDeEncerramento,Descricao,"
            "IdConsultasPublicas,IdAudiencia,Assunto/Title,RevisaoTarifaria/Title",
            60, expand="Assunto,RevisaoTarifaria")
        docs = [(d["ID"], d.get("ConsultasPublicasId"), d.get("Title"), "doc_cp")
                for d in _arsesp_get("/Lists/ConsultasPublicasDocumentos", "ID,Title,ConsultasPublicasId", 100)]
        docs += [(d["ID"], d.get("idAudienciasId"), d.get("Titulo"), "doc_ap")
                 for d in _arsesp_get("/Lists/bdAudienciasPublicasDocumentos", "ID,Titulo,idAudienciasId", 100)]
    except Exception as e:
        print(f"[arsesp] erro: {e}", file=sys.stderr)
        return 0

    topo = {"item": max((it["ID"] for it in itens), default=0)}
    for lista in ("doc_cp", "doc_ap"):
        topo[lista] = max((i for i, _, _, l in docs if l == lista), default=0)
    st = state.get("arsesp")
    if not isinstance(st, dict) or not st:
        state["arsesp"] = {"item_seed": topo["item"], "alertados": [], "doc_cp": topo["doc_cp"], "doc_ap": topo["doc_ap"]}
        print(f"[arsesp] {len(itens)} itens lidos — seed silencioso")
        return 0

    por_id = {it["ID"]: it for it in itens}
    alertados = set(st.get("alertados", []))

    def _rotulo(it):
        return f"{it.get('TipoItem') or 'Consulta Pública'} nº {it.get('Numero')}"

    def _link(it):
        if it.get("IdConsultasPublicas"):
            return ARSESP_DETALHE.format(param="idItemC", id=int(it["IdConsultasPublicas"]))
        if it.get("IdAudiencia"):
            return ARSESP_DETALHE.format(param="idItemA", id=int(it["IdAudiencia"]))
        return ARSESP_SITE

    def _assunto(it, limit):
        s = " ".join((it.get("Descricao") or (it.get("Assunto") or {}).get("Title") or "").split())
        return s if len(s) <= limit else s[:limit - 1].rstrip() + "…"

    msgs, novos = [], set()
    for it in reversed(itens):  # mais antigo primeiro
        if it["ID"] <= st["item_seed"] or it["ID"] in alertados or not _arsesp_no_escopo(it):
            continue
        novos.add(it["ID"])
        alertados.add(it["ID"])
        if "Audi" in (it.get("TipoItem") or ""):
            quando = f"Audiência em {_brt(it.get('DataDeAbertura'), hora=True)}"
        else:
            quando = f"Contribuições: {_brt(it.get('DataDeAbertura'))} a {_brt(it.get('DataDeEncerramento'))}"
        msgs.append((f"💧 ARSESP · {_rotulo(it)}", f"{_assunto(it, 300)}\n{quando}", _link(it)))

    grupos = {}
    for doc_id, pai, titulo, lista in sorted(docs):
        if doc_id > st.get(lista, 0):
            grupos.setdefault(pai, []).append((titulo or "(sem título)").strip())
    for pai, titulos in grupos.items():
        it = por_id.get(pai)
        # Doc de CP/AP nova nesta run já vem coberto pelo alerta de "nova".
        if not it or pai in novos or not _arsesp_no_escopo(it):
            continue
        n = len(titulos)
        lista = "\n".join(f"• {t[:100]}" for t in titulos[:6]) + (f"\n…e mais {n - 6}" if n > 6 else "")
        msgs.append((f"📎 ARSESP · {_rotulo(it)} · {n} documento{'s' if n > 1 else ''} novo{'s' if n > 1 else ''}",
                     f"{_assunto(it, 120)}\n{lista}", _link(it)))

    if not dry:
        for title, body, link in msgs[:PUSH_CAP_PER_RUN]:
            notify.send(title, body, click=link, tags=["droplet"])

    state["arsesp"] = {"item_seed": st["item_seed"], "alertados": sorted(alertados),
                       "doc_cp": max(st.get("doc_cp", 0), topo["doc_cp"]),
                       "doc_ap": max(st.get("doc_ap", 0), topo["doc_ap"])}
    print(f"[arsesp] {len(itens)} itens, {len(docs)} docs lidos, {len(msgs)} alertas")
    return len(msgs)


# ============== ARSAE-MG (Copasa / Copanor / Gasmig) ==============
# Uma página WordPress (tema Divi) por ano, editada à mão: cada CP é um módulo
# "tabs" com título "Consulta e Audiência Pública nº 70 – Tema" e a lista de
# documentos (preliminares, finais, resolução). Tudo da ARSAE alerta, Gasmig
# inclusive (a pedido, 07/10/2026).
ARSAE_PAGE = "https://www.arsae.mg.gov.br/consultas-publicas-{ano}/"
# Só a data de modificação (resposta minúscula): baixa os ~240KB do HTML só
# quando a página mudou.
ARSAE_MODIFIED = ("https://www.arsae.mg.gov.br/wp-json/wp/v2/pages"
                  "?slug=consultas-publicas-{ano}&_fields=modified_gmt")
_ARSAE_MODULO = re.compile(r'<div id="[^"]*" class="et_pb_module et_pb_tabs')
_UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                     "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"}


_ARSAE_INTERMEDIARIO = Path(__file__).with_name("certs") / "sectigo_dv_r36.pem"
_arsae_ca_bundle = None


def _arsae_get(url: str, timeout: int):
    """GET com a cadeia TLS consertada. O servidor da ARSAE manda o intermediário
    ERRADO (o antigo, da Valid, depois da renovação de 04/09/2026) e o Python não
    completa a cadeia sozinho (o Windows/curl sim). Bundle = certifi + o
    intermediário certo, guardado em certs/."""
    global _arsae_ca_bundle
    if _arsae_ca_bundle is None:
        import certifi
        import tempfile
        with tempfile.NamedTemporaryFile("w", suffix=".pem", delete=False, encoding="utf-8") as f:
            f.write(Path(certifi.where()).read_text(encoding="utf-8") + "\n"
                    + _ARSAE_INTERMEDIARIO.read_text(encoding="utf-8"))
        _arsae_ca_bundle = f.name
    try:
        return requests.get(url, headers=_UA, timeout=timeout, verify=_arsae_ca_bundle)
    except requests.exceptions.SSLError:
        # Certificado renovado de novo (o atual vence em 03/2027) e a cadeia
        # continua quebrada: página pública e só leitura, então segue sem
        # verificar em vez de ficar cego calado, avisando no log.
        print("[arsae] cadeia TLS quebrada de novo — lendo sem verificação; atualizar certs/", file=sys.stderr)
        import urllib3
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
        return requests.get(url, headers=_UA, timeout=timeout, verify=False)


def _txt(fragmento: str) -> str:
    return " ".join(html.unescape(re.sub(r"<[^>]+>", " ", fragmento)).split()).strip(" ;.,")


def _arsae_parse(pagina: str) -> dict:
    """{num: {"titulo", "docs": {href: texto}}}. O índice do ano (módulo sem
    "nº") é pulado; o HTML é cortado no <footer> (senão o último bloco leva os
    links de rodapé: Facebook, Spotify…)."""
    pagina = pagina.split("<footer", 1)[0]
    starts = [m.start() for m in _ARSAE_MODULO.finditer(pagina)]
    out = {}
    for a, b in zip(starts, starts[1:] + [len(pagina)]):
        bloco = pagina[a:b]
        m = re.search(r'et_pb_tabs_controls.*?<a href="#">(.*?)</a>', bloco, re.S)
        titulo = _txt(m.group(1)) if m else ""
        num = re.search(r"n[º°o]\s*(\d+)", titulo)
        if not num:
            continue
        docs = {}
        for href, inner in re.findall(r'<a[^>]+href="([^"#][^"]*)"[^>]*>(.*?)</a>', bloco, re.S):
            href = html.unescape(href)
            docs[href] = docs.get(href) or _txt(inner)  # mesmo link 2x, um sem texto
        out[num.group(1)] = {"titulo": titulo, "docs": docs}
    return out


def _h(href: str) -> str:
    return hashlib.sha1(href.encode()).hexdigest()[:10]  # state enxuto


def alert_arsae(state: dict, dry: bool) -> int:
    """CP/AP nova e documentos novos (agrupados por CP) nas páginas do ano
    atual e do anterior (CP de dezembro ganha documento em janeiro).

    State['arsae'] = {"mod": {ano: modified_gmt}, "cps": {num: [hash de href]}}.
    Sem state → só semeia, em silêncio. Página que muda mas não rende nenhuma
    CP (layout mudou?) não avança "mod": tenta de novo e avisa no log."""
    ano = (datetime.now(timezone.utc) - timedelta(hours=3)).year
    st = state.get("arsae")
    seed = not isinstance(st, dict) or not st
    mod = {} if seed else dict(st.get("mod", {}))
    cps = {} if seed else {k: list(v) for k, v in st.get("cps", {}).items()}

    msgs = []
    for a in (ano, ano - 1):
        try:
            r = _arsae_get(ARSAE_MODIFIED.format(ano=a), timeout=20)
            lst = r.json() if r.ok else None
        except Exception:
            lst = None  # REST fora → baixa a página do mesmo jeito
        if lst == []:
            continue  # página do ano ainda não existe (início de janeiro)
        atual = lst[0].get("modified_gmt") if lst else None
        if atual and mod.get(str(a)) == atual:
            continue
        try:
            r = _arsae_get(ARSAE_PAGE.format(ano=a), timeout=30)
            if r.status_code == 404:
                continue
            r.raise_for_status()
        except Exception as e:
            print(f"[arsae] {a}: erro {e}", file=sys.stderr)
            continue
        r.encoding = "utf-8"
        pagina = _arsae_parse(r.text)
        if not pagina:
            print(f"[arsae] {a}: página sem nenhuma CP reconhecida — layout mudou?", file=sys.stderr)
            continue
        link = ARSAE_PAGE.format(ano=a)
        for num, cp in pagina.items():
            hashes = {_h(h): t for h, t in cp["docs"].items()}
            # "Consulta e Audiência Pública nº 70 – Cofaturamento…" → rótulo + tema
            # (às vezes sem espaço antes do traço: "nº 51– Definição…")
            partes = re.split(r"\s*[–—-]\s+", cp["titulo"], maxsplit=1)
            rotulo, tema = partes[0], (partes[1] if len(partes) > 1 else "")
            if num not in cps:
                if not seed:
                    msgs.append((f"🚰 ARSAE-MG · {rotulo}",
                                 f"{tema}\n{len(hashes)} documento{'s' if len(hashes) != 1 else ''} publicado{'s' if len(hashes) != 1 else ''}",
                                 f"{link}#CP{num}"))
            else:
                novos = [t or "(documento)" for h, t in hashes.items() if h not in set(cps[num])]
                if novos:
                    n = len(novos)
                    lista = "\n".join(f"• {t[:100]}" for t in novos[:6]) + (f"\n…e mais {n - 6}" if n > 6 else "")
                    msgs.append((f"📎 ARSAE-MG · {rotulo} · {n} documento{'s' if n > 1 else ''} novo{'s' if n > 1 else ''}",
                                 f"{tema[:120]}\n{lista}", f"{link}#CP{num}"))
            cps[num] = sorted(set(cps.get(num, [])) | set(hashes))
        if atual:
            mod[str(a)] = atual

    if not dry:
        for title, body, click in msgs[:PUSH_CAP_PER_RUN]:
            notify.send(title, body, click=click, tags=["potable_water"])

    state["arsae"] = {"mod": mod, "cps": cps}
    print(f"[arsae] {len(cps)} CPs no state, {len(msgs)} alertas" + (" (seed silencioso)" if seed else ""))
    return len(msgs)


# ============== SEI (processos escolhidos em sei_watch.txt) ==============
# Andamento de tramitação não alerta: é ~90% do histórico (medido em 5 processos
# ANEEL, 07/10/2026). O resto é texto escrito à mão ("PROCESSO DELIBERADO COM A
# PAUTA DA 18ª RPO…", "EM ATENÇÃO PARECER…", "Recurso - 1º LRCAP").
_SEI_ROTINA = re.compile(
    r"^\s*(?:processo\s+(?:recebido|remetido)\b|conclus[ãa]o\s+do\s+processo|reabertura\s+do\s+processo"
    r"|processo\s+[\d./-]+\s+(?:anexado|desanexado)|processo\s+p[úu]blico\s+gerado"
    r"|(?:disponibilizad[oa]|cancelad[oa])\b.*acesso\s+externo)",
    re.IGNORECASE,
)


def alert_sei(state: dict, dry: bool) -> int:
    """Documento novo + andamento fora da rotina, uma mensagem por processo.

    Lê o SEI direto (link público com hash). Se não conseguir (Cloudflare no
    IP do Actions, SEI fora), usa o que o refresh do PC commitou em
    sei_processes.json. State['sei'][id] = {"and": hashes dos andamentos mais
    recentes, "docs": TODOS os protocolos (doc novo pode entrar no meio da
    lista)}. Processo sem state (inclusive recém-adicionado) só semeia."""
    import sei_monitor  # curl_cffi + parser; import tardio: só quem usa paga

    st_all = state.get("sei") if isinstance(state.get("sei"), dict) else {}
    novo_state, msgs = {}, []
    processos = [p for p in sei_monitor.load_processes() if p.get("url") and p.get("ntfy_enabled", True)]
    for i, p in enumerate(processos):
        if i:
            time.sleep(2)  # gentil com o SEI
        try:
            d = sei_monitor.parse_process(p["url"])
            if not d["andamentos"] and not d["documentos"]:
                raise ValueError("página sem andamentos nem documentos (bloqueio?)")
            ands, docs = d["andamentos"], d["documentos"]
            and_hashes, protos = [a["hash"] for a in ands], [x["protocolo"] for x in docs]
            numero = d.get("processo") or p.get("processo")
        except Exception as e:
            if "documentos_protocolos" not in p:  # PC ainda não coletou no formato novo
                print(f"[sei] {p.get('label') or p['url'][-12:]}: sem leitura ({e})", file=sys.stderr)
                if p["id"] in st_all:
                    novo_state[p["id"]] = st_all[p["id"]]
                continue
            ands, docs = p.get("last_andamentos_top10") or [], p.get("last_documentos_top10") or []
            and_hashes, protos = p.get("andamentos_seen") or [], p["documentos_protocolos"]
            numero = p.get("processo")

        prev = st_all.get(p["id"])
        # Andamentos vêm do mais novo pro mais velho: guarda os 60 mais recentes
        # (união com o anterior, senão um fallback mais velho "esquece" o que já foi avisado).
        novo_state[p["id"]] = {
            "and": list(dict.fromkeys(and_hashes[:60] + (prev or {}).get("and", [])))[:120],
            "docs": sorted(set(protos) | set((prev or {}).get("docs", []))),
        }
        if prev is None:
            continue  # seed silencioso

        vistos_and, vistos_docs = set(prev.get("and", [])), set(prev.get("docs", []))
        det_and = {a["hash"]: a for a in ands}
        det_doc = {x["protocolo"]: x for x in docs}
        linhas, anexados = [], 0
        for proto in protos:
            if proto in vistos_docs:
                continue
            x = det_doc.get(proto)
            if "/" in proto:  # processo anexado (recurso, requerimento…) — vira contagem
                anexados += 1
            elif x:
                linhas.append(f"📄 {x.get('tipo')} — {x.get('unidade')} ({x.get('data')})")
            else:
                linhas.append(f"📄 documento {proto}")
        for h in and_hashes[:30]:
            a = det_and.get(h)
            if h in vistos_and or not a or _SEI_ROTINA.match(a.get("descricao") or ""):
                continue
            linhas.append(f"📝 {(a.get('datahora') or '')[:10]} {a.get('unidade')}: {(a.get('descricao') or '')[:140]}")
        if anexados:
            linhas.append(f"+ {anexados} processo{'s' if anexados > 1 else ''} anexado{'s' if anexados > 1 else ''}")
        if not linhas:
            continue
        corpo = "\n".join(linhas[:8]) + (f"\n…e mais {len(linhas) - 8}" if len(linhas) > 8 else "")
        titulo = p.get("label") or numero or "processo"
        msgs.append((f"📂 SEI · {titulo}", (f"{numero}\n" if numero and numero not in titulo else "") + corpo, p["url"]))

    if not dry:
        for title, body, link in msgs[:PUSH_CAP_PER_RUN]:
            notify.send(title, body, click=link, tags=["open_file_folder"])

    state["sei"] = novo_state  # processo tirado da lista sai do state
    print(f"[sei] {len(processos)} processos, {len(msgs)} alertas")
    return len(msgs)


# ============== ANEEL CP / Tomada de Subsídios ==============
# Dois caminhos, que compartilham state['aneel_partic']['avisados'] (o mesmo nº
# de CP/TS nunca alerta duas vezes):
#  1. alert_aneel_noticias: as notícias da ANEEL no gov.br, que o Actions LÊ.
#     Não depende do PC e sai 1-2 dias ANTES da listagem oficial.
#  2. alert_aneel_partic: a listagem oficial (Liferay antigo.aneel), que bloqueia
#     o Actions (0 itens em todo run do GitHub); só o PC coleta e commita
#     aneel_aux_history.json. Reforço, e é o que traz o prazo e a prorrogação.
# AP fica de fora (escolha do user).
ANEEL_HISTORY = Path("aneel_aux_history.json")
ANEEL_PARTIC_TIPOS = {"partic_cp": "Consulta Pública", "partic_ts": "Tomada de Subsídios"}
# Link = listagem: o link de detalhe carrega p_auth (token de sessão).
ANEEL_PARTIC_LISTAGEM = {"partic_cp": "https://antigo.aneel.gov.br/consultas-publicas",
                         "partic_ts": "https://antigo.aneel.gov.br/tomadas-de-subsidios"}


def _dmy(s):
    try:
        return datetime.strptime(s or "", "%d/%m/%Y")
    except ValueError:
        return None


def alert_aneel_partic(state: dict, dry: bool) -> int:
    """CP/TS nova (entry com first_seen — só nasce em item inédito — ainda não
    avisada) e prazo prorrogado (só prazo lido na página de detalhe).

    State['aneel_partic'] = {"avisados": [ids], "prazos": {id: prazo avisado}}.
    Sem state, herda o que o alerta antigo (dentro do aneel_aux, até 08/10/2026)
    gravou no próprio histórico: notified_at / notified_deadline."""
    if not ANEEL_HISTORY.exists():
        return 0
    itens = json.loads(ANEEL_HISTORY.read_text(encoding="utf-8")).get("items", {})
    st = state.get("aneel_partic")
    if not isinstance(st, dict):
        st = {"avisados": [k for k, e in itens.items() if e.get("notified_at")],
              "prazos": {k: e["notified_deadline"] for k, e in itens.items() if e.get("notified_deadline")}}
    avisados, prazos = set(st.get("avisados", [])), dict(st.get("prazos", {}))

    def _resumo(e, limit):
        s = (e.get("summary") or e.get("objeto") or "").replace("**", "").strip()
        return s if len(s) <= limit else s[:limit - 1].rstrip() + "…"

    msgs = []
    for k, e in itens.items():
        rotulo = ANEEL_PARTIC_TIPOS.get(e.get("type"))
        if not rotulo:
            continue
        num = "/".join(k.rsplit("_", 2)[-2:])  # partic_cp_036_2026 → 036/2026
        fim = e.get("deadline") if e.get("deadline_source") == "detalhe" else None
        link = ANEEL_PARTIC_LISTAGEM[e["type"]]
        if e.get("first_seen") and k not in avisados:
            corpo = _resumo(e, 300) + (f"\nContribuições até {e['deadline']}" if e.get("deadline") else "")
            msgs.append((k, fim, f"🏛️ ANEEL · {rotulo} nº {num}", corpo, link))
        elif _dmy(fim) and _dmy(prazos.get(k)) and _dmy(fim) > _dmy(prazos[k]):
            msgs.append((k, fim, f"⏳ ANEEL · {rotulo} nº {num} · prazo prorrogado",
                         f"{_resumo(e, 150)}\nPrazo: {prazos[k]} → {fim}", link))
        elif fim and k not in prazos:
            prazos[k] = fim  # baseline silenciosa

    for k, fim, title, body, link in msgs[:PUSH_CAP_PER_RUN]:
        if not dry and notify.send(title, body, click=link, tags=["classical_building"]):
            avisados.add(k)
            if fim:
                prazos[k] = fim

    # Guarda os avisados de QUALQUER origem: a notícia e o DOU avisam CP que o PC
    # ainda nem coletou. Podar pelo histórico do PC apagava esses avisos e a
    # listagem realertaria quando o PC coletasse (bug de 08/10/2026). Só sai o
    # que é de ano anterior ao passado (o id termina no ano: partic_cp_036_2026).
    ano_min = str(datetime.now().year - 1)
    state["aneel_partic"] = {"avisados": sorted(a for a in avisados if a[-4:] >= ano_min),
                             "prazos": {k: v for k, v in prazos.items() if k[-4:] >= ano_min}}
    print(f"[aneel_partic] {len(msgs)} alertas")
    return len(msgs)


# ---- DOU Seção 3: o aviso OFICIAL de abertura (a garantia) ----
# Toda CP/TS da ANEEL sai como "AVISO DE CONSULTA PÚBLICA Nº 36/2026" / "AVISO DE
# TOMADA DE SUBSÍDIOS Nº 29/2026" na Seção 3, no mesmo dia em que entra no site
# de consultas (medido 10/08-08/10/2026: CPs 27-37 e TSs 20-39, todas lá). O
# Actions lê o in.gov.br; filtrando por órgão, o dia inteiro da ANEEL cabe numa
# resposta (máx. 19 atos/dia) — a paginação da busca do DOU não funciona.
DOU_BUSCA = "https://www.in.gov.br/consulta/-/buscar/dou"
_DOU_NUM = re.compile(r"(consulta\s+p[úu]blica|tomada\s+de\s+subs[íi]dios)\s+n[ºo°.]*\s*(\d{1,3})/(\d{4})", re.IGNORECASE)
_DOU_ABERTURA = re.compile(r"^\s*aviso\s+de\s+(?:consulta\s+p[úu]blica|tomada\s+de\s+subs[íi]dios)\b", re.IGNORECASE)
# "Período para envio: 1º/10/2026 a 30/10/2026" (dia 1 vem com ordinal)
_DOU_PERIODO = re.compile(r"Per[íi]odo[^:]{0,40}:\s*(\d{1,2}º?/\d{1,2}/\d{4})\s*a\s*(\d{1,2}º?/\d{1,2}/\d{4})", re.IGNORECASE)


def _dou_aneel_secao3(dia: str) -> list:
    """Todos os atos da ANEEL na Seção 3 do dia ('dd-mm-aaaa')."""
    r = requests.get(DOU_BUSCA, timeout=40, headers={"User-Agent": "Mozilla/5.0"}, params={
        "q": "*", "s": "do3", "exactDate": "personalizado", "publishFrom": dia, "publishTo": dia, "delta": 50,
        "orgPrin": "Ministério de Minas e Energia", "orgSub": "Agência Nacional de Energia Elétrica"})
    r.raise_for_status()
    m = re.search(r'_BuscaDouPortlet_params"[^>]*>\s*(\{.*?\})\s*</script>', r.text, re.S)
    if not m:
        raise ValueError("busca do DOU sem o JSON de resultados (layout mudou?)")
    return json.loads(m.group(1)).get("jsonArray", [])


def alert_aneel_dou(state: dict, dry: bool) -> int:
    """Aviso de abertura de CP/TS e aviso de prorrogação, do DOU (hoje e o dia útil
    anterior, o que cobre fim de semana e loop parado). Abertura de CP/TS já
    avisada pela notícia ou pela listagem não repete, mas o prazo do DOU vira a
    referência pra prorrogação. Várias TS no mesmo dia (lote de DEC/FEC) = 1
    mensagem. State['aneel_dou'] = urlTitles já processados; sem state, semeia.
    Aviso que gera alerta só vira "visto" depois de enviado."""
    import dou_mme  # _fetch_full_dou: texto completo (tem o "Período para envio")

    hoje = (datetime.now(timezone.utc) - timedelta(hours=3)).date()
    anterior = hoje - timedelta(days={0: 3, 6: 2}.get(hoje.weekday(), 1))  # seg→sex, dom→sex
    atos = []
    for d in (anterior, hoje):
        try:
            atos += _dou_aneel_secao3(d.strftime("%d-%m-%Y"))
        except Exception as e:
            print(f"[aneel_dou] {d}: {e}", file=sys.stderr)
            return 0  # sem o dia completo, não marca nada como visto
    seed = not isinstance(state.get("aneel_dou"), list)
    vistos = list(state.get("aneel_dou") or [])
    partic = state.get("aneel_partic") if isinstance(state.get("aneel_partic"), dict) else {"avisados": [], "prazos": {}}
    avisados, prazos = set(partic.get("avisados", [])), dict(partic.get("prazos", {}))

    def visto(uid):
        if uid not in vistos:
            vistos.append(uid)

    novos, prorrogs = [], []
    for a in atos:
        uid = a.get("urlTitle")
        if not uid or uid in vistos:
            continue
        titulo = re.sub(r"<[^>]+>", "", a.get("title") or "").strip()
        m = _DOU_NUM.search(f"{titulo} {re.sub(r'<[^>]+>', ' ', a.get('content') or '')}")
        prorroga = bool(re.search(r"prorroga", f"{a.get('artType')} {titulo}", re.IGNORECASE))
        if seed or not m or not (prorroga or _DOU_ABERTURA.match(titulo)):
            visto(uid)  # retificação, extrato, resultado…
            continue
        tipo = "partic_cp" if m.group(1).lower().startswith("consulta") else "partic_ts"
        link = f"https://www.in.gov.br/web/dou/-/{uid}"
        completo, objeto = dou_mme._fetch_full_dou(link)
        corpo = " ".join((objeto or completo or "").split())
        p = _DOU_PERIODO.search(corpo) or _DOU_PERIODO.search(completo or "")
        ini, fim = (_dmy(p.group(1).replace("º", "")), _dmy(p.group(2).replace("º", ""))) if p else (None, None)
        item = {"uid": uid, "k": f"{tipo}_{int(m.group(2)):03d}_{m.group(3)}", "tipo": tipo,
                "num": f"{int(m.group(2)):03d}/{m.group(3)}", "pub": a.get("pubDate"), "link": link,
                "inicio": ini.strftime("%d/%m/%Y") if ini else None, "fim": fim.strftime("%d/%m/%Y") if fim else None,
                "objeto": _DOU_PERIODO.sub("", corpo).replace("Modalidade:Intercâmbio de documentos.", "").strip(" .")}
        if prorroga:
            prorrogs.append(item)
        elif item["k"] in avisados:  # notícia/listagem já avisou: só guarda o prazo oficial
            if item["fim"]:
                prazos.setdefault(item["k"], item["fim"])
            visto(uid)
        else:
            novos.append(item)

    msgs = []  # (itens, título, corpo, link)
    for tipo in ("partic_cp", "partic_ts"):
        grupo = sorted((i for i in novos if i["tipo"] == tipo), key=lambda i: i["k"])
        rotulo = ANEEL_PARTIC_TIPOS[tipo]
        if len(grupo) > 3:  # lote (ex.: 15 TS de DEC/FEC no mesmo dia)
            plural = {"partic_cp": "Consultas Públicas", "partic_ts": "Tomadas de Subsídios"}[tipo]
            linhas = "\n".join(f"• nº {i['num']}: {i['objeto'][:90]}" for i in grupo[:5])
            fims = {i["fim"] for i in grupo if i["fim"]}
            nums = ", ".join(i["num"].split("/")[0] for i in grupo)
            msgs.append((grupo, f"🏛️ ANEEL · {len(grupo)} {plural} (DOU)",
                         f"nºs {nums}/{grupo[0]['num'].split('/')[1]}\n{linhas}"
                         + (f"\n…e mais {len(grupo) - 5}" if len(grupo) > 5 else "")
                         + (f"\nContribuições até {next(iter(fims))}" if len(fims) == 1 else ""), grupo[0]["link"]))
        else:
            for i in grupo:
                quando = f"\nContribuições: {i['inicio']} a {i['fim']}" if i["fim"] else ""
                msgs.append(([i], f"🏛️ ANEEL · {rotulo} nº {i['num']}",
                             f"{i['objeto'][:280]}{quando}\n(aviso no DOU de {i['pub']})", i["link"]))
    for i in prorrogs:
        antes = prazos.get(i["k"])
        msgs.append(([i], f"⏳ ANEEL · {ANEEL_PARTIC_TIPOS[i['tipo']]} nº {i['num']} · prazo prorrogado",
                     i["objeto"][:200] + (f"\nPrazo: {antes} → {i['fim']}" if antes and i["fim"] else "")
                     + f"\n(aviso no DOU de {i['pub']})", i["link"]))

    enviados = 0
    for itens, title, body, link in msgs[:PUSH_CAP_PER_RUN]:
        if dry or not notify.send(title, body, click=link, tags=["classical_building"]):
            continue  # não marca como visto: tenta no próximo ciclo
        for i in itens:
            visto(i["uid"])
            avisados.add(i["k"])
            if i["fim"]:
                prazos[i["k"]] = i["fim"]
        enviados += 1

    partic.update(avisados=sorted(avisados), prazos=prazos)
    state["aneel_partic"] = partic
    state["aneel_dou"] = vistos[-500:]
    print(f"[aneel_dou] {len(atos)} atos ANEEL na Seção 3, {enviados} alertas" + (" (seed silencioso)" if seed else ""))
    return enviados


# Título fala de CP/TS (e não de audiência) → candidata. Sai a notícia de
# encerramento/resultado ("ANEEL encerra Consulta Pública e aprova regras…").
# Medido em 08/10/2026: 16 títulos de CP/TS (jan-out), 15 aberturas — muitas sem
# verbo de abertura ("Consulta Pública vai tratar…", "Consulta discutirá…").
_NOT_TEMA = re.compile(r"consulta|tomadas?\s+de\s+subs[íi]dios?", re.IGNORECASE)
_NOT_AP = re.compile(r"audi[êe]ncia", re.IGNORECASE)
_NOT_FECHA = re.compile(r"encerr|conclu[íi]|resultado|\bap[óo]s\b", re.IGNORECASE)
_NOT_APROVA = re.compile(r"\baprova", re.IGNORECASE)
_NOT_ABRE = re.compile(r"\babr(?:e|em|iu)\b|abert[ao]s?\b|abertura", re.IGNORECASE)
_NOT_NUM_CP = re.compile(r"(?:Consulta\s+P[úu]blica|\bCP)\s*(?:n[ºo°.]*\s*)?(\d{1,3})/(\d{4})", re.IGNORECASE)
_NOT_NUM_TS = re.compile(r"(?:Tomadas?\s+de\s+Subs[íi]dios?|\bTS)\s*(?:n[ºo°.]*\s*)?(\d{1,3})/(\d{4})", re.IGNORECASE)
ANEEL_NOTICIA_MAX_DIAS = 4  # 1ª run / loop parado: não despeja notícia velha


def alert_aneel_noticias(state: dict, dry: bool) -> int:
    """CP/TS anunciada nas notícias da ANEEL (gov.br). 1 request pela lista e 1
    por notícia candidata ainda não vista. O nº da CP/TS vem do texto ("Consulta
    Pública nº 37/2026", "CP 035/2026") e entra em aneel_partic.avisados, o que
    silencia o alerta da listagem quando o PC coletar a mesma CP depois.

    State['aneel_noticias'] = ids de notícia já processados. Notícia com mais de
    ANEEL_NOTICIA_MAX_DIAS só é marcada como vista."""
    import aneel_aux  # parser da lista + detalhe (trafilatura); import tardio

    lista = aneel_aux.fetch_news_list()
    if not lista:
        print("[aneel_noticias] lista vazia (gov.br fora ou layout mudou?)", file=sys.stderr)
        return 0
    vistos = list(state.get("aneel_noticias") or [])
    ja = set(vistos)
    partic = state.get("aneel_partic") if isinstance(state.get("aneel_partic"), dict) else {"avisados": [], "prazos": {}}
    avisados = set(partic.get("avisados", []))
    hoje = (datetime.now(timezone.utc) - timedelta(hours=3)).date()

    msgs = []
    for it in lista:
        if it["id"] in ja:
            continue
        titulo = it["title"].strip()
        fecha = _NOT_FECHA.search(titulo) or (_NOT_APROVA.search(titulo) and not _NOT_ABRE.search(titulo))
        if not _NOT_TEMA.search(titulo) or _NOT_AP.search(titulo) or fecha:
            vistos.append(it["id"]); ja.add(it["id"])
            continue
        try:
            data, corpo = aneel_aux.fetch_news_detail(it["link"])
        except Exception as e:
            print(f"[aneel_noticias] {it['link'][-50:]}: {e}", file=sys.stderr)
            continue  # tenta de novo no próximo ciclo
        if not corpo:
            continue
        d = _dmy(data)
        # nº/ano da própria CP/TS (o texto às vezes cita CPs antigas: só vale o ano da notícia)
        ids = [f"partic_cp_{int(n):03d}_{a}" for n, a in _NOT_NUM_CP.findall(corpo) if d and int(a) == d.year]
        ids += [f"partic_ts_{int(n):03d}_{a}" for n, a in _NOT_NUM_TS.findall(corpo) if d and int(a) == d.year]
        ids = list(dict.fromkeys(ids))
        velha = not d or (hoje - d.date()).days > ANEEL_NOTICIA_MAX_DIAS
        if velha or (ids and all(i in avisados for i in ids)):  # velha, ou a listagem (PC) já avisou
            vistos.append(it["id"]); ja.add(it["id"])
            continue
        partes = [f"{ANEEL_PARTIC_TIPOS[i.rsplit('_', 2)[0]]} nº {i.rsplit('_', 2)[1]}/{i.rsplit('_', 2)[2]}" for i in ids[:3]]
        rotulo = " + ".join(partes) or ("Consulta Pública" if re.search(r"consulta", titulo, re.I) else "Tomada de Subsídios")
        # trecho logo depois do título no texto da notícia
        resto = corpo[corpo.find(titulo) + len(titulo):] if titulo in corpo else corpo
        resto = re.sub(r"Publicado em \S+ \S+|Atualizado em \S+ \S+", " ", resto)
        resto = " ".join(resto.split())
        trecho = resto if len(resto) <= 240 else resto[:239].rstrip() + "…"
        msgs.append((it["id"], ids, f"🏛️ ANEEL · {rotulo}", f"{titulo}\n{trecho}\n(notícia de {data})", it["link"]))

    # Só vira "vista" depois de enviada: falha no envio = tenta no próximo ciclo.
    for nid, ids, title, body, link in msgs[:PUSH_CAP_PER_RUN]:
        if not dry and notify.send(title, body, click=link, tags=["classical_building"]):
            avisados.update(ids)
            vistos.append(nid)

    partic["avisados"] = sorted(avisados)
    state["aneel_partic"] = partic
    state["aneel_noticias"] = vistos[-300:]
    print(f"[aneel_noticias] {len(lista)} notícias, {len(msgs)} alertas")
    return len(msgs)


# ============== MAIN ==============
def main():
    # notify usa NTFY_TOPIC do ambiente; espelha o da config se houver.
    if CONFIG.get("ntfy_topic"):
        os.environ.setdefault("NTFY_TOPIC", CONFIG["ntfy_topic"])

    state = load_state()
    news_items = collect_news()  # fetch uma vez (usado pelo score)
    total = 0
    # A pedido do usuário (11/06/2026): SÓ notificar Top do Dia com nota >=60.
    # Notícias por keyword (alert_news) e DOU (alert_dou) foram DESLIGADAS — as
    # funções seguem definidas (fácil reativar), mas não são chamadas. FR/
    # Comunicado continuam vindo do cvm_realtime/cvm_fast.
    total += alert_news_score(news_items, state, dry=False)
    # Consultas públicas do MME (a pedido, 07/10/2026). try: um erro aqui não
    # pode impedir o save_state do news_score acima (senão realerta notícias).
    try:
        total += alert_mme_cp(state, dry=False)
    except Exception as e:
        print(f"[mme_cp] falhou: {e}", file=sys.stderr)
    # ARSESP saneamento/Sabesp (a pedido, 07/10/2026). Mesmo motivo do try acima.
    try:
        total += alert_arsesp(state, dry=False)
    except Exception as e:
        print(f"[arsesp] falhou: {e}", file=sys.stderr)
    # ARSAE-MG Copasa/Copanor/Gasmig (a pedido, 07/10/2026). Mesmo motivo.
    try:
        total += alert_arsae(state, dry=False)
    except Exception as e:
        print(f"[arsae] falhou: {e}", file=sys.stderr)
    # Processos SEI escolhidos em sei_watch.txt (a pedido, 07/10/2026). Mesmo motivo.
    try:
        total += alert_sei(state, dry=False)
    except Exception as e:
        print(f"[sei] falhou: {e}", file=sys.stderr)
    # CP/TS ANEEL coletadas pelo PC (movido do aneel_aux em 08/10/2026). Mesmo motivo.
    try:
        total += alert_aneel_partic(state, dry=False)
    except Exception as e:
        print(f"[aneel_partic] falhou: {e}", file=sys.stderr)
    # Aviso oficial de CP/TS no DOU Seção 3 — a garantia, sem PC (a pedido, 08/10/2026).
    # Antes da notícia: se as duas virem a mesma CP no ciclo, sai a versão com prazo.
    try:
        total += alert_aneel_dou(state, dry=False)
    except Exception as e:
        print(f"[aneel_dou] falhou: {e}", file=sys.stderr)
    # CP/TS ANEEL pelas notícias do gov.br, sem depender do PC (a pedido, 08/10/2026).
    # Depois da listagem: se as duas virem a mesma CP no ciclo, sai só o alerta com prazo.
    try:
        total += alert_aneel_noticias(state, dry=False)
    except Exception as e:
        print(f"[aneel_noticias] falhou: {e}", file=sys.stderr)
    save_state(state)
    print(f"Total: {total} push notifications enviados")


if __name__ == "__main__":
    main()
