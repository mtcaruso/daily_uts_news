import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import quote_plus

import feedparser
import requests

from sources import SOURCES

LOOKBACK_HOURS = int(os.environ.get("LOOKBACK_HOURS", "30"))
MAX_PER_SOURCE = int(os.environ.get("MAX_PER_SOURCE", "15"))
DEDUPE_THRESHOLD = float(os.environ.get("DEDUPE_THRESHOLD", "0.85"))
RESEND_API_KEY = os.environ["RESEND_API_KEY"]
DIGEST_TO = os.environ["DIGEST_TO"]
DIGEST_FROM = os.environ.get("DIGEST_FROM", "Utilities Digest <onboarding@resend.dev>")

BLOCKLIST = [
    "agência de comunicação",
    "patrocinado",
    "publicidade",
    "press release",
    "informe publicitário",
    "branded content",
]


def is_promotional(title):
    title_lower = title.lower()
    return any(term in title_lower for term in BLOCKLIST)


def normalize_title(title):
    s = title.lower()
    s = re.split(r"\s+[\-|–—]\s+", s)[0]
    s = re.sub(r"[^\w\s]", " ", s, flags=re.UNICODE)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def jaccard(a, b):
    wa, wb = set(a.split()), set(b.split())
    if not wa or not wb:
        return 0.0
    return len(wa & wb) / len(wa | wb)


def fetch_source(source):
    url = (
        "https://news.google.com/rss/search?"
        f"q={quote_plus(source['query'])}&hl=pt-BR&gl=BR&ceid=BR:pt-419"
    )
    feed = feedparser.parse(url)
    cutoff = datetime.now(timezone.utc) - timedelta(hours=LOOKBACK_HOURS)
    items = []
    for entry in feed.entries:
        try:
            published = parsedate_to_datetime(entry.published)
        except Exception:
            continue
        if published < cutoff:
            continue
        if is_promotional(entry.title):
            continue
        items.append({"title": entry.title, "link": entry.link, "published": published})
    items.sort(key=lambda i: i["published"], reverse=True)
    return items


def dedupe(by_source):
    """Fuzzy dedupe across sources via Jaccard over normalized titles."""
    kept_norms = []
    out = {}
    for source_name, items in by_source.items():
        kept = []
        for item in items:
            norm = normalize_title(item["title"])
            if any(jaccard(norm, prev) >= DEDUPE_THRESHOLD for prev in kept_norms):
                continue
            kept_norms.append(norm)
            kept.append(item)
        out[source_name] = kept[:MAX_PER_SOURCE]
    return out


def render_html(by_source, date_str):
    total = sum(len(items) for items in by_source.values())
    blocks = []
    for source_name, items in by_source.items():
        if not items:
            continue
        rows = "\n".join(
            f'<li style="margin-bottom:6px"><a href="{item["link"]}" '
            f'style="color:#1a73e8;text-decoration:none">{item["title"]}</a></li>'
            for item in items
        )
        blocks.append(
            f'<h3 style="margin:24px 0 8px;border-bottom:1px solid #eee;padding-bottom:4px">'
            f'{source_name} <span style="color:#888;font-weight:normal;font-size:0.9em">'
            f'({len(items)})</span></h3>'
            f'<ul style="padding-left:18px;line-height:1.5;margin:0">{rows}</ul>'
        )
    body = "\n".join(blocks) if blocks else (
        '<p style="color:#888">Sem manchetes novas nas últimas '
        f'{LOOKBACK_HOURS}h.</p>'
    )
    return f"""<!doctype html>
<html><body style="font-family:-apple-system,Segoe UI,Helvetica,Arial,sans-serif;max-width:680px;margin:auto;color:#222;padding:16px">
<h1 style="font-size:20px;margin:0 0 4px">Utilities Daily &mdash; {date_str}</h1>
<p style="color:#666;font-size:13px;margin:0">{total} manchetes do setor de energia e saneamento.</p>
{body}
<hr style="margin-top:32px;border:none;border-top:1px solid #eee">
<p style="color:#aaa;font-size:11px">Gerado automaticamente via Google News RSS.</p>
</body></html>"""


def send_email(subject, html):
    response = requests.post(
        "https://api.resend.com/emails",
        headers={"Authorization": f"Bearer {RESEND_API_KEY}"},
        json={"from": DIGEST_FROM, "to": [DIGEST_TO], "subject": subject, "html": html},
        timeout=30,
    )
    response.raise_for_status()
    return response.json()


def main():
    by_source = {}
    for source in SOURCES:
        try:
            items = fetch_source(source)
            print(f"  {source['name']}: {len(items)} headlines", file=sys.stderr)
        except Exception as e:
            print(f"[warn] {source['name']}: {e}", file=sys.stderr)
            items = []
        by_source[source["name"]] = items
        time.sleep(1)
    by_source = dedupe(by_source)
    date_str = datetime.now().strftime("%d/%m/%Y")
    subject = f"Utilities Daily — {date_str}"
    html = render_html(by_source, date_str)
    result = send_email(subject, html)
    print(f"sent: id={result.get('id', '?')}")


if __name__ == "__main__":
    main()
