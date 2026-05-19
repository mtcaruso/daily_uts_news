"""Smoke test: fetch each source and report coverage without sending email."""
import os
os.environ.setdefault("RESEND_API_KEY", "dummy")
os.environ.setdefault("DIGEST_TO", "dummy@example.com")

from digest import fetch_source
from sources import SOURCES

total = 0
for src in SOURCES:
    try:
        items = fetch_source(src)
    except Exception as e:
        print(f"  {src['name']:18s} ERROR: {e}")
        continue
    total += len(items)
    sample = items[0]["title"][:80] if items else "(none)"
    print(f"  {src['name']:18s} {len(items):3d} headlines  | ex: {sample}")

print(f"\nTotal: {total} headlines across {len(SOURCES)} sources")
