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

import json
import re
import sys
from datetime import datetime, timedelta
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
    save_state(state)
    print(f"Total: {total} push notifications enviados")


if __name__ == "__main__":
    main()
