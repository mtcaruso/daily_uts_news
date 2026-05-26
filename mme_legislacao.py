"""MME Legislação tracker — coleta Portarias, Decretos, Leis e Portarias
Interministeriais publicadas em gov.br/mme/pt-br/acesso-a-informacao/legislacao.

Estrutura das listagens (Plone CMS) — DUAS variantes:

  ARTICLE-based (rich) — usado pra /portarias/{ano}/ e /decretos/{ano}/:
    <article class="entry">
      <a href="...slug.pdf/view" title="File">TÍTULO</a>
      <span class="documentByLine">... última modificação DD/MM/AAAA HHhMM</span>
      <p class="description discreet">EMENTA</p>
    </article>

  TABLE-based (flat) — usado pra /portarias-interministeriais/ e /leis/:
    <tr class="odd|even">
      <td><a href="...pdf/view">TÍTULO.pdf</a></td>
      <td>Arquivo</td>
      <td>DD/MM/AAAA HHhMM</td>
    </tr>
  (sem ementa no listing — pega no /view depois, opcional)

Persiste em mme_legislacao_history.json, keyed por slug normalizado.
Resume cada item novo via Gemini Flash (markdown **bold** padrão).
Notifica ntfy quando aparece item NOVO (após a primeira run).

Roda no workflow mme-legislacao.yml (2x/dia, Seg-Sex) e também via
local_refresh.bat como backup.
"""
import html as _html_mod
import json
import os
import re
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

from curl_cffi import requests as cf_requests

# Garante UTF-8 no stderr (Windows console pode usar cp1252 e barfa em ✓/✗)
try:
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

HISTORY_FILE = Path("mme_legislacao_history.json")
DIAGNOSTIC_FILE = Path("mme_legislacao_diagnostic.json")
HISTORY_RETENTION_DAYS = 365  # 1 ano de histórico de legislação MME
MAX_SUMMARIZE_PER_RUN = 50
MAX_PAGES_PER_CATEGORY = 10  # safety cap

NTFY_TOPIC = "utl-mtc-621qmvsd"
NTFY_URL = f"https://ntfy.sh/{NTFY_TOPIC}"

BASE = "https://www.gov.br/mme/pt-br/acesso-a-informacao/legislacao"

# (slug do path, label exibido, estratégia, opção)
# Estratégias:
#   - "by_year": URL é /{slug}/{ano}/ — paginate todas as páginas do ano corrente
#   - "flat_recent": URL é /{slug}/ — só ÚLTIMA página (itens mais recentes em order ASC)
CATEGORIES = [
    ("portarias", "Portaria MME", "by_year"),
    ("decretos", "Decreto", "by_year"),
    ("portarias-interministeriais", "Portaria Interministerial", "flat_recent"),
    ("leis", "Lei", "flat_recent"),
]

CURRENT_YEAR = datetime.now().year

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


# ============== HTTP ==============

def _http_get(url, max_attempts=3):
    """GET resiliente com múltiplas impersonations."""
    impersonations = ["chrome120", "chrome116", "edge99"]
    for attempt in range(max_attempts):
        imp = impersonations[attempt % len(impersonations)]
        try:
            r = cf_requests.get(url, impersonate=imp, timeout=30)
            if r.status_code == 200:
                return r
            if attempt < max_attempts - 1:
                time.sleep(2 + attempt * 2)
        except Exception as e:
            if attempt < max_attempts - 1:
                time.sleep(2)
            else:
                print(f"[http] {url}: {e}", file=sys.stderr)
    return None


# ============== PARSE LISTING ==============

# Regex pra capturar cada bloco <article class="entry"> com title, link, data, ementa
_ARTICLE_RE = re.compile(
    r'<article\s+class="entry"[^>]*>(.*?)</article>',
    re.DOTALL,
)
_LINK_RE = re.compile(
    r'<a\s+href="([^"]+\.pdf/view)"[^>]*title="File"[^>]*>([^<]+)</a>',
)
_DATE_RE = re.compile(
    r'(?:última|ultima)\s+modifica\S*?\s+(\d{2}/\d{2}/\d{4})',
    re.IGNORECASE,
)
_EMENTA_RE = re.compile(
    r'<p\s+class="description\s+discreet">([^<]+)</p>',
)


def _clean_title(raw: str) -> str:
    """'Portaria_MME_nº_11/2026' → 'Portaria MME nº 11/2026'."""
    s = _html_mod.unescape(raw).strip()
    # Substitui underline por espaço, mas preserva o número/ano
    s = s.replace("_", " ")
    s = re.sub(r"\s+", " ", s).strip()
    return s


def _slug_from_url(url: str) -> str:
    """'.../portaria-mme-n-11-2026.pdf/view' → 'portaria-mme-n-11-2026'."""
    m = re.search(r"/([^/]+)\.pdf/view$", url)
    if m:
        return m.group(1)
    # Fallback
    return url.rsplit("/", 1)[-1].replace(".pdf/view", "").replace("/view", "")


# Parser TABLE-based (flat categorias): <tr class="odd|even"><td><a>title.pdf</a></td>...
_TR_RE = re.compile(r'<tr\s+class="(?:odd|even)"[^>]*>(.*?)</tr>', re.DOTALL)
_TR_LINK_RE = re.compile(
    r'<a\s+href="([^"]+\.pdf/view)"[^>]*>([^<]+)</a>',
)
_TR_DATE_RE = re.compile(r'(\d{2}/\d{2}/\d{4})')


def _parse_listing_articles(html: str, category_slug: str) -> list:
    """Parser pra layout <article class="entry"> (categorias by_year)."""
    items = []
    for art_block in _ARTICLE_RE.findall(html):
        m_link = _LINK_RE.search(art_block)
        if not m_link:
            continue
        url = m_link.group(1)
        if f"/legislacao/{category_slug}/" not in url:
            continue
        title = _clean_title(m_link.group(2))
        slug = _slug_from_url(url)

        m_date = _DATE_RE.search(art_block)
        date = m_date.group(1) if m_date else None

        m_ementa = _EMENTA_RE.search(art_block)
        ementa = _html_mod.unescape(m_ementa.group(1)).strip() if m_ementa else ""

        pdf_url = url[:-len("/view")] if url.endswith("/view") else url

        items.append({
            "slug": slug,
            "title": title,
            "url": url,
            "pdf_url": pdf_url,
            "date_modified": date,
            "ementa": ementa,
        })
    return items


def _parse_listing_table(html: str, category_slug: str) -> list:
    """Parser pra layout <tr class='odd|even'> (categorias flat).
    Não tem ementa no listing — fica em string vazia, enriquece depois."""
    items = []
    for tr in _TR_RE.findall(html):
        m_link = _TR_LINK_RE.search(tr)
        if not m_link:
            continue
        url = m_link.group(1)
        if f"/legislacao/{category_slug}/" not in url:
            continue
        # Title vem como "Portaria Interministerial MME-MMA n 1-1999.pdf" → strip .pdf
        title = _html_mod.unescape(m_link.group(2)).strip()
        if title.lower().endswith(".pdf"):
            title = title[:-4].strip()
        title = _clean_title(title)
        slug = _slug_from_url(url)

        # Data tá no último <td> da linha
        m_date = _TR_DATE_RE.search(tr)
        date = m_date.group(1) if m_date else None

        pdf_url = url[:-len("/view")] if url.endswith("/view") else url

        items.append({
            "slug": slug,
            "title": title,
            "url": url,
            "pdf_url": pdf_url,
            "date_modified": date,
            "ementa": "",
        })
    return items


def parse_listing(html: str, category_slug: str, strategy: str = "by_year") -> list:
    """Wrapper: escolhe parser conforme estratégia."""
    if strategy == "flat_recent":
        return _parse_listing_table(html, category_slug)
    return _parse_listing_articles(html, category_slug)


# ============== ENRICH (fetch detail meta) ==============

_META_DESC_RE = re.compile(
    r'<meta[^>]+name="description"[^>]+content="([^"]+)"',
)


def fetch_ementa_from_detail(view_url: str) -> str:
    """Visita /view de um PDF e extrai meta description (ementa).
    Pra categorias flat onde o listing não tem ementa."""
    r = _http_get(view_url)
    if not r:
        return ""
    m = _META_DESC_RE.search(r.text)
    if m:
        return _html_mod.unescape(m.group(1)).strip()
    return ""


def _has_next_page(html: str, current_b_start: int) -> bool:
    """Detecta se há páginas com b_start > current_b_start.
    Plone MME usa numeração (1, 2, 3...) sem botão "Próximo" explícito."""
    starts = [int(x) for x in re.findall(r'b_start[%3A:]int=(\d+)', html)]
    return any(s > current_b_start for s in starts)


def _find_last_page_start(category_slug: str) -> int:
    """Pra flat_recent: descobre o b_start da última página seguindo o
    'last' link, depois retorna esse b_start (que tem os 20 itens mais recentes).
    Retorna 0 se não tem paginação."""
    url = f"{BASE}/{category_slug}"
    r = _http_get(url)
    if not r:
        return 0
    # Procura link 'last' tipo: <a class="last" href="...?b_start:int=140">Final »</a>
    m = re.search(
        r'class="last"[^>]*>\s*<a[^>]+href="[^"]*b_start[%3A:]int=(\d+)"',
        r.text,
    )
    if m:
        return int(m.group(1))
    # Fallback: procura qualquer ?b_start:int=N nos links de paginação, pega o maior
    starts = [int(x) for x in re.findall(r'b_start[%3A:]int=(\d+)', r.text)]
    if starts:
        return max(starts)
    return 0


def fetch_category(slug: str, label: str, strategy: str) -> list:
    """Coleta items de uma categoria conforme estratégia.

    by_year       → /{slug}/{CURRENT_YEAR}/ + paginação completa
    flat_recent   → /{slug}/ na última página (b_start = max)
    """
    items = []
    if strategy == "by_year":
        base_url = f"{BASE}/{slug}/{CURRENT_YEAR}"
        b_start = 0
        for page in range(MAX_PAGES_PER_CATEGORY):
            url = base_url if b_start == 0 else f"{base_url}?b_start:int={b_start}"
            r = _http_get(url)
            if not r:
                print(f"  [{slug}] página b_start={b_start}: fetch falhou", file=sys.stderr)
                break
            page_items = parse_listing(r.text, slug, strategy)
            if not page_items:
                break
            items.extend(page_items)
            if not _has_next_page(r.text, b_start):
                break
            b_start += 20
            time.sleep(0.5)
    elif strategy == "flat_recent":
        last_start = _find_last_page_start(slug)
        url = (
            f"{BASE}/{slug}"
            if last_start == 0
            else f"{BASE}/{slug}?b_start:int={last_start}"
        )
        r = _http_get(url)
        if not r:
            print(f"  [{slug}] última página fetch falhou", file=sys.stderr)
            return []
        items = parse_listing(r.text, slug, strategy)
    return items


# ============== SUMMARIZE ==============

def summarize(item: dict, label: str) -> str | None:
    """Gemini Flash gera resumo 1-2 frases baseado na ementa + título."""
    client = _gemini_client()
    if not client:
        return None
    ementa = item.get("ementa") or ""
    title = item.get("title") or ""
    # Se ementa muito curta, não vale resumir — UI mostra ementa crua
    if len(ementa) < 60:
        return None
    try:
        from google.genai import types
        prompt = (
            "Você é um analista do setor brasileiro de utilities resumindo um ato normativo do MME.\n\n"
            "REGRAS RÍGIDAS:\n"
            "- 1-2 frases curtas em PT-BR.\n"
            "- COMECE com o VERBO ou TEMA. NUNCA comece com 'Esta portaria...' ou 'O presente decreto...'.\n"
            "- Exemplos válidos:\n"
            "  - 'Designa **JOÃO SILVA** como substituto eventual do Consultor Jurídico.'\n"
            "  - 'Autoriza **EDP São Paulo** a operar a UHE Mascarenhas — vigência **15/03/2026**.'\n"
            "  - 'Cria grupo de trabalho para Leilão de Reserva de Capacidade — prazo **180 dias**.'\n"
            "- Use **bold** em nomes próprios, empresas, valores R$, datas, números de processo, leis.\n"
            "- Se for ato meramente administrativo (designação/dispensa), seja conciso.\n\n"
            f"Tipo: {label}\n"
            f"Título: {title}\n"
            f"Ementa: {ementa}\n\n"
            "Resumo (1-2 frases, sem filler inicial):"
        )
        for attempt in range(4):
            try:
                r = client.models.generate_content(
                    model="gemini-2.5-flash",
                    contents=prompt,
                    config=types.GenerateContentConfig(
                        temperature=0.2,
                        max_output_tokens=400,
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


# ============== NTFY ==============

def notify_ntfy(new_items: list):
    """Manda push pro ntfy quando há legislação nova.
    Agrupa por tipo, máx 5 items no body."""
    if not new_items:
        return
    n = len(new_items)
    title = f"📜 MME: {n} ato{'s' if n > 1 else ''} de legislação"
    lines = []
    for it in new_items[:5]:
        lines.append(f"{it.get('label', '')} {it.get('title', '')[:60]}")
    if n > 5:
        lines.append(f"...e mais {n - 5}")
    body = "\n".join(lines)
    try:
        cf_requests.post(
            NTFY_URL,
            data=body.encode("utf-8"),
            headers={
                "Title": title.encode("utf-8"),
                "Priority": "default",
                "Tags": "scroll",
            },
            timeout=10,
        )
    except Exception as e:
        print(f"[ntfy] erro: {e}", file=sys.stderr)


# ============== MAIN ==============

def _save(history):
    history["last_updated"] = datetime.now().isoformat()
    HISTORY_FILE.write_text(
        json.dumps(history, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def main():
    if not _gemini_client():
        print(
            "[mme_legislacao] GEMINI_API_KEY não setada — vou coletar mas não resumir",
            file=sys.stderr,
        )

    # Carrega histórico
    if HISTORY_FILE.exists():
        history = json.loads(HISTORY_FILE.read_text(encoding="utf-8"))
    else:
        history = {"last_updated": None, "items": {}, "first_run_done": False}
    history.setdefault("items", {})
    history.setdefault("first_run_done", False)
    seen_summarized = {k for k, v in history["items"].items() if v.get("summary")}

    # Coleta todas as categorias
    all_items_by_cat = {}
    total_collected = 0
    print(f"[mme_legislacao] Coletando {len(CATEGORIES)} categorias…", file=sys.stderr)
    for slug, label, strategy in CATEGORIES:
        try:
            items = fetch_category(slug, label, strategy)
        except Exception as e:
            print(f"  ✗ [{slug}] EXC: {e}", file=sys.stderr)
            items = []
        # Anota categoria/label no item
        for it in items:
            it["category"] = slug
            it["label"] = label
        all_items_by_cat[slug] = items
        total_collected += len(items)
        print(f"  [{slug:35s}] {len(items)} items", file=sys.stderr)

    if total_collected == 0:
        print("[mme_legislacao] Nenhum item coletado — abortando", file=sys.stderr)
        return

    # Flat list pra processar
    all_items = []
    for cat_items in all_items_by_cat.values():
        all_items.extend(cat_items)

    # Detecta items NOVOS (não existem em history["items"] ainda) e pre-registra
    # cada um com placeholder pra não recontá-los como "novos" no próximo run.
    # Sem isso, items pendentes além do MAX_SUMMARIZE_PER_RUN reapareceriam como
    # "novos" run após run, disparando ntfy duplicado.
    new_items = []
    now_iso = datetime.now().isoformat()
    for it in all_items:
        key = f"{it['category']}_{it['slug']}"
        if key not in history["items"]:
            new_items.append(it)
            # Placeholder mínimo (sem summary ainda)
            history["items"][key] = {
                "category": it["category"],
                "label": it.get("label"),
                "slug": it["slug"],
                "title": it["title"],
                "url": it["url"],
                "pdf_url": it["pdf_url"],
                "date_modified": it.get("date_modified"),
                "ementa": it.get("ementa", ""),
                "summary": None,
                "added_at": now_iso,
            }

    print(f"[mme_legislacao] {len(new_items)} items NOVOS (sem entry no history)", file=sys.stderr)

    # Items pendentes de summarize (qualquer um sem summary, novo ou antigo)
    pending = []
    for it in all_items:
        key = f"{it['category']}_{it['slug']}"
        if key in seen_summarized:
            continue
        it["_key"] = key
        pending.append(it)
    pending = pending[:MAX_SUMMARIZE_PER_RUN]
    print(f"[mme_legislacao] {len(pending)} pendentes de summarize (cap {MAX_SUMMARIZE_PER_RUN})", file=sys.stderr)

    done = 0
    failed = 0
    consecutive_failures = 0
    MAX_CONSECUTIVE_FAILURES = 10
    SAVE_EVERY = 15

    for i, item in enumerate(pending, 1):
        key = item["_key"]
        try:
            # Enrich: pra categorias flat (sem ementa no listing), busca meta description
            if not item.get("ementa") and item.get("category") in ("portarias-interministeriais", "leis"):
                ementa_extra = fetch_ementa_from_detail(item["url"])
                if ementa_extra:
                    item["ementa"] = ementa_extra
                time.sleep(0.6)  # gentle ao Plone

            summary = summarize(item, item.get("label", ""))

            # Preserva added_at original (do placeholder) se já existir
            prev_added = history["items"].get(key, {}).get("added_at") or datetime.now().isoformat()
            entry = {
                "category": item["category"],
                "label": item.get("label"),
                "slug": item["slug"],
                "title": item["title"],
                "url": item["url"],
                "pdf_url": item["pdf_url"],
                "date_modified": item.get("date_modified"),
                "ementa": item.get("ementa"),
                "summary": summary,
                "added_at": prev_added,
                "summarized_at": datetime.now().isoformat() if summary else None,
            }
            if not item.get("ementa"):
                entry["error"] = "no_ementa"
            elif not summary:
                entry["error"] = "summarize_failed_or_skipped"
            history["items"][key] = entry

            if summary:
                done += 1
                consecutive_failures = 0
                print(f"  ✓ [{i}/{len(pending)}] ({item['category']}) {summary[:80]}", file=sys.stderr)
            else:
                # Não conta como falha se foi skip por ementa curta
                if len(item.get("ementa", "")) < 60:
                    print(f"  - [{i}/{len(pending)}] ({item['category']}) ementa curta — sem summary", file=sys.stderr)
                else:
                    failed += 1
                    consecutive_failures += 1
                    if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                        print(f"[mme_legislacao] {MAX_CONSECUTIVE_FAILURES} falhas consecutivas — parando", file=sys.stderr)
                        break
        except Exception as e:
            print(f"  ✗ [{i}/{len(pending)}] EXC: {e}", file=sys.stderr)
            failed += 1
            consecutive_failures += 1

        if i % SAVE_EVERY == 0:
            _save(history)
            print(f"[mme_legislacao] checkpoint salvo ({done} ok)", file=sys.stderr)
        time.sleep(0.5)

    # Items novos pra notificar via ntfy — só após primeira run
    if history["first_run_done"] and new_items:
        notify_ntfy(new_items)
        print(f"[mme_legislacao] ntfy enviado: {len(new_items)} novos", file=sys.stderr)
    elif not history["first_run_done"]:
        print(f"[mme_legislacao] primeira run — {len(new_items)} items capturados (sem ntfy)", file=sys.stderr)
        history["first_run_done"] = True

    # Prune retention
    cutoff = (datetime.now() - timedelta(days=HISTORY_RETENTION_DAYS)).isoformat()
    before = len(history["items"])
    history["items"] = {
        k: v for k, v in history["items"].items()
        if v.get("added_at", "") >= cutoff
    }
    pruned = before - len(history["items"])

    _save(history)
    print(
        f"[mme_legislacao] DONE: {done} novos summaries, {failed} fails, "
        f"{len(new_items)} items novos, {pruned} podados",
        file=sys.stderr,
    )

    # Diagnóstico
    DIAGNOSTIC_FILE.write_text(json.dumps({
        "last_run": datetime.now().isoformat(),
        "total_collected": total_collected,
        "by_category": {k: len(v) for k, v in all_items_by_cat.items()},
        "new_items": len(new_items),
        "pending": len(pending),
        "done": done,
        "failed": failed,
        "pruned": pruned,
    }, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
