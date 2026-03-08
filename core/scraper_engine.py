"""
scraper_engine.py — Ana Kazıyıcı Motor

Playwright tabanlı web kazıyıcı:
- Stealth modda tarayıcı açma (bot korumasını aşma)
- Lazy loading aşımı: sayfayı yavaşça kaydırma + networkidle bekleme
- Network interception ile tüm kaynakları yakalama
- data-src / placeholder temizleme
- Tam render edilmiş DOM döndürme
"""

import asyncio
import base64
import hashlib
import re
from pathlib import Path
from urllib.parse import urljoin, urlparse

from PyQt6.QtCore import QObject, pyqtSignal
from playwright.async_api import async_playwright
from playwright_stealth import Stealth

from core.api_mocker import ApiMocker
from core.interaction_engine import InteractionEngine


class ScraperEngine(QObject):
    """Playwright tabanlı ana kazıyıcı motor."""

    # ── PyQt6 Sinyalleri ──
    progress_updated = pyqtSignal(int)                         # ilerleme yüzdesi (0-100)
    log_message = pyqtSignal(str)                              # canlı log mesajı
    # (html_desktop, html_desktop_logged_in, html_mobile, captured_resources, detected_selectors)
    scraping_finished = pyqtSignal(str, str, str, dict, dict)        
    scraping_failed = pyqtSignal(str)                          # hata mesajı

    # ── Scroll Ayarları ──
    SCROLL_STEP_PX = 300          # her adımda kaydırılacak piksel
    SCROLL_DELAY_MS = 500         # adımlar arası bekleme (ms)
    NETWORK_IDLE_TIMEOUT = 20000  # networkidle bekleme süresi (ms)
    MAX_SCROLL_ATTEMPTS = 200     # sonsuz kaydırmayı önleme limiti

    def __init__(self, parent=None):
        super().__init__(parent)
        self._browser = None
        self._playwright = None
        self._is_running = False
        self._captured_resources: dict[str, bytes] = {}   # url → bytes
        self._captured_content_types: dict[str, str] = {} # url → content-type
        
        self.api_mocker: ApiMocker | None = None
        self.last_detected_selectors = {
            "login_form": "",
            "username_input": "",
            "username_display": ""
        }
        self._raw_html: str = "" # Orijinal ağdan gelen HTML

    # ──────────────────────────────────────────────
    #  PUBLIC API
    # ──────────────────────────────────────────────
    
    async def capture_auth_state_ui(self, url: str, output_dir: Path) -> tuple[bool, str]:
        """Kullanıcıdan giriş yapmasını istemek için görünür (headed) bir tarayıcı açar."""
        output_dir.mkdir(parents=True, exist_ok=True)
        try:
            playwright = await async_playwright().start()
            browser = await playwright.chromium.launch(
                headless=False,
                args=["--disable-blink-features=AutomationControlled", "--no-sandbox"]
            )
            context = await browser.new_context(viewport={"width": 1366, "height": 768})
            stealth = Stealth()
            await stealth.apply_stealth_async(context)
            
            # Phase 19: Auto-detect username from login form (stored locally for clone display)
            await context.add_init_script("""
            document.addEventListener('input', (e) => {
                if (e.target.tagName === 'INPUT' && e.target.type !== 'password') {
                    const name = (e.target.name || '').toLowerCase();
                    const id = (e.target.id || '').toLowerCase();
                    const placeholder = (e.target.placeholder || '').toLowerCase();
                    const isUserField = name.includes('user') || name.includes('mail') || id.includes('user') || id.includes('mail') || placeholder.includes('kullanıcı') || placeholder.includes('user') || placeholder.includes('posta') || e.target.type === 'email' || e.target.type === 'text';
                    if (isUserField && e.target.value.trim().length > 2) {
                        localStorage.setItem('__cloner_source_username', e.target.value.trim());
                    }
                }
            }, true);
            """)

            page = await context.new_page()
            
            self.log_message.emit("👀 Please log in from the opened window and CLOSE the browser when done.")
            await page.goto(url)
            
            # Kullanıcı pencereyi kapatana kadar bekle (Daha güvenli döngü)
            while len(context.pages) > 0:
                await asyncio.sleep(1)
                
            # Pencere kapandı ancak context hala yaşıyor, state'i kaydet
            self.log_message.emit("💾 Saving authenticated session state...")
            auth_path = output_dir / "auth_state.json"
            await context.storage_state(path=str(auth_path))
            
            await browser.close()
            await playwright.stop()
            self.log_message.emit("✅ Session cloned successfully!")
            
            # Yakalanan kullanıcı adını auto-extract yap
            source_user = ""
            import json
            try:
                with open(auth_path, "r", encoding="utf-8") as f:
                    state_data = json.load(f)
                for origin in state_data.get("origins", []):
                    for ls in origin.get("localStorage", []):
                        if ls["name"] == "__cloner_source_username":
                            source_user = ls["value"]
                            break
                    if source_user:
                        break
            except Exception:
                pass
                
            return True, source_user
            
        except Exception as e:
            self.log_message.emit(f"⚠️ Session capture error: {e}")
            return False, ""

    async def capture_auth_state_auto(
        self,
        login_url: str,
        username: str,
        password: str,
        output_dir: Path,
        success_selector: str = "",
    ) -> tuple[bool, str]:
        """
        Programatik giriş: Formu otomatik doldurur, butona basar,
        başarı doğrulaması yapar ve auth_state.json kaydeder.
        """
        output_dir.mkdir(parents=True, exist_ok=True)
        try:
            playwright = await async_playwright().start()
            browser = await playwright.chromium.launch(
                headless=True,
                args=["--disable-blink-features=AutomationControlled", "--no-sandbox"]
            )
            context = await browser.new_context(
                viewport={"width": 1920, "height": 1080},
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            )
            stealth = Stealth()
            await stealth.apply_stealth_async(context)

            page = await context.new_page()
            self.log_message.emit(f"🔐 Starting auto-login: {login_url}")

            await page.goto(login_url, wait_until="domcontentloaded", timeout=30000)
            try:
                await page.wait_for_load_state("networkidle", timeout=15000)
            except Exception:
                pass

            # Preloader atlatma
            await self._wait_for_real_content(page)

            # ── Form alanlarını bul ve doldur ──
            self.log_message.emit("📝 Filling login form fields...")

            # Kullanıcı adı alanı: email, text veya user tipi input
            user_filled = False
            user_selectors = [
                'input[type="email"]',
                'input[type="text"][name*="user"]', 'input[type="text"][name*="mail"]',
                'input[type="text"][name*="login"]', 'input[type="text"][name*="account"]',
                'input[name*="user"]', 'input[name*="mail"]', 'input[name*="login"]',
                'input[id*="user"]', 'input[id*="mail"]', 'input[id*="login"]',
                'input[placeholder*="kullanıcı"]', 'input[placeholder*="user"]',
                'input[placeholder*="e-posta"]', 'input[placeholder*="email"]',
                'input[type="text"]',  # Genel fallback
            ]

            for sel in user_selectors:
                try:
                    el = await page.query_selector(sel)
                    if el and await el.is_visible():
                        await el.click()
                        await el.fill(username)
                        user_filled = True
                        self.log_message.emit(f"   ✅ Username entered: {sel}")
                        break
                except Exception:
                    continue

            if not user_filled:
                self.log_message.emit("⚠️ Username field not found — falling back to manual login")
                await browser.close()
                await playwright.stop()
                return await self.capture_auth_state_ui(login_url, output_dir)

            # Şifre alanı
            password_filled = False
            try:
                pw_el = await page.query_selector('input[type="password"]')
                if pw_el and await pw_el.is_visible():
                    await pw_el.click()
                    await pw_el.fill(password)
                    password_filled = True
                    self.log_message.emit("   ✅ Password entered")
            except Exception:
                pass

            if not password_filled:
                self.log_message.emit("⚠️ Password field not found — falling back to manual login")
                await browser.close()
                await playwright.stop()
                return await self.capture_auth_state_ui(login_url, output_dir)

            await asyncio.sleep(0.5)

            # ── Giriş Yap butonuna bas ──
            self.log_message.emit("🖱️ Clicking login button...")
            submit_clicked = False
            submit_selectors = [
                'button[type="submit"]',
                'input[type="submit"]',
                'button:has-text("Giriş")', 'button:has-text("Login")',
                'button:has-text("Sign in")', 'button:has-text("Oturum")',
                'form button', 'form input[type="button"]',
            ]

            for sel in submit_selectors:
                try:
                    el = await page.query_selector(sel)
                    if el and await el.is_visible():
                        await el.click()
                        submit_clicked = True
                        self.log_message.emit(f"   ✅ Login button clicked: {sel}")
                        break
                except Exception:
                    continue

            if not submit_clicked:
                # Enter tuşu ile form gönder
                try:
                    await page.keyboard.press("Enter")
                    submit_clicked = True
                    self.log_message.emit("   ✅ Form submitted via Enter key")
                except Exception:
                    pass

            # ── Başarılı giriş doğrulaması ──
            self.log_message.emit("⏳ Verifying login...")
            await asyncio.sleep(2)

            try:
                await page.wait_for_load_state("networkidle", timeout=15000)
            except Exception:
                pass

            login_success = False
            if success_selector:
                try:
                    await page.wait_for_selector(success_selector, state="visible", timeout=15000)
                    login_success = True
                    self.log_message.emit(f"   ✅ Success indicator found: {success_selector}")
                except Exception:
                    self.log_message.emit(f"   ⚠️ Success indicator not found: {success_selector}")
            else:
                # success_selector yoksa, URL değişimi veya password alanının kaybolmasıyla doğrula
                try:
                    pw_still = await page.query_selector('input[type="password"]')
                    if pw_still and await pw_still.is_visible():
                        self.log_message.emit("   ⚠️ Password field still visible — login may have failed")
                    else:
                        login_success = True
                        self.log_message.emit("   ✅ Password field disappeared — login successful!")
                except Exception:
                    login_success = True  # Sayfa tamamen değiştiyse başarılı kabul et

            # ── Auth state kaydet ──
            auth_path = output_dir / "auth_state.json"
            self.log_message.emit("💾 Saving session data...")
            await context.storage_state(path=str(auth_path))

            await browser.close()
            await playwright.stop()

            if login_success:
                self.log_message.emit("✅ Auto-login successful! Session cloned.")
            else:
                self.log_message.emit("⚠️ Login ambiguous — session saved anyway.")

            return True, username

        except Exception as e:
            self.log_message.emit(f"⚠️ Auto-login error: {e}")
            self.log_message.emit("🔄 Switching to manual login...")
            return await self.capture_auth_state_ui(login_url, output_dir)

    async def scrape_page(self, url: str, output_dir: str | Path, use_auth: bool = False, dual_pass: bool = False) -> None:
        """
        Ana sayfayı klonla (Masaüstü ve seçiliyse Mobil çift geçiş).
        """
        self._is_running = True
        self._captured_resources.clear()
        self._captured_content_types.clear()
        output_dir = Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)
        
        self.api_mocker = ApiMocker(output_dir)
        self.api_mocker.log_message.connect(self.log_message.emit)

        try:
            self.log_message.emit(f"🔍 Starting scrape: {url}")
            self.log_message.emit("🚀 Launching browser (stealth mode)...")
            self.progress_updated.emit(5)

            self._playwright = await async_playwright().start()
            self._browser = await self._playwright.chromium.launch(
                headless=True,
                args=[
                    "--disable-blink-features=AutomationControlled",
                    "--no-sandbox",
                ]
            )

            # ── 1. Gecis: Masaüstü (Duruma Göre Çıkış Yapılmış veya Normal) ──
            # (Phase 18) Eğer oturum varsa bile, İLK ÖNCE ÇIKIŞ YAPILMIŞ (Çerezsiz) hali alacağız!
            
            UAS = [
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:122.0) Gecko/20100101 Firefox/122.0",
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.2 Safari/605.1.15",
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36"
            ]
            import random
            
            context_args_desktop = {
                "viewport": {"width": 1920, "height": 1080},
                "user_agent": random.choice(UAS),
                "java_script_enabled": True,
                "locale": "en-US",
                "timezone_id": "Europe/London",
                "geolocation": {"latitude": 51.5074, "longitude": -0.1278},
                "permissions": ["geolocation"],
            }
            # Eğer use_auth kapalıysa, bu zaten normal taramadır
            # Eğer use_auth açıksa, BU tarama "Logged-Out" (Kayıt/Giriş butonu görünen) taramadır.
            context_out = await self._browser.new_context(**context_args_desktop)
            stealth_out = Stealth()
            await stealth_out.apply_stealth_async(context_out)
            
            # ── Pillar 1: Browser Fingerprint Normalization (Canvas/WebGL) ──
            anti_detect_js = """
            // Canvas zehirleme
            const originalGetContext = HTMLCanvasElement.prototype.getContext;
            HTMLCanvasElement.prototype.getContext = function(type, contextAttributes) {
                const context = originalGetContext.apply(this, arguments);
                if (type === '2d' && context) {
                    const originalFillText = context.fillText;
                    context.fillText = function() {
                        originalFillText.apply(this, arguments);
                        // Görünmez gürültü ekle
                        this.fillStyle = 'rgba(255, 255, 255, 0.01)';
                        this.fillRect(0, 0, 1, 1);
                    };
                }
                return context;
            };
            // WebGL zehirleme
            const getParameterProxy = new Proxy(WebGLRenderingContext.prototype.getParameter, {
                apply(target, thisArg, argumentsList) {
                    const param = argumentsList[0];
                    if (param === 37445) return 'Google Inc. (Apple)'; // UNMASKED_VENDOR_WEBGL
                    if (param === 37446) return 'ANGLE (Apple, Apple M1 Pro, OpenGL 4.1)'; // UNMASKED_RENDERER_WEBGL
                    return Reflect.apply(target, thisArg, argumentsList);
                }
            });
            WebGLRenderingContext.prototype.getParameter = getParameterProxy;
            if (typeof WebGL2RenderingContext !== 'undefined') {
                WebGL2RenderingContext.prototype.getParameter = getParameterProxy;
            }
            // Navigator maskeleme (Stealth eklentisine destek)
            Object.defineProperty(navigator, 'webdriver', {get: () => false});

            // WebSocket mock — offline ortamda bağlantı hatalarını engelle
            const _OrigWS = window.WebSocket;
            window.WebSocket = function(url, protocols) {
                try { return new _OrigWS(url, protocols); } catch(e) {
                    const mock = { readyState: 3, send: ()=>{}, close: ()=>{},
                        addEventListener: ()=>{}, removeEventListener: ()=>{} };
                    return mock;
                }
            };
            window.WebSocket.prototype = _OrigWS.prototype;
            window.WebSocket.CONNECTING = 0; window.WebSocket.OPEN = 1;
            window.WebSocket.CLOSING = 2; window.WebSocket.CLOSED = 3;

            // Fetch/XHR interceptor — API çağrılarını kaydet
            const _origFetch = window.fetch;
            window.fetch = async function(...args) {
                const resp = await _origFetch.apply(this, args);
                return resp;
            };

            // WebRTC IP sızıntısı engelleyici
            if (typeof RTCPeerConnection !== 'undefined') {
                const _origRTC = window.RTCPeerConnection;
                window.RTCPeerConnection = function(cfg) {
                    if (cfg && cfg.iceServers) cfg.iceServers = [];
                    return new _origRTC(cfg || {});
                };
                window.RTCPeerConnection.prototype = _origRTC.prototype;
            }

            // Geolocation sahtesi — İngiltere/Londra koordinatları
            if (navigator.geolocation) {
                navigator.geolocation.getCurrentPosition = (success) => {
                    success({
                        coords: { latitude: 51.5074, longitude: -0.1278, accuracy: 10,
                                  altitude: null, altitudeAccuracy: null, heading: null, speed: null },
                        timestamp: Date.now()
                    });
                };
                navigator.geolocation.watchPosition = (success) => {
                    success({
                        coords: { latitude: 51.5074, longitude: -0.1278, accuracy: 10,
                                  altitude: null, altitudeAccuracy: null, heading: null, speed: null },
                        timestamp: Date.now()
                    });
                    return 0;
                };
            }

            // Timezone sahtesi — scraping sırasında tutarlı locale
            const _origDateTZ = Intl.DateTimeFormat.prototype.resolvedOptions;
            Intl.DateTimeFormat.prototype.resolvedOptions = function() {
                const opts = _origDateTZ.call(this);
                return Object.assign({}, opts, { timeZone: 'Europe/London' });
            };
            """
            await context_out.add_init_script(anti_detect_js)
            
            page_out = await context_out.new_page()

            # ── Service Worker Killer ──
            async def _sw_route_handler(route):
                url_lower = route.request.url.lower()
                if 'serviceworker' in url_lower or 'service-worker' in url_lower or 'sw.js' in url_lower:
                    self.log_message.emit(f"🛡️ Service Worker engellendi: {route.request.url[:80]}")
                    await route.abort()
                else:
                    await route.continue_()
            await page_out.route("**/*.js", _sw_route_handler)

            self.log_message.emit("📡 Setting up network listener...")
            self.progress_updated.emit(10)
            await self._setup_network_listener(page_out, url)

            self.log_message.emit(f"🌐 Opening desktop view: {url}")
            self.progress_updated.emit(15)
            
            # (Phase 7) Raw HTML Kaydı - İsteği yakala
            resp = await page_out.goto(url, wait_until="domcontentloaded", timeout=60000)
            if resp:
                try:
                    self._raw_html = await resp.text()
                    self.log_message.emit(f"📝 Raw HTML response received: {len(self._raw_html):,} bytes")
                except Exception as e:
                    self.log_message.emit(f"⚠️ Raw HTML unreadable (Protocol Error): {e}")
                    self._raw_html = ""

            self.log_message.emit("⏳ Waiting for page load...")
            self.progress_updated.emit(20)
            try:
                await page_out.wait_for_load_state("networkidle", timeout=self.NETWORK_IDLE_TIMEOUT)
            except Exception:
                self.log_message.emit("⚠️  networkidle timeout — continuing")

            # ── Preloader/Intro Atlatma ──
            await self._wait_for_real_content(page_out)

            # Anti-Bot Emulation: Rastgele fare hareketleri ve bekleme
            self.log_message.emit("🤖 Bypassing anti-bot: simulating mouse movements...")
            import random
            for _ in range(5):
                await page_out.mouse.move(random.randint(100, 800), random.randint(100, 600))
                await asyncio.sleep(0.2)
            await asyncio.sleep(2)

            self.log_message.emit("📜 Triggering lazy load — scrolling page...")
            self.progress_updated.emit(25)
            await self._scroll_to_bottom(page_out)
            self.progress_updated.emit(50)

            # --- Phase 7: Interaction Engine ---
            self.log_message.emit("⚡ Micro-Clone Engine: triggering interactive elements...")
            interaction = InteractionEngine(page_out, log_callback=self.log_message.emit)
            await interaction.run_all()
            
            # Etkileşimlerden sonra ağın sakinleşmesini bekle
            try:
                await page_out.wait_for_load_state("networkidle", timeout=5000)
            except: pass

            self.progress_updated.emit(70)

            # --- Cookie ve Popup Oto-Kabul ---
            self.log_message.emit("🍪 Clearing cookie/consent popups...")
            cookie_js = """() => {
                const keywords = ['kabul', 'accept', 'agree', 'anladım', 'got it', 'tamam', 'ok'];
                document.querySelectorAll('button, a, div[role="button"]').forEach(btn => {
                    const text = (btn.innerText || '').toLowerCase();
                    if (keywords.some(kw => text.includes(kw))) {
                        // Fazla büyük butonları (tüm sayfa overlay'leri) tıkla, 
                        // ya da cookie class'ına sahip olanları tıkla.
                        if (btn.className.toLowerCase().includes('cookie') || btn.className.toLowerCase().includes('consent') || 
                            btn.id.toLowerCase().includes('cookie')) {
                            btn.click();
                        } else if (btn.offsetHeight < 100 && btn.offsetWidth < 300) {
                             // Sadece küçük 'kabul et' butonlarını tıkla, rastgele ana menü butonlarına tıklama ihtimaline karşı.
                             btn.click();
                        }
                    }
                });
            }"""
            try:
                await page_out.evaluate(cookie_js)
                await asyncio.sleep(1)
            except Exception:
                pass

            self.log_message.emit("🔄 Applying data-src / placeholder conversions...")
            self.progress_updated.emit(75)
            await self._convert_lazy_attributes(page_out)

            self.progress_updated.emit(82)
            # CSS inlining disabled: CSS files are downloaded locally and rewritten by asset_manager

            await self._extract_shadow_dom(page_out)
            await self._extract_iframes(page_out, url)
            await self._capture_interactive_states(page_out)
            await self._freeze_canvases(page_out)
            await self._capture_background_images(page_out)
            await self._capture_favicons(page_out)
            await self._inline_svg_sprites(page_out)
            await self._capture_prefetch_resources(page_out, url)
            await self._capture_feeds_and_sitemap(page_out, url)
            await self._capture_dark_mode_css(url, context_args_desktop)
            await self._capture_ab_variants(url, output_dir, context_args_desktop)
            await self._detect_captcha(page_out)

            screenshot_path = output_dir / "original_screenshot.png"
            await page_out.screenshot(path=str(screenshot_path), full_page=True)
            self.log_message.emit(f"📸 Screenshot captured: {screenshot_path.name}")
            self.progress_updated.emit(85)

            html_content_desktop = await page_out.content()
            self.log_message.emit(f"✅ Desktop DOM captured — {len(html_content_desktop):,} characters")
            self.progress_updated.emit(90)

            # --- Phase 6: Automatic Selector Detection (Logged-Out) ---
            self.log_message.emit("🔍 Auto-detecting login form and username display...")
            selectors = await self._detect_mock_selectors(page_out)
            self.last_detected_selectors.update(selectors)
            
            await context_out.close()
            
            # ── Phase 18: Çerezli (Logged-in) Ana Geçi̇ş (Aynı Tarayıcıda) ──
            html_content_desktop_logged_in = ""
            auth_path = output_dir / "auth_state.json"
            
            # --- Phase 7: source_user Fix ---
            current_source_user = ""
            if auth_path.exists():
                import json
                try:
                    with open(auth_path, "r", encoding="utf-8") as f:
                        state_data = json.load(f)
                    for origin in state_data.get("origins", []):
                        for ls in origin.get("localStorage", []):
                            if ls["name"] == "__cloner_source_username":
                                current_source_user = ls["value"]
                                break
                        if current_source_user: break
                except: pass

            if use_auth and auth_path.exists():
                self.log_message.emit("🔑 (Phase 18) Extracting authenticated desktop view (for header diffing)...")
                context_args_in = context_args_desktop.copy()
                context_args_in["storage_state"] = str(auth_path)
                
                context_in = await self._browser.new_context(**context_args_in)
                stealth_in = Stealth()
                await stealth_in.apply_stealth_async(context_in)
                await context_in.add_init_script(anti_detect_js)
                
                page_in = await context_in.new_page()
                await self._setup_network_listener(page_in, url)
                await page_in.goto(url, wait_until="domcontentloaded", timeout=60000)
                try:
                    await page_in.wait_for_load_state("networkidle", timeout=10000)
                except:
                    pass

                # ── Preloader/Intro Atlatma (Logged-in) ──
                await self._wait_for_real_content(page_in)
                
                # Zaten yukarıda her şeyi dönüştürdük, logged-in sayfasının sadece DOM'una ihtiyacımız var (Header sökülecek)
                await self._scroll_to_bottom(page_in)
                html_content_desktop_logged_in = await page_in.content()
                
                # --- Phase 6: Automatic Selector Detection (Logged-In) ---
                if current_source_user:
                    self.log_message.emit(f"🔍 Scanning username field with value '{current_source_user}'...")
                    display_sel = await self._detect_username_display(page_in, current_source_user)
                    if display_sel:
                        self.last_detected_selectors["username_display"] = display_sel
                        self.log_message.emit(f"✨ Detected: {display_sel}")

                self.log_message.emit(f"✅ Authenticated desktop DOM captured — {len(html_content_desktop_logged_in):,} characters")
                await context_in.close()
            
            # ── 2. Mobil Geçiş (Dual Pass) ──
            html_content_mobile = ""
            if dual_pass and self._is_running:
                self.log_message.emit("📱 Second pass: scanning mobile view (iPhone)...")
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

                context_m = await self._browser.new_context(**context_args_mobile)
                stealth_m = Stealth()
                await stealth_m.apply_stealth_async(context_m)
                await context_m.add_init_script(anti_detect_js)
                
                page_m = await context_m.new_page()
                await self._setup_network_listener(page_m, url)
                await page_m.goto(url, wait_until="domcontentloaded", timeout=60000)
                
                try:
                    await page_m.wait_for_load_state("networkidle", timeout=self.NETWORK_IDLE_TIMEOUT)
                except Exception:
                    pass
                    
                await self._scroll_to_bottom(page_m)
                await self._convert_lazy_attributes(page_m)
                await self._freeze_canvases(page_m)
                await self._capture_favicons(page_m)
                await self._inline_svg_sprites(page_m)
                await self._capture_prefetch_resources(page_m, url)

                html_content_mobile = await page_m.content()
                self.log_message.emit(f"✅ Mobile DOM captured — {len(html_content_mobile):,} characters")
                await context_m.close()

            # ── Cleanup ──
            await self._browser.close()
            await self._playwright.stop()

            # ── Zero-Leak: Auth state dosyalarını sil ──
            for state_file in ['auth_state.json', 'state.json']:
                state_path = output_dir / state_file
                if state_path.exists():
                    state_path.unlink()
                    self.log_message.emit(f"🔐 {state_file} deleted (Zero-Leak)")
                # Proje kökünden de sil
                root_state = Path(state_file)
                if root_state.exists():
                    root_state.unlink()
                    self.log_message.emit(f"🔐 Root/{state_file} deleted (Zero-Leak)")

            self.log_message.emit(
                f"📦 Total captured resources: {len(self._captured_resources)} files"
            )
            self.progress_updated.emit(95)

            # (Selector detection artık page_out kapanmadan önce satır 460-463'te yapılıyor)

            # ── Sonucu bildir ──
            # Önce anlık görüntü al, sonra orijinal dict'i temizle → RAM'i yarıya indirir.
            resources_snapshot = dict(self._captured_resources)
            self._captured_resources.clear()
            content_types_snapshot = dict(self._captured_content_types)
            self._captured_content_types.clear()

            # Anlık görüntüleri sinyale gönder (byte'lar sadece tek kopya olarak kalır)
            self.scraping_finished.emit(
                self._raw_html or html_content_desktop, # Raw HTML tercih et
                html_content_desktop_logged_in,
                html_content_mobile,
                resources_snapshot,
                self.last_detected_selectors if hasattr(self, "last_detected_selectors") else {}
            )
            # content_types_snapshot'ı property üzerinden erişilebilir kıl
            self._captured_content_types_snapshot = content_types_snapshot
            self.progress_updated.emit(100)
            self.log_message.emit("🎉 Scraping complete!")

        except Exception as exc:
            self.log_message.emit(f"❌ Error: {exc}")
            self.scraping_failed.emit(str(exc))
            await self._cleanup()
        finally:
            self._is_running = False

    async def _detect_mock_selectors(self, page) -> dict:
        """Playwright üzerinde JS çalıştırarak form ve inputları tahmin eder."""
        js_code = """
        () => {
            function getCssSelector(el) {
                if (!el) return '';
                if (el.id) return `#${el.id}`;
                if (el.name) return `${el.tagName.toLowerCase()}[name="${el.name}"]`;
                if (el.className) {
                    const cls = el.className.split(/\s+/).filter(c => c && !c.includes(':')).join('.');
                    if (cls) return `.${cls}`;
                }
                return el.tagName.toLowerCase();
            }

            const res = { login_form: '', username_input: '' };
            const passwordInput = document.querySelector('input[type="password"]');
            if (passwordInput) {
                const form = passwordInput.closest('form') || document.body;
                res.login_form = getCssSelector(form === document.body ? null : form) || 'form';
                
                const userInputs = Array.from(form.querySelectorAll('input:not([type="password"]):not([type="hidden"]):not([type="submit"]):not([type="button"])'));
                const userField = userInputs.find(i => {
                    const attr = (i.name + i.id + i.placeholder + i.type).toLowerCase();
                    return attr.includes('user') || attr.includes('mail') || attr.includes('login') || attr.includes('text') || attr.includes('email');
                }) || userInputs[0];
                
                if (userField) {
                    res.username_input = getCssSelector(userField);
                }
            }
            return res;
        }
        """
        try:
            return await page.evaluate(js_code)
        except:
            return {}

    async def _detect_username_display(self, page, username: str) -> str:
        """Giriş yaptıktan sonra ekrandaki ismin olduğu yeri bulur."""
        js_code = """
        (username) => {
            function getCssSelector(el) {
                if (!el) return '';
                if (el.id) return `#${el.id}`;
                if (el.className) {
                    const cls = el.className.split(/\s+/).filter(c => c && !c.includes(':')).join('.');
                    if (cls) return `.${cls}`;
                }
                return el.tagName.toLowerCase();
            }
            
            const walk = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT, null, false);
            let node;
            const target = username.toLowerCase().trim();
            while(node = walk.nextNode()) {
                const txt = node.textContent.toLowerCase().trim();
                if (txt === target || (target.length > 3 && txt.includes(target))) {
                    const parent = node.parentElement;
                    if (parent && parent.tagName !== 'SCRIPT' && parent.tagName !== 'STYLE') {
                        return getCssSelector(parent);
                    }
                }
            }
            return '';
        }
        """
        try:
            return await page.evaluate(js_code, username)
        except:
            return ""

    async def stop(self) -> None:
        """Kazıma işlemini durdur."""
        self._is_running = False
        await self._cleanup()
        self.log_message.emit("🛑 Scraping stopped.")

    # ──────────────────────────────────────────────
    #  LAZY LOADING SCROLL
    # ──────────────────────────────────────────────

    async def _scroll_to_bottom(self, page) -> None:
        """
        Sayfayı yavaşça en alta kadar kaydır.

        - Her adımda SCROLL_STEP_PX piksel aşağı kaydır
        - Her adımda SCROLL_DELAY_MS bekle
        - IntersectionObserver tetiklenmelerini ve lazy load'ları bekle
        - scrollHeight artmayı bırakıp, viewport en alta ulaştığında dur
        """
        previous_height = 0
        current_height = 0
        attempt = 0

        while self._is_running and attempt < self.MAX_SCROLL_ATTEMPTS:
            attempt += 1

            # Scroll + metrics TEK IPC çağrısında birleştirildi (2 IPC → 1 IPC per tick)
            metrics = await page.evaluate(
                f"""() => {{
                    window.scrollBy({{ top: {self.SCROLL_STEP_PX}, behavior: 'smooth' }});
                    return {{ h: document.body.scrollHeight, p: window.scrollY + window.innerHeight }};
                }}"""
            )
            await asyncio.sleep(self.SCROLL_DELAY_MS / 1000)

            current_height = metrics["h"]
            scroll_position = metrics["p"]

            # İlerleme hesapla ve bildir
            if current_height > 0:
                scroll_pct = min(scroll_position / current_height, 1.0)
                overall_pct = int(25 + (scroll_pct * 45))  # 25-70 arası
                self.progress_updated.emit(overall_pct)

            # Her 10 adımda log
            if attempt % 10 == 0:
                self.log_message.emit(
                    f"   📜 Scrolling: step {attempt} — "
                    f"position: {int(scroll_position)}px / {current_height}px"
                )

            # En alta ulaştık mı?
            if scroll_position >= current_height - 5 and current_height == previous_height:
                # Biraz daha bekle (son lazy load'lar için)
                self.log_message.emit("   📜 Reached end of page — waiting extra...")
                await asyncio.sleep(1.5)

                # Tekrar kontrol et (infinite scroll tetiklenmiş olabilir)
                new_height = await page.evaluate("document.body.scrollHeight")
                if new_height == current_height:
                    break  # Gerçekten bitti
                else:
                    self.log_message.emit("   📜 New content loaded — continuing scroll...")

            previous_height = current_height

        # Son scroll durumu logu — ayrı IPC yerine döngüdeki son değeri kullan
        self.log_message.emit(
            f"   ✅ Scrolling complete: {attempt} steps, "
            f"total height: {current_height}px"
        )

        # Scroll sonrası networkidle bekle
        try:
            await page.wait_for_load_state("networkidle", timeout=self.NETWORK_IDLE_TIMEOUT)
        except Exception:
            self.log_message.emit("   ⚠️  networkidle timeout after scroll")

    # ──────────────────────────────────────────────
    #  CSS INLINE (SingleFile-like)
    # ──────────────────────────────────────────────

    async def _inline_all_css(self, page) -> None:
        """
        Sayfadaki TÜM CSS kurallarını <style> bloğu olarak <head>'e göm.

        Bu yaklaşım SingleFile benzeri: sayfa açıldığında tarayıcının
        yüklediği tüm stylesheet'ler okunur ve inline hale getirilir.
        Bu sayede harici CSS dosyalarına bağımlılık ortadan kalkar.
        """
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
                    } catch (e) {
                        // CORS engeli olan stylesheet'ler atlanır
                    }
                }
            } catch (e) {}

            if (allRules.length === 0) return 0;

            // DİKKAT: Mevcut <style> etiketlerini SİLMİYORUZ. 
            // Çünkü JS ile eklenen dinamik stilleri veya var olan sayfaya özel animasyonları bozabiliyor.
            // Sadece eksik olabilecek external (harici) kuralları garanti olması adına ekliyoruz.

            // Inline CSS'i <head>'e ekle
            let styleEl = document.createElement('style');
            styleEl.id = 'cloned-inline-styles';
            styleEl.textContent = allRules.join('\\n');
            document.head.appendChild(styleEl);

            return allRules.length;
        }
        """
        try:
            rule_count = await page.evaluate(inline_script)
            self.log_message.emit(f"   🎨 {rule_count} CSS rules inlined")
        except Exception as e:
            self.log_message.emit(f"   ⚠️  CSS inline error: {e}")

    async def _capture_background_images(self, page) -> None:
        """
        Sayfadaki tüm elementlerin computed background-image URL'lerini yakala.
        Bu URL'ler ağ dinleyicisi tarafından yakalanmamış olabilir.
        """
        bg_script = """
        () => {
            let urls = [];
            let elements = document.querySelectorAll('*');
            for (let el of elements) {
                let bg = getComputedStyle(el).backgroundImage;
                if (bg && bg !== 'none' && bg.includes('url(')) {
                    let matches = bg.match(/url\\(["']?([^"')]+)["']?\\)/g);
                    if (matches) {
                        for (let match of matches) {
                            let url = match.replace(/url\\(["']?/, '').replace(/["']?\\)/, '');
                            if (url && !url.startsWith('data:')) {
                                urls.push(url);
                            }
                        }
                    }
                }
            }
            return [...new Set(urls)];
        }
        """
        try:
            bg_urls = await page.evaluate(bg_script)
            if bg_urls:
                self.log_message.emit(f"   🖼️  {len(bg_urls)} background images detected")
                import asyncio
                
                async def fetch_bg(url):
                    if url in self._captured_resources:
                        return None
                    try:
                        resp = await page.context.request.get(url, timeout=10000, ignore_https_errors=True)
                        if resp.ok:
                            body = await resp.body()
                            ct = resp.headers.get("content-type", "application/octet-stream")
                            return url, ct, body
                    except Exception:
                        pass
                    return None
                
                tasks = [fetch_bg(u) for u in bg_urls]
                results = await asyncio.gather(*tasks)
                
                for res in results:
                    if res:
                        u, ct, req_body = res
                        self._captured_resources[u] = req_body
                        self._captured_content_types[u] = ct
        except Exception as e:
            self.log_message.emit(f"   ⚠️  Background image capture error: {e}")

    async def _freeze_canvases(self, page) -> None:
        """
        Sayfadaki tüm <canvas> (WebGL/2D) elementlerinin anlık görüntüsünü alıp
        dondurulmuş bir <img> etiketi olarak HTML'e gömer.
        Bu sayede çevrimdışı Slot/Aviator oyunları siyah ekran görünmez.
        """
        try:
            canvases = await page.query_selector_all("canvas")
            if not canvases:
                return
                
            self.log_message.emit(f"   🧊 {len(canvases)} Canvas elements detected, freezing...")
            frozen_count = 0
            
            for canvas in canvases:
                is_visible = await canvas.is_visible()
                if not is_visible:
                    continue
                    
                # Playwright ile elementi direkt resim olarak çek
                img_bytes = await canvas.screenshot(type="png")
                b64_img = base64.b64encode(img_bytes).decode('utf-8')
                data_url = f"data:image/png;base64,{b64_img}"
                
                # HTML tarafında canvas'ı resimle değiştir
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
                self.log_message.emit(f"   ❄️ {frozen_count} Canvas(es) frozen successfully.")
        except Exception as e:
            self.log_message.emit(f"   ⚠️ Canvas freeze error: {e}")

    async def _capture_favicons(self, page) -> None:
        """Sayfanın faviconlarını (icon, shortcut icon, apple-touch-icon vb.) bul ve indir."""
        favicon_script = """
        () => {
            let urls = [];
            // <link rel="*icon*"> taglarını bul
            document.querySelectorAll('link[rel*="icon"], link[rel="apple-touch-icon"]').forEach(el => {
                const href = el.getAttribute('href');
                if (href && !href.startsWith('data:')) {
                    urls.push(href);
                }
            });
            // Varsayılan favicon yolunu da ekle
            urls.push('/favicon.ico');
            return [...new Set(urls)];
        }
        """
        try:
            fav_urls = await page.evaluate(favicon_script)
            if fav_urls:
                self.log_message.emit(f"   🔖  {len(fav_urls)} Favicon URL(s) detected, downloading...")
                for url in fav_urls:
                    if url not in self._captured_resources:
                        try:
                            # Playwright evaluate ile fetch kullan
                            response = await page.evaluate(f"""
                                async () => {{
                                    try {{
                                        let resp = await fetch('{url}');
                                        if (!resp.ok) return null;
                                        let blob = await resp.blob();
                                        let buffer = await blob.arrayBuffer();
                                        return Array.from(new Uint8Array(buffer));
                                    }} catch(e) {{ return null; }}
                                }}
                            """)
                            if response:
                                self._captured_resources[url] = bytes(response)
                        except Exception:
                            pass
        except Exception as e:
            self.log_message.emit(f"   ⚠️  Favicon download error: {e}")

    # ──────────────────────────────────────────────
    #  SVG SPRITE INLINE
    # ──────────────────────────────────────────────

    async def _inline_svg_sprites(self, page) -> None:
        """<use xlink:href="#..."> SVG sprite referanslarını inline SVG içeriğiyle değiştirir."""
        try:
            result = await page.evaluate("""
            () => {
                let inlined = 0;
                document.querySelectorAll('use[href], use[xlink\\:href]').forEach(useEl => {
                    const ref = useEl.getAttribute('href') || useEl.getAttribute('xlink:href') || '';
                    if (!ref.startsWith('#')) return;
                    const target = document.querySelector(ref);
                    if (!target) return;
                    const clone = target.cloneNode(true);
                    clone.removeAttribute('id');
                    const svgNS = 'http://www.w3.org/2000/svg';
                    const svg = document.createElementNS(svgNS, 'svg');
                    const origSvg = useEl.closest('svg');
                    if (origSvg) {
                        svg.setAttribute('viewBox', origSvg.getAttribute('viewBox') || '');
                        svg.setAttribute('width', origSvg.getAttribute('width') || '');
                        svg.setAttribute('height', origSvg.getAttribute('height') || '');
                    }
                    svg.appendChild(clone);
                    useEl.parentNode && useEl.parentNode.replaceChild(svg, useEl);
                    inlined++;
                });
                return inlined;
            }
            """)
            if result:
                self.log_message.emit(f"   🎨 {result} SVG sprite(s) inlined.")
        except Exception as e:
            self.log_message.emit(f"   ⚠️ SVG sprite inline error: {e}")

    # ──────────────────────────────────────────────
    #  PREFETCH / PRELOAD YAKALAMA
    # ──────────────────────────────────────────────

    async def _capture_prefetch_resources(self, page, base_url: str) -> None:
        """<link rel='prefetch|preload'> kaynaklarını tarayıcı ağ dinleyicisine yükler."""
        try:
            urls = await page.evaluate("""
            () => {
                const links = [];
                document.querySelectorAll('link[rel="prefetch"], link[rel="preload"], link[rel="modulepreload"]').forEach(el => {
                    const href = el.getAttribute('href');
                    if (href && !href.startsWith('data:')) links.push(href);
                });
                return [...new Set(links)];
            }
            """)
            if not urls:
                return
            self.log_message.emit(f"   🔗 {len(urls)} prefetch/preload resource(s) detected...")
            from urllib.parse import urljoin
            for href in urls:
                abs_url = urljoin(base_url, href)
                if abs_url not in self._captured_resources:
                    try:
                        resp_bytes = await page.evaluate(f"""
                        async () => {{
                            try {{
                                const r = await fetch('{abs_url}');
                                if (!r.ok) return null;
                                const buf = await r.arrayBuffer();
                                return Array.from(new Uint8Array(buf));
                            }} catch(e) {{ return null; }}
                        }}
                        """)
                        if resp_bytes:
                            self._captured_resources[abs_url] = bytes(resp_bytes)
                    except Exception:
                        pass
        except Exception as e:
            self.log_message.emit(f"   ⚠️ Prefetch capture error: {e}")

    # ──────────────────────────────────────────────
    #  A1 — SHADOW DOM EXTRACTION
    # ──────────────────────────────────────────────

    async def _extract_shadow_dom(self, page) -> None:
        """
        Shadow DOM içeriklerini ana DOM'a 'flatten' eder.
        <custom-element> içindeki shadow root'ları <div data-shadow> olarak yerleştirir.
        """
        try:
            flattened = await page.evaluate("""
            () => {
                function flattenShadow(root, depth=0) {
                    if (depth > 5) return 0;
                    let count = 0;
                    root.querySelectorAll('*').forEach(el => {
                        if (el.shadowRoot) {
                            const wrapper = document.createElement('div');
                            wrapper.setAttribute('data-shadow-host', el.tagName.toLowerCase());
                            wrapper.innerHTML = el.shadowRoot.innerHTML;
                            el.appendChild(wrapper);
                            count++;
                            flattenShadow(el.shadowRoot, depth + 1);
                        }
                    });
                    return count;
                }
                return flattenShadow(document);
            }
            """)
            if flattened:
                self.log_message.emit(f"   🌑 {flattened} Shadow DOM component(s) flattened.")
        except Exception as e:
            self.log_message.emit(f"   ⚠️ Shadow DOM extraction error: {e}")

    # ──────────────────────────────────────────────
    #  A3 — IFRAME EXTRACTION
    # ──────────────────────────────────────────────

    async def _extract_iframes(self, page, base_url: str) -> None:
        """
        Sayfadaki same-origin iframe'lerin HTML içeriğini ve varlıklarını yakalar.
        Cross-origin frame'ler için srcdoc snapshot alır.
        """
        try:
            frames = page.frames
            if len(frames) <= 1:
                return
            self.log_message.emit(f"   🖼️ {len(frames) - 1} iframe(s) detected, capturing contents...")
            for frame in frames[1:]:  # ilk frame ana sayfa
                try:
                    frame_url = frame.url
                    if not frame_url or frame_url in ("about:blank", ""):
                        continue
                    # Frame'deki tüm kaynakları zaten network listener yakalıyor
                    # Ek olarak frame DOM'unu srcdoc olarak inline et
                    frame_html = await frame.content()
                    if frame_html and len(frame_html) > 100:
                        # Frame'in parent elementini bul ve srcdoc'u güncelle
                        await page.evaluate("""
                        ([furl, fhtml]) => {
                            const iframes = document.querySelectorAll('iframe');
                            for (const iframe of iframes) {
                                if (iframe.src && iframe.src.includes(furl.split('/').pop())) {
                                    iframe.removeAttribute('src');
                                    iframe.srcdoc = fhtml;
                                    break;
                                }
                            }
                        }
                        """, [frame_url, frame_html])
                except Exception:
                    pass
        except Exception as e:
            self.log_message.emit(f"   ⚠️ iframe extraction error: {e}")

    # ──────────────────────────────────────────────
    #  A4 — DARK MODE CSS YAKALAMA
    # ──────────────────────────────────────────────

    async def _capture_dark_mode_css(self, url: str, context_args: dict) -> None:
        """
        Ayrı bir dark mode context açarak @prefers-color-scheme: dark
        ile tetiklenen CSS dosyalarını yakalar ve mevcut resource dict'ine ekler.
        """
        try:
            dm_args = context_args.copy()
            dm_args["color_scheme"] = "dark"
            context_dm = await self._browser.new_context(**dm_args)
            try:
                page_dm = await context_dm.new_page()
                dark_resources: dict[str, bytes] = {}

                async def _on_dark_response(resp):
                    try:
                        req = resp.request
                        if req.resource_type not in ("stylesheet", "script"):
                            return
                        if resp.status < 200 or resp.status >= 400:
                            return
                        body = await resp.body()
                        if body:
                            dark_resources[resp.url] = body
                    except Exception:
                        pass

                page_dm.on("response", _on_dark_response)
                await page_dm.goto(url, wait_until="domcontentloaded", timeout=30000)
                await asyncio.sleep(2)

                new_dark = {u: b for u, b in dark_resources.items() if u not in self._captured_resources}
                self._captured_resources.update(new_dark)
                if new_dark:
                    self.log_message.emit(f"   🌙 {len(new_dark)} additional dark mode CSS/JS resource(s) captured.")
            finally:
                await context_dm.close()
        except Exception as e:
            self.log_message.emit(f"   ⚠️ Dark mode CSS capture error: {e}")

    # ──────────────────────────────────────────────
    #  A5 — POPUP / DROPDOWN STATE YAKALAMA
    # ──────────────────────────────────────────────

    async def _capture_interactive_states(self, page) -> None:
        """
        Hover, focus ile açılan menü/tooltip/dropdown içeriklerini tetikleyerek
        CSS visibility/display durumlarını görünür hale getirir ve DOM'a kılar.
        """
        try:
            triggered = await page.evaluate("""
            () => {
                let count = 0;
                // Dropdown tetikleyicileri
                const triggers = document.querySelectorAll(
                    '[data-toggle="dropdown"], [data-bs-toggle="dropdown"], ' +
                    '.dropdown-toggle, [aria-haspopup="true"], ' +
                    '[data-toggle="collapse"], .nav-item.dropdown > a'
                );
                triggers.forEach(el => {
                    try {
                        el.dispatchEvent(new MouseEvent('mouseenter', {bubbles: true}));
                        el.dispatchEvent(new MouseEvent('mouseover', {bubbles: true}));
                        count++;
                    } catch(e) {}
                });

                // Gizli dropdown menülerini görünür yap (CSS override)
                document.querySelectorAll(
                    '.dropdown-menu, .nav-dropdown, [role="menu"], ' +
                    '.submenu, .mega-menu, .flyout-menu'
                ).forEach(menu => {
                    const style = window.getComputedStyle(menu);
                    if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') {
                        menu.setAttribute('data-cloner-revealed', 'true');
                        menu.style.display = 'block';
                        menu.style.visibility = 'visible';
                        menu.style.opacity = '1';
                    }
                });
                return count;
            }
            """)
            await asyncio.sleep(0.5)  # animasyon tamamlanması için bekle
            if triggered:
                self.log_message.emit(f"   🖱️ {triggered} interactive element(s) triggered (dropdown/menu).")
        except Exception as e:
            self.log_message.emit(f"   ⚠️ Interactive state capture error: {e}")

    # ──────────────────────────────────────────────
    #  A8 — CAPTCHA DEDEKTÖRÜ
    # ──────────────────────────────────────────────

    async def _detect_captcha(self, page) -> bool:
        """
        Sayfada CAPTCHA varlığını tespit eder.
        True dönerse kullanıcı uyarılmalı.
        """
        try:
            found = await page.evaluate("""
            () => {
                const signals = [
                    document.querySelector('iframe[src*="hcaptcha"]'),
                    document.querySelector('iframe[src*="recaptcha"]'),
                    document.querySelector('.cf-challenge-form'),
                    document.querySelector('[data-sitekey]'),
                    document.querySelector('#challenge-form'),
                    document.querySelector('.g-recaptcha'),
                    document.querySelector('iframe[src*="turnstile"]'),
                ];
                return signals.some(Boolean);
            }
            """)
            if found:
                self.log_message.emit("⚠️ CAPTCHA/Bot-Protection detected! Page may not be fully cloneable.")
            return found
        except Exception:
            return False

    # ──────────────────────────────────────────────
    #  A7 — A/B TEST VARIANT YAKALAMA
    # ──────────────────────────────────────────────

    async def _capture_ab_variants(self, url: str, output_dir: Path, context_args: dict, count: int = 3) -> None:
        """
        Aynı URL'yi farklı User-Agent + boş cookie ile n kez ziyaret ederek
        A/B test varyantlarını ab_variant_N.html olarak kaydeder.
        """
        UAS_EXTRA = [
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36",
            "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1",
            "Mozilla/5.0 (iPad; CPU OS 17_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) CriOS/120.0.6099.119 Mobile/15E148 Safari/604.1",
        ]
        variants_found = 0
        try:
            for i in range(min(count, len(UAS_EXTRA))):
                variant_args = context_args.copy()
                variant_args["user_agent"] = UAS_EXTRA[i]
                ctx = await self._browser.new_context(**variant_args)
                try:
                    pg = await ctx.new_page()
                    await pg.goto(url, wait_until="domcontentloaded", timeout=30000)
                    await asyncio.sleep(2)
                    html = await pg.content()
                    variant_path = output_dir / f"ab_variant_{i+1}.html"
                    variant_path.write_text(html, encoding="utf-8")
                    variants_found += 1
                finally:
                    await ctx.close()
            if variants_found:
                self.log_message.emit(f"   🧪 {variants_found} A/B test variant(s) saved (ab_variant_N.html).")
        except Exception as e:
            self.log_message.emit(f"   ⚠️ A/B variant capture error: {e}")

    # ──────────────────────────────────────────────
    #  B17 — RSS / ATOM / SITEMAP YAKALAMA
    # ──────────────────────────────────────────────

    async def _capture_feeds_and_sitemap(self, page, base_url: str) -> None:
        """
        Sayfadaki <link rel="alternate" type="application/rss+xml"> ve
        /sitemap.xml, /sitemap_index.xml, /robots.txt endpoint'lerini indirir.
        """
        from urllib.parse import urljoin
        try:
            feed_urls = await page.evaluate("""
            () => {
                const feeds = [];
                document.querySelectorAll('link[type="application/rss+xml"], link[type="application/atom+xml"]').forEach(el => {
                    const href = el.getAttribute('href');
                    if (href) feeds.push(href);
                });
                return feeds;
            }
            """)
        except Exception:
            feed_urls = []

        standard_paths = ["/sitemap.xml", "/sitemap_index.xml", "/robots.txt", "/feed", "/rss.xml", "/atom.xml"]
        all_targets = [urljoin(base_url, p) for p in standard_paths]
        for fu in feed_urls:
            all_targets.append(urljoin(base_url, fu))

        fetched = 0
        for target_url in all_targets:
            if target_url in self._captured_resources:
                continue
            try:
                resp = await page.context.request.get(target_url, timeout=8000, ignore_https_errors=True)
                if resp.ok:
                    body = await resp.body()
                    if body:
                        self._captured_resources[target_url] = body
                        ct = resp.headers.get("content-type", "text/xml")
                        self._captured_content_types[target_url] = ct
                        fetched += 1
            except Exception:
                pass

        if fetched:
            self.log_message.emit(f"   📰 {fetched} feed/sitemap/robots file(s) captured.")

    # ──────────────────────────────────────────────
    #  DATA-SRC DÖNÜŞÜMÜ
    # ──────────────────────────────────────────────

    async def _convert_lazy_attributes(self, page) -> None:
        """
        Sayfadaki tüm lazy-load attribute'larını gerçek src'ye dönüştür.

        - data-src → src
        - data-srcset → srcset
        - data-bg → background-image style
        - loading="lazy" kaldırma
        """
        convert_script = """
        () => {
            let converted = 0;

            // data-src → src
            document.querySelectorAll('[data-src]').forEach(el => {
                const realSrc = el.getAttribute('data-src');
                if (realSrc && realSrc.trim()) {
                    el.setAttribute('src', realSrc);
                    el.removeAttribute('data-src');
                    converted++;
                }
            });

            // data-srcset → srcset
            document.querySelectorAll('[data-srcset]').forEach(el => {
                const realSrcset = el.getAttribute('data-srcset');
                if (realSrcset && realSrcset.trim()) {
                    el.setAttribute('srcset', realSrcset);
                    el.removeAttribute('data-srcset');
                    converted++;
                }
            });

            // data-bg → inline background-image style
            document.querySelectorAll('[data-bg]').forEach(el => {
                const bgUrl = el.getAttribute('data-bg');
                if (bgUrl && bgUrl.trim()) {
                    el.style.backgroundImage = `url('${bgUrl}')`;
                    el.removeAttribute('data-bg');
                    converted++;
                }
            });

            // data-background-image
            document.querySelectorAll('[data-background-image]').forEach(el => {
                const bgUrl = el.getAttribute('data-background-image');
                if (bgUrl && bgUrl.trim()) {
                    el.style.backgroundImage = `url('${bgUrl}')`;
                    el.removeAttribute('data-background-image');
                    converted++;
                }
            });

            // loading="lazy" kaldır (zaten yüklendi)
            document.querySelectorAll('[loading="lazy"]').forEach(el => {
                el.removeAttribute('loading');
            });

            // noscript içindeki img'leri de çöz
            document.querySelectorAll('noscript').forEach(noscript => {
                const tmp = document.createElement('div');
                tmp.innerHTML = noscript.textContent;
                const imgs = tmp.querySelectorAll('img');
                imgs.forEach(img => {
                    noscript.parentNode.insertBefore(img, noscript);
                });
            });

            return converted;
        }
        """
        converted_count = await page.evaluate(convert_script)
        self.log_message.emit(
            f"   🔄 {converted_count} lazy-load attribute(s) converted"
        )

    # ──────────────────────────────────────────────
    #  NETWORK INTERCEPTION & TRACKING BLOCKER
    # ──────────────────────────────────────────────

    async def _setup_network_listener(self, page, base_url: str) -> None:
        """
        Gelişmiş Ağ Yöneticisi:
        1. İstenmeyen tracking/analytics domainlerine giden istekleri iptal eder (abort).
        2. Geçerli istekleri kaydeder.
        """
        TRACKER_DOMAINS = {
            "google-analytics.com", "googletagmanager.com", "doubleclick.net",
            "facebook.net", "facebook.com/tr/", "yandex.ru/metrika", "mc.yandex.ru", 
            "hotjar.com", "clarity.ms", "tawk.to", "smartsupp.com", "tidio.com"
        }

        async def route_handler(route):
            url = route.request.url
            if any(t in url.lower() for t in TRACKER_DOMAINS):
                self.log_message.emit(f"   🛡️ Tracking Request Blocked: {Path(urlparse(url).path).name}")
                await route.abort()
            else:
                await route.continue_()

        # Tüm istekleri route et
        await page.route("**/*", route_handler)

        CAPTURABLE_TYPES = {"stylesheet", "script", "image", "font", "media", "other", "fetch", "xhr"}
        EXCLUDED_EXTENSIONS = {".html", ".htm", ".php", ".asp", ".aspx", ".jsp"}
        CDN_EXTENSIONS = {
            ".css", ".woff", ".woff2", ".ttf", ".png", ".jpg", ".jpeg",
            ".svg", ".gif", ".webp", ".mp4", ".webm", ".js",
            ".m3u8", ".ts", ".ico", ".apng", ".avif", ".wasm"
        }
        
        parsed_base = urlparse(base_url)
        base_domain = parsed_base.netloc

        async def on_response(response):
            try:
                request = response.request
                resource_type = request.resource_type

                if resource_type not in CAPTURABLE_TYPES:
                    return

                url = response.url
                parsed = urlparse(url)
                ext = Path(parsed.path).suffix.lower()

                if ext in EXCLUDED_EXTENSIONS or url.startswith("data:"):
                    return

                if parsed.netloc and parsed.netloc != base_domain:
                    if resource_type in ("image", "media", "font") or ext in CDN_EXTENSIONS or "css" in url.lower():
                        pass
                    else:
                        return

                status = response.status
                if status < 200 or status >= 400:
                    return

                try:
                    body = await response.body()
                except Exception as e:
                    # 'No resource with given identifier' hatasını burada yutuyoruz
                    self.log_message.emit(f"🛡️ Tracking Request Content Skipped ({Path(urlparse(url).path).suffix}): {e}")
                    return

                if body and len(body) > 0:
                    content_type = ""
                    try:
                        headers = await response.all_headers()
                        content_type = headers.get("content-type", "")
                    except Exception:
                        pass
                        
                    if "application/json" in content_type.lower() or ext == ".json":
                        if self.api_mocker:
                            self.api_mocker.save_api_response(url, body)
                            
                    if ext in (".m3u8", ".ts") or "mpegurl" in content_type.lower() or "mp2t" in content_type.lower():
                        self.log_message.emit(f"🎬 Live Stream (HLS) Segment Captured: {Path(parsed.path).name}")
                        if not content_type:
                            content_type = "application/vnd.apple.mpegurl" if ext == ".m3u8" else "video/mp2t"

                    # GraphQL POST yanıtlarını API mocker'a kaydet
                    if resource_type in ("fetch", "xhr") and "graphql" in url.lower():
                        if self.api_mocker:
                            self.api_mocker.save_api_response(url, body)

                    self._captured_resources[url] = body
                    self._captured_content_types[url] = content_type

            except Exception:
                pass

        page.on("response", on_response)

        # ── WebSocket mesaj yakalama ──
        # Playwright websocket eventleri: 'websocket' → frame_received / frame_sent
        async def on_websocket(ws):
            async def on_frame(payload):
                try:
                    data = payload.get("payload", "")
                    if isinstance(data, str) and len(data) > 10:
                        ws_key = f"__ws_{hashlib.md5(ws.url.encode()).hexdigest()[:8]}_{len(self._captured_resources)}"
                        self._captured_resources[ws_key] = data.encode("utf-8")
                        self._captured_content_types[ws_key] = "application/json"
                        if self.api_mocker:
                            self.api_mocker.save_api_response(ws_key, data.encode("utf-8"))
                except Exception:
                    pass
            ws.on("framereceived", lambda payload: asyncio.ensure_future(on_frame({"payload": payload})))

        page.on("websocket", on_websocket)

        # ── Dynamic import() chunk yakalama ──
        # Webpack/Vite chunk'larını script olarak ağda yakalamak için
        # on_response zaten bunu yapıyor (resource_type == "script")
        # Ek olarak: "__webpack_require__.e" hata durumunda chunk URL'sini logla
        await page.add_init_script("""
        (function() {
            if (typeof __webpack_require__ !== 'undefined' && __webpack_require__.e) {
                const _origE = __webpack_require__.e;
                __webpack_require__.e = function(chunkId) {
                    return _origE(chunkId).catch(err => {
                        console.warn('[Cloner] Chunk yok:', chunkId);
                        return {};
                    });
                };
            }
        })();
        """)

    # ──────────────────────────────────────────────
    #  PRELOADER / INTRO BYPASS
    # ──────────────────────────────────────────────

    async def _wait_for_real_content(self, page) -> None:
        """
        Sayfa preloader/intro ekranını atlatır:
        1. <body> görünür olana kadar bekler.
        2. Yaygın loader/spinner overlay'lerinin kaybolmasını bekler.
        3. Kaybolmazlarsa JS ile zorla kaldırır.
        """
        self.log_message.emit("🔄 Bypassing Preloader/Intro...")

        # 1. body görünür olsun
        try:
            await page.wait_for_selector("body", state="visible", timeout=10000)
        except Exception:
            pass

        # 2. Yaygın preloader selector'ları
        LOADER_SELECTORS = [
            "[class*='preloader']", "[id*='preloader']",
            "[class*='loader']", "[id*='loader']",
            "[class*='spinner']", "[id*='spinner']",
            "[class*='loading']", "[id*='loading']",
            "[class*='splash']", "[id*='splash']",
            "[class*='intro-screen']", "[id*='intro']",
            "[class*='overlay-loading']",
            ".page-loader", ".site-loader", ".fullscreen-loader",
        ]

        for selector in LOADER_SELECTORS:
            try:
                el = await page.query_selector(selector)
                if el and await el.is_visible():
                    self.log_message.emit(f"   ⏳ Preloader found: {selector} — waiting for it to disappear...")
                    try:
                        await page.wait_for_selector(selector, state="hidden", timeout=15000)
                        self.log_message.emit(f"   ✅ Preloader disappeared: {selector}")
                    except Exception:
                        # Kaybolmadıysa zorla kaldır
                        self.log_message.emit(f"   🔨 Forcing preloader removal: {selector}")
                        try:
                            await page.evaluate(f"""
                                document.querySelectorAll('{selector}').forEach(el => {{
                                    el.style.display = 'none';
                                    el.style.visibility = 'hidden';
                                    el.style.opacity = '0';
                                    el.style.pointerEvents = 'none';
                                    el.remove();
                                }});
                            """)
                        except Exception:
                            pass
            except Exception:
                continue

        # 3. Kalan kapayıcı overlay'leri temizle (position:fixed + z-index yüksek)
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
                            cls.includes('splash') || cls.includes('preload') || cls.includes('overlay') ||
                            id.includes('loader') || id.includes('loading') || id.includes('preload')) {
                            el.remove();
                        }
                    }
                });
            """)
        except Exception:
            pass

        self.log_message.emit("✅ Preloader bypass complete")

    # ──────────────────────────────────────────────
    #  YARDIMCI METODLAR
    # ──────────────────────────────────────────────

    async def _cleanup(self) -> None:
        """Tarayıcı kaynaklarını temizle."""
        try:
            if self._browser:
                await self._browser.close()
            if self._playwright:
                await self._playwright.stop()
        except Exception as e:
            import logging
            logging.getLogger(__name__).warning("Cleanup error: %s", e)
        self._browser = None
        self._playwright = None

    @property
    def is_running(self) -> bool:
        return self._is_running

    @property
    def captured_resources(self) -> dict[str, bytes]:
        return dict(self._captured_resources)

    @property
    def captured_content_types(self) -> dict[str, str]:
        # Eğer scraping bitti ve _captured_content_types temizlendiyse, snapshot'ı kullan
        if self._captured_content_types:
            return dict(self._captured_content_types)
        return getattr(self, "_captured_content_types_snapshot", {})
