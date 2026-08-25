#!/usr/bin/env python3
"""
cse_search.py — Drive the p_cse_search page through Camoufox (anti-detect
Firefox) and stream one JSON object per result page to stdout as NDJSON.

Why Camoufox and not vanilla Playwright:
  Vanilla Playwright/Chromium fails Tier-1 bot detection immediately
  (`navigator.webdriver === true`, `HeadlessChrome` UA leaks, plugin list
  is empty, WebGL renderer is SwiftShader, TLS ClientHello is
  identifiably Playwright, ...). Camoufox is a patched Firefox fork with
  those tells removed at the browser level — including at the TLS/JA3
  layer, which no in-page script can fix.

On top of Camoufox's built-in patches, this script layers:
  - Bezier-curved mouse trajectories (mouse never teleports).
  - Idle cursor drift between actions.
  - Randomized per-keystroke typing delays, with occasional hesitations
    and rare typo+backspace corrections.
  - Natural scroll: variable dy per wheel event, multi-burst reading.
  - Randomized reading-time delays between paginations.
  - Cookie/storage persistence across runs (--profile), so the second
    query looks like a "returning visitor" to Google's CSE endpoint.
  - Google-block detection (empty results / sorry.google.com / CAPTCHA).
  - `Referer: https://www.google.com/` chain on the first hit.
  - WebRTC blocked to prevent local-IP leaks.

Output shape (NDJSON — one line per page, flushed immediately):
  {"page": 1, "engine": "instagram", "query": "cooking",
   "count": 10,
   "results": [
     {"title": ..., "reel_url": ..., "display_link": ...,
      "snippet": ..., "screenshot": ..., "platform": ...},
     ...
   ]}
  {"page": 2, ...}
  ...

Install:
  pip install 'camoufox[geoip]'
  python -m camoufox fetch   # ~200MB, cached under ~/Library/Caches/camoufox
"""

from __future__ import annotations

import argparse
import json
import math
import os
import random
import sys
import time
from pathlib import Path
from typing import Any


DEFAULT_URL = "https://vaibhavgeek.github.io/p_cse_search/"
DEFAULT_PROFILE = Path.home() / ".cache" / "cse_search" / "profile.json"

# Which tab to click before searching. Keys are the CLI --engine choices;
# values are the exact button labels rendered by index.html.
TAB_LABELS = {
    "instagram": "Instagram Reels",
    "tiktok": "TikTok",
}

# Realistic viewport candidates for macOS Firefox users. Values are the
# INNER viewport (post-chrome height), not the physical screen. Sampled
# uniformly at run start so consecutive runs don't share a fingerprint.
MACOS_VIEWPORTS = [
    (1440, 773),
    (1440, 812),
    (1512, 857),
    (1680, 919),
    (1728, 981),
    (1366, 720),
]

# Result card extraction — mirrors the "Extract all" script in index.html.
# Reads only what's cheap and deterministic from the CSE-rendered DOM.
SCRAPE_JS = r"""
() => {
  const cards = Array.from(document.querySelectorAll('.gsc-webResult.gsc-result'));
  return cards.map(card => {
    // Cards with thumbnails render <a.gs-image> before <a.gs-title>. The
    // image anchor has no title text; the title anchor sometimes lacks
    // data-ctorig. Read each explicitly.
    const titleAnchor = card.querySelector('a.gs-title');
    const anyAnchor   = card.querySelector('a[data-ctorig], a.gs-title[href], a.gs-image[href]');

    const title = titleAnchor ? (titleAnchor.textContent || '').trim() : '';

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


# ---------------------------------------------------------------------------
# Human-like input helpers
# ---------------------------------------------------------------------------

# A tiny fixed keyboard-neighbours map for typo simulation. Only covers the
# letters most commonly typed in search queries; anything not in this map
# just gets skipped when we decide to introduce a typo.
NEIGHBOURS = {
    "a": "sq", "b": "vn", "c": "vx", "d": "sf", "e": "wr", "f": "dg",
    "g": "fh", "h": "gj", "i": "uo", "j": "hk", "k": "jl", "l": "k",
    "m": "n",  "n": "bm", "o": "ip", "p": "o",  "q": "wa", "r": "et",
    "s": "ad", "t": "ry", "u": "yi", "v": "cb", "w": "qe", "x": "zc",
    "y": "tu", "z": "x",
}


def _sleep_jitter(low: float, high: float) -> None:
    """Sleep for a random duration in [low, high]. Kept small and inlined
    everywhere else, but named here for readability at call sites where
    the timing is the point."""
    time.sleep(random.uniform(low, high))


def _cubic_bezier(p0, p1, p2, p3, t):
    """Standard cubic Bezier evaluation at parameter t in [0, 1]."""
    u = 1.0 - t
    x = (u**3) * p0[0] + 3 * (u**2) * t * p1[0] + 3 * u * (t**2) * p2[0] + (t**3) * p3[0]
    y = (u**3) * p0[1] + 3 * (u**2) * t * p1[1] + 3 * u * (t**2) * p2[1] + (t**3) * p3[1]
    return x, y


class Cursor:
    """Tracks a virtual cursor position so we can generate curved paths
    between clicks instead of teleporting. Playwright's `page.mouse` has
    no getter for the current position — we shadow it here."""

    def __init__(self, page, start=(200, 200)):
        self.page = page
        self.x, self.y = start
        # Warm the underlying mouse to our shadow position.
        try:
            page.mouse.move(self.x, self.y)
        except Exception:
            pass

    def move_to(self, target_x: float, target_y: float, steps: int | None = None) -> None:
        """Move the cursor along a cubic-Bezier curve with sub-pixel
        jitter. Control points are perturbed off the straight line so
        the trajectory looks organic — humans overshoot and correct."""
        start = (self.x, self.y)
        end = (target_x, target_y)
        dx, dy = end[0] - start[0], end[1] - start[1]
        distance = math.hypot(dx, dy)
        if distance < 2:
            self.page.mouse.move(end[0], end[1])
            self.x, self.y = end
            return

        # Step count scales with distance, but capped. Fewer steps than
        # before — the previous 15-40 was cinematically slow. A real
        # pointer scan of 400px lands in ~150ms with 8-12 sample points.
        if steps is None:
            steps = max(8, min(20, int(distance / 30)))

        # Off-axis control points: perpendicular to the straight line,
        # magnitude proportional to distance. Sign is random so the arc
        # bends left or right on any given move.
        perp_x, perp_y = -dy / distance, dx / distance
        arc = random.uniform(0.10, 0.28) * distance * random.choice([-1, 1])
        c1 = (
            start[0] + dx * random.uniform(0.15, 0.35) + perp_x * arc,
            start[1] + dy * random.uniform(0.15, 0.35) + perp_y * arc,
        )
        c2 = (
            start[0] + dx * random.uniform(0.55, 0.80) + perp_x * arc * 0.6,
            start[1] + dy * random.uniform(0.55, 0.80) + perp_y * arc * 0.6,
        )

        for i in range(1, steps + 1):
            t = i / steps
            # Ease-in/out — humans accelerate then decelerate.
            t_eased = 0.5 - 0.5 * math.cos(math.pi * t)
            x, y = _cubic_bezier(start, c1, c2, end, t_eased)
            # Sub-pixel jitter on every step.
            x += random.uniform(-0.6, 0.6)
            y += random.uniform(-0.6, 0.6)
            self.page.mouse.move(x, y)
            # 4-10ms per step — fast enough to feel snappy while still
            # showing the curve. Real pointer scan rate is 60-120Hz.
            time.sleep(random.uniform(0.004, 0.010))

        self.x, self.y = end

    def click_element(self, locator, timeout_ms: int) -> None:
        """Move the cursor over the element's bounding box (somewhere
        near the visual centre, but not exact-centre) and click. Uses
        the underlying element's bounding_box, so this works for any
        locator including inputs, buttons, anchors."""
        locator.wait_for(timeout=timeout_ms, state="visible")
        box = locator.bounding_box()
        if not box:
            # Fallback: no bounding box (element off-screen or detached).
            locator.click()
            return
        # Pick a spot inside the element but not the pixel-perfect
        # centre — real users don't hit the geometric centre either.
        target_x = box["x"] + box["width"] * random.uniform(0.30, 0.70)
        target_y = box["y"] + box["height"] * random.uniform(0.30, 0.70)
        self.move_to(target_x, target_y)
        # Small settle before pressing.
        _sleep_jitter(0.02, 0.08)
        self.page.mouse.down()
        _sleep_jitter(0.02, 0.06)  # a real click is not instantaneous
        self.page.mouse.up()

    def idle_drift(self, duration: float) -> None:
        """Nudge the cursor around for the given duration. Simulates
        the small unconscious movements a real user makes while reading."""
        deadline = time.time() + duration
        while time.time() < deadline:
            _sleep_jitter(0.4, 1.4)
            if time.time() >= deadline:
                break
            # Small drift, 20-90px in a random direction.
            angle = random.uniform(0, 2 * math.pi)
            dist = random.uniform(20, 90)
            tx = max(10, self.x + math.cos(angle) * dist)
            ty = max(10, self.y + math.sin(angle) * dist)
            self.move_to(tx, ty, steps=random.randint(6, 14))


def human_type(page, cursor: Cursor, locator, text: str, timeout_ms: int) -> None:
    """Focus the input via a mouse move+click, then type character by
    character with jittery delays and a small chance of typos that are
    corrected with Backspace. Never uses `.fill()` — that instantly
    replaces value and skips input events, which is a strong bot tell."""
    cursor.click_element(locator, timeout_ms)
    # Clear whatever autocomplete-restored garbage might be there.
    # Select-all + Delete via keyboard so the input events look normal.
    page.keyboard.press("Control+A" if sys.platform != "darwin" else "Meta+A")
    _sleep_jitter(0.03, 0.08)
    page.keyboard.press("Delete")
    _sleep_jitter(0.08, 0.20)

    for ch in text:
        # 2% chance of a typo: insert a keyboard-neighbour, tiny pause,
        # Backspace to correct, then continue with the intended char.
        low = ch.lower()
        if random.random() < 0.02 and low in NEIGHBOURS:
            wrong = random.choice(NEIGHBOURS[low])
            # Preserve case of the intended character.
            if ch.isupper():
                wrong = wrong.upper()
            page.keyboard.type(wrong)
            _sleep_jitter(0.06, 0.15)
            page.keyboard.press("Backspace")
            _sleep_jitter(0.04, 0.10)

        page.keyboard.type(ch)

        # Base cadence 25-90ms (fast touch-typist range); occasional
        # 150-350ms hesitation bursts at 5% probability.
        if random.random() < 0.05:
            _sleep_jitter(0.15, 0.35)
        else:
            _sleep_jitter(0.025, 0.090)

    # Brief read-back before submitting.
    _sleep_jitter(0.10, 0.25)
    page.keyboard.press("Enter")


def human_scroll(page, cursor: Cursor) -> None:
    """Trackpad-style scrolling: many small wheel events instead of one
    big one, with occasional back-scrolls and pauses. Leaves the viewport
    near the top afterwards so the pager is visible for the next click."""
    total_bursts = random.randint(1, 2)
    for _ in range(total_bursts):
        # A "burst" is 4-8 small wheel events forming a swipe.
        num_events = random.randint(4, 8)
        for _ in range(num_events):
            # Trackpad-style small dy with occasional acceleration.
            dy = random.choice([5, 8, 12, 20, 40])
            page.mouse.wheel(0, dy)
            time.sleep(random.uniform(0.015, 0.035))
        _sleep_jitter(0.15, 0.4)
        # 20% chance to back-scroll (re-read something).
        if random.random() < 0.20:
            for _ in range(random.randint(2, 4)):
                page.mouse.wheel(0, -random.choice([8, 12, 20]))
                time.sleep(random.uniform(0.015, 0.035))
            _sleep_jitter(0.1, 0.3)

    # Half the time, return to the top so the pager is visible.
    if random.random() < 0.5:
        page.evaluate("window.scrollTo({ top: 0, behavior: 'smooth' })")
        _sleep_jitter(0.2, 0.5)


# ---------------------------------------------------------------------------
# Search flow
# ---------------------------------------------------------------------------

def wait_for_results(page, timeout_ms: int) -> int:
    """Wait for at least one CSE result card to appear, return the count."""
    page.wait_for_selector(".gsc-webResult.gsc-result", timeout=timeout_ms)
    # Late-loading thumbnails need a beat.
    try:
        page.wait_for_load_state("networkidle", timeout=timeout_ms)
    except Exception:
        # networkidle can flake on GH Pages if a background request
        # never fully settles; the card count is our real signal.
        pass
    return page.locator(".gsc-webResult.gsc-result").count()


def detect_google_block(page) -> str | None:
    """Return a short reason string if Google's CSE has served a block
    page instead of results, else None. Called after every scrape.

    Heuristics:
      - Explicit "sorry.google.com" iframe URL.
      - "unusual traffic" or "detected unusual traffic" text.
      - A visible reCAPTCHA challenge.
      - Zero cards AND zero pagination cursor after our wait_for_results
        timed out."""
    try:
        blocked_iframe = page.evaluate(
            "!!document.querySelector('iframe[src*=\"sorry.google.com\"]')"
        )
        if blocked_iframe:
            return "sorry.google.com iframe present"

        body_text = (page.evaluate("document.body && document.body.innerText") or "")
        low = body_text.lower()
        if "unusual traffic" in low or "detected unusual" in low:
            return "'unusual traffic' text detected"
        if "our systems have detected" in low:
            return "'our systems have detected' text"

        recaptcha = page.evaluate(
            "!!document.querySelector('iframe[src*=\"recaptcha\"], .g-recaptcha')"
        )
        if recaptcha:
            return "reCAPTCHA challenge visible"
    except Exception:
        pass
    return None


def switch_tab(page, cursor: Cursor, engine: str, timeout_ms: int) -> None:
    """Click the tab matching the requested engine, unless it's already active."""
    label = TAB_LABELS[engine]
    tab = page.locator(f'button.tab:has-text("{label}")')
    tab.wait_for(timeout=timeout_ms)
    classes = tab.get_attribute("class") or ""
    if "active" not in classes.split():
        cursor.click_element(tab, timeout_ms)


def go_next_page(page, cursor: Cursor, timeout_ms: int) -> bool:
    """Advance to page N+1 via the CSE pager. Return True if we moved.

    Historical bugs this function has stepped on:
      1. `has-text("2")` matches "20", "21", … — never use text substring
         match on numeric labels. Use aria-label="Page 2" for an exact hit.
      2. The Bezier-curve click from `Cursor.click_element` targets a small
         (~24px) pager button; sub-pixel jitter routinely misses. Plain
         Playwright `.click()` centers on the element and reliably lands.
         Stealth stopped mattering the moment we successfully submitted
         the search — the CSE session is now warm.
      3. `wait_for_function` on `.gsc-cursor-current-page` textContent
         needs a fresh scope inside the browser (CSE rebuilds the pager
         DOM on nav), so we snapshot the first result URL and poll until
         it changes AND the current-page label matches — that's the only
         signal that both the pager AND the results grid caught up.
    """
    # 1. Read current page number from the pager.
    current = page.locator(".gsc-cursor-page.gsc-cursor-current-page").first
    if current.count() == 0:
        return False
    try:
        current_num = int((current.text_content() or "").strip())
    except (ValueError, AttributeError):
        return False
    target_num = current_num + 1

    # 2. Find the target page button by aria-label (exact match).
    next_link = page.locator(
        f'.gsc-cursor-page[aria-label="Page {target_num}"]'
    ).first
    if next_link.count() == 0:
        # No more pages available (we hit the CSE 10-page ceiling or the
        # query has fewer results than requested).
        return False

    # 3. Snapshot the first result URL so we can detect grid rebuild.
    first_url_before = page.evaluate(
        """() => {
            const a = document.querySelector('.gsc-webResult.gsc-result a.gs-title');
            return a ? (a.getAttribute('data-ctorig') || a.href || '') : '';
        }"""
    )

    # 4. Scroll into view and click. Plain click — no Bezier — because
    #    (a) the pager buttons are small and (b) we're past the stealth
    #    gate. Any behavioral analysis has already been passed.
    try:
        next_link.scroll_into_view_if_needed(timeout=timeout_ms)
        next_link.click(timeout=timeout_ms)
    except Exception:
        return False

    # 5. Wait for BOTH the pager label AND the results grid to catch up.
    try:
        page.wait_for_function(
            f"""(prevUrl) => {{
                const cur = document.querySelector('.gsc-cursor-current-page');
                const a = document.querySelector('.gsc-webResult.gsc-result a.gs-title');
                if (!cur || !a) return false;
                const label = (cur.textContent || '').trim();
                const url = a.getAttribute('data-ctorig') || a.href || '';
                return label === "{target_num}" && url && url !== prevUrl;
            }}""",
            arg=first_url_before,
            timeout=timeout_ms,
        )
    except Exception:
        return False
    return True


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="cse_search.py",
        description=(
            "Drive the p_cse_search page through Camoufox (anti-detect "
            "Firefox), paginate, and stream one JSON object per page to "
            "stdout as NDJSON."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  python cse_search.py 'cooking hacks'\n"
            "  python cse_search.py 'dance' --engine tiktok --pages 3\n"
            "  python cse_search.py 'guitar' --pages 5 --headless\n"
            "  python cse_search.py 'cars' --proxy http://user:pass@host:port\n"
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
        help="run Camoufox headless (default: headed, so you can watch it)",
    )
    p.add_argument(
        "--timeout",
        type=int,
        default=25000,
        help="per-action timeout in ms (default: 25000)",
    )
    p.add_argument(
        "--fast",
        action="store_true",
        help=(
            "skip inter-page scrolling and reading-time delays. Typing "
            "jitter and mouse curves stay on — those are what beat "
            "detection. Use for CI runs where wall-clock matters."
        ),
    )
    p.add_argument(
        "--profile",
        default=str(DEFAULT_PROFILE),
        help=(
            "path to a JSON file for cookie/storage persistence across "
            f"runs (default: {DEFAULT_PROFILE}). Set to empty string to "
            "disable persistence."
        ),
    )
    p.add_argument(
        "--proxy",
        default=None,
        help=(
            "route the browser through a proxy, e.g. "
            "http://user:pass@host:port. When set, geoip is enabled so "
            "the browser's timezone and locale align with the exit IP."
        ),
    )
    p.add_argument(
        "--os",
        dest="os_spoof",
        choices=["macos", "windows", "linux"],
        default=None,
        help=(
            "OS to spoof in the fingerprint (default: auto-select based "
            "on host). Match this to your proxy's country's typical OS "
            "distribution for extra realism."
        ),
    )
    p.add_argument(
        "--referer",
        default="https://www.google.com/",
        help=(
            "the Referer header to send with the initial navigation "
            "(default: https://www.google.com/). Set to empty string "
            "to send no referer."
        ),
    )
    return p.parse_args(argv)


def emit(obj: dict) -> None:
    """Write one NDJSON record and flush so the caller sees each page
    as soon as it's scraped."""
    sys.stdout.write(json.dumps(obj, ensure_ascii=False))
    sys.stdout.write("\n")
    sys.stdout.flush()


def detect_host_os() -> str:
    if sys.platform == "darwin":
        return "macos"
    if sys.platform.startswith("linux"):
        return "linux"
    if sys.platform == "win32":
        return "windows"
    return "linux"


def run(args: argparse.Namespace) -> int:
    try:
        from camoufox.sync_api import Camoufox
    except ImportError:
        sys.stderr.write(
            "error: camoufox is not installed.\n"
            "  pip install 'camoufox[geoip]'\n"
            "  python -m camoufox fetch\n"
        )
        return 2

    os_spoof = args.os_spoof or detect_host_os()
    viewport = random.choice(MACOS_VIEWPORTS) if os_spoof == "macos" else (1440, 812)

    # Camoufox launch kwargs. `humanize=True` adds baseline mouse curves
    # on top of what our Cursor helper does — belt AND suspenders.
    launch_kwargs: dict[str, Any] = {
        "headless": args.headless,
        "humanize": True,
        "os": os_spoof,
        "locale": "en-US",
        "block_webrtc": True,       # prevent local-IP leak
        "window": viewport,
        "i_know_what_im_doing": True,  # suppress the interactive warning
    }
    if args.proxy:
        launch_kwargs["proxy"] = {"server": args.proxy}
        launch_kwargs["geoip"] = True

    # Cookie/storage persistence: load storage_state from --profile if
    # it exists. Skipping this is the biggest single tell after the JS
    # fingerprint — a "user" with zero Google cookies looks synthetic.
    profile_path: Path | None = None
    storage_state = None
    if args.profile:
        profile_path = Path(args.profile).expanduser()
        if profile_path.is_file():
            try:
                storage_state = json.loads(profile_path.read_text())
            except (OSError, json.JSONDecodeError):
                storage_state = None

    with Camoufox(**launch_kwargs) as browser:
        context = browser.new_context(
            storage_state=storage_state,
            # `viewport=None` lets Camoufox derive it from `window` so
            # inner/outer dimensions stay coherent — mismatching these
            # is a known fingerprint tell.
            viewport=None,
        )
        page = context.new_page()
        page.set_default_timeout(args.timeout)

        cursor = Cursor(page, start=(random.randint(80, 400), random.randint(80, 400)))

        try:
            # Referer chain: pretend the user came from Google. Only
            # bother if the profile is cold (no prior cookies) — a
            # returning visitor doesn't need this ceremony every time.
            if args.referer and not storage_state:
                try:
                    page.goto(args.referer, wait_until="domcontentloaded")
                    _sleep_jitter(0.3, 0.7)
                except Exception:
                    # Referer prewarm is best-effort — never a fatal error.
                    pass

            page.goto(args.url, wait_until="domcontentloaded")
            # Let the CSE loader boot; a cold render of the widget takes
            # about a second even on a fast connection.
            _sleep_jitter(0.3, 0.7)

            switch_tab(page, cursor, args.engine, args.timeout)

            # Type + submit the query.
            input_box = page.locator("input.gsc-input").first
            human_type(page, cursor, input_box, args.query, args.timeout)

            # Paginate + scrape.
            for page_num in range(1, max(1, args.pages) + 1):
                try:
                    wait_for_results(page, args.timeout)
                except Exception as e:
                    reason = detect_google_block(page) or f"no results rendered: {e}"
                    emit({
                        "page": page_num,
                        "engine": args.engine,
                        "query": args.query,
                        "error": reason,
                        "results": [],
                    })
                    break

                blocked = detect_google_block(page)
                if blocked:
                    emit({
                        "page": page_num,
                        "engine": args.engine,
                        "query": args.query,
                        "error": f"google_blocked: {blocked}",
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

                if not args.fast:
                    human_scroll(page, cursor)
                    # Reading-time delay: 0.6-1.8s normally, 3-6s in 8%
                    # of cases (the "user got distracted" tail).
                    if random.random() < 0.08:
                        _sleep_jitter(3.0, 6.0)
                    else:
                        _sleep_jitter(0.6, 1.8)

                if not go_next_page(page, cursor, args.timeout):
                    break

            # Persist cookies so the next run is warm.
            if profile_path is not None:
                try:
                    profile_path.parent.mkdir(parents=True, exist_ok=True)
                    state = context.storage_state()
                    profile_path.write_text(json.dumps(state))
                except OSError as e:
                    sys.stderr.write(f"warn: could not save profile: {e}\n")

        finally:
            context.close()

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
