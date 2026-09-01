"""Deterministic acquisition layer.

Everything here is plain code on purpose: fetching pages, caching bytes, and
recording provenance are simple, repeatable operations with no ambiguity. No
model is involved until the prose actually has to be understood (see extract.py).

Every fetch writes a sidecar provenance record so any row in the final database
can be traced back to a specific URL, a specific byte payload, and the moment we
retrieved it.
"""
from __future__ import annotations

import hashlib
import html as htmllib
import json
import re
import time
from dataclasses import dataclass, asdict
from datetime import datetime, timezone

from curl_cffi import requests

from config import (
    IMPERSONATE, INDEX_URL, POLITE_DELAY_SEC, RAW, REQUEST_TIMEOUT, ca_bundle,
)

MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}


@dataclass
class Provenance:
    url: str
    fetched_at: str
    http_status: int
    sha256: str
    n_bytes: int


@dataclass
class Announcement:
    article_id: str
    title: str
    url: str
    announced_date: str | None   # ISO date parsed from the title


def _session() -> requests.Session:
    """A session whose TLS handshake looks like Chrome's.

    war.gov sits behind Akamai, which fingerprints the TLS/JA3 handshake, not just
    headers. Verified empirically: `requests` sending the *exact* browser header set
    still gets 403, while curl with the same headers gets 200. curl_cffi replicates
    Chrome's handshake, which is what actually clears the check.
    """
    s = requests.Session(impersonate=IMPERSONATE)
    s.verify = ca_bundle()
    return s


def fetch(sess: requests.Session, url: str, cache_path, force: bool = False) -> tuple[str, Provenance]:
    """Fetch a URL, caching the body and its provenance sidecar next to it."""
    prov_path = cache_path.with_suffix(cache_path.suffix + ".prov.json")
    if cache_path.exists() and prov_path.exists() and not force:
        return (cache_path.read_text(encoding="utf-8", errors="replace"),
                Provenance(**json.loads(prov_path.read_text())))

    resp = sess.get(url, timeout=REQUEST_TIMEOUT)
    body = resp.text
    prov = Provenance(
        url=url,
        fetched_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        http_status=resp.status_code,
        sha256=hashlib.sha256(resp.content).hexdigest(),
        n_bytes=len(resp.content),
    )
    resp.raise_for_status()
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(body, encoding="utf-8")
    prov_path.write_text(json.dumps(asdict(prov), indent=2), encoding="utf-8")
    time.sleep(POLITE_DELAY_SEC)
    return body, prov


def parse_title_date(title: str) -> str | None:
    """'Contracts for Aug. 31, 2026' -> '2026-08-31'."""
    m = re.search(r"([A-Za-z]{3})[a-z]*\.?\s+(\d{1,2}),\s+(\d{4})", title)
    if not m:
        return None
    mon = MONTHS.get(m.group(1).lower())
    return f"{int(m.group(3)):04d}-{mon:02d}-{int(m.group(2)):02d}" if mon else None


def parse_index(page_html: str) -> list[Announcement]:
    """Pull announcement stubs out of the listing page.

    The listing is a Vue component, but the data is server-rendered into the
    component's attributes, so no API reverse-engineering or JS execution is
    needed -- we read article-id / article-title / article-url directly.
    """
    out: list[Announcement] = []
    for block in re.findall(r"<[^>]*article-id=.*?>", page_html, re.S):
        if 'content-type-name="Contracts"' not in block:
            continue
        aid = re.search(r'article-id="(\d+)"', block)
        title = re.search(r'article-title="([^"]*)"', block)
        url = re.search(r'article-url="([^"]*)"', block)
        if not (aid and title and url):
            continue
        t = htmllib.unescape(title.group(1))
        out.append(Announcement(aid.group(1), t, htmllib.unescape(url.group(1)), parse_title_date(t)))
    return out


def body_text(article_html: str) -> str:
    """Reduce an announcement page to the announcement prose."""
    h = re.sub(r"<script.*?</script>|<style.*?</style>", " ", article_html, flags=re.S | re.I)
    m = re.search(r'<div[^>]*class="[^"]*\bbody\b[^"]*"[^>]*>(.*?)</div>\s*</div>', h, re.S | re.I)
    seg = m.group(1) if m else h
    t = htmllib.unescape(re.sub(r"<[^>]+>", "\n", seg))
    t = re.sub(r"[ \t\xa0]+", " ", t)
    t = re.sub(r"\n\s*\n+", "\n", t).strip()
    # Announcements start at the first service header.
    m2 = re.search(r"^\s*(ARMY|NAVY|AIR FORCE|DEFENSE|SPACE FORCE|MARINE)", t, re.M)
    return t[m2.start():] if m2 else t


def collect(pages: int = 5, force: bool = False) -> list[dict]:
    """Fetch index pages, then every announcement they reference. Returns manifest rows."""
    sess = _session()
    seen: dict[str, Announcement] = {}
    for p in range(1, pages + 1):
        url = INDEX_URL if p == 1 else f"{INDEX_URL}?Page={p}"
        body, _ = fetch(sess, url, RAW / "index" / f"page{p}.html", force=force)
        for a in parse_index(body):
            seen.setdefault(a.article_id, a)

    manifest = []
    for a in sorted(seen.values(), key=lambda x: x.announced_date or "", reverse=True):
        path = RAW / "articles" / f"{a.article_id}.html"
        try:
            raw, prov = fetch(sess, a.url, path, force=force)
        except Exception as e:  # one bad article must not abort a 50-article backfill
            print(f"  ! skip {a.article_id}: {type(e).__name__}: {e}")
            continue
        manifest.append({**asdict(a), "cache_path": str(path.relative_to(RAW.parent)),
                         "provenance": asdict(prov), "body_chars": len(body_text(raw))})
    (RAW / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return manifest


if __name__ == "__main__":
    import sys
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 5
    rows = collect(pages=n)
    print(f"announcements cached: {len(rows)}")
    if rows:
        d = [r["announced_date"] for r in rows if r["announced_date"]]
        print(f"date range: {min(d)} .. {max(d)}")
        print(f"median body chars: {sorted(r['body_chars'] for r in rows)[len(rows)//2]}")
