"""
Gera summaries pra notícias do Google News.
Roda no workflow daily.yml após o digest.py.
Persiste em news_history.json (keyed por URL do Google News).
"""
import os
os.environ.setdefault("RESEND_API_KEY", "_unused")
os.environ.setdefault("DIGEST_TO", "_unused")

import json
import re
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

import requests
import trafilatura
from googlenewsdecoder import gnewsdecoder

import gemini_util
from digest import fetch_source as fetch_news
from sources import SOURCES

# cloudscraper bypassa Cloudflare quando regular requests é bloqueado por IP
# (caso do Canal Energia em GHA). Lazy import — se faltar, só não usa fallback.
try:
    import cloudscraper
    _CLOUDSCRAPER = cloudscraper.create_scraper()
except Exception:
    _CLOUDSCRAPER = None

# curl_cffi impersona TLS handshake real do Chrome — bypassa Cloudflare Pro
# que verifica fingerprint além de IP. Última tentativa quando os outros falham.
try:
    from curl_cffi import requests as _curl_requests
except Exception:
    _curl_requests = None

HISTORY_FILE = Path("news_history.json")
HISTORY_RETENTION_DAYS = 14  # 2x folga sobre o lookback de 30h: cobre fim de
# semana/downtime sem purgar notícia que ainda não conseguiu summary (achado
# da auditoria: com 7d, item que falhava e saía do RSS era deletado sem nunca
# ser resumido)
MAX_SUMMARIZE_PER_RUN = 200  # cap pra controlar custo Gemini
BATCH_SIZE = 5               # itens por chamada Gemini → ~5x menos chamadas (cota)
MAX_RERESUME_ATTEMPTS = 3    # qtas vezes re-tentar upgrade LLM de um extractive
RERESUME_WINDOW_DAYS = 2     # só re-tenta upgrade de extractive recente; depois congela

# Instrução de resumo COMPARTILHADA (single + batch) pra os prompts não divergirem.
NEWS_INSTRUCTION = (
    "Você é um analista do setor brasileiro de utilities resumindo notícias. "
    "Para CADA item, em 2-3 frases curtas e diretas em PORTUGUÊS BRASILEIRO, diga: "
    "o QUE aconteceu (fato principal), QUEM está envolvido (empresas, órgãos, "
    "pessoas-chave) e VALORES/DATAS/números relevantes. Use **bold** pra destacar "
    "empresas e valores. PULE introduções vagas."
)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "pt-BR,pt;q=0.9",
}

_GEMINI_CLIENT = None


def _gemini_client():
    global _GEMINI_CLIENT
    if _GEMINI_CLIENT is not None:
        return _GEMINI_CLIENT
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        return None
    try:
        from google import genai
        _GEMINI_CLIENT = genai.Client(api_key=api_key)
        return _GEMINI_CLIENT
    except Exception as e:
        print(f"[gemini] init falhou: {e}", file=sys.stderr)
        return None


_PAYWALL_MARKERS = [
    "assine o", "assinar para continuar", "cadastre-se para ler",
    "para continuar lendo", "conteúdo exclusivo para assinantes",
    "você atingiu o limite de matérias",
]

# Padrão que aparece muito quando trafilatura captura sidebar de "notícias relacionadas"
# (ex: "21 de maio de 2026", "20 de maio de 2026" repetidos)
_DATE_LIST_RE = re.compile(
    r"\d{1,2}\s+de\s+(?:janeiro|fevereiro|março|abril|maio|junho|julho|"
    r"agosto|setembro|outubro|novembro|dezembro)\s+de\s+\d{4}",
    re.IGNORECASE,
)


def _is_paywall(text):
    if not text or len(text) < 300:
        return True
    tail = text[-500:].lower()
    return any(m in tail for m in _PAYWALL_MARKERS)


def _looks_like_sidebar(text):
    """Detecta quando trafilatura pegou lista de notícias relacionadas
    em vez do corpo (CanalEnergia, alguns Next.js)."""
    if not text:
        return False
    # 3+ datas no texto = provavelmente sidebar list de related news
    return len(_DATE_LIST_RE.findall(text)) >= 3


def _extract_meta_description(html_bytes):
    """Extrai descrição estruturada (JSON-LD ou <meta name=description>).
    Útil como fallback quando o body é JS-hidratado."""
    try:
        html = html_bytes.decode("utf-8", errors="ignore") if isinstance(html_bytes, bytes) else html_bytes
    except Exception:
        return ""
    # JSON-LD description (preferido — geralmente é o lead/sub-headline)
    m = re.search(
        r'<script[^>]*type="application/ld\+json"[^>]*>(.*?)</script>',
        html, re.DOTALL | re.IGNORECASE,
    )
    if m:
        ld = m.group(1)
        # pega "description":"..." (handles JSON escape)
        d = re.search(r'"description"\s*:\s*"((?:[^"\\]|\\.)*)"', ld)
        if d:
            desc = d.group(1).replace('\\"', '"').replace("\\/", "/")
            if len(desc) > 80:
                return desc[:1500]
    # Fallback: meta og:description ou name=description
    for pat in [
        r'<meta[^>]*property=["\']og:description["\'][^>]*content=["\']([^"\']+)["\']',
        r'<meta[^>]*name=["\']description["\'][^>]*content=["\']([^"\']+)["\']',
    ]:
        m = re.search(pat, html, re.IGNORECASE)
        if m and len(m.group(1)) > 80:
            return m.group(1)[:1500]
    return ""


def _resolve_url(google_news_url):
    """Decodifica URL encriptada do Google News pra URL real."""
    try:
        result = gnewsdecoder(google_news_url, interval=1)
        if result.get("status") and result.get("decoded_url"):
            return result["decoded_url"]
    except Exception:
        pass
    return None


_FETCH_FAIL_REASONS = {}  # diagnostic: {reason: count}


def _do_request(url):
    """Faz GET com requests; em caso de 403 (anti-bot), escalata:
    1) cloudscraper (TLS fingerprint custom)
    2) curl_cffi (impersona Chrome real)
    """
    r = requests.get(url, headers=HEADERS, timeout=15, allow_redirects=True)
    if r.status_code != 403:
        return r

    # Fallback 1: cloudscraper
    if _CLOUDSCRAPER is not None:
        try:
            r2 = _CLOUDSCRAPER.get(url, timeout=20, allow_redirects=True)
            if r2.status_code == 200:
                _FETCH_FAIL_REASONS["bypass_cloudscraper"] = _FETCH_FAIL_REASONS.get("bypass_cloudscraper", 0) + 1
                return r2
        except Exception:
            pass

    # Fallback 2: curl_cffi (impersona Chrome real)
    if _curl_requests is not None:
        try:
            r3 = _curl_requests.get(url, impersonate="chrome120", timeout=20, allow_redirects=True)
            if r3.status_code == 200:
                _FETCH_FAIL_REASONS["bypass_curl_cffi"] = _FETCH_FAIL_REASONS.get("bypass_curl_cffi", 0) + 1
                return r3
        except Exception:
            pass

    return r  # mantém o 403 original


def _fetch_article(google_news_url, max_retries=2):
    """Fetcha o artigo real (não a página do Google News). Retorna texto ou ""."""
    real_url = _resolve_url(google_news_url)
    if not real_url:
        _FETCH_FAIL_REASONS["resolve_failed"] = _FETCH_FAIL_REASONS.get("resolve_failed", 0) + 1
        return ""
    for attempt in range(max_retries):
        try:
            r = _do_request(real_url)
            if r.status_code == 403:
                _FETCH_FAIL_REASONS["http_403"] = _FETCH_FAIL_REASONS.get("http_403", 0) + 1
                return ""  # paywall hard
            if r.status_code in (502, 503, 504):
                if attempt == max_retries - 1:
                    _FETCH_FAIL_REASONS[f"http_{r.status_code}"] = _FETCH_FAIL_REASONS.get(f"http_{r.status_code}", 0) + 1
                time.sleep(2 ** attempt)
                continue
            if r.status_code != 200:
                _FETCH_FAIL_REASONS[f"http_{r.status_code}"] = _FETCH_FAIL_REASONS.get(f"http_{r.status_code}", 0) + 1
                return ""
            text = trafilatura.extract(r.content, favor_recall=True, include_comments=False)
            # Fallback pra sites SPA/JS-hidratados (CanalEnergia & cia):
            # trafilatura captura só sidebar de related news. Usa meta description / JSON-LD.
            if not text or _looks_like_sidebar(text):
                meta_desc = _extract_meta_description(r.content)
                if meta_desc:
                    first_line = (text or "").split("\n")[0].strip() if text else ""
                    combined = (first_line + ". " + meta_desc) if first_line and first_line.lower() not in meta_desc.lower() else meta_desc
                    return combined[:6000]
                else:
                    _FETCH_FAIL_REASONS["no_meta_desc"] = _FETCH_FAIL_REASONS.get("no_meta_desc", 0) + 1
            if _is_paywall(text or ""):
                _FETCH_FAIL_REASONS["paywall_or_short"] = _FETCH_FAIL_REASONS.get("paywall_or_short", 0) + 1
                return ""
            return text[:6000]
        except requests.RequestException as e:
            _FETCH_FAIL_REASONS[f"exc_{type(e).__name__}"] = _FETCH_FAIL_REASONS.get(f"exc_{type(e).__name__}", 0) + 1
            time.sleep(2 ** attempt)
            continue
    return ""


def _summarize(title, body):
    """Gemini Flash gera resumo. Resiliente a free tier (throttle/backoff/circuit
    breaker via gemini_util). Se o Gemini estiver indisponível (créditos/quota),
    cai pra resumo extrativo. Retorna (summary, source) onde source é
    'llm' | 'extractive' | None."""
    if not body or len(body) < 80:
        return None, None
    prompt = (
        f"{NEWS_INSTRUCTION}\n\n"
        f"Título: {title}\n\n"
        f"Texto:\n{body}\n\n"
        "Resumo (2-3 frases):"
    )
    try:
        s = gemini_util.generate(prompt, max_output_tokens=500)
        return (s, "llm") if s else (None, None)
    except gemini_util.GeminiCircuitOpen:
        fb = gemini_util.extractive_summary(body)
        return (fb, "extractive") if fb else (None, None)


def _record_success(history, url, item, title, summary, source):
    """Grava resumo OK. PRESERVA added_at (retention/janela corretos — antes,
    extractive resetava o relógio a cada run). No caso extractive, conta
    reresume_attempts pra congelar após N upgrades-tentados."""
    _prev = history["items"].get(url, {})
    entry = {
        "title": title,
        "summary": summary,
        "source": item.get("source", ""),
        "added_at": _prev.get("added_at") or datetime.now().isoformat(),
    }
    if source == "extractive":
        entry["summary_source"] = "extractive"
        entry["reresume_attempts"] = int(_prev.get("reresume_attempts") or 0) + 1
    history["items"][url] = entry


def _record_failure(history, url, item, title, error):
    """Grava falha preservando added_at e incrementando attempts (o cap em
    MAX_ITEM_ATTEMPTS tira o item do pending e protege a cota)."""
    _prev = history["items"].get(url, {})
    history["items"][url] = {
        "title": title,
        "summary": None,
        "source": item.get("source", ""),
        "added_at": _prev.get("added_at") or datetime.now().isoformat(),
        "error": error,
        "attempts": int(_prev.get("attempts") or 0) + 1,
    }


def _summarize_fetched_in_batches(history, fetched):
    """Resume os artigos (que têm corpo) em BATCHES de BATCH_SIZE itens por
    chamada Gemini. Itens que não voltarem no JSON do batch — ou todos, se o
    circuito abrir (cota esgotada) — caem pro extractive sem gastar chamada.
    Retorna nº de sucessos (llm + extractive). Função separada pra ser testável."""
    done = 0
    circuit_open = gemini_util.circuit_open()
    for start in range(0, len(fetched), BATCH_SIZE):
        chunk = fetched[start:start + BATCH_SIZE]
        batch_map = {}
        if not circuit_open:
            try:
                batch_map = gemini_util.generate_batch(
                    [{"id": u, "title": t, "text": b} for (u, _it, t, b) in chunk],
                    NEWS_INSTRUCTION,
                )
            except gemini_util.GeminiCircuitOpen:
                circuit_open = True  # cota esgotou — resto do run vai de extractive

        for (url, item, title, body) in chunk:
            if url in batch_map:
                _record_success(history, url, item, title, batch_map[url], "llm")
                done += 1
                print(f"  ✓ {batch_map[url][:80]}", file=sys.stderr)
            else:
                # Faltou no JSON do batch OU circuito aberto → fallback extrativo.
                fb = gemini_util.extractive_summary(body)
                if fb:
                    _record_success(history, url, item, title, fb, "extractive")
                    done += 1
                    print(f"  ≈ {fb[:80]}", file=sys.stderr)
                else:
                    _record_failure(history, url, item, title, "summarize_failed")
        _save(history)  # checkpoint após cada batch
    return done


def main():
    if not _gemini_client():
        print("[news_summarize] GEMINI_API_KEY não setada — pulando", file=sys.stderr)
        return

    # Carrega histórico existente
    if HISTORY_FILE.exists():
        history = json.loads(HISTORY_FILE.read_text(encoding="utf-8"))
    else:
        history = {"last_updated": None, "items": {}}
    # Considera "seen" só itens com summary OK ou paywall summarize_failed.
    # Itens com error=no_article serão reprocessados (o fix do meta-description
    # pode resgatar parte deles).
    # Só pula items que JÁ TÊM summary OK. Items com error (no_article OR
    # summarize_failed) são reprocessados — podem ter falhado por bug transitório
    # (API key errada, IP block resolvido, Gemini 503, etc.)
    # "Seen" = tem summary LLM. Resumos extrativos (fallback de quando o Gemini
    # estava indisponível) NÃO contam como seen → são re-resumidos com LLM quando
    # os créditos/quota voltam.
    # Também contam como seen (param de ser retentados):
    #   - error='no_article' (paywall/403) com 3+ dias — o site não vai destravar
    #   - qualquer error com attempts >= MAX_ITEM_ATTEMPTS — item travado a cada
    #     run queimava a cota diária do free tier (48 runs/dia × N itens) sem
    #     produzir nada; 8 tentativas a 30min de cadência cobrem ~4h de problema
    #     transitório (503, cota por minuto) com folga.
    MAX_ITEM_ATTEMPTS = 8
    _err_cutoff = (datetime.now() - timedelta(days=3)).isoformat()
    _extractive_cutoff = (datetime.now() - timedelta(days=RERESUME_WINDOW_DAYS)).isoformat()
    seen_urls = {
        url for url, v in history.get("items", {}).items()
        if (v.get("summary") and v.get("summary_source") != "extractive")
        or (v.get("error") == "no_article" and v.get("added_at", "") < _err_cutoff)
        or (v.get("error") and int(v.get("attempts") or 0) >= MAX_ITEM_ATTEMPTS)
        # Extractive vira "good enough" e CONGELA (para de re-resumir) quando já
        # foi re-tentado MAX_RERESUME_ATTEMPTS vezes OU não é mais recente. Antes,
        # os 148 extractive eram re-resumidos em TODO run — vazamento de cota.
        or (v.get("summary") and v.get("summary_source") == "extractive"
            and (int(v.get("reresume_attempts") or 0) >= MAX_RERESUME_ATTEMPTS
                 or v.get("added_at", "") < _extractive_cutoff))
    }

    # Fetch todas as fontes (mesma lógica do digest.py)
    print(f"[news_summarize] Fetching {len(SOURCES)} sources…", file=sys.stderr)
    all_items = []
    source_counts = {}
    for src in SOURCES:
        try:
            items = fetch_news(src)
            n = len(items)
            print(f"  [{src['name']}] {n} items", file=sys.stderr)
            source_counts[src['name']] = n
            all_items.extend(items)
        except Exception as e:
            print(f"[fetch] {src['name']}: {e}", file=sys.stderr)
            source_counts[src['name']] = f"ERR: {e}"
    print(f"[news_summarize] {len(all_items)} items coletados", file=sys.stderr)

    # Dedupe por URL
    by_url = {}
    for item in all_items:
        url = item.get("link")
        if url and url not in by_url:
            by_url[url] = item

    # Identifica novos (que faltam summary)
    pending = [(url, item) for url, item in by_url.items() if url not in seen_urls]
    print(f"[news_summarize] {len(pending)} novos pendentes (cap {MAX_SUMMARIZE_PER_RUN})", file=sys.stderr)

    pending = pending[:MAX_SUMMARIZE_PER_RUN]
    SAVE_EVERY = 25

    # ---- FASE 1: fetch dos artigos (rede; não dá pra batchar) ----
    # Separa quem tem corpo (vai pro batch) de quem falhou (no_article já gravado).
    fetched = []  # (url, item, title, body)
    consecutive_fetch_fail = 0
    MAX_CONSECUTIVE_FETCH_FAIL = 15
    for i, (url, item) in enumerate(pending, 1):
        title = item.get("title", "")
        body = _fetch_article(url)
        if body:
            fetched.append((url, item, title, body))
            consecutive_fetch_fail = 0
        else:
            _record_failure(history, url, item, title, "no_article")
            consecutive_fetch_fail += 1
            if consecutive_fetch_fail >= MAX_CONSECUTIVE_FETCH_FAIL:
                print(f"[news_summarize] {MAX_CONSECUTIVE_FETCH_FAIL} fetches falharam seguidos — parando fetch", file=sys.stderr)
                break
        if i % SAVE_EVERY == 0:
            _save(history)
        time.sleep(0.3)
    print(f"[news_summarize] {len(fetched)} artigos com corpo → batch de {BATCH_SIZE}", file=sys.stderr)

    # ---- FASE 2: resumo em BATCH (N itens por chamada Gemini → ~5x menos cota) ----
    done = _summarize_fetched_in_batches(history, fetched)

    # Prune itens antigos (retention)
    cutoff = (datetime.now() - timedelta(days=HISTORY_RETENTION_DAYS)).isoformat()
    before = len(history["items"])
    history["items"] = {
        url: v for url, v in history["items"].items()
        if v.get("added_at", "") >= cutoff
    }
    pruned = before - len(history["items"])

    _save(history)
    print(f"[news_summarize] {done} novos summaries, {pruned} antigos podados", file=sys.stderr)
    if _FETCH_FAIL_REASONS:
        print(f"[news_summarize] Fail breakdown: {_FETCH_FAIL_REASONS}", file=sys.stderr)

    # Dump diagnóstico pra debug remoto (commitado junto)
    diag = {
        "last_run": datetime.now().isoformat(),
        "source_counts": source_counts,
        "all_items_total": len(all_items),
        "pending_count": len(pending),
        "done": done,
        "pruned": pruned,
        "fetch_fail_reasons": _FETCH_FAIL_REASONS,
    }
    Path("news_diagnostic.json").write_text(
        json.dumps(diag, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def _save(history):
    history["last_updated"] = datetime.now().isoformat()
    HISTORY_FILE.write_text(
        json.dumps(history, ensure_ascii=False, indent=2), encoding="utf-8"
    )


if __name__ == "__main__":
    main()
