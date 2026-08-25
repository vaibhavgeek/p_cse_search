#!/usr/bin/env python3
"""
cse_search.py — Drive the live p_cse_search page in a real browser,
paginate through results, and stream one JSON object per page to stdout
as NDJSON (newline-delimited JSON).

Runs the same code path a user hits at:
  https://vaibhavgeek.github.io/p_cse_search/

Does NOT use the OpenAI-powered "Extract all as JSON" button — this is
a pure DOM scrape of the CSE result cards, so no OPENAI_API_KEY is
required and no LLM costs are incurred. Per-result fields are the ones
we can derive without a model:

  {
    "title":        str,          # e.g. "Cooking tip — Instagram"
    "reel_url":     str,          # unmangled destination URL
    "display_link": str,          # e.g. "Instagram › reel"
    "snippet":      str | None,   # search-result snippet
    "screenshot":   str | None,   # thumbnail image URL
    "platform":     "instagram" | "tiktok" | None,
  }

Each page is emitted as one JSON object:

  {"page": 1, "engine": "instagram", "query": "cooking", "results": [ ... ]}
  {"page": 2, ... }
  ...

Usage:
  python cse_search.py "cooking hacks"
  python cse_search.py "dance" --engine tiktok --pages 3
  python cse_search.py "guitar" --pages 5 --headless
  python cse_search.py "cars" --url https://vaibhavgeek.github.io/p_cse_search/

Install:
  pip install playwright
  python -m playwright install chromium
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from typing import Any

DEFAULT_URL = "https://vaibhavgeek.github.io/p_cse_search/"

# The tabs in index.html — key must match the data-cx attribute the tab
# button uses in the page. We just click the matching tab by its label.
TAB_LABELS = {
    "instagram": "Instagram Reels",
    "tiktok": "TikTok",
}

# The JS the page evaluates on our behalf to pull each result card's
# fields directly from the CSE-rendered DOM. Mirrors the logic in the
# batch-extract script in index.html so the shape stays consistent.
#
# NOTE: We deliberately don't call the "Extract all as JSON" button —
# that path calls OpenAI. This scrape is free.
SCRAPE_JS = r"""
() => {
  const cards = Array.from(document.querySelectorAll('.gsc-webResult.gsc-result'));
  return cards.map(card => {
    // The title anchor and the URL-bearing anchor can be different elements
    // when a card has a thumbnail: `a.gs-image` comes first in DOM order but
    // has no title text, while `a.gs-title` has the text but sometimes not
    // the resolved URL. Handle each explicitly and prefer whichever anchor
    // has real content.
    const titleAnchor = card.querySelector('a.gs-title');
    const anyAnchor   = card.querySelector('a[data-ctorig], a.gs-title[href], a.gs-image[href]');

    const title = titleAnchor ? (titleAnchor.textContent || '').trim() : '';

    // Google mangles outbound links with click-tracking; data-ctorig holds
    // the true destination when present. Try title first, then any anchor.
    let url = '';
    for (const a of [titleAnchor, anyAnchor]) {
      if (!a) continue;
      url = a.getAttribute('data-ctorig') || a.getAttribute('href') || '';
      if (url) break;
    }

    const displayEl = card.querySelector('.gs-visibleUrl, .gs-visibleUrl-long, .gs-visibleUrl-short');
    const display_link = displayEl ? (displayEl.textContent || '').trim() : '';

    const snippetEl = card.querySelector('.gs-snippet, .gs-bidi-start-align.gs-snippet');
    const snippet = snippetEl ? (snippetEl.textContent || '').replace(/\s+/g, ' ').trim() : null;

    const thumbEl = card.querySelector('img.gs-image, .gsc-thumbnail img');
    const screenshot = (thumbEl && thumbEl.src) ? thumbEl.src : null;

    let platform = null;
    if (/instagram\.com\//.test(url))    platform = 'instagram';
    else if (/tiktok\.com\//.test(url))  platform = 'tiktok';

    return {
      title:        title || null,
      reel_url:     url || null,
      display_link: display_link || null,
      snippet:      snippet || null,
      screenshot,
      platform,
    };
  });
}
"""


def parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="cse_search.py",
        description=(
            "Drive the p_cse_search page in a real browser, paginate, "
            "and stream one JSON object per page to stdout (NDJSON)."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  python cse_search.py 'cooking hacks'\n"
            "  python cse_search.py 'dance' --engine tiktok --pages 3\n"
            "  python cse_search.py 'guitar' --pages 5 --headless\n"
        ),
    )
    p.add_argument("query", help="search query to type into the CSE input")
    p.add_argument(
        "--engine",
        choices=sorted(TAB_LABELS.keys()),
        default="instagram",
        help="which tab to click before searching (default: instagram)",
    )
    p.add_argument(
        "--pages",
        type=int,
        default=1,
        help="how many pages of results to fetch (default: 1)",
    )
    p.add_argument(
        "--url",
        default=DEFAULT_URL,
        help=f"which p_cse_search deployment to hit (default: {DEFAULT_URL})",
    )
    p.add_argument(
        "--headless",
        action="store_true",
        help="run Chromium headless (default: headed, so you can watch it)",
    )
    p.add_argument(
        "--timeout",
        type=int,
        default=20000,
        help="per-action timeout in ms (default: 20000)",
    )
    p.add_argument(
        "--wait-between",
        type=float,
        default=0.5,
        help=(
            "seconds to sleep after each pagination click, on top of the "
            "explicit waits for the new results to render (default: 0.5)"
        ),
    )
    return p.parse_args(argv)


def emit(obj: dict) -> None:
    """Write one NDJSON record and flush immediately so the caller sees
    each page as soon as it's scraped."""
    sys.stdout.write(json.dumps(obj, ensure_ascii=False))
    sys.stdout.write("\n")
    sys.stdout.flush()


def wait_for_results(page, timeout_ms: int) -> int:
    """Wait for at least one CSE result card to appear, return the count."""
    # The results container fills in asynchronously after the CSE loader
    # boots. Wait for the first card, then let the rest of the batch
    # settle (small networkidle-ish tail).
    page.wait_for_selector(".gsc-webResult.gsc-result", timeout=timeout_ms)
    # Small settle so late-loading thumbnails have their src attribute
    # populated by the time we scrape.
    page.wait_for_load_state("networkidle", timeout=timeout_ms)
    return page.locator(".gsc-webResult.gsc-result").count()


def switch_tab(page, engine: str, timeout_ms: int) -> None:
    """Click the tab matching the requested engine, if it's not already active."""
    label = TAB_LABELS[engine]
    tab = page.locator(f'button.tab:has-text("{label}")')
    tab.wait_for(timeout=timeout_ms)
    # Only click if not already active — clicking the active tab is a no-op
    # in the page's tab code, but avoiding it keeps behaviour predictable.
    classes = tab.get_attribute("class") or ""
    if "active" not in classes.split():
        tab.click()


def submit_query(page, query: str, timeout_ms: int) -> None:
    """Type the query into the CSE input and hit Enter."""
    box = page.locator("input.gsc-input").first
    box.wait_for(timeout=timeout_ms)
    box.click()
    # Clear anything the CSE widget may have restored from the URL hash.
    box.fill("")
    box.type(query, delay=20)
    box.press("Enter")


def go_next_page(page, timeout_ms: int) -> bool:
    """Click the next-page cursor in the CSE pager. Return True if we
    advanced, False if there is no next page."""
    # Google's pager renders each page number in a .gsc-cursor-page span,
    # and the currently-selected page carries .gsc-cursor-current-page.
    # The immediately-following sibling — if any — is our "next".
    current = page.locator(".gsc-cursor-page.gsc-cursor-current-page").first
    if current.count() == 0:
        return False
    # Grab the current page number and try to click page N+1.
    try:
        current_num = int((current.text_content() or "").strip())
    except (ValueError, AttributeError):
        return False
    next_label = str(current_num + 1)
    next_link = page.locator(
        f'.gsc-cursor-page:not(.gsc-cursor-current-page):has-text("{next_label}")'
    ).first
    if next_link.count() == 0:
        return False
    next_link.click()
    # Wait for the new page's cards to render. CSE re-renders the whole
    # list in place, so the count changes back to 10 (or the tail count).
    try:
        page.wait_for_function(
            f"""() => {{
              const cur = document.querySelector('.gsc-cursor-page.gsc-cursor-current-page');
              return cur && cur.textContent && cur.textContent.trim() === "{next_label}";
            }}""",
            timeout=timeout_ms,
        )
    except Exception:
        return False
    return True


def run(args: argparse.Namespace) -> int:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        sys.stderr.write(
            "error: playwright is not installed.\n"
            "  pip install playwright\n"
            "  python -m playwright install chromium\n"
        )
        return 2

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=args.headless)
        context = browser.new_context(
            viewport={"width": 1280, "height": 900},
            # A recent user-agent — the CSE widget behaves better than with
            # Playwright's default headless UA string.
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/126.0.0.0 Safari/537.36"
            ),
        )
        page = context.new_page()
        page.set_default_timeout(args.timeout)

        try:
            page.goto(args.url, wait_until="domcontentloaded")

            # 1. Switch to the requested tab (Instagram / TikTok).
            switch_tab(page, args.engine, args.timeout)

            # 2. Submit the query.
            submit_query(page, args.query, args.timeout)

            # 3. Loop pages: wait for cards, scrape, click next, repeat.
            for page_num in range(1, max(1, args.pages) + 1):
                try:
                    count = wait_for_results(page, args.timeout)
                except Exception as e:
                    emit({
                        "page": page_num,
                        "engine": args.engine,
                        "query": args.query,
                        "error": f"no results rendered: {e}",
                        "results": [],
                    })
                    break

                results: list[dict[str, Any]] = page.evaluate(SCRAPE_JS)
                emit({
                    "page": page_num,
                    "engine": args.engine,
                    "query": args.query,
                    "count": len(results),
                    "results": results,
                })

                if page_num >= args.pages:
                    break

                # Advance to the next page. If the pager has no next
                # cursor, we've hit the end.
                if not go_next_page(page, args.timeout):
                    break
                time.sleep(args.wait_between)

        finally:
            context.close()
            browser.close()

    return 0


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    try:
        return run(args)
    except KeyboardInterrupt:
        sys.stderr.write("\ninterrupted.\n")
        return 130


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
