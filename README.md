# disprobe

disprobe is a small command-line tool to check installed distro versions against upstream release sources (Distrowatch, RSS feeds, or arbitrary URLs). It was born to keep a Ventoy disk up-to-date but is generic enough to track any distribution release versions.

## Features
- RSS-first prefetch for speed and low load
- Playwright browser fallback for sites that require rendering
- Per-distro overrides: custom URL, feed, or regex extraction
- Batch mode with parallel page fetches
- Filters and output formats: table, CSV, JSON
- Optional `--no-browser` mode to avoid Playwright entirely (returns UNKNOWN for non-RSS distros)
- Debug logging to JSON lines for easy tracing

## Requirements
- Python 3.10+
- Packages: playwright, httpx, colorama
- Playwright browsers (if not using `--no-browser`)

Install dependencies:
```bash
python -m pip install playwright httpx colorama
python -m playwright install chromium
```

On Windows use PowerShell / CMD as appropriate.

## Quick install (example)
```powershell
cd C:\path\to\repo
python -m pip install -r requirements.txt
python -m playwright install chromium
```
Note that releases have the requirements bundled and the program compiled to a single executable for windows.  compile flags are as follows
```pyinstaller --onefile --console disprobe.py```

## Usage
Basic:
```powershell
python .\disprobe.py
```

Help:
```powershell
python .\disprobe.py --help
```

Useful flags:
- `--no-browser`  do not attempt to fall back to Playwright
- `--csv <path>` / `--json <path>` - write outputs
- `--urls` - print collected Distrowatch URLs only
- `--help` - show additional flags
  
Exit codes:
- 0 - all up to date
- 1 - at least one update available
- 2 - at least one local-ahead
- 3 - mix of updates and local-ahead

## Config file (distros.txt)
Plain text file, one distro per line:
- Format: `distro=local_version`
- Blank lines and `#` comments ignored
- Simple example:
```
fedora=43
ubuntu=22.04
alpine=3.18
```

Per-distro overrides (semicolon-separated metadata):
```
fedora=43;source=url;url=https://example.org/releases;regex=Release:\s.*?(\d+)
mydistro=1.2;source=rss;feed=https://example.org/feed.xml;regex=(\d+\.\d+)
```
Supported override keys:
- `source`: `distrowatch` (default), `rss`, or `url`
- `url` / `feed` / `uri`: explicit page or feed
- `regex`: a Python regex to extract the version (first capture group preferred)

## Examples
Run without browser fallback, push debug to file:
```powershell
python .\disprobe.py --no-browser --debug --debug-file debug.json
```

Write JSON output:
```powershell
python .\disprobe.py --json results.json
```

Print only Distrowatch URLs:
```powershell
python .\disprobe.py --urls
```

## Debugging
Enable debug logging to capture structured events:
```powershell
python .\disprobe.py --debug --debug-file debug.json
```
The JSON-lines file contains helpful events (rss_session_created, rss_prefetch, rss_http_status, playwright errors, etc.)

Common runtime issues
- Server-side blocking (403 / connection refused) - Distrowatch will block your connection for 10 hours if they think you're trying to DDoS them.  If you've got an extremely large list it's recommended to split your distros.txt into multiple parts and use the --file flag.
I went for what I thought was a good balance of speed and not getting hit with IP blocking but you may be able to find a better balance for your own use-case

## Contributing
- Small fixes, better parsing heuristics, or additional sources are welcome.
- Keep changes minimal and add tests for parsing functions where possible.
