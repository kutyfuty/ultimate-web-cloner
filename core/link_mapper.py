"""
link_mapper.py — Multi-Page Cloning and Link Mapping (v3)

v3 Features:
- SINGLE browser instance is SHARED
- CSS INLINE: all CSS rules are embedded as a <style> block in each subpage
- max_pages limit (default 100) — game pages (/play/) are queued last
- Only NEW resources are saved
- CSS files are rewritten after each subpage
- Background images are captured
"""

import asyncio
import re
import hashlib
import os
from pathlib import Path
from urllib.parse import urljoin, urlparse, unquote

from PyQt6.QtCore import QObject, pyqtSignal

from core.frontend_mocker import FrontendMocker
from playwright.async_api import async_playwright
from playwright_stealth import Stealth

from core.asset_manager import AssetManager
from core.modal_capturer import ModalCapturer
from core.sitemap_generator import SitemapGenerator
from core.interaction_engine import InteractionEngine


class LinkMapper(QObject):
    """Multi-page cloning and link mapping manager."""

    # ── Signals ──
    log_message = pyqtSignal(str)
    progress_updated = pyqtSignal(int)
    page_cloned = pyqtSignal(str, str)
    all_pages_finished = pyqtSignal(int)
    cloning_failed = pyqtSignal(str)

    # Phase 11 New Signals (Granular Progress)
    total_pages_detected = pyqtSignal(int)
    page_progress = pyqtSignal(int, int) # (current_index, total_pages)

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

    # Game/single-page patterns (low priority)
    GAME_PATTERNS = ["/play/", "/game/casino/play/", "/game/live-casino/play/"]

    SCROLL_STEP_PX = 300
    SCROLL_DELAY_MS = 400
    NETWORK_IDLE_TIMEOUT = 8000
    MAX_SCROLL_ATTEMPTS = 150

    def __init__(self, parent=None):
        super().__init__(parent)
        self._scraped_pages: dict[str, str] = {}
        self._is_running = False
        self._saved_urls: set[str] = set()
        self._sitemap_gen: SitemapGenerator | None = None

    # ──────────────────────────────────────────────
    #  PUBLIC API
    # ──────────────────────────────────────────────

    async def clone_all_pages(
        self,
        base_url: str,
        main_html: str,
        output_dir: str | Path,
        captured_resources: dict[str, bytes],
        content_types: dict[str, str],
        max_pages: int = 100,
        deep_crawl: bool = False,
        max_depth: int = 3,
        api_mocker=None,
        use_auth: bool = False,
        dual_pass: bool = False,
        hide_username: str = "",
        mocker: FrontendMocker = None,
    ) -> None:
        """
        Discover and clone all internal links from the main page.
        max_pages: maximum number of subpages to clone.
        deep_crawl: if active, recursively extracts the full site map first.
        """
        self._is_running = True
        output_dir = Path(output_dir)

        parsed_base = urlparse(base_url)
        base_domain = parsed_base.netloc

        main_path = parsed_base.path.rstrip("/") or "/"
        self._scraped_pages[main_path] = "index.html"
        self._saved_urls = set(captured_resources.keys())

        # Extract URL map
        internal_links = {}
        if deep_crawl:
            self.log_message.emit(f"🗺️ SITE MAP MODE: Crawling up to depth {max_depth}...")
            self._sitemap_gen = SitemapGenerator()
            self._sitemap_gen.log_message.connect(lambda msg: self.log_message.emit(msg))
            self._sitemap_gen.progress_updated.connect(self.progress_updated.emit)
            internal_links = await self._sitemap_gen.generate(base_url, max_pages, max_depth)
            self._sitemap_gen = None
            if not self._is_running:
                return
        else:
            # Only links from the main page
            self.log_message.emit("🔍 Discovering internal links from main page only (Fast Mode)...")
            internal_links = self._discover_internal_links(main_html, base_url, base_domain)

        # Smart prioritization: structural pages first, game pages last
        structural, game_pages = self._prioritize_links(internal_links)

        # ── (Phase 9) IFRAME Discovery Scan ──
        iframe_links = self._discover_iframes(main_html, base_url)
        # ─────────────────────────────────────

        # Collect all candidate pages into a single pool
        # Priority: Structural > Game > Iframes
        all_candidates = dict(structural)
        for p, u in game_pages.items():
            if p not in all_candidates:
                all_candidates[p] = u
        for p, u in iframe_links.items():
            if p not in all_candidates:
                all_candidates[p] = u

        total_discovered = len(all_candidates)

        # Limit checks (remaining limit after index.html is already saved)
        if max_pages > 0:
            limit = max_pages - 1 # index.html already saved
            if len(all_candidates) > limit:
                # Trim to limit
                items = list(all_candidates.items())[:limit]
                all_links = dict(items)
                skipped = total_discovered - len(all_links)
                self.log_message.emit(f"⏩ {skipped} pages skipped (within max_pages={max_pages} limit)")
            else:
                all_links = all_candidates
        else:
            # Unlimited
            all_links = all_candidates
            self.log_message.emit("⚡ UNLIMITED DOWNLOAD ACTIVE. ALL found pages will be downloaded.")

        if not all_links:
            self.log_message.emit("ℹ️  No additional internal links found")
            await self._rewrite_all_links(output_dir, base_url, base_domain)
            self.all_pages_finished.emit(1) # index.html only
            self._is_running = False
            return

        from core.state_manager import StateManager
        state_mgr = StateManager(output_dir / "crawler_state.db")
        state_mgr.reset_processing()

        # Load previously discovered pages into memory
        visited_pages = state_mgr.get_all_visited()
        self._scraped_pages.update(visited_pages)

        # ── LAUNCH SINGLE BROWSER ──
        self.log_message.emit("🚀 Starting shared browser...")
        playwright = await async_playwright().start()
        browser = await playwright.chromium.launch(
            headless=True,
            args=["--disable-blink-features=AutomationControlled", "--no-sandbox"]
        )

        shared_asset_mgr = AssetManager(output_dir, mocker=mocker)
        shared_asset_mgr.log_message.connect(
            lambda msg: self.log_message.emit(f"   {msg}")
        )
        # Write original main page resources to disk and free memory!
        self.log_message.emit("🔥 Initial resources saving to disk to free core RAM...")
        await shared_asset_mgr.save_resources(captured_resources, content_types)
        captured_resources.clear()
        content_types.clear()

        try:
            total_pages = len(all_links)
            self.total_pages_detected.emit(total_pages)
            semaphore = asyncio.Semaphore(5)

            async def scrape_task(idx, url_path, full_url):
                async with semaphore:
                    if not self._is_running:
                        return

                    real_idx = idx + 1 # 1-based for UI
                    if state_mgr.is_visited(full_url):
                        self.page_progress.emit(real_idx, total_pages)
                        self.log_message.emit(f"⏭️  [{real_idx}/{total_pages}] {url_path} (Crashed session recovery: Skipped)")
                        return

                    state_mgr.add_url(full_url)
                    state_mgr.get_next_url() # Processing

                    # If this is an iframe, save to dedicated folder
                    if url_path.startswith("/iframes/"):
                        local_filename = url_path.lstrip("/")
                        self._scraped_pages[full_url] = local_filename
                    else:
                        local_filename = self._path_to_filename(url_path)
                        self._scraped_pages[url_path] = local_filename

                    self.page_progress.emit(real_idx, total_pages)
                    self.progress_updated.emit(int((real_idx / total_pages) * 100))
                    self.log_message.emit(f"📄 [{real_idx}/{total_pages}] {url_path}")

                    max_retries = 3
                    for attempt_num in range(max_retries):
                        try:
                            await self._scrape_subpage(
                                browser=browser,
                                url=full_url,
                                output_dir=output_dir,
                                local_filename=local_filename,
                                shared_asset_mgr=shared_asset_mgr,
                                api_mocker=api_mocker,
                                use_auth=use_auth,
                                dual_pass=dual_pass,
                                hide_username=hide_username,
                            )
                            state_mgr.mark_visited(full_url, local_filename)
                            self.page_cloned.emit(full_url, local_filename)
                            break
                        except Exception as e:
                            if attempt_num < max_retries - 1:
                                wait_time = 2 ** attempt_num
                                self.log_message.emit(
                                    f"   🔄 Retrying [{attempt_num + 1}/{max_retries}] "
                                    f"({url_path}) — waiting {wait_time}s..."
                                )
                                await asyncio.sleep(wait_time)
                            else:
                                state_mgr.mark_failed(full_url)
                                self.log_message.emit(f"   ❌ Failed ({url_path}): {e}")

            if all_links:
                tasks = [scrape_task(idx, u_p, f_u) for idx, (u_p, f_u) in enumerate(all_links.items(), 1)]
                await asyncio.gather(*tasks)

            if not self._is_running:
                self.log_message.emit("🛑 Stopped")

            # ── LOGIN / REGISTER MODAL CAPTURE ──
            self.log_message.emit("")
            self.log_message.emit("🔐 Capturing Login/Register modals...")
            modal_result = {'files': [], 'fragments': {}}
            try:
                modal_capturer = ModalCapturer()
                modal_capturer.log_message.connect(
                    lambda msg: self.log_message.emit(f"  {msg}")
                )

                # Open main page in a new context
                ctx = await browser.new_context(
                    viewport={"width": 1920, "height": 1080}
                )
                stealth = Stealth()
                await stealth.apply_stealth_async(ctx)
                modal_page = await ctx.new_page()
                await modal_page.goto(base_url, wait_until="domcontentloaded", timeout=30000)
                try:
                    await modal_page.wait_for_load_state("networkidle", timeout=8000)
                except Exception:
                    pass
                await asyncio.sleep(3)

                modal_result = await modal_capturer.capture_modals(
                    page=modal_page,
                    base_url=base_url,
                    output_dir=output_dir,
                    shared_asset_mgr=shared_asset_mgr,
                )

                created = modal_result.get('files', [])
                for fname in created:
                    self._scraped_pages[f"/{fname.replace('.html', '')}"] = fname
                    self.page_cloned.emit(base_url, fname)

                if created:
                    self.log_message.emit(f"✅ {len(created)} modal(s) captured: {', '.join(created)}")
                else:
                    self.log_message.emit("ℹ️  No modals found")

                await ctx.close()
            except Exception as e:
                self.log_message.emit(f"⚠️  Modal capture error: {e}")

        finally:
            try:
                await browser.close()
                await playwright.stop()
            except Exception:
                pass

        # ── Final CSS file correction ──
        self.log_message.emit("🎨 Rewriting all CSS files...")
        await shared_asset_mgr.rewrite_css_files(base_url)

        # ── Update links ──
        self.log_message.emit("🔗 Updating all HTML links...")
        await self._rewrite_all_links(output_dir, base_url, base_domain)

        # ── Inject modal popups into HTML pages ──
        fragments = modal_result.get('fragments', {})
        if fragments:
            self.log_message.emit("🔌 Injecting modal popups into HTML pages...")
            self._inject_modals_into_html(output_dir, fragments)
            self.log_message.emit("✅ Modal popups injected")

        html_files = list(output_dir.glob("*.html"))

        # ── Inject Mock API Engine (Phase 9) ──
        if api_mocker and api_mocker.captured_count > 0:
            self.log_message.emit(f"🧩 Embedding Mock API Engine with {api_mocker.captured_count} routes into pages...")
            await asyncio.to_thread(api_mocker.inject_mock_script, html_files)

        # ── Inject Fake Interaction Engine (Phase 8) ──
        try:
            self.log_message.emit("🎭 Adding Interaction Engine script to pages...")
            await asyncio.to_thread(InteractionEngine.inject_into_html, html_files)
        except Exception as e:
            self.log_message.emit(f"   ⚠️ Interaction Engine Injection Error: {e}")

        # ── Generate Sitemap ──
        try:
            import os
            from datetime import datetime
            out_path = os.path.join(output_dir, "sitemap.xml")
            with open(out_path, "w", encoding="utf-8") as f:
                f.write('<?xml version="1.0" encoding="UTF-8"?>\n')
                f.write('<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n')
                today = datetime.now().strftime("%Y-%m-%d")
                for path, local in self._scraped_pages.items():
                    # Build full URL from base_url and path
                    full_url = urljoin(base_url, path)
                    if not any(full_url.endswith(e) for e in self.SKIP_EXTENSIONS) and "/iframes/" not in path:
                        f.write("  <url>\n")
                        f.write(f"    <loc>{full_url}</loc>\n")
                        f.write(f"    <lastmod>{today}</lastmod>\n")
                        f.write("    <changefreq>daily</changefreq>\n")
                        f.write("    <priority>0.8</priority>\n")
                        f.write("  </url>\n")
                f.write("</urlset>\n")
            self.log_message.emit(f"✅ sitemap.xml created: {out_path}")
        except Exception as e:
            self.log_message.emit(f"⚠️ sitemap.xml could not be written: {e}")

        self.log_message.emit(
            f"✅ Cloning complete: {len(self._scraped_pages)} pages"
        )
        self.all_pages_finished.emit(len(self._scraped_pages))
        self._is_running = False

    def stop(self):
        self._is_running = False
        if self._sitemap_gen:
            self._sitemap_gen.stop()

    # ──────────────────────────────────────────────
    #  SUBPAGE CLONING
    # ──────────────────────────────────────────────

    async def _scrape_subpage(
        self,
        browser,
        url: str,
        output_dir: Path,
        local_filename: str,
        shared_asset_mgr: AssetManager,
        api_mocker=None,
        use_auth: bool = False,
        dual_pass: bool = False,
        hide_username: str = "",
    ) -> None:
        """Clone a subpage: Desktop and optionally Mobile (Dual-Pass) double pass."""

        context_args = {
            "viewport": {"width": 1920, "height": 1080},
            "user_agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
            "java_script_enabled": True,
        }

        auth_path = output_dir / "auth_state.json"
        if use_auth and auth_path.exists():
            context_args["storage_state"] = str(auth_path)

        context = await browser.new_context(**context_args)

        stealth = Stealth()
        await stealth.apply_stealth_async(context)

        page = await context.new_page()
        new_resources: dict[str, bytes] = {}
        new_content_types: dict[str, str] = {}

        # Features allowed from CDN (CSS, Font, Media, etc.)
        CDN_EXTENSIONS = {
            ".css", ".woff", ".woff2", ".ttf", ".png", ".jpg", ".jpeg",
            ".svg", ".gif", ".webp", ".mp4", ".webm", ".js",
            ".m3u8", ".ts", ".ico", ".apng", ".avif"
        }
        parsed_base = urlparse(url)
        base_domain = parsed_base.netloc

        # ── Network Manager & Tracker Blocker ──
        TRACKER_DOMAINS = {
            "google-analytics.com", "googletagmanager.com", "doubleclick.net",
            "facebook.net", "facebook.com/tr/", "yandex.ru/metrika", "mc.yandex.ru",
            "hotjar.com", "clarity.ms", "tawk.to", "smartsupp.com", "tidio.com"
        }

        async def route_handler(route):
            url = route.request.url
            if any(t in url.lower() for t in TRACKER_DOMAINS):
                await route.abort()
            else:
                await route.continue_()

        await page.route("**/*", route_handler)

        async def on_response(response):
            try:
                rt = response.request.resource_type
                if rt not in {"stylesheet", "script", "image", "font", "media", "other", "fetch", "xhr"}:
                    return
                res_url = response.url
                if res_url.startswith("data:") or res_url in self._saved_urls:
                    return

                parsed_res = urlparse(res_url)
                ext = Path(parsed_res.path).suffix.lower()

                if ext in {".html", ".htm", ".php"}:
                    return

                # External domain CDN check
                if parsed_res.netloc and parsed_res.netloc != base_domain:
                    # Loose check: media/image/font always allowed
                    if rt in ("image", "media", "font") or ext in CDN_EXTENSIONS or "css" in res_url.lower():
                        pass
                    else:
                        return

                if not (200 <= response.status < 400):
                    return
                body = await response.body()
                if body and len(body) > 0:
                    ct = ""
                    try:
                        headers = await response.all_headers()
                        ct = headers.get("content-type", "")
                    except Exception:
                        pass

                    # JSON mock
                    if "application/json" in ct.lower() or ext == ".json":
                        if api_mocker:
                            api_mocker.save_api_response(res_url, body)

                    new_resources[res_url] = body
                    self._saved_urls.add(res_url)
                    new_content_types[res_url] = ct
            except Exception:
                pass

        page.on("response", on_response)

        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=30000)
            try:
                await page.wait_for_load_state("networkidle", timeout=self.NETWORK_IDLE_TIMEOUT)
            except Exception:
                pass

            # ── Preloader/Intro Bypass ──
            await self._wait_for_real_content(page)

            # Lazy scroll
            await self._scroll_page(page)

            # data-src conversion
            await self._convert_lazy_attrs(page)

            # ── CSS INLINE (SingleFile-like) ──
            await self._inline_css(page)

            # ── Background images (Computed) ──
            await self._capture_bg_images(page, new_resources)

            # ── Aggressive Media Downloader (DOM Scan) ──
            await self._aggressive_media_download(page, new_resources, new_content_types)

            # ── Phase 10: Deep UI Clicker ──
            await self._click_all_interactives(page)

            # networkidle final wait
            try:
                await page.wait_for_load_state("networkidle", timeout=5000)
            except Exception:
                pass

            # Get DOM
            html = await page.content()

        finally:
            await context.close()

        # Save new resources (write once to disk and clear from RAM to avoid memory bloat)
        if new_resources:
            saved = await shared_asset_mgr.save_resources(new_resources, new_content_types)
            self.log_message.emit(f"   💾 {len(saved)} new resource(s)")
            new_resources.clear()
            new_content_types.clear()
        else:
            self.log_message.emit("   💾 No new resources")

        # Rewrite HTML + save (offload to thread to avoid blocking UI)
        rewritten = await asyncio.to_thread(shared_asset_mgr.rewrite_html, html, url, local_filename, hide_username)
        await asyncio.to_thread(shared_asset_mgr.save_html, rewritten, local_filename)
        self.log_message.emit(f"   ✅ {local_filename}")

        # ── 2. Mobile Pass (Dual Pass) ──
        if dual_pass and self._is_running:
            self.log_message.emit(f"   📱 Second Pass (Mobile): {url}")
            context_args_mobile = {
                "viewport": {"width": 430, "height": 932}, # iPhone 14 Pro Max
                "user_agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 16_6 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.6 Mobile/15E148 Safari/604.1",
                "device_scale_factor": 3,
                "is_mobile": True,
                "has_touch": True,
                "java_script_enabled": True,
            }
            if use_auth and auth_path.exists():
                context_args_mobile["storage_state"] = str(auth_path)

            playwright_m = await async_playwright().start()
            try:
                browser_m = await playwright_m.chromium.launch(
                    headless=True,
                    args=["--disable-blink-features=AutomationControlled", "--no-sandbox"]
                )
                context_m = await browser_m.new_context(**context_args_mobile)
                stealth_m = Stealth()
                await stealth_m.apply_stealth_async(context_m)

                page_m = await context_m.new_page()

                # Network Listener Setup
                await self._setup_network_listener(
                    page_m,
                    shared_asset_mgr,
                    base_url=url, # 'url' used instead of base_url
                    api_mocker=api_mocker,
                    new_res=new_resources,
                    new_ct=new_content_types
                )

                await page_m.goto(url, wait_until="domcontentloaded", timeout=60000)

                try:
                    await page_m.wait_for_load_state("networkidle", timeout=10000)
                except Exception:
                    pass

                await self._scroll_page(page_m) # _scroll_to_bottom -> _scroll_page
                await self._convert_lazy_attrs(page_m) # _convert_lazy_attributes -> _convert_lazy_attrs
                await self._inline_css(page_m)
                await self._freeze_canvases(page_m)

                # Trigger interactive elements on mobile too
                await self._click_all_interactives(page_m)

                html_mobile = await page_m.content()

                await context_m.close()
                await browser_m.close()

                # Save new mobile resources
                if new_resources:
                    saved = await shared_asset_mgr.save_resources(new_resources, new_content_types)
                    new_resources.clear()
                    new_content_types.clear()

                # Save mobile HTML
                mobile_filename = str(local_filename).replace(".html", "_mobile.html")
                rewritten_mobile = await asyncio.to_thread(shared_asset_mgr.rewrite_html, html_mobile, url, mobile_filename, hide_username)
                await asyncio.to_thread(shared_asset_mgr.save_html, rewritten_mobile, mobile_filename)
                self.log_message.emit(f"   ✅ [Mobile] {mobile_filename}")
            finally:
                await playwright_m.stop()

    # ──────────────────────────────────────────────
    #  CSS INLINE (SingleFile-like)
    # ──────────────────────────────────────────────

    async def _inline_css(self, page) -> None:
        """Embed ALL CSS rules from the page as a <style> block in <head>."""
        inline_script = """
        () => {
            let allRules = [];
            try {
                for (let sheet of document.styleSheets) {
                    try {
                        let rules = sheet.cssRules || sheet.rules;
                        if (!rules) continue;
                        for (let rule of rules) {
                            allRules.push(rule.cssText);
                        }
                    } catch (e) {}
                }
            } catch (e) {}
            if (allRules.length === 0) return 0;

            let styleEl = document.createElement('style');
            styleEl.id = 'cloned-inline-styles';
            styleEl.textContent = allRules.join('\\n');
            document.head.appendChild(styleEl);
            return allRules.length;
        }
        """
        try:
            count = await page.evaluate(inline_script)
            self.log_message.emit(f"   🎨 {count} CSS rules inlined")
        except Exception as e:
            self.log_message.emit(f"   ⚠️  CSS inline error: {e}")

    async def _convert_lazy_attrs(self, page) -> None:
        """data-src → src conversion."""
        script = """
        () => {
            let c = 0;
            document.querySelectorAll('[data-src]').forEach(el => {
                let s = el.getAttribute('data-src');
                if (s && s.trim()) { el.setAttribute('src', s); el.removeAttribute('data-src'); c++; }
            });
            document.querySelectorAll('[data-srcset]').forEach(el => {
                let s = el.getAttribute('data-srcset');
                if (s && s.trim()) { el.setAttribute('srcset', s); el.removeAttribute('data-srcset'); c++; }
            });
            document.querySelectorAll('[data-bg]').forEach(el => {
                let b = el.getAttribute('data-bg');
                if (b && b.trim()) { el.style.backgroundImage = `url('${b}')`; el.removeAttribute('data-bg'); c++; }
            });
            document.querySelectorAll('[loading="lazy"]').forEach(el => el.removeAttribute('loading'));
            return c;
        }
        """
        try:
            await page.evaluate(script)
        except Exception:
            pass

    async def _capture_bg_images(self, page, resources: dict) -> None:
        """Capture computed background-image URLs (including pseudo-elements and SVG)."""
        script = """
        () => {
            let urls = [];
            const checkBg = (bg) => {
                if (bg && bg !== 'none' && bg.includes('url(')) {
                    let m = bg.match(/url\\(["']?(.*?)["']?\\)/g);
                    if (m) {
                        m.forEach(x => {
                            let u = x.replace(/^url\\(["']?/, '').replace(/["']?\\)$/, '');
                            if (u && !u.startsWith('data:')) urls.push(u);
                        });
                    }
                }
            };
            for (let el of document.querySelectorAll('*')) {
                checkBg(getComputedStyle(el).backgroundImage);
                checkBg(getComputedStyle(el, '::before').backgroundImage);
                checkBg(getComputedStyle(el, '::after').backgroundImage);
            }
            return [...new Set(urls)];
        }
        """
        try:
            bg_urls = await page.evaluate(script)
            if bg_urls:
                import asyncio

                async def fetch_bg(url):
                    if url in self._saved_urls or url in resources:
                        return None
                    try:
                        resp = await page.context.request.get(url, timeout=10000, ignore_https_errors=True)
                        if resp.ok:
                            body = await resp.body()
                            return url, body
                    except Exception:
                        pass
                    return None

                tasks = [fetch_bg(u) for u in bg_urls]
                results = await asyncio.gather(*tasks)

                for res in results:
                    if res:
                        u, req_body = res
                        resources[u] = req_body
                        self._saved_urls.add(u)
        except Exception:
            pass

    async def _aggressive_media_download(self, page, resources: dict, content_types: dict) -> None:
        """
        Method that finds all img, picture, source, and inline style backgrounds
        in the DOM and aggressively downloads them via Playwright fetch. (To bypass lazy-load)
        """
        script = """
        () => {
            let urls = new Set();
            document.querySelectorAll('*').forEach(el => {
                // Known image/media attributes
                ['src', 'poster', 'href', 'xlink:href', 'data-src', 'data-lazy', 'data-original', 'data-lazy-src', 'data-bg', 'data-background', 'data-image'].forEach(attr => {
                    let val = el.getAttribute(attr);
                    if (val && !val.startsWith('data:')) urls.add(val.trim());
                });

                // srcset handling
                let srcset = el.getAttribute('srcset') || el.getAttribute('data-srcset');
                if (srcset) {
                    srcset.split(',').forEach(s => {
                        let url = s.trim().split(' ')[0];
                        if (url && !url.startsWith('data:')) urls.add(url);
                    });
                }

                // Computed background image (catches CSS classes)
                const cs = window.getComputedStyle(el);
                const bg = cs.backgroundImage;
                if (bg && bg !== 'none' && bg.includes('url(')) {
                    const m = bg.match(/url\\(["']?([^"')]+)["']?\\)/g);
                    if (m) {
                        m.forEach(x => {
                            let u = x.replace(/^url\\(["']?/, '').replace(/["']?\\)$/, '');
                            if (u && !u.startsWith('data:')) urls.add(u);
                        });
                    }
                }
            });
            return Array.from(urls);
        }
        """
        try:
            urls = await page.evaluate(script)

            # Download only those not previously found
            download_targets = [u for u in urls if u not in self._saved_urls and u not in resources]
            if download_targets:
                self.log_message.emit(f"   🔥 Aggressive Downloader: Downloading {len(download_targets)} hidden media from DOM via Python...")

                async def fetch_media(url):
                    try:
                        # Use Playwright Node context request to bypass CORS issues instead of JS fetch
                        resp = await page.context.request.get(url, timeout=10000, ignore_https_errors=True)
                        if resp.ok:
                            body = await resp.body()
                            ct = resp.headers.get("content-type", "application/octet-stream")
                            return url, ct, body
                    except Exception:
                        pass
                    return None

                # Download in parallel
                import asyncio
                tasks = [fetch_media(u) for u in download_targets]
                results = await asyncio.gather(*tasks)

                for res in results:
                    if res:
                        u, ct, req_body = res
                        resources[u] = req_body
                        content_types[u] = ct
                        self._saved_urls.add(u)
        except Exception as e:
            self.log_message.emit(f"   ⚠️ Aggressive Downloader Error: {str(e)[:50]}")

    async def _scroll_page(self, page) -> None:
        """Subpage lazy load scroll."""
        prev = 0
        for _ in range(self.MAX_SCROLL_ATTEMPTS):
            if not self._is_running:
                break
            await page.evaluate(f"window.scrollBy({{top:{self.SCROLL_STEP_PX},behavior:'smooth'}})")
            await asyncio.sleep(self.SCROLL_DELAY_MS / 1000)
            metrics = await page.evaluate("() => ({h: document.body.scrollHeight, p: window.scrollY + window.innerHeight})")
            h, p = metrics["h"], metrics["p"]
            if p >= h - 5 and h == prev:
                await asyncio.sleep(0.8)
                if await page.evaluate("document.body.scrollHeight") == h:
                    break
            prev = h

    async def _wait_for_real_content(self, page) -> None:
        """Preloader/Intro bypass: body visible + loader hidden."""
        try:
            await page.wait_for_selector("body", state="visible", timeout=10000)
        except Exception:
            pass

        LOADER_SELECTORS = [
            "[class*='preloader']", "[id*='preloader']",
            "[class*='loader']", "[id*='loader']",
            "[class*='spinner']", "[id*='spinner']",
            "[class*='loading']", "[id*='loading']",
            "[class*='splash']", "[id*='splash']",
            "[class*='intro-screen']", "[id*='intro']",
            ".page-loader", ".site-loader", ".fullscreen-loader",
        ]

        for selector in LOADER_SELECTORS:
            try:
                el = await page.query_selector(selector)
                if el and await el.is_visible():
                    try:
                        await page.wait_for_selector(selector, state="hidden", timeout=10000)
                    except Exception:
                        try:
                            await page.evaluate(f"""
                                document.querySelectorAll('{selector}').forEach(el => {{
                                    el.remove();
                                }});
                            """)
                        except Exception:
                            pass
            except Exception:
                continue

        # Full-screen overlay cleanup
        try:
            await page.evaluate("""
                document.querySelectorAll('*').forEach(el => {
                    const style = getComputedStyle(el);
                    if (style.position === 'fixed' && parseInt(style.zIndex) > 9000 &&
                        el.offsetWidth >= window.innerWidth * 0.8 &&
                        el.offsetHeight >= window.innerHeight * 0.8) {
                        const cls = (el.className || '').toLowerCase();
                        const id = (el.id || '').toLowerCase();
                        if (cls.includes('loader') || cls.includes('loading') || cls.includes('spinner') ||
                            cls.includes('splash') || cls.includes('preload') ||
                            id.includes('loader') || id.includes('loading') || id.includes('preload')) {
                            el.remove();
                        }
                    }
                });
            """)
        except Exception:
            pass

    async def _click_all_interactives(self, page) -> None:
        """(Phase 10) Click all tabs and dropdowns on the page."""
        script = """
        async () => {
            const selectors = [
                '.dropdown-toggle', '[data-bs-toggle="dropdown"]', '[data-toggle="dropdown"]',
                '[role="tab"]', '[data-bs-toggle="tab"]', '[data-toggle="tab"]',
                '.nav-link:not([href])', '.nav-link[href="#"]', '.nav-link[href^="#"]',
                '.tab-item', '.tabs li', 'button.accordion-button'
            ];
            let count = 0;
            for (const sel of selectors) {
                const els = document.querySelectorAll(sel);
                for (let el of els) {
                    try {
                        const rect = el.getBoundingClientRect();
                        if (rect.width > 0 && rect.height > 0) {
                            // Temporarily change href to prevent link navigation
                            const oldHref = el.getAttribute('href');
                            if (el.tagName.toLowerCase() === 'a') {
                                el.setAttribute('href', 'javascript:void(0)');
                            }

                            el.click();
                            count++;
                            await new Promise(r => setTimeout(r, 250)); // UI animation + Fetch API wait

                            if (el.tagName.toLowerCase() === 'a' && oldHref !== null) {
                                el.setAttribute('href', oldHref);
                            }
                        }
                    } catch(e) {}
                }
            }
            return count;
        }
        """
        try:
            count = await page.evaluate(script)
            if count and count > 0:
                self.log_message.emit(f"   🤖 Deep UI Clicker: {count} menu(s)/tab(s) triggered (Fetching data)")
        except Exception as e:
            self.log_message.emit(f"   ⚠️ Deep UI Clicker Error: {str(e)[:50]}")

    async def _freeze_canvases(self, page) -> None:
        """Freeze all <canvas> elements on the page (for WebGL/Slot games)."""
        try:
            canvases = await page.query_selector_all("canvas")
            if not canvases:
                return

            self.log_message.emit(f"   🧊 Freezing {len(canvases)} Canvas element(s)...")
            frozen_count = 0
            import base64

            for canvas in canvases:
                is_visible = await canvas.is_visible()
                if not is_visible: continue

                img_bytes = await canvas.screenshot(type="png")
                b64_img = base64.b64encode(img_bytes).decode('utf-8')
                data_url = f"data:image/png;base64,{b64_img}"

                await page.evaluate(f'''(canvas) => {{
                    let img = document.createElement('img');
                    img.src = "{data_url}";
                    img.style.cssText = canvas.style.cssText;
                    img.style.width = canvas.offsetWidth + 'px';
                    img.style.height = canvas.offsetHeight + 'px';

                    let pos = window.getComputedStyle(canvas).position;
                    img.style.position = (pos === 'static') ? 'relative' : pos;
                    img.style.top = canvas.style.top;
                    img.style.left = canvas.style.left;
                    img.style.zIndex = canvas.style.zIndex;
                    img.className = canvas.className;
                    img.setAttribute('data-frozen-canvas', 'true');

                    canvas.parentNode.insertBefore(img, canvas);
                    canvas.style.display = 'none';
                    canvas.setAttribute('data-frozen', 'true');
                }}''', canvas)
                frozen_count += 1

            if frozen_count > 0:
                self.log_message.emit(f"   ❄️ {frozen_count} Canvas(es) frozen.")
        except Exception as e:
            pass

    # ──────────────────────────────────────────────
    #  LINK DISCOVERY + PRIORITIZATION
    # ──────────────────────────────────────────────

    # Regex patterns for link/iframe discovery (no HTML parser needed)
    _A_HREF_RE = re.compile(
        r'<a\b[^>]*\bhref=["\']([^"\']+)["\']',
        re.IGNORECASE,
    )
    _IFRAME_SRC_RE = re.compile(
        r'<iframe\b[^>]*\bsrc=["\']([^"\']+)["\']',
        re.IGNORECASE,
    )
    _SPA_PATH_RE = re.compile(r'["\'](/[a-zA-Z0-9_\-\./]+)["\']')

    def _discover_internal_links(
        self, html: str, base_url: str, base_domain: str
    ) -> dict[str, str]:
        links: dict[str, str] = {}

        def _process_href(href: str) -> None:
            href = href.strip()
            if "#" in href:
                href = href.split("#")[0]
            if not href or any(href.startswith(p) for p in self.SKIP_PATTERNS):
                return
            full_url = urljoin(base_url, href)
            parsed = urlparse(full_url)
            if parsed.netloc and parsed.netloc != base_domain:
                return
            ext = Path(parsed.path).suffix.lower()
            if ext in self.SKIP_EXTENSIONS:
                return
            clean_path = parsed.path.rstrip("/") or "/"
            if len(clean_path) < 2:
                return
            if clean_path not in links and clean_path not in self._scraped_pages:
                links[clean_path] = full_url

        for m in self._A_HREF_RE.finditer(html):
            _process_href(m.group(1))

        # Dynamic/SPA Link Discovery: relative paths inside JavaScript or ng-href etc.
        for m in self._SPA_PATH_RE.finditer(html):
            p = m.group(1)
            full_url = urljoin(base_url, p)
            parsed = urlparse(full_url)
            if parsed.netloc and parsed.netloc != base_domain:
                continue
            ext = Path(parsed.path).suffix.lower()
            if ext and ext not in {'.html', '.htm', '.php', '.asp', '.aspx', '.jsp'}:
                continue
            if '/api/' in parsed.path.lower() or '/v1/' in parsed.path.lower() or '/iframes/' in parsed.path.lower():
                continue
            clean_path = parsed.path.rstrip("/") or "/"
            if len(clean_path) >= 2 and clean_path not in links and clean_path not in self._scraped_pages:
                links[clean_path] = full_url

        return links

    def _discover_iframes(self, html: str, base_url: str) -> dict[str, str]:
        """Find all iframe src values on the page."""
        iframes: dict[str, str] = {}
        tracking = ["zendesk", "tawk", "google", "facebook", "yandex", "crisp"]

        for m in self._IFRAME_SRC_RE.finditer(html):
            src = m.group(1).strip()
            if not src or "javascript:" in src or "about:" in src:
                continue
            full_url = urljoin(base_url, src)
            if any(t in full_url.lower() for t in tracking):
                continue
            url_hash = hashlib.md5(full_url.encode()).hexdigest()[:8]
            iframes[f"/iframes/frame_{url_hash}.html"] = full_url

        return iframes

    def _prioritize_links(
        self, links: dict[str, str]
    ) -> tuple[dict[str, str], dict[str, str]]:
        """Structural pages (casino, sport, etc.) first, game pages last."""
        structural: dict[str, str] = {}
        game_pages: dict[str, str] = {}

        for path, url in links.items():
            is_game = any(pat in path for pat in self.GAME_PATTERNS)
            if is_game:
                game_pages[path] = url
            else:
                structural[path] = url

        return structural, game_pages

    # ──────────────────────────────────────────────
    #  LINK REWRITING
    # ──────────────────────────────────────────────

    async def _rewrite_all_links(
        self, output_dir: Path, base_url: str, base_domain: str
    ) -> None:
        html_files = list(output_dir.glob("*.html"))
        self.log_message.emit(f"🔗 Updating links in {len(html_files)} HTML file(s)...")
        for html_file in html_files:
            try:
                content = html_file.read_text(encoding="utf-8", errors="replace")
                # Offload parser to async thread
                updated = await asyncio.to_thread(self._rewrite_links_in_html, content, base_url, base_domain)
                html_file.write_text(updated, encoding="utf-8")
            except Exception as e:
                self.log_message.emit(f"   ⚠️  {html_file.name}: {e}")
        self.log_message.emit("✅ Links updated")

    def _rewrite_links_in_html(
        self, html: str, base_url: str, base_domain: str
    ) -> str:
        """Rewrite <a href> and <iframe src> using regex — no HTML parser,
        so SVG paths, CSS custom properties, and Tailwind JIT class names
        are never corrupted."""

        def _map_href(href: str) -> str:
            href = href.strip()
            if not href or href.startswith("#"):
                return href
            if href.endswith(".html") and not href.startswith("http"):
                return href
            if any(href.startswith(p) for p in self.SKIP_PATTERNS):
                return "#" if href.startswith("javascript:") else href
            full_url = urljoin(base_url, href)
            parsed = urlparse(full_url)
            if full_url in self._scraped_pages:
                return self._scraped_pages[full_url]
            if parsed.netloc == base_domain or not parsed.netloc:
                if parsed.path.startswith("/api/") or parsed.path.startswith("/v1/"):
                    return "#"
                clean_path = parsed.path.rstrip("/") or "/"
                return self._scraped_pages.get(clean_path, "#")
            return "#"

        def _replace_a_href(m: re.Match) -> str:
            return m.group(1) + _map_href(m.group(2)) + m.group(3)

        def _replace_iframe_src(m: re.Match) -> str:
            src = m.group(2).strip()
            full_url = urljoin(base_url, src)
            if full_url in self._scraped_pages:
                return m.group(1) + self._scraped_pages[full_url] + m.group(3)
            return m.group(0)

        # <a href="..."> and <a href='...'>
        html = re.sub(r'(<a\b[^>]*\bhref=")([^"]*?)(")', _replace_a_href, html)
        html = re.sub(r"(<a\b[^>]*\bhref=')([^']*?)(')", _replace_a_href, html)

        # <iframe src="..."> and <iframe src='...'>
        html = re.sub(r'(<iframe\b[^>]*\bsrc=")([^"]*?)(")', _replace_iframe_src, html)
        html = re.sub(r"(<iframe\b[^>]*\bsrc=')([^']*?)(')", _replace_iframe_src, html)

        return html

    # ──────────────────────────────────────────────
    #  MODAL POPUP INJECTION
    # ──────────────────────────────────────────────

    def _inject_modals_into_html(
        self, output_dir: Path, fragments: dict
    ) -> None:
        """
        Inject modal popups into cloned HTML pages.

        fragments: {
            'login':    {'html': '...', 'is_popup': True, 'button_text': '...', ...},
            'register': {'html': '...', 'is_popup': True, 'button_text': '...', ...},
        }
        """
        # Only inject popup-type modals
        popup_fragments = {
            k: v for k, v in fragments.items() if v.get('is_popup', True)
        }
        if not popup_fragments:
            self.log_message.emit("   ℹ️  No popup-type modals, skipping injection")
            return

        # Build modal overlay HTML + show/hide JS
        modal_divs = ""
        for modal_name, frag in popup_fragments.items():
            modal_id = f"cloner-{modal_name}-modal"
            modal_divs += f"""
<!-- CLONER MODAL: {modal_name} -->
<div id="{modal_id}" class="cloner-modal-overlay" style="display:none;position:fixed;inset:0;z-index:99999;background:rgba(234,238,250,0.3);backdrop-filter:blur(12px);-webkit-backdrop-filter:blur(12px);overflow-y:auto;align-items:center;justify-content:center;padding:20px;">
<div class="cloner-modal-inner" style="margin:auto;position:relative;">
{frag['html']}
</div>
</div>
"""

        # Button matching + popup JS
        popup_js = """
<style>
.cloner-modal-overlay { display:none; }
.cloner-modal-overlay[data-visible="true"] {
    display:flex !important;
}
.cloner-modal-inner {
    width: 100%;
    height: 100%;
    display: flex;
    align-items: center;
    justify-content: center;
}
/* PrimeNG p-dialog-mask: neutralize nested fixed overlay */
.cloner-modal-overlay .p-dialog-mask,
.cloner-modal-overlay .p-component-overlay {
    position: relative !important;
    inset: unset !important;
    width: auto !important;
    height: auto !important;
    background: transparent !important;
    display: flex !important;
    align-items: center !important;
    justify-content: center !important;
    overflow: visible !important;
}
/* Generic modal/dialog wrappers */
.cloner-modal-overlay [class*="modal-mask"],
.cloner-modal-overlay [class*="dialog-mask"],
.cloner-modal-overlay [class*="overlay-mask"] {
    position: relative !important;
    inset: unset !important;
    background: transparent !important;
}
/* p-dialog and content overflow fix */
.cloner-modal-overlay .p-dialog,
.cloner-modal-overlay .p-dynamic-dialog {
    overflow: visible !important;
    max-height: none !important;
}
.cloner-modal-overlay .p-dialog-content {
    overflow: visible !important;
    max-height: none !important;
}
/* Auth layout: banner + form two-column layout */
.cloner-modal-overlay .auth {
    overflow: visible !important;
    display: flex !important;
}
.cloner-modal-overlay .auth-banner {
    width: 380px !important;
    min-width: 380px !important;
    max-width: 380px !important;
    flex: 0 0 380px !important;
    overflow: hidden !important;
}
.cloner-modal-overlay .auth-banner img,
.cloner-modal-overlay .auth-banner svg {
    max-width: 100% !important;
    height: auto !important;
}
.cloner-modal-overlay .auth-container {
    flex: 1 1 auto !important;
    min-width: 0 !important;
    overflow-y: auto !important;
    overflow-x: hidden !important;
    max-height: 80vh !important;
    padding: 30px !important;
}
@media (max-width: 768px) {
    .cloner-modal-overlay {
        padding: 0 !important;
    }
    .cloner-modal-overlay .auth {
        flex-direction: column !important;
    }
    .cloner-modal-overlay .auth-banner {
        width: 100% !important;
        min-width: unset !important;
        max-width: 100% !important;
        flex: 0 0 auto !important;
        max-height: 200px !important;
    }
    .cloner-modal-overlay .auth-container {
        max-height: none !important;
    }
}
</style>
<script>
(function() {
    // Modal open/close functions
    function openModal(id) {
        var m = document.getElementById(id);
        if (m) { m.setAttribute('data-visible', 'true'); m.style.display = 'flex'; }
    }
    function closeModal(id) {
        var m = document.getElementById(id);
        if (m) { m.removeAttribute('data-visible'); m.style.display = 'none'; }
    }
    function closeAllModals() {
        document.querySelectorAll('.cloner-modal-overlay').forEach(function(m) {
            m.removeAttribute('data-visible');
            m.style.display = 'none';
        });
    }

    // Click on overlay background → close
    document.querySelectorAll('.cloner-modal-overlay').forEach(function(overlay) {
        overlay.addEventListener('click', function(e) {
            if (e.target === overlay) closeAllModals();
        });
    });

    // Escape key → close
    document.addEventListener('keydown', function(e) {
        if (e.key === 'Escape') closeAllModals();
    });

    // Close buttons (× and ✕ and close class)
    document.querySelectorAll('.cloner-modal-overlay').forEach(function(overlay) {
        overlay.querySelectorAll(
            'button[class*="close"], [class*="close-btn"], button[class*="header-close"]'
        ).forEach(function(btn) {
            btn.addEventListener('click', function(e) {
                e.preventDefault();
                e.stopPropagation();
                closeAllModals();
            });
        });
    });

    // Find and bind login buttons
    var loginTexts = ['giriş', 'giris', 'login', 'sign in', 'signin'];
    var registerTexts = ['üye ol', 'uye ol', 'kayıt', 'kayit', 'register', 'sign up', 'signup'];

    function matchesText(el, textList) {
        var t = (el.textContent || '').trim().toLowerCase();
        for (var i = 0; i < textList.length; i++) {
            if (t === textList[i]) return true;
        }
        return false;
    }

    function wireButtons() {
        // Scan all button and link elements
        var allClickable = document.querySelectorAll(
            'a, button, [role="button"], [class*="login"], [class*="register"], [class*="signin"], [class*="signup"]'
        );

        allClickable.forEach(function(el) {
            // Skip elements inside modal overlay itself
            if (el.closest('.cloner-modal-overlay')) return;

            // Links pointing to login.html or register.html
            var href = (el.getAttribute('href') || '').toLowerCase();

            if (href === 'login.html' || matchesText(el, loginTexts)) {
                if (document.getElementById('cloner-login-modal')) {
                    el.addEventListener('click', function(e) {
                        e.preventDefault();
                        e.stopPropagation();
                        closeAllModals();
                        openModal('cloner-login-modal');
                    });
                    if (el.tagName === 'A') el.setAttribute('href', 'javascript:void(0)');
                }
            }

            if (href === 'register.html' || matchesText(el, registerTexts)) {
                if (document.getElementById('cloner-register-modal')) {
                    el.addEventListener('click', function(e) {
                        e.preventDefault();
                        e.stopPropagation();
                        closeAllModals();
                        openModal('cloner-register-modal');
                    });
                    if (el.tagName === 'A') el.setAttribute('href', 'javascript:void(0)');
                }
            }
        });

        // Cross-links inside modals (e.g., "Register" inside login modal, "Login" inside register modal)
        var loginModal = document.getElementById('cloner-login-modal');
        var regModal = document.getElementById('cloner-register-modal');

        if (loginModal) {
            loginModal.querySelectorAll('a, button').forEach(function(el) {
                if (matchesText(el, registerTexts)) {
                    el.addEventListener('click', function(e) {
                        e.preventDefault(); e.stopPropagation();
                        closeAllModals();
                        openModal('cloner-register-modal');
                    });
                }
            });
        }
        if (regModal) {
            regModal.querySelectorAll('a, button').forEach(function(el) {
                if (matchesText(el, loginTexts)) {
                    el.addEventListener('click', function(e) {
                        e.preventDefault(); e.stopPropagation();
                        closeAllModals();
                        openModal('cloner-login-modal');
                    });
                }
            });
        }
    }

    // Bind buttons when DOM is ready
    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', wireButtons);
    } else {
        wireButtons();
    }
})();
</script>
"""

        injection_block = modal_divs + popup_js

        # Inject into all HTML files
        html_files = list(output_dir.glob("*.html"))
        injected_count = 0
        for html_file in html_files:
            # login.html and register.html are already standalone modals
            if html_file.name in ('login.html', 'register.html'):
                continue
            try:
                content = html_file.read_text(encoding="utf-8", errors="replace")

                # Clear old injection blocks
                import re as _inj_re
                content = _inj_re.sub(
                    r'<!-- CLONER MODAL:.*?</script>\s*',
                    '',
                    content,
                    flags=_inj_re.DOTALL
                )

                # Inject before </body> tag
                if "</body>" in content:
                    content = content.replace(
                        "</body>",
                        injection_block + "\n</body>"
                    )
                elif "</html>" in content:
                    content = content.replace(
                        "</html>",
                        injection_block + "\n</html>"
                    )
                else:
                    content += injection_block

                html_file.write_text(content, encoding="utf-8")
                injected_count += 1
            except Exception as e:
                self.log_message.emit(f"   ⚠️  {html_file.name}: {e}")

        self.log_message.emit(
            f"   🔌 Modal popup injected into {injected_count} HTML file(s)"
        )

    async def _setup_network_listener(
        self,
        page,
        shared_asset_mgr: AssetManager,
        base_url: str,
        api_mocker=None,
        new_res: dict = None,
        new_ct: dict = None
    ) -> None:
        """Sets up tracker blocker and resource capture listeners for a page."""
        TRACKER_DOMAINS = {
            "google-analytics.com", "googletagmanager.com", "doubleclick.net",
            "facebook.net", "facebook.com/tr/", "yandex.ru/metrika", "mc.yandex.ru",
            "hotjar.com", "clarity.ms", "tawk.to", "smartsupp.com", "tidio.com"
        }
        CDN_EXTENSIONS = {
            ".css", ".woff", ".woff2", ".ttf", ".png", ".jpg", ".jpeg",
            ".svg", ".gif", ".webp", ".mp4", ".webm", ".js",
            ".m3u8", ".ts", ".ico", ".apng", ".avif"
        }
        parsed_base = urlparse(base_url)
        base_domain = parsed_base.netloc

        async def route_handler(route):
            url = route.request.url
            if any(t in url.lower() for t in TRACKER_DOMAINS):
                await route.abort()
            else:
                await route.continue_()

        async def on_response(response):
            try:
                rt = response.request.resource_type
                if rt not in {"stylesheet", "script", "image", "font", "media", "other", "fetch", "xhr"}:
                    return
                res_url = response.url
                if res_url.startswith("data:") or res_url in self._saved_urls:
                    return

                parsed_res = urlparse(res_url)
                ext = Path(parsed_res.path).suffix.lower()

                if ext in {".html", ".htm", ".php"}:
                    return

                if parsed_res.netloc and parsed_res.netloc != base_domain:
                    if rt in ("image", "media", "font") or ext in CDN_EXTENSIONS or "css" in res_url.lower():
                        pass
                    else:
                        return

                if not (200 <= response.status < 400):
                    return

                body = await response.body()
                if body and len(body) > 0:
                    ct = ""
                    try:
                        headers = await response.all_headers()
                        ct = headers.get("content-type", "")
                    except Exception:
                        pass

                    if "application/json" in ct.lower() or ext == ".json":
                        if api_mocker:
                            api_mocker.save_api_response(res_url, body)

                    if new_res is not None: new_res[res_url] = body
                    self._saved_urls.add(res_url)
                    if new_ct is not None: new_ct[res_url] = ct
            except Exception:
                pass

        await page.route("**/*", route_handler)
        page.on("response", on_response)

    # ──────────────────────────────────────────────
    #  HELPERS
    # ──────────────────────────────────────────────

    def _path_to_filename(self, url_path: str) -> str:
        clean = url_path.strip("/")
        if not clean:
            return "index.html"
        clean = unquote(clean).replace("/", "_")
        # Strict Slugification (strip invalid characters)
        clean = re.sub(r'[^a-zA-Z0-9_\-\.]', '_', clean)
        clean = clean.strip(". _")
        if not clean:
            return "page.html"
        # Truncate to stay within 255-char OS filename limit (.html = 5 chars)
        if len(clean) > 250:
            clean = clean[:250]
        return f"{clean}.html"

    @property
    def scraped_pages(self) -> dict[str, str]:
        return dict(self._scraped_pages)

    @property
    def is_running(self) -> bool:
        return self._is_running
