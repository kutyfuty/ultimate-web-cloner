"""
sitemap_generator.py — Deep Site Map Extraction (Phase 7/8)

This module recursively discovers all internal links BEFORE the download
process begins. It crawls the entire tree up to the specified depth
(max_depth) or without limit (max_pages=0).
"""

import asyncio
import re
import os
import hashlib
from datetime import datetime
from pathlib import Path
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup
from PyQt6.QtCore import QObject, pyqtSignal
from playwright.async_api import async_playwright
from playwright_stealth import Stealth


class SitemapGenerator(QObject):
    log_message = pyqtSignal(str)
    progress_updated = pyqtSignal(int)

    SKIP_EXTENSIONS = {
        ".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".ico",
        ".css", ".js", ".woff", ".woff2", ".ttf", ".eot", ".otf",
        ".mp4", ".webm", ".mp3", ".ogg", ".pdf", ".zip", ".rar",
        ".exe", ".apk", ".dmg",
    }

    SKIP_PATTERNS = [
        "javascript:", "mailto:", "tel:", "data:", "blob:",
        "whatsapp:", "tg:", "viber:",
    ]

    def __init__(self, parent=None):
        super().__init__(parent)
        self._is_running = False
        self._visited: set[str] = set()
        self._queue: list[tuple[str, int]] = []  # (url, depth)
        self._sitemap: dict[str, str] = {}       # path -> full_url
        self.output_dir = None

    def stop(self):
        self._is_running = False

    async def generate(self, base_url: str, max_pages: int = 100, max_depth: int = 3) -> dict[str, str]:
        """
        Crawls the site's sub-pages to the given depth and returns a unique (path -> url) dictionary.
        Runs without limit if max_pages = 0. (Long-running)
        """
        self._is_running = True
        self._visited.clear()
        self._sitemap.clear()

        parsed_base = urlparse(base_url)
        base_domain = parsed_base.netloc
        base_path = parsed_base.path.rstrip("/") or "/"

        self._queue = [(base_url, 0)]
        self._visited.add(base_path)

        self.log_message.emit("🕸️ Extracting deep site map...")

        playwright = await async_playwright().start()
        browser = await playwright.chromium.launch(
            headless=True,
            args=["--disable-blink-features=AutomationControlled", "--no-sandbox"]
        )
        context = await browser.new_context(viewport={"width": 1920, "height": 1080})
        stealth = Stealth()
        await stealth.apply_stealth_async(context)
        page = await context.new_page()

        # Block image/CSS downloads but allow SCRIPTs (SPAs cannot render without JS)
        await page.route("**/*", lambda route: route.continue_() if route.request.resource_type in ["document", "xhr", "fetch", "script"] else route.abort())

        pages_found = 0

        try:
            while self._queue and self._is_running:
                current_url, current_depth = self._queue.pop(0)

                # Limit check
                if max_pages > 0 and len(self._sitemap) >= max_pages:
                    self.log_message.emit(f"⚠️ Maximum page limit reached ({max_pages}).")
                    break

                if current_depth > max_depth:
                    continue

                self.log_message.emit(f"   🔎 Depth [{current_depth}]: {current_url}")

                try:
                    await page.goto(current_url, wait_until="domcontentloaded", timeout=15000)
                    # Wait for links to be rendered by JS on SPA (Single Page Application) sites
                    try:
                        await page.wait_for_load_state("networkidle", timeout=5000)
                    except Exception:
                        await asyncio.sleep(2)

                    html = await page.content()
                    new_links = self._extract_links(html, current_url, base_domain)

                    for n_path, n_url in new_links.items():
                        if n_path not in self._visited:
                            self._visited.add(n_path)
                            self._sitemap[n_path] = n_url
                            pages_found += 1
                            self._queue.append((n_url, current_depth + 1))

                            # In-limit check
                            if max_pages > 0 and len(self._sitemap) >= max_pages:
                                break

                except Exception as e:
                    self.log_message.emit(f"   ⚠️ Error ({current_url}): {str(e)[:50]}")

        finally:
            await browser.close()
            await playwright.stop()

        self._is_running = False
        self.log_message.emit(f"✅ Map complete. Total unique pages: {len(self._sitemap)}")

        # Generate XML Sitemap
        if self.output_dir:
            try:
                out_path = os.path.join(self.output_dir, "sitemap.xml")
                with open(out_path, "w", encoding="utf-8") as f:
                    f.write('<?xml version="1.0" encoding="UTF-8"?>\n')
                    f.write('<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n')
                    today = datetime.now().strftime("%Y-%m-%d")
                    for path, url in self._sitemap.items():
                        # Only add valid pages
                        if not any(url.endswith(ext) for ext in self.SKIP_EXTENSIONS) and not "iframes" in path:
                            f.write("  <url>\n")
                            f.write(f"    <loc>{url}</loc>\n")
                            f.write(f"    <lastmod>{today}</lastmod>\n")
                            f.write("    <changefreq>daily</changefreq>\n")
                            f.write("    <priority>0.8</priority>\n")
                            f.write("  </url>\n")
                    f.write("</urlset>\n")
                self.log_message.emit(f"✅ sitemap.xml created: {out_path}")
            except Exception as e:
                self.log_message.emit(f"⚠️ sitemap.xml could not be written: {e}")

        return self._sitemap

    def _extract_links(self, html: str, current_url: str, base_domain: str) -> dict[str, str]:
        soup = BeautifulSoup(html, "lxml")
        links: dict[str, str] = {}

        for a_tag in soup.find_all("a", href=True):
            href = a_tag["href"].strip()
            if any(href.startswith(p) for p in self.SKIP_PATTERNS) or href.startswith("#"):
                continue

            full_url = urljoin(current_url, href)
            parsed = urlparse(full_url)

            # LOOSE DOMAIN CHECK
            def _get_clean_netloc(netloc: str) -> str:
                return netloc.lower().replace("www.", "")

            if parsed.netloc and _get_clean_netloc(parsed.netloc) != _get_clean_netloc(base_domain):
                continue

            # PATH CHECK
            ext = Path(parsed.path).suffix.lower()
            if ext in self.SKIP_EXTENSIONS:
                continue

            clean_path = parsed.path.rstrip("/") or "/"
            if len(clean_path) < 2:
                continue

            links[clean_path] = full_url

        # Dynamic/SPA Link Discovery
        raw_paths = re.findall(r'["\'](/[a-zA-Z0-9_\-\./#\?]{3,120})["\']', html)
        for p in raw_paths:
            if "#" in p or "?" in p: p = p.split("#")[0].split("?")[0]
            if len(p) < 3 or any(p.startswith(sp) for sp in self.SKIP_PATTERNS): continue

            full_url = urljoin(current_url, p)
            parsed = urlparse(full_url)
            if parsed.netloc and _get_clean_netloc(parsed.netloc) != _get_clean_netloc(base_domain):
                continue

            ext = Path(parsed.path).suffix.lower()
            if ext and ext not in {'.html', '.htm', '.php', '.asp', '.aspx', '.jsp'}:
                continue
            if '/api/' in parsed.path.lower() or '/v1/' in parsed.path.lower():
                continue
            clean_path = parsed.path.rstrip("/") or "/"
            if len(clean_path) >= 2 and clean_path not in links:
                links[clean_path] = full_url

        # Also collect iframe srcs
        for iframe in soup.find_all("iframe", src=True):
            src = iframe["src"].strip()
            if not src or "javascript:" in src or "about:" in src:
                continue
            full_url = urljoin(current_url, src)
            tracking = ["zendesk", "tawk", "google", "facebook", "yandex", "crisp"]
            if any(t in full_url.lower() for t in tracking):
                continue
            url_hash = hashlib.md5(full_url.encode()).hexdigest()[:8]
            links[f"/iframes/frame_{url_hash}.html"] = full_url

        return links
