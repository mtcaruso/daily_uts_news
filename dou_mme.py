"""
Digest diário do DOU (seção 1) — atos do MME e ANEEL, exceto ANM/ANP/SNGMTM.

Modos:
    python dou_mme.py                    # busca data de hoje, envia por email
    python dou_mme.py --date 19-05-2026  # data específica
    python dou_mme.py --dry-run          # imprime no stdout, não envia email
"""
import argparse
import json
import os
import re
import sys
from datetime import datetime
from html import unescape

import requests

BASE_URL = "https://www.in.gov.br/consulta/-/buscar/dou"
HEADERS = {"User-Agent": "Mozilla/5.0 (dou-mme-digest)"}

# Queries usadas para varrer o dia. Como o DOU não tem filtro nativo por órgão
# via URL, a gente puxa vários termos amplos e depois filtra pela hierarchyStr.
QUERIES = ["ANEEL", "Minas e Energia", "energia elétrica", "leilão energia"]

TARGET_PREFIXES = [
    "ministério de minas e energia",
    "ministerio de minas e energia",
]

# Suborgãos do MME a IGNORAR (mineração e petróleo não interessam).
SUBORG_BLOCKLIST = [
    "agência nacional de mineração",
    "agência nacional do petróleo",
    "secretaria nacional de geologia",
]


def fetch_page(date_str, query, page):
    params = {
        "q": query,
        "s": "do1",
        "exactDate": "personalizado",
        "publishFrom": date_str,
        "publishTo": date_str,
        "delta": 20,
        "currentPage": page,
        "newPage": page,
    }
    r = requests.get(BASE_URL, params=params, headers=HEADERS, timeout=30)
    r.raise_for_status()
    m = re.search(
        r'_BuscaDouPortlet_params"[^>]*>\s*(\{.*?\})\s*</script>',
        r.text,
        re.DOTALL,
    )
    if not m:
        return [], 0
    data = json.loads(m.group(1))
    tp_match = re.search(r"totalPages\s*:\s*(\d+)", r.text)
    total_pages = int(tp_match.group(1)) if tp_match else 1
    return data.get("jsonArray", []), total_pages


def fetch_all(date_str, query):
    hits, total_pages = fetch_page(date_str, query, 1)
    out = list(hits)
    for p in range(2, total_pages + 1):
        more, _ = fetch_page(date_str, query, p)
        out.extend(more)
    return out


def sub_org(hit):
    lst = hit.get("hierarchyList") or []
    return lst[1] if len(lst) > 1 else "(sem subórgão)"


def is_mme(hit):
    h = (hit.get("hierarchyStr") or "").lower()
    if not any(h.startswith(p) for p in TARGET_PREFIXES):
        return False
    sub = sub_org(hit).lower()
    return not any(b in sub for b in SUBORG_BLOCKLIST)


def link_for(hit):
    return f"https://www.in.gov.br/web/dou/-/{hit['urlTitle']}"


_PARA_RE = re.compile(
    r'<p class="dou-paragraph"[^>]*>(.*?)</p>', re.DOTALL
)
_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")
SUMMARY_MAX = 280


def fetch_summary(url):
    """Pega o 2º parágrafo do ato (1º é sempre o preâmbulo) e trunca."""
    try:
        r = requests.get(url, headers=HEADERS, timeout=20)
        r.raise_for_status()
    except requests.RequestException:
        return ""
    paras = []
    for m in _PARA_RE.finditer(r.text):
        text = _WS_RE.sub(" ", unescape(_TAG_RE.sub("", m.group(1)))).strip()
        if text:
            paras.append(text)
    if len(paras) < 2:
        return paras[0][:SUMMARY_MAX] if paras else ""
    body = paras[1]
    if len(body) <= SUMMARY_MAX:
        return body
    cut = body.rfind(" ", 0, SUMMARY_MAX)
    return body[: cut if cut > 0 else SUMMARY_MAX].rstrip(",;:.") + "…"


def collect(date_str, with_summary=True):
    seen = {}
    for q in QUERIES:
        for h in fetch_all(date_str, q):
            seen.setdefault(h["urlTitle"], h)
    hits = [h for h in seen.values() if is_mme(h)]
    if with_summary:
        for h in hits:
            h["summary"] = fetch_summary(link_for(h))
    return hits


def group_by_sub(hits):
    by_sub = {}
    for h in hits:
        by_sub.setdefault(sub_org(h), []).append(h)
    return by_sub


def render_text(by_sub, date_str):
    out = [f"=== DOU {date_str} — Seção 1 — MME + ANEEL ==="]
    total = sum(len(v) for v in by_sub.values())
    out.append(f"Total: {total} atos\n")
    for sub in sorted(by_sub):
        items = by_sub[sub]
        out.append(f"## {sub}  ({len(items)})")
        for h in items:
            tail = h["hierarchyList"][2:] if len(h["hierarchyList"]) > 2 else []
            out.append(f"  - {h['title']}")
            if tail:
                out.append(f"    [{' / '.join(tail)}]")
            if h.get("summary"):
                out.append(f"    {h['summary']}")
            out.append(f"    {link_for(h)}")
        out.append("")
    return "\n".join(out)


def render_html(by_sub, date_str):
    total = sum(len(v) for v in by_sub.values())
    blocks = []
    for sub in sorted(by_sub):
        items = by_sub[sub]
        rows = []
        for h in items:
            tail = h["hierarchyList"][2:] if len(h["hierarchyList"]) > 2 else []
            tail_html = (
                f'<div style="color:#888;font-size:12px;margin:2px 0 0">'
                f'{" / ".join(tail)}</div>'
                if tail
                else ""
            )
            summary_html = (
                f'<div style="color:#444;font-size:13px;margin:4px 0 0;'
                f'line-height:1.45">{h["summary"]}</div>'
                if h.get("summary")
                else ""
            )
            rows.append(
                f'<li style="margin-bottom:14px">'
                f'<a href="{link_for(h)}" '
                f'style="color:#1a73e8;text-decoration:none">{h["title"]}</a>'
                f"{tail_html}{summary_html}</li>"
            )
        blocks.append(
            f'<h3 style="margin:24px 0 8px;border-bottom:1px solid #eee;'
            f'padding-bottom:4px">{sub} '
            f'<span style="color:#888;font-weight:normal;font-size:0.9em">'
            f"({len(items)})</span></h3>"
            f'<ul style="padding-left:18px;line-height:1.4;margin:0">'
            f'{"".join(rows)}</ul>'
        )
    body = (
        "\n".join(blocks)
        if blocks
        else '<p style="color:#888">Nenhum ato do MME/ANEEL publicado hoje.</p>'
    )
    return f"""<!doctype html>
<html><body style="font-family:-apple-system,Segoe UI,Helvetica,Arial,sans-serif;max-width:680px;margin:auto;color:#222;padding:16px">
<h1 style="font-size:20px;margin:0 0 4px">DOU MME/ANEEL &mdash; {date_str}</h1>
<p style="color:#666;font-size:13px;margin:0">{total} atos publicados hoje na Se&ccedil;&atilde;o 1.</p>
{body}
<hr style="margin-top:32px;border:none;border-top:1px solid #eee">
<p style="color:#aaa;font-size:11px">Fonte: Imprensa Nacional &mdash; in.gov.br</p>
</body></html>"""


def send_email(subject, html):
    api_key = os.environ["RESEND_API_KEY"]
    to_addr = os.environ["DIGEST_TO"]
    from_addr = os.environ.get("DIGEST_FROM", "DOU Digest <onboarding@resend.dev>")
    r = requests.post(
        "https://api.resend.com/emails",
        headers={"Authorization": f"Bearer {api_key}"},
        json={"from": from_addr, "to": [to_addr], "subject": subject, "html": html},
        timeout=30,
    )
    r.raise_for_status()
    return r.json()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--date",
        default=datetime.now().strftime("%d-%m-%Y"),
        help="Data de publicação no formato dd-mm-aaaa (padrão: hoje)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Imprime no stdout em vez de enviar email",
    )
    args = parser.parse_args()

    print(f"Buscando DOU {args.date} (seção 1)…", file=sys.stderr)
    hits = collect(args.date)
    by_sub = group_by_sub(hits)
    print(f"  {len(hits)} atos do MME/ANEEL", file=sys.stderr)

    pretty_date = args.date.replace("-", "/")
    subject = f"DOU MME/ANEEL — {pretty_date}"

    if args.dry_run:
        sys.stdout.reconfigure(encoding="utf-8")
        print(render_text(by_sub, pretty_date))
        return

    html = render_html(by_sub, pretty_date)
    result = send_email(subject, html)
    print(f"sent: id={result.get('id', '?')}", file=sys.stderr)


if __name__ == "__main__":
    main()
