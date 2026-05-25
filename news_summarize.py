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
HISTORY_RETENTION_DAYS = 7  # nota: lookback de news é 30h, então 7d é folga
MAX_SUMMARIZE_PER_RUN = 200  # cap pra controlar custo Gemini

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
    """Gemini Flash gera resumo. Retorna None em erro."""
    client = _gemini_client()
    if not client:
        return None
    if not body or len(body) < 80:
        return None
    try:
        from google.genai import types
        prompt = (
            "Você é um analista do setor brasileiro de utilities resumindo uma notícia. "
            "Em 2-3 frases curtas e diretas em PORTUGUÊS BRASILEIRO, diga:\n"
            "- O QUE aconteceu (fato principal).\n"
            "- QUEM está envolvido (empresas, órgãos, pessoas-chave).\n"
            "- VALORES, DATAS ou números relevantes.\n"
            "Use **bold** pra destacar empresas e valores. PULE introduções vagas.\n\n"
            f"Título: {title}\n\n"
            f"Texto:\n{body}\n\n"
            "Resumo (2-3 frases):"
        )
        for attempt in range(4):
            try:
                r = client.models.generate_content(
                    model="gemini-2.5-flash",
                    contents=prompt,
                    config=types.GenerateContentConfig(
                        temperature=0.2,
                        max_output_tokens=500,
                        thinking_config=types.ThinkingConfig(thinking_budget=0),
                    ),
                )
                return (r.text or "").strip() or None
            except Exception as e:
                err = str(e)
                if "503" in err or "UNAVAILABLE" in err:
                    if attempt < 3:
                        time.sleep(10 * (attempt + 1))
                        continue
                print(f"[gemini] erro: {e}", file=sys.stderr)
                return None
        return None
    except Exception as e:
        print(f"[summarize] erro: {e}", file=sys.stderr)
        return None


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
    seen_urls = {
        url for url, v in history.get("items", {}).items()
        if v.get("summary")
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
    done = 0
    consecutive_failures = 0
    MAX_CONSECUTIVE_FAILURES = 15
    SAVE_EVERY = 25

    for i, (url, item) in enumerate(pending, 1):
        title = item.get("title", "")
        body = _fetch_article(url)
        if body:
            summary = _summarize(title, body)
            if summary:
                history["items"][url] = {
                    "title": title,
                    "summary": summary,
                    "source": item.get("source", ""),
                    "added_at": datetime.now().isoformat(),
                }
                done += 1
                consecutive_failures = 0
                print(f"  ✓ [{i}/{len(pending)}] {summary[:80]}", file=sys.stderr)
            else:
                # Gemini falhou
                history["items"][url] = {
                    "title": title,
                    "summary": None,
                    "source": item.get("source", ""),
                    "added_at": datetime.now().isoformat(),
                    "error": "summarize_failed",
                }
                consecutive_failures += 1
                if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                    print(f"[news_summarize] {MAX_CONSECUTIVE_FAILURES} falhas consecutivas — parando", file=sys.stderr)
                    break
        else:
            # Não conseguiu pegar o artigo (paywall, 403, redirect falhou)
            history["items"][url] = {
                "title": title,
                "summary": None,
                "source": item.get("source", ""),
                "added_at": datetime.now().isoformat(),
                "error": "no_article",
            }
            consecutive_failures += 1
            if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                break

        # Checkpoint
        if i % SAVE_EVERY == 0:
            _save(history)
            print(f"[news_summarize] checkpoint salvo ({done} sucesso)", file=sys.stderr)
        time.sleep(0.5)

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
