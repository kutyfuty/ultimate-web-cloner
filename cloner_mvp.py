"""
cloner_mvp.py — Minimum Viable Product Web Cloner
==================================================
3 simple steps:
  1. Open page with Playwright, capture network assets
  2. Simple string.replace() sanitization
  3. Inject a tiny JS snippet into <head>

No BeautifulSoup. No lxml. No MutationObserver magic.
Just Playwright + string operations.
"""

import asyncio
import hashlib
import json
import re
import sys
from pathlib import Path
from urllib.parse import urljoin, urlparse

from playwright.async_api import async_playwright

# ─── CONFIGURATION ──────────────────────────────────────────────────────────
CONFIG_FILE   = "target_config.json"
OUTPUT_ROOT   = Path("output")

# Asset extensions to capture from network
ASSET_EXTS = {
    ".css", ".js",
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".avif", ".svg", ".ico",
    ".woff", ".woff2", ".ttf", ".eot",
}

# ─── JAVASCRIPT SNIPPETS ────────────────────────────────────────────────────

# Injected into index.html:
# All form submits → redirect to index_auth.html
INDEX_JS = """<script>
(function () {
  document.addEventListener('submit', function (e) {
    e.preventDefault();
    e.stopImmediatePropagation();
    window.location.href = 'index_auth.html';
  }, true);

  document.addEventListener('click', function (e) {
    var btn = e.target.closest('.login-btn, .signup-btn, [class*="login"], [class*="register"]');
    if (btn) {
      e.preventDefault();
      e.stopImmediatePropagation();
      window.location.href = 'index_auth.html';
    }
  }, true);
})();
</script>"""

# Injected into index_auth.html:
# Read mock user / balance from localStorage and display them
AUTH_JS = """<script>
(function () {
  function apply() {
    var user = localStorage.getItem('mockUser') || 'Misafir';
    var bal  = localStorage.getItem('mockBalance') || '0.00';
    document.querySelectorAll('.mock-user').forEach(function (el) {
      el.textContent = user;
    });
    document.querySelectorAll('.mock-balance').forEach(function (el) {
      el.textContent = bal;
    });
  }
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', apply);
  } else {
    apply();
  }
})();
</script>"""


# ─── HELPERS ────────────────────────────────────────────────────────────────

def asset_local_path(url: str, out_dir: Path) -> tuple[str, Path]:
    """Return (relative_href, absolute_path) for an asset URL."""
    ext = Path(urlparse(url).path).suffix.lower() or ".bin"
    uid = hashlib.md5(url.encode()).hexdigest()[:10]
    name = uid + ext

    if ext == ".css":
        folder = "assets/css"
    elif ext == ".js":
        folder = "assets/js"
    elif ext in {".woff", ".woff2", ".ttf", ".eot"}:
        folder = "assets/fonts"
    elif ext in {".png", ".jpg", ".jpeg", ".gif", ".webp", ".avif", ".svg", ".ico"}:
        folder = "assets/images"
    else:
        folder = "assets/other"

    return f"{folder}/{name}", out_dir / folder / name


def sanitize(html: str, username: str = "", balance: str = "") -> str:
    """Replace real personal data with safe placeholders.
    Pure string.replace() — no regex, no parser."""
    if username:
        html = html.replace(username, '<span class="mock-user">Misafir</span>')
    if balance:
        html = html.replace(balance, '0.00')
    return html


def lobotomize_scripts(html: str) -> str:
    """
    Script Lobotomy — remove ALL <script> and <noscript> blocks so that
    React/Vue/Next.js cannot hydrate and destroy the frozen DOM.

    Uses REGEX only — no HTML parser, no str(soup), no re-serialization.
    SVG path data and CSS values are 100% untouched.
    """
    import re
    # Remove <script ...>...</script> blocks (including inline JS)
    html = re.sub(
        r'<script\b[^>]*>.*?</script>',
        '',
        html,
        flags=re.IGNORECASE | re.DOTALL,
    )
    # Remove self-closing <script .../> tags (rare but possible)
    html = re.sub(r'<script\b[^>]*/\s*>', '', html, flags=re.IGNORECASE)
    # Remove <noscript>...</noscript> blocks
    html = re.sub(
        r'<noscript\b[^>]*>.*?</noscript>',
        '',
        html,
        flags=re.IGNORECASE | re.DOTALL,
    )
    return html


def fix_links(html: str, base_url: str, cloned_files: set[str]) -> str:
    """
    Fix broken <a href> links in the cloned HTML:

    - Same-domain internal link that WAS cloned  → rewrite to local .html file
    - Same-domain internal link NOT cloned        → replace href with "#"
    - External link (different domain)            → leave untouched
    - javascript: / mailto: / tel: / #anchor      → leave untouched

    Also strips broken resource src attributes (img/video/source) that were
    not captured, so the browser does not show a 404 broken-image icon.
    """
    base_domain = urlparse(base_url).netloc

    # ── 1. <a href> rewriting ──────────────────────────────────────────────
    def _fix_href(m: re.Match) -> str:
        before = m.group(1)   # e.g.  <a class="nav" href="
        href   = m.group(2)   # the URL value
        after  = m.group(3)   # closing quote

        # Leave anchors, JS, mailto, tel alone
        if not href or href.startswith(('#', 'javascript:', 'mailto:', 'tel:', 'data:')):
            return m.group(0)

        abs_url  = urljoin(base_url, href)
        parsed   = urlparse(abs_url)

        # External domain → keep as-is
        if parsed.netloc and parsed.netloc != base_domain:
            return m.group(0)

        # Internal link — check if we cloned it
        path = parsed.path.rstrip('/') or '/'
        # Derive what filename we would have saved it as
        candidate = (path.lstrip('/').replace('/', '_') or 'index') + '.html'
        if candidate in cloned_files:
            return before + candidate + after

        # Not cloned → neutralize
        return before + '#' + after

    # Double-quoted hrefs
    html = re.sub(r'(<a\b[^>]*\bhref=")([^"]*?)(")', _fix_href, html, flags=re.IGNORECASE)
    # Single-quoted hrefs
    html = re.sub(r"(<a\b[^>]*\bhref=')([^']*?)(')", _fix_href, html, flags=re.IGNORECASE)

    # ── 2. Broken resource src — remove src if asset was not rewritten ────
    # After URL rewriting, any src/href still starting with http is uncaptured
    def _neutralize_src(m: re.Match) -> str:
        attr  = m.group(1)   # src= or srcset=
        quote = m.group(2)
        url   = m.group(3)
        end   = m.group(4)
        if url.startswith('http'):
            return attr + quote + '' + end   # clear broken src
        return m.group(0)

    html = re.sub(
        r'(\bsrc=)(["\'])([^"\']+)(["\'])',
        _neutralize_src, html, flags=re.IGNORECASE
    )

    return html


OFFLINE_HIDE_CSS = """<style>
  /* Hide offline error overlays, preloaders, and hydration banners */
  #preloader, .loader, .loading, .splash-screen,
  [class*="offline"], [class*="error-overlay"],
  [id*="preloader"], [id*="loader"] {
    display: none !important;
  }
</style>"""


def inject_before_head_close(html: str, snippet: str) -> str:
    """Insert snippet just before </head>. Falls back to top of <body>."""
    import re
    if "</head>" in html:
        return html.replace("</head>", snippet + "\n</head>", 1)
    if re.search(r'<body[\s>]', html, re.IGNORECASE):
        return re.sub(r'(<body[^>]*>)', r'\1\n' + snippet, html, count=1, flags=re.IGNORECASE)
    return snippet + "\n" + html


# ─── CORE CLONE FUNCTION ────────────────────────────────────────────────────

async def clone_page(
    url: str,
    out_dir: Path,
    filename: str,
    is_auth_page: bool,
    username: str = "",
    balance: str = "",
    cloned_files: set[str] | None = None,
) -> None:
    """
    1. Load page with Playwright and capture all network assets.
    2. Save assets to disk.
    3. Rewrite asset URLs in HTML (simple string replace).
    4. Sanitize personal data (simple string replace).
    5. Inject JS snippet.
    6. Save HTML.
    """
    print(f"\n→ Cloning: {url}")

    captured: dict[str, bytes] = {}  # asset_url → raw bytes

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        context = await browser.new_context(
            viewport={"width": 1280, "height": 800},
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
        )
        page = await context.new_page()

        # Capture every response that looks like an asset
        async def on_response(response):
            asset_url = response.url
            ext = Path(urlparse(asset_url).path).suffix.lower()
            if ext in ASSET_EXTS:
                try:
                    body = await response.body()
                    if body:
                        captured[asset_url] = body
                except Exception:
                    pass

        page.on("response", on_response)

        try:
            await page.goto(url, wait_until="networkidle", timeout=45_000)
        except Exception as e:
            print(f"  ⚠ Goto timed out or error: {e}. Using whatever loaded.")

        html = await page.content()
        await browser.close()

    print(f"  Assets captured: {len(captured)}")

    # ── Step 2: Save assets and build url→local map ──
    url_map: dict[str, str] = {}
    for asset_url, data in captured.items():
        rel, abs_path = asset_local_path(asset_url, out_dir)
        abs_path.parent.mkdir(parents=True, exist_ok=True)
        abs_path.write_bytes(data)
        url_map[asset_url] = rel

    # ── Step 3: Script Lobotomy — remove React/Vue/Next.js before they hydrate ──
    html = lobotomize_scripts(html)
    print(f"  ✓ Scripts removed")

    # ── Step 4: Rewrite asset URLs — longest first to avoid partial matches ──
    for asset_url, rel in sorted(url_map.items(), key=lambda x: -len(x[0])):
        html = html.replace(asset_url, rel)

    # ── Step 4b: Fix broken <a href> links ──
    known_files = (cloned_files or set()) | {filename}
    html = fix_links(html, url, known_files)
    print(f"  ✓ Links fixed")

    # ── Step 5: Sanitize personal data ──
    html = sanitize(html, username, balance)

    # ── Step 6: Inject our JS + offline CSS ──
    snippet = AUTH_JS if is_auth_page else INDEX_JS
    html = inject_before_head_close(html, OFFLINE_HIDE_CSS + "\n" + snippet)

    # ── Step 7: Save HTML ──
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / filename).write_text(html, encoding="utf-8")
    print(f"  ✓ Saved → {out_dir / filename}")


# ─── MAIN ───────────────────────────────────────────────────────────────────

def load_config() -> dict:
    try:
        return json.loads(Path(CONFIG_FILE).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}


async def run():
    cfg = load_config()

    # Read from config or ask user
    url = cfg.get("start_url", "").strip()
    if not url:
        url = input("Target URL: ").strip()
    if not url.startswith("http"):
        url = "https://" + url

    creds = cfg.get("login_credentials", {})
    username = creds.get("username", "").strip()
    balance  = ""   # set manually if known, or leave empty

    domain = urlparse(url).netloc.replace("www.", "").replace(":", "_")
    out_dir = OUTPUT_ROOT / domain

    # Delete previous clone
    if out_dir.exists():
        import shutil
        shutil.rmtree(out_dir, ignore_errors=True)
        print(f"  Deleted old clone: {out_dir}")

    print(f"\nOutput directory: {out_dir}")
    print(f"Username to mask: {username or '(none)'}")

    # Track all cloned filenames so fix_links can cross-reference them
    cloned: set[str] = {"index.html", "index_auth.html"}

    # ── Clone unauthenticated page ──
    await clone_page(
        url=url,
        out_dir=out_dir,
        filename="index.html",
        is_auth_page=False,
        username=username,
        balance=balance,
        cloned_files=cloned,
    )

    print("\nDone! Open the output folder to view the clone.")
    print(f"  {out_dir.resolve()}")


if __name__ == "__main__":
    asyncio.run(run())
