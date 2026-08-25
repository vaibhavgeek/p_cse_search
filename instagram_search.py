"""
Instagram Reel Search via Google CSE
Handles cse_tok automatically.
"""

import requests
import json
import re
import time
from datetime import datetime

CX       = "013354601007441521110:enwkzivtj-o"
BASE_URL = "https://cse.google.com/cse/element/v1"

HEADERS = {
    "User-Agent":      "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    "Accept":          "*/*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer":         "https://www.google.com/",
}


def get_cse_token() -> tuple[requests.Session, str]:
    """
    Spin up a session and grab a fresh cse_tok from Google's JS file.
    Without this token every search returns 403.
    """
    session = requests.Session()
    session.headers.update(HEADERS)

    print("[*] Fetching fresh cse_tok ...")
    r = session.get(f"https://cse.google.com/cse.js?cx={CX}", timeout=15)
    r.raise_for_status()

    match = re.search(r'"cse_tok"\s*:\s*"([^"]+)"', r.text)
    if not match:
        raise RuntimeError(
            "cse_tok not found in cse.js — Google may have changed the format."
        )

    token = match.group(1)
    print(f"[+] Token acquired: {token[:40]}...")
    return session, token


def search(
    session: requests.Session,
    token: str,
    query: str,
    start: int = 0,
    sort_by_date: bool = True,
) -> dict:
    """Single page search call."""
    r = session.get(
        BASE_URL,
        params={
            "rsz":      "filtered_cse",
            "num":      10,
            "hl":       "en",
            "source":   "gcsc",
            "cx":       CX,
            "q":        query,
            "oq":       query,
            "safe":     "off",
            "cse_tok":  token,
            "filter":   "0",
            "sort":     "date" if sort_by_date else "",
            "start":    start,
            "callback": "google.search.cse.api",
        },
        timeout=15,
    )
    r.raise_for_status()

    # Strip JSONP wrapper -> raw JSON
    raw = r.text.strip()
    return json.loads(raw[raw.index("(") + 1 : raw.rindex(")")])


def parse(data: dict) -> list[dict]:
    """Pull clean fields out of a raw CSE response."""
    results = []
    for item in data.get("results", []):
        meta  = item.get("richSnippet", {}).get("metatags", {})
        thumb = item.get("richSnippet", {}).get("cseThumbnail", {})
        img   = item.get("richSnippet", {}).get("cseImage", {})
        results.append({
            "title":     item.get("titleNoFormatting", ""),
            "url":       item.get("unescapedUrl", ""),
            "snippet":   item.get("contentNoFormatting", ""),
            "site":      item.get("visibleUrl", ""),
            "thumbnail": thumb.get("src") or img.get("src", ""),
            "og_desc":   meta.get("ogDescription", ""),
        })
    return results


def run(query: str, pages: int = 2, sort_by_date: bool = True) -> list[dict]:
    session, token = get_cse_token()
    all_results    = []

    for page in range(pages):
        start = page * 10
        print(f"[*] Page {page + 1}/{pages}  (offset={start})")

        data    = search(session, token, query, start=start, sort_by_date=sort_by_date)
        results = parse(data)

        if not results:
            print("[!] No more results.")
            break

        all_results.extend(results)

        if page == 0:
            cursor = data.get("cursor", {})
            print(f"[i] ~{cursor.get('resultCount', '?')} total results  |  fetched in {cursor.get('searchResultTime', '?')}s")

        if page < pages - 1:
            time.sleep(0.8)   # be polite

    return all_results


def display(results: list[dict]):
    print(f"\n{'=' * 65}")
    for i, r in enumerate(results, 1):
        print(f"\n[{i:02d}] {r['title']}")
        print(f"      {r['url']}")
        print(f"      {r['snippet'][:120]}")
        if r["thumbnail"]:
            print(f"      thumb: {r['thumbnail']}")
    print(f"\n{'=' * 65}")
    print(f"Fetched {len(results)} results total.")


# ── Change these ──────────────────────────────────────────────
QUERY        = "spiderman"
PAGES        = 2          # each page = 10 results, max 10 pages (100 results)
SORT_BY_DATE = True       # True = newest first | False = most relevant
SAVE_JSON    = True
# ─────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print(f"\nQuery: '{QUERY}'  |  sort_by_date={SORT_BY_DATE}  |  pages={PAGES}")
    print("-" * 65)

    results = run(QUERY, pages=PAGES, sort_by_date=SORT_BY_DATE)
    display(results)

    if SAVE_JSON and results:
        fname = f"results_{QUERY}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
        with open(fname, "w", encoding="utf-8") as f:
            json.dump(results, f, indent=2, ensure_ascii=False)
        print(f"Saved -> {fname}")