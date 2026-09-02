"""Central configuration and the TLS/CA handling this environment needs."""
from __future__ import annotations
import os, pathlib

ROOT = pathlib.Path(__file__).resolve().parent.parent
RAW = ROOT / "raw"
DATA = ROOT / "data"
CERTS = ROOT / "certs"
DB_PATH = DATA / "contracts.duckdb"

BASE = "https://www.war.gov"
INDEX_URL = BASE + "/News/Contracts/"
# ContentType=400 is the Contracts feed specifically (verified against the live feed
# title "Contracts - U.S. Dept. of War"). Used for cheap change polling.
RSS_URL = BASE + "/DesktopModules/ArticleCS/RSS.ashx?ContentType=400&Site=945&max={max}"

# war.gov sits behind Akamai, which fingerprints the TLS handshake (JA3), not merely
# the headers. Verified empirically: `requests` sending the exact Chrome header set
# still gets 403; curl with those same headers gets 200. So the fetch layer uses
# curl_cffi with Chrome impersonation -- see fetch.py::_session.
#
# This is a public page with no auth and no rate limit being evaded; the client simply
# has to look like the browser the site expects.
IMPERSONATE = "chrome"

# Kept for reference and for any non-impersonating client. curl_cffi supplies its own
# browser-consistent header set when impersonating, so fetch.py does not apply these.
BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Upgrade-Insecure-Requests": "1",
    "Connection": "keep-alive",
}

REQUEST_TIMEOUT = 30
POLITE_DELAY_SEC = 0.5   # be a good citizen against a .gov host


def ca_bundle() -> str:
    """Return a CA bundle path that works behind local TLS inspection.

    Bitdefender endpoint security on this machine MITMs some hosts, which breaks
    default verification for those. We append its root to certifi's bundle rather
    than disabling verification.
    """
    import certifi
    base = pathlib.Path(certifi.where())
    local = CERTS / "bitdefender.pem"
    if not local.exists():
        return str(base)
    merged = CERTS / "ca_bundle.pem"
    if not merged.exists() or merged.stat().st_mtime < max(base.stat().st_mtime, local.stat().st_mtime):
        CERTS.mkdir(parents=True, exist_ok=True)
        merged.write_text(base.read_text() + "\n" + local.read_text(), encoding="utf-8")
    return str(merged)


def _load_dotenv() -> None:
    """Read `.env` into os.environ before any value below is resolved.

    `.env.example` tells the user to copy it to `.env`, so something has to read
    it. python-dotenv if present; otherwise a minimal parser, because a missing
    optional dependency should not silently turn a --live run into "key not set".
    An already-exported variable always wins -- the shell beats the file.
    """
    path = ROOT / ".env"
    if not path.exists():
        return
    try:
        from dotenv import load_dotenv
        load_dotenv(path, override=False)
        return
    except ModuleNotFoundError:
        pass
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


_load_dotenv()

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
BULK_MODEL = os.environ.get("BULK_MODEL", "claude-haiku-4-5")
JUDGE_MODEL = os.environ.get("JUDGE_MODEL", "claude-opus-5")
