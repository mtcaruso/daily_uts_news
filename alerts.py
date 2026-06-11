"""
Sistema de alertas. Roda a cada 30min via GitHub Actions, checa news/DOU
contra config (alerts_config.py) + ranking (nota do Top do Dia), e envia push
via notify.py (WhatsApp CallMeBot + ntfy, com retry/fallback).

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
import sys
from datetime import datetime
from pathlib import Path

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


# ============== MAIN ==============
def main():
    # notify usa NTFY_TOPIC do ambiente; espelha o da config se houver.
    if CONFIG.get("ntfy_topic"):
        os.environ.setdefault("NTFY_TOPIC", CONFIG["ntfy_topic"])

    state = load_state()
    news_items = collect_news()  # fetch uma vez, compartilhado
    total = 0
    total += alert_news(news_items, CONFIG, state, dry=False)
    total += alert_news_score(news_items, state, dry=False)
    total += alert_dou(CONFIG, state, dry=False)
    save_state(state)
    print(f"Total: {total} push notifications enviados")


if __name__ == "__main__":
    main()
