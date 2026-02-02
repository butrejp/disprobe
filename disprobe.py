import os
import sys
from pathlib import Path
import re
import csv
import json
import asyncio
from colorama import init, Fore
from playwright.async_api import async_playwright

init(autoreset=True)
 
# Use a portable browser path when frozen
if getattr(sys, "frozen", False):
    exe_dir = Path(sys.executable).parent
    pw_browsers = exe_dir / "playwright-browsers"
    os.environ["PLAYWRIGHT_BROWSERS_PATH"] = str(pw_browsers)

# --------------------
# Resolve base directory
# --------------------
if getattr(sys, "frozen", False):
    base_dir = Path(sys.executable).resolve().parent
else:
    base_dir = Path(__file__).resolve().parent

# Defaults that other code expects
sleep_time_ms = 500
max_parallel_tabs = 8
config_file = base_dir / "distros.txt"
csv_output = None
json_output = None
 
# Timeouts and retries for page loads (milliseconds)
# Accept either *_MS or legacy env names for convenience.
timeout_ms = 15000
retries = 2
retry_delay_ms = 1000
rss_concurrency = 8

# RSS performance tuning
rss_jitter_min = 0.05
rss_jitter_max = 0.25

# Filters and flags defaults
filter_updates = False
filter_ahead = False
filter_unknown = False
no_pause = False
no_browser = False
debug = False
debug_file = None
urls_only = False
version = "0.0.1"

USAGE = """Usage: disprobe [options]

Options:
  -s<ms>                  Sleep time between page loads (ms)
  -p<tabs>                Max parallel tabs
  --file <path>           Config file (default: distros.txt)
  --csv <path>            Write CSV output to path
  --json <path>           Write JSON output to path
  --timeout <ms>          Page load timeout in milliseconds
  --retry-delay <ms>      Initial retry delay in milliseconds (default: 1000)
  --retries <n>           Number of retries for page navigation (default: 2)
  --rss-concurrency <n>   Max concurrent RSS fetches (default: 8)
  --no-browser            Skip browser fallback; return UNKNOWN for non-RSS distros
  --only-updates          Show only distros with updates available
  --only-ahead            Show only distros where local version is ahead
  --only-unknown          Show only unknown/failed distros
  --no-pause              Do not prompt before exit
  --urls                  Print only collected Distrowatch URLs (one per line)
  --debug                 Enable debug logging
  --debug-file <path>     Append debug JSON lines to file.  Requires --debug.
  --version               Show program version and exit
  -h, --help              Show this help message and exit
"""

if "-h" in sys.argv or "--help" in sys.argv:
    print(USAGE)
    sys.exit(0)

args = sys.argv[1:]
i = 0
while i < len(args):
    arg = args[i]
    if arg.startswith("-s") and len(arg) > 2:
        sleep_time_ms = int(arg[2:])
    elif arg.startswith("-p") and len(arg) > 2:
        max_parallel_tabs = int(arg[2:])
    elif arg == "--file" and i + 1 < len(args):
        config_file = Path(args[i + 1]).expanduser().resolve()
        i += 1
    elif arg == "--csv" and i + 1 < len(args):
        csv_output = Path(args[i + 1]).expanduser().resolve()
        i += 1
    elif arg == "--json" and i + 1 < len(args):
        json_output = Path(args[i + 1]).expanduser().resolve()
        i += 1
    elif arg == "--timeout" and i + 1 < len(args):
        # CLI expects milliseconds
        timeout_ms = int(args[i + 1])
        i += 1
    elif arg == "--retries" and i + 1 < len(args):
        retries = int(args[i + 1])
        i += 1
    elif arg == "--retry-delay" and i + 1 < len(args):
        # CLI expects milliseconds
        retry_delay_ms = int(args[i + 1])
        i += 1
    elif arg == "--rss-concurrency" and i + 1 < len(args):
        rss_concurrency = int(args[i + 1])
        i += 1
    elif arg == "--no-browser":
        no_browser = True
    elif arg == "--only-updates":
        filter_updates = True
    elif arg == "--only-ahead":
        filter_ahead = True
    elif arg == "--only-unknown":
        filter_unknown = True
    elif arg == "--no-pause":
        no_pause = True
    elif arg == "--debug":
        debug = True
    elif arg == "--debug-file" and i + 1 < len(args):
        debug_file = args[i + 1]
        i += 1
    elif arg == "--urls":
        urls_only = True
    elif arg == "--version":
        print(version)
        sys.exit(0)
    i += 1

# --------------------
# Load config
# --------------------
local_versions = {}
overrides = {}
if not config_file.exists():
    print(f"[ERROR] Config file not found: {config_file}")
    sys.exit(4)

with open(config_file, "r", encoding="utf-8") as f:
    for lineno, line in enumerate(f, start=1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            print(f"[WARN] Line {lineno} ignored, missing '=': {line}")
            continue
        if line.count("=") > 1:
            print(f"[WARN] Line {lineno} has multiple '=' signs; ignoring: {line}")
            continue

        distro, version = line.split("=", 1)
        distro = distro.strip()
        version = version.strip()

        # support per-distro overrides using semicolon-delimited metadata
        # e.g. fedora=38;source=url;url=https://example.org/releases;regex=Release:\\s.*?(\d+)
        if ";" in version:
            base, meta = version.split(";", 1)
            version = base.strip()
            meta_map = {}
            for part in meta.split(";"):
                if not part:
                    continue
                if "=" in part:
                    k, v = part.split("=", 1)
                    meta_map[k.strip().lower()] = v.strip()
            if meta_map:
                overrides[distro] = meta_map

        if not distro or not version:
            print(f"[WARN] Line {lineno} ignored, empty distro or version: {line}")
            continue
        if not re.search(r"\d", version):
            print(f"[WARN] Line {lineno} ignored, version has no digits: {line}")
            continue

        local_versions[distro] = version

if not local_versions:
    print("[ERROR] No valid distros found in config file")
    sys.exit(4)

DW_URL = "https://distrowatch.com/table.php?distribution={}"

# --------------------
# Helpers
# --------------------
def version_tuple(v):
    return tuple(int(x) for x in re.findall(r"\d+", v))
 
def color(status):
    return {
        "UP TO DATE": Fore.CYAN,
        "UPDATE AVAILABLE": Fore.LIGHTYELLOW_EX,
        "LOCAL AHEAD": Fore.LIGHTMAGENTA_EX,
        "UNKNOWN": Fore.WHITE,
    }[status] + f"[{status}]"

def passes_filter(status):
    if filter_updates and status != "UPDATE AVAILABLE":
        return False
    if filter_ahead and status != "LOCAL AHEAD":
        return False
    if filter_unknown and status != "UNKNOWN":
        return False
    return True


def parse_rss_text(text: str) -> tuple[str, str] | None:
    if not text:
        return None
    # Look for item/entry blocks first
    items = re.findall(r"<item[\s\S]*?</item>", text, re.I)
    if not items:
        items = re.findall(r"<entry[\s\S]*?</entry>", text, re.I)
    if items:
        for item in items:
            # title-based detection
            mtitle = re.search(r"<title[^>]*>([^<]+)</title>", item, re.I)
            if mtitle:
                t = mtitle.group(1)
                mver = re.search(r"\d+(?:\.\d+)*", t)
                if mver:
                    latest = mver.group(0)
                    lnk = re.search(r"<link[^>]*>(.*?)</link>", item, re.I)
                    link_from_feed = lnk.group(1).strip() if lnk else ""
                    return latest, link_from_feed
    # No item matches: try loose title tags anywhere
    titles = re.findall(r"<title[^>]*>(.*?)</title>", text, re.I)
    for tval in titles:
        tplain = re.sub(r"<[^>]+>", "", tval).strip()
        m = re.search(r"([0-9]+(?:\.[0-9]+)+(?:[-.][A-Za-z0-9]+)?)", tplain)
        if m:
            return m.group(1), ""
    # Site-wide Distribution Release pattern
    m = re.search(r"Distribution Release:\s*[^\n<]*?(\d+(?:\.\d+)*)", text, re.I)
    if m:
        return m.group(1), ""
    return None


def debug_log(event: str, **data) -> None:
    """Emit structured debug as JSON lines to stderr or a debug file when enabled.

    Fields: ts (unix time), event (string), plus provided data.
    """
    if not globals().get("debug"):
        return
    try:
        import time, json, sys

        payload = {"ts": time.time(), "event": event}
        payload.update(data)
        line = json.dumps(payload, default=str)
        df = globals().get("debug_file")
        if df:
            try:
                with open(df, "a", encoding="utf-8") as f:
                    f.write(line + "\n")
            except Exception:
                sys.stderr.write(line + "\n")
        else:
            sys.stderr.write(line + "\n")
    except Exception:
        # Best-effort: avoid raising from debug logging
        try:
            import sys

            sys.stderr.write(f"{{\"ts\":\"{time.time()}\",\"event\":\"{event}\"}}\n")
        except Exception:
            pass

# --------------------
# Best-effort progress bar for long fetch operations
async def _progress_bar(tasks: list, prefix: str = "Progress", width: int = 40, interval: float = 0.12) -> None:
    """Render a simple text progress bar for the given asyncio tasks list.

    The bar updates until all tasks are done. Writes to stderr so it doesn't
    interfere with regular program output which is printed later.
    """
    try:
        total = len(tasks)
        if total == 0:
            return
        import sys as _sys, math

        while True:
            completed = sum(1 for t in tasks if t.done())
            frac = completed / total if total else 1.0
            filled = int(math.floor(frac * width))
            bar = ("#" * filled) + ("-" * (width - filled))
            line = f"{prefix}: [{bar}] {completed}/{total}"
            try:
                _sys.stderr.write("\r" + line)
                _sys.stderr.flush()
            except Exception:
                pass
            if completed >= total:
                try:
                    _sys.stderr.write("\n")
                    _sys.stderr.flush()
                except Exception:
                    pass
                break
            try:
                await asyncio.sleep(interval)
            except Exception:
                break
    except Exception:
        # progress bar is best-effort; don't raise on failure
        return

# --------------------
# Fetch one distro
# --------------------
async def fetch(browser, distro, local_version, sem):
    async with sem:
        import time, traceback
        page = await browser.new_page()
        # Block images, stylesheets, fonts and media to speed up page loads
        try:
            async def _route_cb(route, request):
                rtype = request.resource_type
                if rtype in ("image", "stylesheet", "font", "media"):
                    try:
                        await route.abort()
                    except Exception:
                        pass
                else:
                    try:
                        await route.continue_()
                    except Exception:
                        pass

            await page.route("**/*", _route_cb)
        except Exception:
            # routing may not be available in some environments; continue without it
            pass
        # Determine target URL/source for this distro (allow per-distro overrides)
        override = overrides.get(distro, {})
        source = override.get("source", "distrowatch").lower()
        if source == "url":
            target_url = override.get("url") or override.get("uri")
        elif source == "rss":
            target_url = override.get("feed") or override.get("url")
        else:
            target_url = DW_URL.format(distro.lower())
        url = target_url

        # Try RSS-first for distrowatch or explicit rss sources using aiohttp (async)
        latest = None
        link_from_feed = ""
        # Prefer RSS when available: use a shared aiohttp session and semaphore
        latest = None
        link_from_feed = ""
        feed_url = None
        if source == "rss":
            feed_url = override.get("feed") or override.get("url")
        elif source == "distrowatch":
            feed_url = f"https://distrowatch.com/news/distro/{distro.lower()}.xml"

        session = globals().get("rss_session")
        rss_sem = globals().get("rss_sem")
        if feed_url and session:
            try:
                debug_log("rss_fetch", distro=distro, feed=feed_url)
                sem = rss_sem or asyncio.Semaphore(1)
                async with sem:
                    # Attempt a single lightweight httpx fetch and parse
                    text = None
                    status = None
                    headers = {}
                    try:
                        async with session.stream("GET", feed_url) as resp:
                            status = resp.status_code
                            try:
                                headers = dict(resp.headers)
                            except Exception:
                                headers = {}
                            debug_log("rss_http_status", distro=distro, status=status, url=feed_url, headers=headers)
                            if status == 200:
                                try:
                                    b = await resp.aread()
                                    try:
                                        text = b.decode()
                                    except Exception:
                                        text = b.decode(errors="ignore")
                                except Exception:
                                    text = None
                    except Exception as e:
                        debug_log("rss_http_exception", distro=distro, exc=str(e))

                    content_type = headers.get("Content-Type", "")
                    need_browser_fallback = False
                    if status != 200 or not text:
                        need_browser_fallback = True
                    else:
                        if content_type and "html" in content_type.lower() and not re.search(r"<(?:rss|feed|entry|item)[\s>]", text, re.I):
                            need_browser_fallback = True

                    if need_browser_fallback:
                        debug_log("rss_http_error", distro=distro, status=status, headers=headers)
                        # Prefer any existing playwright_rss_browser, otherwise use the browser passed to `fetch`
                        br = globals().get("playwright_rss_browser") or browser
                        if not br:
                            # No dedicated RSS browser available; skip Playwright fallback here
                            debug_log("rss_playwright_unavailable", distro=distro)

                        if br:
                            try:
                                page = await br.new_page()
                                await page.goto(feed_url, timeout=timeout_ms, wait_until="domcontentloaded")
                                await asyncio.sleep(0.12)
                                page_text = await page.content()
                                await page.close()
                                debug_log("rss_raw_snippet", distro=distro, snippet=(page_text[:20000] if page_text else ""), content_type=content_type, content_length=len(page_text) if page_text else None)
                                m = re.search(r"<div[^>]+id=[\"']webkit-xml-viewer-source-xml[\"'][^>]*>(.*?)</div>", page_text, re.S | re.I)
                                if m:
                                    text = m.group(1)
                                else:
                                    m2 = re.search(r"<(?:rss|feed)[\s>].*", page_text, re.S | re.I)
                                    if m2:
                                        text = m2.group(0)
                                    else:
                                        text = page_text
                                content_type = "application/xml"
                            except Exception as e:
                                debug_log("playwright_rss_fetch_error", distro=distro, error=str(e), exc=traceback.format_exc())
                    else:
                        debug_log("rss_raw_snippet", distro=distro, snippet=(text[:20000] if text else ""), content_type=content_type, content_length=len(text) if text else None)

                    if text:
                        parsed = parse_rss_text(text)
                        if parsed:
                            latest, link_from_feed = parsed
                            debug_log("rss_prefetch_match", distro=distro, latest=latest, method="rss_item_title")
                        else:
                            debug_log("rss_no_items", distro=distro)
            except Exception as e:
                debug_log("rss_error", distro=distro, feed=feed_url, exc=str(e))

        # If RSS found a latest, return early without loading the page
        if latest:
            await page.close()
            ver_tuple = version_tuple(local_version)
            try:
                latest_tuple = version_tuple(latest)
            except Exception:
                latest_tuple = ()
            if ver_tuple == latest_tuple:
                st = "UP TO DATE"
            elif ver_tuple > latest_tuple:
                st = "LOCAL AHEAD"
            else:
                st = "UPDATE AVAILABLE"
            return distro, local_version, latest, st, (link_from_feed or url), "rss"

        attempts = retries + 1
        html = None
        for attempt in range(1, attempts + 1):
            start = time.monotonic()
            try:
                # Playwright timeout is already in milliseconds
                await page.goto(url, timeout=timeout_ms, wait_until="domcontentloaded")
                await page.wait_for_timeout(sleep_time_ms)
                html = await page.content()
                elapsed = time.monotonic() - start
                debug_log(
                    "page_load",
                    distro=distro,
                    attempt=attempt,
                    elapsed=elapsed,
                    html_len=len(html),
                    url=url,
                    timeout_ms=timeout_ms,
                )
                break
            except Exception:
                debug_log("fetch_attempt_failed", distro=distro, attempt=attempt, url=url, exc=traceback.format_exc())
                if attempt >= attempts:
                    await page.close()
                    return distro, local_version, "N/A", "UNKNOWN", ""
                # exponential backoff in milliseconds -> convert to seconds for sleep
                backoff_ms = retry_delay_ms * (2 ** (attempt - 1))
                debug_log("retry_sleep", distro=distro, attempt=attempt, backoff_ms=backoff_ms)
                try:
                    await asyncio.sleep(backoff_ms / 1000.0)
                except Exception:
                    pass

        if "Distribution Name Query" in html:
            await page.close()
            return distro, local_version, "N/A", "UNKNOWN", ""
        latest = None

        # If override source is URL with regex, apply the provided regex directly
        if source == "url" and override.get("regex"):
            pattern = override.get("regex")
            try:
                m = re.search(pattern, html, re.I | re.DOTALL)
                if m:
                    latest = m.group(1) if m.groups() else m.group(0)
                    debug_log("match", distro=distro, method="url_regex", value=latest, pattern=pattern)
            except re.error:
                debug_log("regex_error", distro=distro, pattern=pattern)

        # If override source is RSS, try to parse items and apply regex (or fallback to numbers)
        if not latest and source == "rss":
            feed_pattern = override.get("regex")
            # find <item> or <entry> blocks
            items = re.findall(r"<item[\s\S]*?</item>", html, re.I)
            if not items:
                items = re.findall(r"<entry[\s\S]*?</entry>", html, re.I)
            for item in items:
                text = item
                if feed_pattern:
                    try:
                        m = re.search(feed_pattern, text, re.I | re.DOTALL)
                        if m:
                            latest = m.group(1) if m.groups() else m.group(0)
                            debug_log("match", distro=distro, method="rss_regex", value=latest)
                            break
                    except re.error:
                        debug_log("regex_error", distro=distro, pattern=feed_pattern)
                # fallback: look in title tags for digit sequences
                m2 = re.search(r"<title[^>]*>([^<]+)</title>", text, re.I)
                if m2:
                    t = m2.group(1)
                    mver = re.search(r"\d+(?:\.\d+)*", t)
                    if mver:
                        latest = mver.group(0)
                        debug_log("match", distro=distro, method="rss_title", value=latest)
                        break

        # Default Distrowatch parsing if no override matched or no override provided
        if not latest:
            section = re.findall(r"Releases announcements.*?</b>(.*?)</td>", html, re.DOTALL)
            if section:
                for line in section[0].splitlines():
                    if "Distribution Release:" in line:
                        m_spec = re.search(r"Distribution Release:\s*[^\d\n]*?(\d+(?:\.\d+)*)", line)
                        if m_spec:
                            latest = m_spec.group(1)
                            debug_log("match", distro=distro, method="spec", value=latest, line=line.strip())
                            break

                        m = re.search(r"\d+(?:\.\d+)+", line)
                        if m:
                            latest = m.group(0)
                            debug_log("match", distro=distro, method="dotted", value=latest, line=line.swtrip())
                            break

                        m2 = re.search(r"version[:\s]*([^\s<]+)", line, re.I)
                        if m2:
                            cand = re.sub(r"[^0-9.]", "", m2.group(1))
                            if cand:
                                latest = cand
                                debug_log("match", distro=distro, method="version_token", value=latest, line=line.strip())
                                break

                        m3 = re.search(r"\b\d+(?:\.\d+)*\b", line)
                        if m3:
                            latest = m3.group(0)
                            debug_log("match", distro=distro, method="fallback", value=latest, line=line.strip())
                            break
        # If we didn't find a version in the expected section, try a broader search
        if not latest:
            debug_log("fallback_search", distro=distro, note="section not found or no match, searching whole HTML")
            for line in html.splitlines():
                if "Distribution Release:" in line:
                    m_spec = re.search(r"Distribution Release:\s*[^\d\n]*?(\d+(?:\.\d+)*)", line)
                    if m_spec:
                        latest = m_spec.group(1)
                        debug_log("match", distro=distro, method="spec_whole", value=latest, line=line.strip())
                        break
                    m = re.search(r"\d+(?:\.\d+)+", line)
                    if m:
                        latest = m.group(0)
                        debug_log("match", distro=distro, method="dotted_whole", value=latest, line=line.strip())
                        break
                    m2 = re.search(r"version[:\s]*([^\s<]+)", line, re.I)
                    if m2:
                        cand = re.sub(r"[^0-9.]", "", m2.group(1))
                        if cand:
                            latest = cand
                            debug_log("match", distro=distro, method="version_token_whole", value=latest, line=line.strip())
                            break

        await page.close()

        if not latest:
            return distro, local_version, "N/A", "UNKNOWN", "", "browser"

        lv = version_tuple(local_version)
        dv = version_tuple(latest)

        if lv == dv:
            return distro, local_version, latest, "UP TO DATE", "", "browser"
        if lv > dv:
            return distro, local_version, latest, "LOCAL AHEAD", "", "browser"
        return distro, local_version, latest, "UPDATE AVAILABLE", url, "browser"

# --------------------
# Main
# --------------------
async def try_rss_only(p, distro, local_version):
    """Top-level RSS prefetch helper used by `main` tasks.

    Uses globals() to read shared `rss_session`, `rss_sem`, and other
    configuration so it can be defined at module scope.
    """
    session = globals().get("rss_session")
    rss_sem_local = globals().get("rss_sem")
    override = globals().get("overrides", {}).get(distro, {})
    source = override.get("source", "distrowatch").lower()
    feed_url = None
    if source == "rss":
        feed_url = override.get("feed") or override.get("url")
    elif source == "distrowatch":
        feed_url = f"https://distrowatch.com/news/distro/{distro.lower()}.xml"
    if not feed_url or not session:
        return None
    debug_log("rss_prefetch", distro=distro, feed=feed_url)
    sem_local = rss_sem_local or asyncio.Semaphore(1)
    async with sem_local:
        try:
            import random

            await asyncio.sleep(random.uniform(globals().get("rss_jitter_min", 0.05), globals().get("rss_jitter_max", 0.25)))
        except Exception:
            pass
        try:
            hdrs = dict(globals().get('headers')) if 'headers' in globals() else None
            if hdrs:
                ua_choices = [hdrs.get('User-Agent', ''), 'Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:120.0) Gecko/20100101 Firefox/120.0']
                import random

                hdrs['User-Agent'] = random.choice(ua_choices)
        except Exception:
            hdrs = None

        text = None
        used_playwright = False
        try:
            req_args = {'headers': hdrs} if hdrs else {}
            async with session.stream("GET", feed_url, **req_args) as resp:
                try:
                    info_headers = dict(resp.headers)
                except Exception:
                    info_headers = {}
                status = resp.status_code
                debug_log("rss_http_status", distro=distro, status=status, url=str(getattr(resp, 'url', feed_url)), headers=info_headers)
                if status != 200:
                    used_playwright = True
                else:
                    buf = ""
                    async for chunk in resp.aiter_bytes(chunk_size=2048):
                        try:
                            chunk_text = chunk.decode()
                        except Exception:
                            chunk_text = chunk.decode(errors="ignore")
                        buf += chunk_text
                        parsed = parse_rss_text(buf)
                        if parsed:
                            text = buf
                            break
                        if len(buf) > 64 * 1024:
                            buf = buf[-32 * 1024:]
                    if not text:
                        text = buf
                    ct = info_headers.get('Content-Type') or info_headers.get('content-type')
                    debug_log("rss_raw_snippet", distro=distro, snippet=(text[:2000] if text else ""), content_type=ct, content_length=len(text) if text else None)
                    if ct and 'html' in ct.lower() and not re.search(r"<(?:rss|feed|entry|item)[\s>]", text, re.I):
                        used_playwright = True
        except Exception as e:
            debug_log("rss_http_exception", distro=distro, exc=str(e))
            used_playwright = True

        if used_playwright:
            try:
                browser = globals().get("playwright_rss_browser")
                if not browser:
                    browser = await p.chromium.launch(headless=True)
                    globals()["playwright_rss_browser"] = browser
                page = await browser.new_page()
                await page.goto(feed_url, timeout=globals().get('timeout_ms', 20000), wait_until="domcontentloaded")
                page_text = await page.content()
                await page.close()
                m = re.search(r"<div[^>]+id=[\"']webkit-xml-viewer-source-xml[\"'][^>]*>(.*?)</div>", page_text, re.S | re.I)
                if m:
                    text = m.group(1)
                else:
                    m2 = re.search(r"<(?:rss|feed)[\s>].*", page_text, re.S | re.I)
                    if m2:
                        text = m2.group(0)
                    else:
                        text = re.sub(r"<(/?)(html|body)[^>]*>", "", page_text, flags=re.I)
                debug_log("rss_raw_snippet", distro=distro, snippet=(text[:20000] if text else ""), via="playwright_fallback")
            except Exception as e:
                debug_log("rss_playwright_error", distro=distro, exc=str(e))
                return None

        parsed = parse_rss_text(text)
        if parsed:
            latest, link_from_feed = parsed
            debug_log("rss_prefetch_match", distro=distro, value=latest, via=("playwright" if used_playwright else "httpx"))
            debug_log("rss_prefetch_return", distro=distro, latest=latest, via="rss_try")
            return latest, link_from_feed or ""
        return None
async def main():
    # Ensure `resolved` exists in the function scope before any early references
    resolved = {}
    try:
        import time, signal
        main_start = time.monotonic()
        async with async_playwright() as p:
            # Attempt to create a shared httpx session for RSS fetching (prefer HTTP/2)
            rss_session = None
            rss_sem = None
            try:
                import httpx

                timeout = httpx.Timeout(timeout_ms / 1000.0)
                # Use a richer set of browser-like headers to reduce server-side blocking
                headers = {
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                    # Prefer RSS/XML when available but accept other types as fallback
                    "Accept": "application/rss+xml, application/xml;q=0.9, text/xml;q=0.8, */*;q=0.7",
                    "Accept-Language": "en-US,en;q=0.9",
                    "Accept-Encoding": "gzip, deflate, br",
                    "Connection": "keep-alive",
                    "Referer": "https://distrowatch.com/",
                    "DNT": "1",
                    "Upgrade-Insecure-Requests": "1",
                    # Common client hints and fetch metadata headers seen from browsers
                    "sec-ch-ua": '"Chromium";v="120", "Google Chrome";v="120", "Not A(Brand)";v="99"',
                    "sec-ch-ua-mobile": "?0",
                    "sec-ch-ua-platform": '"Windows"',
                    "sec-fetch-site": "same-origin",
                    "sec-fetch-mode": "navigate",
                    "sec-fetch-user": "?1",
                    "sec-fetch-dest": "document",
                    "Pragma": "no-cache",
                    "Cache-Control": "no-cache",
                }
                # Increase connection limits and avoid honor system proxies (faster direct connections)
                limits = httpx.Limits(max_connections=max(20, rss_concurrency * 4), max_keepalive_connections=max(10, rss_concurrency))
                rss_session = httpx.AsyncClient(timeout=timeout, headers=headers, http2=True, limits=limits, trust_env=False)
                rss_sem = asyncio.Semaphore(rss_concurrency)
                globals()["rss_session"] = rss_session
                globals()["rss_sem"] = rss_sem
                debug_log("rss_session_created", concurrency=rss_concurrency)
            except Exception as e:
                debug_log("rss_session_unavailable", error=str(e))
                try:
                    import sys as _sys

                    _sys.stderr.write(f"[INFO] RSS disabled (httpx unavailable): {e}\n")
                except Exception:
                    try:
                        import random

                        await asyncio.sleep(random.uniform(rss_jitter_min, rss_jitter_max))
                    except Exception:
                        pass
                    try:
                        import random

                        hdrs = dict(headers) if 'headers' in globals() or 'headers' in locals() else None
                        if hdrs:
                            ua_choices = [
                                hdrs.get('User-Agent', ''),
                                'Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:120.0) Gecko/20100101 Firefox/120.0',
                            ]
                            hdrs['User-Agent'] = random.choice(ua_choices)
                    except Exception:
                        hdrs = None

                    text = None
                    used_playwright = False

                    # duplicate RSS attempt block removed (module-level try_rss_only used instead)

                    # Collect resolved entries from RSS prefetch
                    resolved = {}

                    # nested try_rss_only removed (module-level implementation will be used)

            # build prefetch tasks for all distros (RSS or Distrowatch feed)
            try:
                pre_tasks = [asyncio.create_task(try_rss_only(p, d, v)) for d, v in local_versions.items()]
            except Exception as e:
                import traceback, sys as _sys
                # Log whether the name is present in locals/globals (avoids directly referencing a possibly-unbound local)
                has_in_locals = 'try_rss_only' in locals()
                has_in_globals = 'try_rss_only' in globals()
                debug_log("prefetch_task_creation_error", error=str(e), has_in_locals=has_in_locals, has_in_globals=has_in_globals, exc=traceback.format_exc())
                _sys.stderr.write(f"[ERROR] prefetch task creation failed: {e}\n")
                raise

            monitor_pre = asyncio.create_task(_progress_bar(pre_tasks, "Fetching RSS"))
            pre_results = await asyncio.gather(*pre_tasks, return_exceptions=True)
            try:
                await monitor_pre
            except Exception:
                pass
            remaining = []
            for (distro, lv), res in zip(local_versions.items(), pre_results):
                if isinstance(res, tuple) and res[0]:
                    latest, link = res
                    lv_tuple = version_tuple(lv)
                    try:
                        latest_tuple = version_tuple(latest)
                    except Exception:
                        latest_tuple = ()
                    if lv_tuple == latest_tuple:
                        st = "UP TO DATE"
                    elif lv_tuple > latest_tuple:
                        st = "LOCAL AHEAD"
                    else:
                        st = "UPDATE AVAILABLE"
                    resolved[distro] = (distro, lv, latest, st, link or "", "rss")
                else:
                    remaining.append((distro, lv))

            # Diagnostic summary: show which distros were resolved via RSS and which remain
            try:
                resolved_names = sorted(list(resolved.keys()))
                remaining_names = [d for d, _ in remaining]
                debug_log("prefetch_summary", resolved=resolved_names, remaining=remaining_names)
                import sys as _sys
                _sys.stderr.write(f"Resolved via RSS: {len(resolved_names)} -> {resolved_names}\n")
                if not globals().get("no_browser"):
                    _sys.stderr.write(f"Remaining (will use browser): {len(remaining_names)} -> {remaining_names}\n")
            except Exception:
                pass

            # If all resolved via RSS, skip launching Playwright
            results = list(resolved.values())

            # If --no-browser is set, convert remaining distros to UNKNOWN and skip Playwright
            if remaining and globals().get("no_browser"):
                for distro, lv in remaining:
                    results.append((distro, lv, "N/A", "UNKNOWN", "", "skipped_no_browser"))
                remaining = []

            tasks = []
            browser = None
            browser_results = []
            if remaining:
                browser = await p.chromium.launch(headless=True)
                sem = asyncio.Semaphore(max_parallel_tabs)
                # create tasks so we can cancel on signals
                tasks = [
                    asyncio.create_task(fetch(browser, d, v, sem))
                    for d, v in remaining
                ]
                monitor_pages = asyncio.create_task(_progress_bar(tasks, "Fetching pages"))

            loop = asyncio.get_running_loop()

            def _cancel_all() -> None:
                debug_log("signal", msg="cancelling all tasks")
                for t in tasks:
                    try:
                        t.cancel()
                    except Exception:
                        pass

            # register signal handlers (best-effort)
            try:
                loop.add_signal_handler(signal.SIGINT, _cancel_all)
                loop.add_signal_handler(signal.SIGTERM, _cancel_all)
            except Exception:
                import signal as _signal

                def _handler(sig, frame):
                    loop.call_soon_threadsafe(_cancel_all)

                _signal.signal(_signal.SIGINT, _handler)
                try:
                    _signal.signal(_signal.SIGTERM, _handler)
                except Exception:
                    pass

            if tasks:
                browser_results = await asyncio.gather(*tasks, return_exceptions=True)
                try:
                    await monitor_pages
                except Exception:
                    pass
            if browser:
                await browser.close()
            # close shared rss session if created
            try:
                if rss_session:
                    try:
                        if hasattr(rss_session, "aclose"):
                            await rss_session.aclose()
                        elif hasattr(rss_session, "close"):
                            rss_session.close()
                        debug_log("rss_session_closed")
                    except Exception as _e:
                        debug_log("rss_session_close_error", error=str(_e))
            except Exception as e:
                debug_log("rss_session_close_error", error=str(e))
            finally:
                globals().pop("rss_session", None)
                globals().pop("rss_sem", None)
            # close any playwright browser created for RSS fetching
            try:
                br = globals().pop("playwright_rss_browser", None)
                if br:
                    await br.close()
                    debug_log("playwright_rss_browser_closed")
            except Exception as e:
                debug_log("playwright_rss_browser_close_error", error=str(e))
            finally:
                # remove legacy keys if present
                globals().pop("playwright_browser", None)
                globals().pop("playwright_instance", None)
            # merge any browser results with the RSS-resolved results
            if browser_results:
                for r in browser_results:
                    if isinstance(r, Exception):
                        debug_log("task_exception", exc=str(r))
                        continue
                    # prefer prefetch/resolved entries (RSS) — skip browser result
                    try:
                        distro_name = r[0]
                    except Exception:
                        results.append(r)
                        continue
                    if distro_name in resolved:
                        debug_log("merge_skip", distro=distro_name, reason="already_resolved_via_rss")
                        continue
                    results.append(r)
    except Exception as e:
        print(f"[ERROR] Playwright failure: {e}")
        import traceback
        debug_log("playwright_error", error=str(e), exc=traceback.format_exc())
        return 4, []

    results.sort(key=lambda x: x[0])

    updates = False
    local_ahead = False

    print("\nDistro           Local       Latest      Status")
    print("-------------------------------------------------------------")

    filtered_results = []
    urls = []

    for row in results:
        if isinstance(row, (list, tuple)) and len(row) >= 5:
            distro, lv, dv, status, link = row[:5]
            src = row[5] if len(row) >= 6 else ""
        else:
            continue
        if status == "UPDATE AVAILABLE":
            updates = True
        elif status == "LOCAL AHEAD":
            local_ahead = True

        if passes_filter(status):
            filtered_results.append((distro, lv, dv, status, link, src))
            if link:
                idx = len(urls) + 1
                urls.append(link)
                link_display = f"  [{idx}]"
            else:
                link_display = ""

            print(
                f"{distro.ljust(15)}"
                f"{lv.ljust(12)}"
                f"{dv.ljust(12)}"
                f"{color(status)}"
                + link_display
            )

    # If user only asked for URLs, print them one-per-line and exit
    if urls_only:
        # compute exit code from result flags before returning
        exit_code = (
            3 if updates and local_ahead else
            1 if updates else
            2 if local_ahead else
            0
        )
        for u in urls:
            print(u)
        return exit_code, filtered_results

    # CSV
    if csv_output:
        with open(csv_output, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(
                ["distro", "local_version", "latest_version", "status", "distrowatch_url", "source"]
            )
            for row in filtered_results:
                # row may include source as sixth element
                writer.writerow([row[0], row[1], row[2], row[3], row[4], row[5] if len(row) > 5 else ""])

    # JSON
    if json_output:
        exit_code = (
            3 if updates and local_ahead else
            1 if updates else
            2 if local_ahead else
            0
        )
        list_of_results = []
        for row in filtered_results:
            d, lv, dv, st, link = row[:5]
            src = row[5] if len(row) > 5 else ""
            list_of_results.append({
                "distro": d,
                "local_version": lv,
                "latest_version": dv,
                "status": st,
                "distrowatch_url": link,
                "source": src,
            })
        data = {
            "summary": {
                "updates_available": updates,
                "local_ahead": local_ahead,
                "exit_code": exit_code,
            },
            "results": list_of_results,
        }
        with open(json_output, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)

    debug_log("total_runtime", total=(__import__("time").monotonic() - main_start))

    if updates and local_ahead:
        exit_code = 3
    elif updates:
        exit_code = 1
    elif local_ahead:
        exit_code = 2
    else:
        exit_code = 0

    return exit_code, filtered_results

def _parse_selection(s: str, max_idx: int) -> list[int]:
    out = set()
    for part in s.split(','):
        part = part.strip()
        if not part:
            continue
        if '-' in part:
            try:
                a, b = part.split('-', 1)
                a = int(a)
                b = int(b)
            except Exception:
                continue
            if a > b:
                a, b = b, a
            for i in range(max(1, a), min(b, max_idx) + 1):
                out.add(i)
        else:
            try:
                v = int(part)
            except Exception:
                continue
            if 1 <= v <= max_idx:
                out.add(v)
    return sorted(out)


def _read_selection_line() -> str | None:
    try:
        if os.name == 'nt':
            import msvcrt

            buf = ''
            while True:
                ch = msvcrt.getwch()
                if ch == '\r' or ch == '\n':
                    print()
                    return buf
                if ch == '\x1b':
                    return None
                if ch == '\x08':
                    if buf:
                        buf = buf[:-1]
                        sys.stdout.write('\b \b')
                        sys.stdout.flush()
                    continue
                if ch.isprintable():
                    buf += ch
                    sys.stdout.write(ch)
                    sys.stdout.flush()
        else:
            import sys as _sys, tty, termios

            fd = _sys.stdin.fileno()
            old = termios.tcgetattr(fd)
            try:
                tty.setraw(fd)
                buf = ''
                while True:
                    ch = _sys.stdin.read(1)
                    if ch == '\r' or ch == '\n':
                        print()
                        return buf
                    if ch == '\x1b':
                        return None
                    if ch == '\x7f':
                        if buf:
                            buf = buf[:-1]
                            sys.stdout.write('\b \b')
                            sys.stdout.flush()
                        continue
                    if ch.isprintable():
                        buf += ch
                        sys.stdout.write(ch)
                        sys.stdout.flush()
            finally:
                termios.tcsetattr(fd, termios.TCSADRAIN, old)
    except Exception:
        try:
            return input()
        except Exception:
            return None


def _interactive_exit(links: list[str] | None = None, prompt: str | None = None) -> None:
    # Respect global no_pause flag
    if globals().get("no_pause"):
        return

    links = links or []
    web_links = [l for l in links if l]
    header = prompt or "Press ESC or Enter to exit"
    if web_links:
        print(f"{header} — enter numbers/ranges (e.g. 1,3-5) or 'a' to open all then Enter")
        # links were already printed inline with indexes in the table
        print("Selection: ", end='', flush=True)
        sel = _read_selection_line()
        if sel is None:
            return
        sel_str = sel.strip()
        if not sel_str:
            return
        if sel_str.lower() in ("a", "all"):
            import webbrowser
            for link in web_links:
                webbrowser.open(link)
            return
        choices = _parse_selection(sel_str, len(web_links))
        if not choices:
            return
        import webbrowser
        for c in choices:
            webbrowser.open(web_links[c - 1])
    else:
        print(f"{header}...")
        # Use the raw reader so ESC is detected rather than using blocking input()
        sel = _read_selection_line()
        # _read_selection_line returns None on ESC, and '' on Enter
        return


if __name__ == "__main__":
    try:
        exit_code, links = asyncio.run(main())
    except SystemExit as e:
        # early exits (help, config errors)
        _interactive_exit(None)
        raise
    except KeyboardInterrupt:
        # Graceful keyboard interrupt handling
        debug_log("shutdown", reason="keyboard_interrupt")
        _interactive_exit(None)
        sys.exit(130)
    except Exception:
        _interactive_exit(None, "An unexpected error occurred. Press ESC to exit")
        raise

    # links is a list of tuples (distro, lv, dv, status, link) — extract urls
    urls = [row[4] for row in links] if links else []
    _interactive_exit(urls)
    sys.exit(exit_code)
