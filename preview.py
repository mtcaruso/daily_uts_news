"""Renders the digest HTML to a local file without sending email."""
import os
import time
import sys
from datetime import datetime

os.environ.setdefault("RESEND_API_KEY", "dummy")
os.environ.setdefault("DIGEST_TO", "dummy@example.com")

from digest import fetch_source, dedupe, render_html
from sources import SOURCES

by_source = {}
for src in SOURCES:
    try:
        items = fetch_source(src)
    except Exception as e:
        print(f"[warn] {src['name']}: {e}", file=sys.stderr)
        items = []
    by_source[src["name"]] = items
    time.sleep(0.5)
by_source = dedupe(by_source)

date_str = datetime.now().strftime("%d/%m/%Y")
html = render_html(by_source, date_str)
out_path = "preview.html"
with open(out_path, "w", encoding="utf-8") as f:
    f.write(html)
print(f"wrote {out_path} ({sum(len(v) for v in by_source.values())} headlines)")
