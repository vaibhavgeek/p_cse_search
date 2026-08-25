#!/usr/bin/env python3
"""
cse_search.py — Query Google Programmable Search Engines and emit JSON.

Uses the official Custom Search JSON API:
  https://developers.google.com/custom-search/v1/using_rest

Authentication (in priority order — override with --auth):
  1. OAuth 2.0 user credentials via Application Default Credentials (ADC).
     The stored refresh token must have the `cse` scope. Set up once with:
       gcloud auth application-default login \\
         --scopes=openid,email,\\
https://www.googleapis.com/auth/cloud-platform,\\
https://www.googleapis.com/auth/cse
  2. Service account JSON pointed to by GOOGLE_APPLICATION_CREDENTIALS
     (or auto-discovered: any *.json in cwd whose 'type' == 'service_account').
     Exchanges the JWT for an OAuth2 access token with the `cse` scope.
  3. API key from GOOGLE_CSE_API_KEY (env or .env).

Enable the API at:
  https://console.cloud.google.com/apis/library/customsearch.googleapis.com

Example:
  python cse_search.py -q "cooking hacks" -e instagram --sort date --pretty
  python cse_search.py -q "dance" -e tiktok -n 10 --pages 2 --pretty
  python cse_search.py -q "guitar" --auth oauth --pretty
"""

from __future__ import annotations

import argparse
import base64
import glob
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

API_URL = "https://www.googleapis.com/customsearch/v1"
TOKEN_URL = "https://oauth2.googleapis.com/token"
SCOPE = "https://www.googleapis.com/auth/cse"


def load_dotenv(path: str = ".env") -> None:
    """Minimal .env loader: KEY=VALUE per line, # comments, no quoting tricks.
    Does not overwrite variables already set in the environment.
    """
    if not os.path.isfile(path):
        return
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, val = line.partition("=")
                key = key.strip()
                val = val.strip().strip('"').strip("'")
                if key and key not in os.environ:
                    os.environ[key] = val
    except OSError:
        pass


load_dotenv()

# Engines mirror the tabs in index.html. Env vars override the defaults.
ENGINES = {
    "instagram": os.environ.get("CSE_CX_INSTAGRAM", "004d8c32d5f194762"),
    "tiktok": os.environ.get("CSE_CX_TIKTOK", "648a2fb9e56034558"),
}

SORT_MAP = {
    "relevance": None,
    "date": "date",
    "date-asc": "date:a:s",
}


# ---------------------------------------------------------------------------
# OAuth 2.0 (ADC — user credentials)
# ---------------------------------------------------------------------------

def _adc_paths() -> list[str]:
    """Standard locations gcloud writes application default credentials to."""
    candidates: list[str] = []
    env = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")
    if env:
        candidates.append(env)
    if sys.platform == "win32":
        appdata = os.environ.get("APPDATA")
        if appdata:
            candidates.append(
                os.path.join(appdata, "gcloud", "application_default_credentials.json")
            )
    else:
        home = os.path.expanduser("~")
        candidates.append(
            os.path.join(home, ".config", "gcloud", "application_default_credentials.json")
        )
    return candidates


def _find_adc_user_file() -> str | None:
    """Return the path to an ADC JSON of type 'authorized_user', if present."""
    for path in _adc_paths():
        if not os.path.isfile(path):
            continue
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, json.JSONDecodeError):
            continue
        if data.get("type") == "authorized_user" and data.get("refresh_token"):
            return path
    return None


def get_oauth_access_token(adc_path: str) -> tuple[str, list[str], str | None]:
    """Refresh ADC user credentials. Returns (access_token, scopes, quota_project)."""
    with open(adc_path, "r", encoding="utf-8") as f:
        adc = json.load(f)

    body = urllib.parse.urlencode(
        {
            "client_id": adc["client_id"],
            "client_secret": adc["client_secret"],
            "refresh_token": adc["refresh_token"],
            "grant_type": "refresh_token",
        }
    ).encode("ascii")
    req = urllib.request.Request(
        TOKEN_URL,
        data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body_text = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(
            f"OAuth refresh failed ({e.code}): {body_text}"
        ) from e

    token = data["access_token"]
    scope_str = data.get("scope", "")
    scopes = scope_str.split() if scope_str else []
    quota_project = adc.get("quota_project_id")
    return token, scopes, quota_project


# ---------------------------------------------------------------------------
# Service account auth (JWT bearer -> OAuth2 access token)
# ---------------------------------------------------------------------------

def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _find_service_account_file() -> str | None:
    """Explicit env var wins; otherwise scan cwd for a service-account JSON."""
    env_path = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")
    if env_path and os.path.isfile(env_path):
        return env_path
    for path in sorted(glob.glob("*.json")):
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict) and data.get("type") == "service_account":
                return path
        except (OSError, json.JSONDecodeError):
            continue
    return None


def _sign_rs256(private_key_pem: str, message: bytes) -> bytes:
    """RS256 sign using cryptography. Raises ImportError if not installed."""
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding

    key = serialization.load_pem_private_key(
        private_key_pem.encode("utf-8"), password=None
    )
    return key.sign(message, padding.PKCS1v15(), hashes.SHA256())


_token_cache: dict = {"access_token": None, "exp": 0}


def get_access_token(sa_path: str) -> str:
    """Return a cached or fresh OAuth2 access token for the service account."""
    now = int(time.time())
    if _token_cache["access_token"] and _token_cache["exp"] - 60 > now:
        return _token_cache["access_token"]

    with open(sa_path, "r", encoding="utf-8") as f:
        sa = json.load(f)

    header = {"alg": "RS256", "typ": "JWT", "kid": sa.get("private_key_id")}
    claims = {
        "iss": sa["client_email"],
        "scope": SCOPE,
        "aud": TOKEN_URL,
        "iat": now,
        "exp": now + 3600,
    }
    signing_input = (
        _b64url(json.dumps(header, separators=(",", ":")).encode("utf-8"))
        + "."
        + _b64url(json.dumps(claims, separators=(",", ":")).encode("utf-8"))
    ).encode("ascii")
    signature = _sign_rs256(sa["private_key"], signing_input)
    assertion = signing_input.decode("ascii") + "." + _b64url(signature)

    body = urllib.parse.urlencode(
        {
            "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
            "assertion": assertion,
        }
    ).encode("ascii")
    req = urllib.request.Request(
        TOKEN_URL,
        data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body_text = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"token exchange failed ({e.code}): {body_text}") from e

    _token_cache["access_token"] = data["access_token"]
    _token_cache["exp"] = now + int(data.get("expires_in", 3600))
    return _token_cache["access_token"]


# ---------------------------------------------------------------------------
# API call
# ---------------------------------------------------------------------------

def build_params(
    api_key: str | None,
    cx: str,
    query: str,
    num: int,
    start: int,
    sort: str,
    site: str | None,
    safe: str,
    lang: str | None,
) -> dict:
    params = {
        "cx": cx,
        "q": query,
        "num": max(1, min(10, num)),
        "start": max(1, start),
        "safe": safe,
    }
    if api_key:
        params["key"] = api_key
    sort_val = SORT_MAP.get(sort)
    if sort_val:
        params["sort"] = sort_val
    if site:
        params["siteSearch"] = site
        params["siteSearchFilter"] = "i"
    if lang:
        params["lr"] = lang
    return params


def call_api(
    params: dict,
    access_token: str | None = None,
    quota_project: str | None = None,
    retries: int = 1,
) -> dict:
    url = API_URL + "?" + urllib.parse.urlencode(params)
    headers = {"Accept": "application/json"}
    if access_token:
        headers["Authorization"] = f"Bearer {access_token}"
    if quota_project:
        headers["X-Goog-User-Project"] = quota_project
    last_err: Exception | None = None
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", errors="replace")
            try:
                parsed = json.loads(body)
            except json.JSONDecodeError:
                parsed = {"raw": body}
            if 500 <= e.code < 600 and attempt < retries:
                time.sleep(1.5 * (attempt + 1))
                last_err = e
                continue
            return {
                "error": {
                    "http_status": e.code,
                    "message": str(e),
                    "response": parsed,
                }
            }
        except urllib.error.URLError as e:
            if attempt < retries:
                time.sleep(1.5 * (attempt + 1))
                last_err = e
                continue
            return {"error": {"message": f"network error: {e}"}}
    return {"error": {"message": f"exhausted retries: {last_err}"}}


# ---------------------------------------------------------------------------
# Response shaping
# ---------------------------------------------------------------------------

def trim_item(raw: dict) -> dict:
    pagemap = raw.get("pagemap", {}) or {}
    thumb = None
    image = None
    if isinstance(pagemap.get("cse_thumbnail"), list) and pagemap["cse_thumbnail"]:
        thumb = pagemap["cse_thumbnail"][0].get("src")
    if isinstance(pagemap.get("cse_image"), list) and pagemap["cse_image"]:
        image = pagemap["cse_image"][0].get("src")
    return {
        "title": raw.get("title"),
        "link": raw.get("link"),
        "display_link": raw.get("displayLink"),
        "snippet": raw.get("snippet"),
        "thumbnail": thumb,
        "image": image,
        "mime": raw.get("mime"),
        "file_format": raw.get("fileFormat"),
    }


def trim_response(
    responses: list[dict],
    query: str,
    engine: str | None,
    cx: str,
    sort: str,
) -> dict:
    all_items: list[dict] = []
    total_results = None
    search_time = 0.0
    next_page_start = None

    for r in responses:
        if "error" in r:
            return {
                "query": query,
                "engine": engine,
                "cx": cx,
                "sort": sort,
                "error": r["error"],
            }
        items = r.get("items", []) or []
        all_items.extend(trim_item(it) for it in items)
        info = r.get("searchInformation", {}) or {}
        total_results = info.get("totalResults", total_results)
        try:
            search_time += float(info.get("searchTime", 0) or 0)
        except (TypeError, ValueError):
            pass
        queries = r.get("queries", {}) or {}
        np = queries.get("nextPage")
        if isinstance(np, list) and np:
            next_page_start = np[0].get("startIndex")
        else:
            next_page_start = None

    return {
        "query": query,
        "engine": engine,
        "cx": cx,
        "sort": sort,
        "total_results": total_results,
        "search_time_seconds": round(search_time, 4),
        "returned": len(all_items),
        "items": all_items,
        "next_page_start": next_page_start,
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="cse_search.py",
        description="Query a Google Programmable Search Engine and print JSON.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Auth (in priority order):\n"
            "  1. Service account JSON — GOOGLE_APPLICATION_CREDENTIALS or auto-discovered\n"
            "     from any *.json in the current directory.\n"
            "  2. API key — GOOGLE_CSE_API_KEY env var (or .env).\n\n"
            "Environment:\n"
            "  GOOGLE_APPLICATION_CREDENTIALS  path to service account JSON\n"
            "  GOOGLE_CSE_API_KEY              API key alternative\n"
            "  CSE_CX_INSTAGRAM / CSE_CX_TIKTOK  override built-in engine cx values\n"
        ),
    )
    p.add_argument("-q", "--query", required=True, help="search query")
    p.add_argument(
        "-e",
        "--engine",
        choices=sorted(ENGINES.keys()),
        default="instagram",
        help="which preconfigured engine to use (default: instagram)",
    )
    p.add_argument("--cx", help="override with a raw CSE id")
    p.add_argument(
        "--sort",
        choices=sorted(SORT_MAP.keys()),
        default="relevance",
        help="sort order (default: relevance)",
    )
    p.add_argument(
        "-n", "--num", type=int, default=10, help="results per page 1-10 (default: 10)"
    )
    p.add_argument(
        "--start", type=int, default=1, help="1-based start index (default: 1)"
    )
    p.add_argument(
        "--pages",
        type=int,
        default=1,
        help="auto-paginate this many pages (each <=10 results, default: 1)",
    )
    p.add_argument("--site", help="siteSearch restriction, e.g. instagram.com/reel")
    p.add_argument(
        "--safe", choices=["off", "active"], default="off", help="SafeSearch (default: off)"
    )
    p.add_argument("--lang", help="restrict language, e.g. lang_en")
    p.add_argument("-o", "--output", help="write JSON to this file instead of stdout")
    p.add_argument("--pretty", action="store_true", help="pretty-print JSON")
    p.add_argument(
        "--raw",
        action="store_true",
        help="emit the raw API response(s) instead of the trimmed shape",
    )
    p.add_argument(
        "--auth",
        choices=["auto", "oauth", "service-account", "api-key"],
        default="auto",
        help=(
            "force an auth method (default: auto — OAuth ADC if available with "
            "cse scope, else service account, else API key)"
        ),
    )
    return p.parse_args(argv)


def main(argv: list[str]) -> int:
    args = parse_args(argv)

    # Resolve auth.
    sa_path: str | None = None
    adc_path: str | None = None
    api_key: str | None = None
    access_token: str | None = None
    auth_method: str | None = None
    quota_project: str | None = None

    # --- OAuth (ADC user credentials) ---
    if args.auth in ("auto", "oauth"):
        adc_path = _find_adc_user_file()
    if args.auth == "oauth" and not adc_path:
        sys.stderr.write(
            "error: --auth oauth requested but no ADC user credentials found.\n"
            "Run: gcloud auth application-default login "
            "--scopes=openid,email,"
            "https://www.googleapis.com/auth/cloud-platform,"
            "https://www.googleapis.com/auth/cse\n"
        )
        return 2

    if adc_path:
        try:
            token, scopes, qp = get_oauth_access_token(adc_path)
        except Exception as e:
            if args.auth == "oauth":
                sys.stderr.write(f"error: OAuth refresh failed: {e}\n")
                return 2
            token, scopes, qp = None, [], None  # fall through
        else:
            has_cse = any(
                s in scopes
                for s in (
                    "https://www.googleapis.com/auth/cse",
                    "https://www.googleapis.com/auth/cse.readonly",
                )
            )
            if has_cse or args.auth == "oauth":
                access_token = token
                auth_method = "oauth"
                quota_project = qp
            else:
                access_token = None

    # --- Service account ---
    if not access_token and args.auth in ("auto", "service-account"):
        sa_path = _find_service_account_file()
        if args.auth == "service-account" and not sa_path:
            sys.stderr.write(
                "error: --auth service-account requested but no service "
                "account JSON found (set GOOGLE_APPLICATION_CREDENTIALS or "
                "place *.json in cwd).\n"
            )
            return 2
        if sa_path:
            try:
                access_token = get_access_token(sa_path)
                auth_method = "service-account"
            except Exception as e:
                sys.stderr.write(f"error: service account auth failed: {e}\n")
                return 2

    # --- API key ---
    if not access_token and args.auth in ("auto", "api-key"):
        api_key = os.environ.get("GOOGLE_CSE_API_KEY")
        if api_key:
            auth_method = "api-key"

    if not access_token and not api_key:
        sys.stderr.write(
            "error: no credentials found.\n"
            "  OAuth:   gcloud auth application-default login "
            "--scopes=openid,email,"
            "https://www.googleapis.com/auth/cloud-platform,"
            "https://www.googleapis.com/auth/cse\n"
            "  Service account:  set GOOGLE_APPLICATION_CREDENTIALS to a JSON file\n"
            "  API key:          set GOOGLE_CSE_API_KEY\n"
        )
        return 2

    cx = args.cx or ENGINES.get(args.engine)
    if not cx:
        sys.stderr.write(f"error: no cx resolved for engine {args.engine!r}\n")
        return 2

    responses: list[dict] = []
    start = args.start
    pages_requested = max(1, args.pages)

    for _ in range(pages_requested):
        params = build_params(
            api_key=api_key,
            cx=cx,
            query=args.query,
            num=args.num,
            start=start,
            sort=args.sort,
            site=args.site,
            safe=args.safe,
            lang=args.lang,
        )
        resp = call_api(params, access_token=access_token, quota_project=quota_project)
        responses.append(resp)

        if "error" in resp:
            break

        queries = resp.get("queries", {}) or {}
        np = queries.get("nextPage")
        if not (isinstance(np, list) and np):
            break
        start = np[0].get("startIndex")
        if not start:
            break

    if args.raw:
        output: dict | list = {
            "query": args.query,
            "engine": args.engine,
            "cx": cx,
            "sort": args.sort,
            "auth": auth_method,
            "responses": responses,
        }
    else:
        output = trim_response(
            responses=responses,
            query=args.query,
            engine=args.engine,
            cx=cx,
            sort=args.sort,
        )
        if isinstance(output, dict):
            output["auth"] = auth_method

    dump_kwargs: dict = {"ensure_ascii": False}
    if args.pretty:
        dump_kwargs["indent"] = 2

    text = json.dumps(output, **dump_kwargs)

    if args.output:
        with open(args.output, "w", encoding="utf-8") as f:
            f.write(text)
            f.write("\n")
    else:
        sys.stdout.write(text)
        sys.stdout.write("\n")

    if isinstance(output, dict) and "error" in output:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

