"""
asset_manager.py — Varlık Yöneticisi

Yakalanan ağ kaynaklarını yerel dosya sistemine kaydetme
ve HTML/CSS içindeki tüm URL referanslarını yerel yollara dönüştürme.
"""

import hashlib
import mimetypes
import os
import re
from pathlib import Path
from urllib.parse import unquote, urljoin, urlparse
from functools import lru_cache

from bs4 import BeautifulSoup
from PyQt6.QtCore import QObject, pyqtSignal

from core.frontend_mocker import FrontendMocker
from core.sanitizer import DataSanitizer

import asyncio


class AssetManager(QObject):
    """Varlık indirme, saklama ve yol yeniden yazma yöneticisi."""

    log_message = pyqtSignal(str)
    progress_updated = pyqtSignal(int)

    # ── Dosya uzantısı → klasör eşlemesi ──
    FOLDER_MAP = {
        ".css": "css",
        ".js": "js",
        ".png": "images",
        ".jpg": "images",
        ".jpeg": "images",
        ".gif": "images",
        ".webp": "images",
        ".avif": "images",
        ".svg": "images",
        ".ico": "images",
        ".bmp": "images",
        ".woff": "fonts",
        ".woff2": "fonts",
        ".ttf": "fonts",
        ".eot": "fonts",
        ".otf": "fonts",
        ".mp4": "media",
        ".webm": "media",
        ".mp3": "media",
        ".ogg": "media",
        ".m3u8": "media",
        ".ts": "media",
        ".wasm": "other",
    }

    # Content-type → uzantı yedek eşlemesi
    CONTENT_TYPE_MAP = {
        "text/css": ".css",
        "application/javascript": ".js",
        "text/javascript": ".js",
        "image/png": ".png",
        "image/jpeg": ".jpg",
        "image/gif": ".gif",
        "image/webp": ".webp",
        "image/svg+xml": ".svg",
        "image/apng": ".png",
        "image/avif": ".avif",
        "application/json": ".json",
        "application/octet-stream": ".bin",
        "video/mp4": ".mp4",
        "video/webm": ".webm",
        "image/x-icon": ".ico",
        "font/woff": ".woff",
        "font/woff2": ".woff2",
        "application/font-woff": ".woff",
        "application/font-woff2": ".woff2",
        "font/ttf": ".ttf",
        "application/font-sfnt": ".ttf",
        "application/vnd.apple.mpegurl": ".m3u8",
        "video/mp2t": ".ts",
    }

    def __init__(self, output_dir: str | Path, mocker: FrontendMocker = None, config=None, parent=None):
        super().__init__(parent)
        self.output_dir = Path(output_dir)
        self.assets_dir = self.output_dir / "assets"
        self.mocker = mocker
        self._config = config  # TargetConfig nesnesi
        self._url_to_local: dict[str, str] = {}   # orijinal URL → yerel dosya yolu
        self._filename_counter: dict[str, int] = {}  # çakışma önleme sayacı
        self._fast_lookup_cache: dict[tuple[str, str], str] = {}
        self._lookup_initialized = False
        self._hash_to_local: dict[str, str] = {}   # B13: MD5 → yerel yol (dedup)
        self._integrity_map: dict[str, str] = {}   # B15: yerel yol → SHA256
        
        # ── Pillar 4: Auto-Service Worker (PWA Payload) ──
        self._generate_pwa_files()

    def _generate_pwa_files(self):
        """Klonlanan sitenin yüklenebilir bir PWA (Progressive Web App) olmasını sağlayan dosyaları oluşturur."""
        try:
            # 1. manifest.json
            manifest_path = self.output_dir / "manifest.json"
            manifest_content = {
                "name": "Universal Web Clone",
                "short_name": "Clone",
                "start_url": "./index.html",
                "display": "standalone",
                "background_color": "#000000",
                "theme_color": "#000000",
                "icons": [
                    {
                        "src": "assets/images/favicon.ico",
                        "sizes": "192x192",
                        "type": "image/png"
                    }
                ]
            }
            import json
            manifest_path.write_text(json.dumps(manifest_content, indent=2), encoding="utf-8")
            
            # 2. offline-sw.js (Cache-First / Stale-While-Revalidate Network Strategy)
            sw_path = self.output_dir / "offline-sw.js"
            sw_content = """
const CACHE_NAME = 'universal-clone-v1';

self.addEventListener('install', (event) => {
    self.skipWaiting();
});

self.addEventListener('activate', (event) => {
    event.waitUntil(clients.claim());
});

self.addEventListener('fetch', (event) => {
    // Sadece GET isteklerini cache'le (API mocker post'ları zaten yakalıyor)
    if (event.request.method !== 'GET') return;
    
    event.respondWith(
        caches.match(event.request).then((response) => {
            // Cache'de varsa dön, yoksa ağa git (zaten yerel dosyalar okunduğu için ağa gitmesi de lokal çalışır)
            return response || fetch(event.request).then((fetchRes) => {
                return caches.open(CACHE_NAME).then((cache) => {
                    cache.put(event.request, fetchRes.clone());
                    return fetchRes;
                });
            });
        }).catch(() => {
            // Fallback (örneğin internet yokken ve cache'de de yokken)
            return new Response('Offline', { status: 503, statusText: 'Service Unavailable' });
        })
    );
});
"""
            sw_path.write_text(sw_content.strip(), encoding="utf-8")
        except Exception as e:
            if hasattr(self, 'log_message'):
                self.log_message.emit(f"⚠️ PWA dosyaları oluşturulamadı: {e}")

    def _init_fast_lookup(self):
        """O(1) aramalar için netloc + path sözlüğünü bir kez oluştur."""
        if self._lookup_initialized:
            return
        self._fast_lookup_cache.clear()
        for original_url, local_path in self._url_to_local.items():
            parsed = urlparse(original_url)
            self._fast_lookup_cache[(parsed.netloc, parsed.path)] = local_path
        self._lookup_initialized = True

    # ──────────────────────────────────────────────
    #  PUBLIC API
    # ──────────────────────────────────────────────

    async def save_resources(
        self,
        captured_resources: dict[str, bytes],
        content_types: dict[str, str] | None = None,
    ) -> dict[str, str]:
        """
        Yakalanan tüm kaynakları asenkron ve eşzamanlı olarak diske kaydet.
        """
        import asyncio
        content_types = content_types or {}
        total = len(captured_resources)
        self.log_message.emit(f"💾 {total} kaynak dosyası sıraya alındı...")

        semaphore = asyncio.Semaphore(50)
        
        async def _save_task(url: str, data: bytes, ct: str):
            async with semaphore:
                # Disk IO'sunu bloke etmemesi için to_thread
                local_path = await asyncio.to_thread(self._save_single_resource, url, data, ct)
                if local_path:
                    self._url_to_local[url] = local_path

        tasks = []
        for url, data in captured_resources.items():
            ct = content_types.get(url, "")
            tasks.append(_save_task(url, data, ct))

        if tasks:
            await asyncio.gather(*tasks)

        self.log_message.emit(f"✅ Toplam {len(self._url_to_local)} dosya kaydedildi")
        self._lookup_initialized = False
        return dict(self._url_to_local)

    # ══════════════════════════════════════════════════════════════
    # STATİK MÜHÜRLEME (Static Sealing) — React/Vue bağlarını kopar
    # ══════════════════════════════════════════════════════════════

    # Framework'lerin DOM'dan silmek için kullandığı attribute pattern'leri
    _FRAMEWORK_ATTR_PATTERNS = re.compile(
        r'^(data-v-|data-reactid|data-react|ng-|v-|:|@|_v-|data-ng|'
        r'data-bind|data-action|data-controller|data-target|'
        r'x-data|x-bind|x-on|x-show|x-if|x-ref|'
        r'wire:|hx-|data-turbo|data-stimulus)',
        re.IGNORECASE
    )

    def _seal_static_elements(self, soup: BeautifulSoup) -> int:
        """
        Config'ten gelen seçicilerle eşleşen elementleri bulur,
        üzerlerindeki tüm framework niteliklerini temizler ve
        zorla görünür hale getirir.
        
        Returns: mühürlenen element sayısı
        """
        if not self._config:
            return 0

        selectors_to_seal = []
        sels = self._config.selectors if hasattr(self._config, 'selectors') else None
        if not sels:
            return 0

        # Config'ten gelen seçicileri topla
        if hasattr(sels, 'login_button') and sels.login_button:
            selectors_to_seal.append(sels.login_button)
        if hasattr(sels, 'register_button') and sels.register_button:
            selectors_to_seal.append(sels.register_button)
        if hasattr(sels, 'preserve_nav') and sels.preserve_nav:
            selectors_to_seal.append(sels.preserve_nav)

        if not selectors_to_seal:
            # Config'te seçici yoksa, yaygın login/register butonlarını otomatik bul
            selectors_to_seal = [
                'a[href*="login"]', 'a[href*="register"]', 'a[href*="signup"]',
                'a[href*="giris"]', 'a[href*="kayit"]', 'a[href*="uye-ol"]',
                'button[class*="login"]', 'button[class*="register"]',
                'button[class*="sign"]', '[class*="login-btn"]', '[class*="register-btn"]',
            ]

        sealed_count = 0

        for selector in selectors_to_seal:
            try:
                elements = soup.select(selector)
            except Exception:
                continue

            for el in elements:
                # ── 1. Framework niteliklerini temizle ──
                attrs_to_remove = []
                for attr_name in el.attrs:
                    if self._FRAMEWORK_ATTR_PATTERNS.match(attr_name):
                        attrs_to_remove.append(attr_name)
                for attr_name in attrs_to_remove:
                    del el[attr_name]

                # ── 2. İç elementlerdeki framework niteliklerini de temizle ──
                for child in el.find_all(True):
                    child_attrs_to_remove = []
                    for attr_name in child.attrs:
                        if self._FRAMEWORK_ATTR_PATTERNS.match(attr_name):
                            child_attrs_to_remove.append(attr_name)
                    for attr_name in child_attrs_to_remove:
                        del child[attr_name]

                # ── 3. Zorla görünür yap ──
                existing_style = el.get("style", "")
                force_css = "display: flex !important; visibility: visible !important; opacity: 1 !important;"
                if existing_style:
                    el["style"] = existing_style.rstrip(";") + "; " + force_css
                else:
                    el["style"] = force_css

                sealed_count += 1

        return sealed_count

    def rewrite_html(self, html_content: str, base_url: str, local_filename: str = "", hide_username: str = "", is_auth_page: bool = False) -> str:
        """
        HTML içindeki tüm URL referanslarını yerel yollarla değiştir.

        İşlenen attribute'lar:
        - src, href, srcset, poster, data-src, data-srcset
        - style attribute'ındaki url(...)
        - <style> blokları içindeki url(...)
        - <link> ve <script> etiketlerinin href/src'leri
        """
        self.log_message.emit("🔗 HTML yolları yerel referanslara dönüştürülüyor...")
        
        # (Phase 9) Alt klasör derinlik hesaplaması (Örn: iframes/xyz.html -> depth=1 -> ../)
        depth_prefix = "./"
        if local_filename:
            normalized_name = local_filename.replace("\\", "/")
            if "/" in normalized_name:
                depth = normalized_name.count("/")
                depth_prefix = "../" * depth

        # ── Tam Veri Maskeleme (Sanitizer) ──
        # Kullanıcı adı + otomatik bakiye + telefon + e-posta + kripto + IBAN
        sanitizer = DataSanitizer()
        detected = sanitizer.auto_detect(html_content)
        if any(detected.values()):
            found_summary = ", ".join(
                f"{k}:{len(v)}" for k, v in detected.items() if v
            )
            self.log_message.emit(f"   🔍 Kişisel veri tespiti: {found_summary}")

        html_content = sanitizer.sanitize(html_content, real_user=hide_username or "")
        if hide_username and len(hide_username) >= 2:
            self.log_message.emit(f"   🔐 '{hide_username}' + bakiye/telefon/email/kripto maskelendi")

        # ── SVG Koruma: lxml parse sırasında inline SVG'ler bozulabilir.
        # Tüm <svg>...</svg> bloklarını placeholder ile değiştir, parse sonrası geri koy.
        svg_blocks: list[str] = []
        svg_placeholder_re = re.compile(r'<svg[\s>].*?</svg>', re.DOTALL | re.IGNORECASE)

        def _extract_svg(m: re.Match) -> str:
            idx = len(svg_blocks)
            svg_blocks.append(m.group(0))
            return f'<div data-svg-placeholder="{idx}"></div>'

        html_content = svg_placeholder_re.sub(_extract_svg, html_content)

        soup = BeautifulSoup(html_content, "lxml")

        # ── Statik Mühürleme: Guest-mode sayfalarında login/register butonlarını koru ──
        if not is_auth_page:
            sealed = self._seal_static_elements(soup)
            if sealed > 0:
                self.log_message.emit(f"   🔒 {sealed} element statik mühürlendi (React/Vue bağları koparıldı)")

        # ── (Phase 21) Smart Offline & Script Lobotomy — Head Düzenlemeleri ──
        head = soup.find("head")
        if head:
            # 1. Anti-Duplication (Çiftleme Katili - Gözlemci)
            # Sitenin orijinal JS'inin aynı elementleri çiftlemesini engeller.
            anti_dup_script = soup.new_tag("script")
            anti_dup_script.string = """
window.addEventListener('DOMContentLoaded', () => {
  const observer = new MutationObserver((mutations) => {
    mutations.forEach((mutation) => {
      mutation.addedNodes.forEach((node) => {
        if (node.nodeType === 1 && node.previousElementSibling) {
          // Eğer yeni eklenen element, bir önceki elementle BİREBİR aynı HTML'e sahipse, onu anında yok et
          if (node.outerHTML === node.previousElementSibling.outerHTML) {
            node.remove();
          }
        }
      });
    });
  });
  observer.observe(document.body, { childList: true, subtree: true });
});
            """
            head.insert(0, anti_dup_script)

            # 2. Kök Dizin Yönlendirmesi ve Global Yakalayıcı (404 Çözümü)
            redirect_script = soup.new_tag("script")
            redirect_script.string = f"""
document.addEventListener('click', function(e) {{
  const submitBtn = e.target.closest('button[type="submit"], .login-btn, [class*="login"]');
  const form = e.target.closest('form') || document.querySelector('input[type="password"]')?.closest('div');

  if (submitBtn || (form && form.contains(e.target))) {{
    e.preventDefault(); e.stopPropagation(); e.stopImmediatePropagation();
    const pwdInput = document.querySelector('input[type="password"]');
    if (pwdInput) {{
      // Redirect to the pre-cloned authenticated view
      window.location.href = '{depth_prefix}index_auth.html';
    }}
  }}
}}, true);
            """
            head.insert(1, redirect_script)

            # 3. Permissive CSP meta tag — localhost'ta CORS hatalarını susturur
            existing_csp = head.find("meta", attrs={"http-equiv": "Content-Security-Policy"})
            if not existing_csp:
                csp_tag = soup.new_tag("meta")
                csp_tag["http-equiv"] = "Content-Security-Policy"
                csp_tag["content"] = "default-src * 'unsafe-inline' 'unsafe-eval' data: blob:;"
                head.insert(2, csp_tag)

            # Viewport — orijinal viewport'u koru, yoksa standart değer ekle
            existing_vp = head.find("meta", attrs={"name": "viewport"})
            if not existing_vp:
                vp_tag = soup.new_tag("meta")
                vp_tag["name"] = "viewport"
                vp_tag["content"] = "width=device-width, initial-scale=1"
                head.insert(3, vp_tag)

            # ── Pillar 4: Auto-Service Worker (PWA Payload) ──
            # 1. Manifest tag'i ekle
            manifest_tag = soup.new_tag("link")
            manifest_tag["rel"] = "manifest"
            manifest_tag["href"] = depth_prefix + "manifest.json"
            head.insert(1, manifest_tag)
            
            # 2. Service Worker Kayıt Script'i
            sw_script = soup.new_tag("script")
            sw_script.string = f"""
if ('serviceWorker' in navigator) {{
    window.addEventListener('load', function() {{
        navigator.serviceWorker.register('{depth_prefix}offline-sw.js').then(function(registration) {{
            console.log('PWA ServiceWorker registration successful with scope: ', registration.scope);
        }}, function(err) {{
            console.log('PWA ServiceWorker registration failed: ', err);
        }});
    }});
}}
"""
            head.insert(2, sw_script)

            # ── Preloader İmha CSS'i ──
            preloader_css = soup.new_tag("style")
            preloader_css.string = """
#preloader, .preloader, .loading, .loader, .spinner, .page-loader,
.loader-wrapper, .loading-screen, .loading-overlay, .splash-screen,
#loader, [data-loader], .sk-spinner, .lds-ring, .pace, .nprogress {
    display: none !important;
    opacity: 0 !important;
    visibility: hidden !important;
    z-index: -9999 !important;
    pointer-events: none !important;
}
body {
    overflow: auto !important;
    position: static !important;
}
html {
    overflow: auto !important;
}
"""
            head.append(preloader_css)

        # ── Preloader DOM Silme (Decompose) ──
        preloader_selectors = [
            '#preloader', '.preloader', '.loader', '.loader-wrapper',
            '.loading-screen', '.loading-overlay', '.splash-screen',
            '#loader', '[data-loader]', '.page-loader', '.spinner',
            '.sk-spinner', '.lds-ring', '.pace', '.nprogress',
        ]
        preloader_removed = 0
        for sel in preloader_selectors:
            for el in soup.select(sel):
                el.decompose()
                preloader_removed += 1
        if preloader_removed > 0:
            self.log_message.emit(f"   🗑️ {preloader_removed} preloader elementi DOM'dan silindi")
        
        # ── Lazy-Load Dönüştürücü (data-src → src) ──
        lazy_attrs = ['data-src', 'data-lazy', 'data-original', 'data-bg', 'data-image']
        lazy_srcset = ['data-srcset', 'data-lazy-srcset']
        lazy_converted = 0
        for tag in soup.find_all(True):
            # data-src → src
            for attr in lazy_attrs:
                val = tag.get(attr)
                if val and not tag.get('src'):
                    tag['src'] = val
                    del tag[attr]
                    lazy_converted += 1
                elif val:
                    del tag[attr]
            # data-srcset → srcset
            for attr in lazy_srcset:
                val = tag.get(attr)
                if val and not tag.get('srcset'):
                    tag['srcset'] = val
                    del tag[attr]
                    lazy_converted += 1
                elif val:
                    del tag[attr]
            # loading="lazy" kaldır
            if tag.get('loading') == 'lazy':
                del tag['loading']

        if lazy_converted > 0:
            self.log_message.emit(f"   🖼️ {lazy_converted} lazy-load attribute → src/srcset dönüştürüldü")

        # ── <base> etiketini bul ve çözümleme için kullan ──
        base_url_for_assets = base_url
        base_tag = soup.find("base")
        if base_tag and base_tag.get("href"):
            b_href = base_tag.get("href")
            base_url_for_assets = urljoin(base_url, b_href)
            
        # Göreceli yollari bozmamak icin base tagini kaldiralim
        for bt in soup.find_all("base"):
            bt.decompose()

        # ── Viewport meta etiketi ekle (mobil uyumluluk) ──
        if not soup.find("meta", attrs={"name": "viewport"}):
            head = soup.find("head")
            if head:
                from bs4 import Tag
                viewport_meta = Tag(name="meta")
                viewport_meta["name"] = "viewport"
                viewport_meta["content"] = "width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no"
                head.insert(0, viewport_meta)

        # ── İllüzyonist Motoru (Sadece Temel Araçlar — Klon Kalitesi İçin) ──
        illusion_script = """
        (() => {
            console.log('[Cloner] İllüzyonist Motoru Başlatılıyor...');

            // 1. ZAMAN YOLCULUĞU (TIME-TRAVEL)
            // Countdown/geri sayım mekanizmalarını sonsuz döngüde tutar
            const originalDateNow = Date.now;
            const START_TIME = originalDateNow();
            Date.now = function() {
                let elapsed = originalDateNow() - START_TIME;
                let loopedElapsed = elapsed % (5 * 60 * 1000);
                return START_TIME + loopedElapsed;
            };

            // 2. FORM SPOOFER (Başarılı Toast)
            // Herhangi bir form gönderildiğinde çökmeyi önler
            const showSuccessToast = (message = "İşleminiz başarıyla gerçekleştirildi.") => {
                let toast = document.createElement('div');
                toast.innerHTML = `
                    <div style="position:fixed; top:20px; right:20px; max-width: 350px; background:#10B981; color:white; padding:16px 24px; border-radius:8px; display:flex; align-items:center; gap:12px; font-family:system-ui,-apple-system,sans-serif; font-size:15px; font-weight:500; z-index:999999; box-shadow:0 10px 15px -3px rgba(0,0,0,0.1); transform:translateX(120%); transition:transform 0.4s cubic-bezier(0.175, 0.885, 0.32, 1.275);">
                        <svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><path d="M22 11.08V12a10 10 0 1 1-5.93-9.14"></path><polyline points="22 4 12 14.01 9 11.01"></polyline></svg>
                        <span>${message}</span>
                    </div>
                `;
                document.body.appendChild(toast);
                requestAnimationFrame(() => {
                    requestAnimationFrame(() => {
                        toast.firstElementChild.style.transform = 'translateX(0)';
                    });
                });
                setTimeout(() => {
                    toast.firstElementChild.style.transform = 'translateX(120%)';
                    setTimeout(() => toast.remove(), 400);
                }, 3000);
            };

            // Form submit'lerini yakala
            document.addEventListener('submit', (e) => {
                e.preventDefault();
                e.stopPropagation();
                showSuccessToast();
            }, true);

            // 3. KIRIK GÖRSELLERİ GİZLE
            window.addEventListener('error', function(e) {
                if (e.target && e.target.tagName === 'IMG') {
                    e.target.style.display = 'none';
                    e.target.style.opacity = '0';
                    e.target.style.visibility = 'hidden';
                }
            }, true);

            // 4. DİNAMİK İSİM DEĞİŞTİRİCİ (Username Spoofer)
            // LocalStorage'daki ismi bulur ve sayfadaki 'Misafir' veya 'Log In' alanlarına yerleştirir
            const updateDynamicUsernames = () => {
                const injectedUser = "__CLONER_USERNAME__";
                const mockUser = localStorage.getItem('universalMockUser') || injectedUser || localStorage.getItem('__cloner_source_username');
                if (mockUser && mockUser !== "Misafir" && !mockUser.includes("__CLONER")) {
                    // Sayfadaki 'Misafir', 'Giriş', 'Login', 'Üye Ol' içeren metinleri tara
                    const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT, null, false);
                    let node;
                    while(node = walker.nextNode()) {
                        if (node.parentElement.tagName === 'SCRIPT' || node.parentElement.tagName === 'STYLE') continue;
                        const text = node.nodeValue;
                        if (text.includes('Misafir') || text.includes('Giriş Yap') || text.includes('Login') || text.includes('Register')) {
                            node.nodeValue = text.replace(/Misafir|Giriş Yap|Login|Register/g, mockUser);
                        }
                    }
                }
            };
            window.addEventListener('load', updateDynamicUsernames);
            setTimeout(updateDynamicUsernames, 1000); 

            // 5. AUTH STATE
            window.__IS_AUTH = __IS_AUTH_TARGET__;

        })();
        """
        # Phase 20: is_auth_page değişkenini JS içine göm
        auth_js_val = "true" if is_auth_page else "false"
        illusion_script = illusion_script.replace("__IS_AUTH_TARGET__", auth_js_val)
        illusion_script = illusion_script.replace("__CLONER_USERNAME__", hide_username or "")
        
        head = soup.find("head")
        if head:
            illusion_tag = soup.new_tag("script")
            illusion_tag.string = illusion_script
            head.insert(0, illusion_tag)

        # ── State Stealer: LocalStorage Geri Yükleme (Phase 12) ──
        # Phase 20: Sadece index_auth.html'ye (is_auth_page=True) enjekte et!
        # index.html'in temiz/çıkış yapılmış kalması gerekiyor.
        auth_file = self.output_dir / "auth_state.json"
        if auth_file.exists() and is_auth_page:
            head = soup.find("head")
            if head:
                try:
                    import json
                    auth_data = json.loads(auth_file.read_text(encoding="utf-8"))
                    origins = auth_data.get("origins", [])
                    if origins:
                        # Extract localStorage from the first origin
                        ls_items = origins[0].get("localStorage", [])
                        if ls_items:
                            ls_script = "(() => {\n"
                            ls_script += "  console.log('[Cloner] Geri yüklenen LocalStorage verileri...');\n"
                            for item in ls_items:
                                key = item.get("name", "").replace("'", "\\'")
                                val = item.get("value", "").replace("'", "\\'")
                                ls_script += f"  localStorage.setItem('{key}', '{val}');\n"
                            ls_script += "})();"
                            
                            from bs4 import Tag
                            script_tag = soup.new_tag("script")
                            script_tag.string = ls_script
                            head.insert(0, script_tag)
                            self.log_message.emit("🔄 State Stealer: LocalStorage verileri index_auth.html içine gömüldü.")
                except Exception as e:
                    self.log_message.emit(f"⚠️ LocalStorage gömme hatası: {e}")

        # ── God Mode Phase 12: HLS Video Kesintisiz Döngü (Live Stream Spoofer) ──
        # Bu sistem, çekilen ".m3u8" videolarının çevrimdışıyken hata vermesi yerine
        # tarayıcıda sonsuza kadar döngüde oynamasını sağlar.
        
        # (Phase 7) Redundant CSP Removed - Already handled in Phase 21 Block

        hls_script = """
        document.addEventListener('DOMContentLoaded', () => {
            const hlsPlayerBase = "https://cdn.jsdelivr.net/npm/hls.js@latest";
            let hlsInjected = false;

            const makeVideoLoop = (video) => {
                video.autoplay = true;
                video.loop = true;
                video.muted = true;
                video.controls = false;
                video.setAttribute('playsinline', '');
                video.style.pointerEvents = 'none';

                let src = video.getAttribute('src');
                let sourceUrl = src;

                if (!src) {
                    let sourceTag = video.querySelector('source');
                    if (sourceTag) sourceUrl = sourceTag.getAttribute('src');
                }

                if (sourceUrl && sourceUrl.includes('.m3u8')) {
                    if (video.canPlayType('application/vnd.apple.mpegurl')) {
                        // Native HLS (Safari)
                        video.src = sourceUrl;
                    } else {
                        // HLS.js polyfill
                        if (!hlsInjected) {
                            let script = document.createElement('script');
                            script.src = hlsPlayerBase;
                            script.onload = () => bindHls(video, sourceUrl);
                            document.head.appendChild(script);
                            hlsInjected = true;
                        } else if (window.Hls) {
                            bindHls(video, sourceUrl);
                        } else {
                            setTimeout(() => { if (window.Hls) bindHls(video, sourceUrl); }, 500);
                        }
                    }
                }
            };

            const bindHls = (video, url) => {
                if (Hls.isSupported()) {
                    const hls = new Hls({ debug: false, maxBufferLength: 5 });
                    hls.loadSource(url);
                    hls.attachMedia(video);
                    hls.on(Hls.Events.MANIFEST_PARSED, () => video.play().catch(e=>console.log(e)));
                    hls.on(Hls.Events.ERROR, (e, data) => {
                        if (data.fatal && data.type === Hls.ErrorTypes.NETWORK_ERROR) {
                            console.log('[Cloner] HLS Loop Restoring...');
                            hls.startLoad();
                        }
                    });
                }
            };

            document.querySelectorAll('video').forEach(makeVideoLoop);

            // Dinamik yüklenen videolar için
            new MutationObserver((mutations) => {
                mutations.forEach(m => {
                    m.addedNodes.forEach(n => {
                        if (n.tagName === 'VIDEO') makeVideoLoop(n);
                        else if (n.querySelectorAll) {
                            n.querySelectorAll('video').forEach(makeVideoLoop);
                        }
                    });
                });
            }).observe(document.body, {childList: true, subtree: true});
        });
        """
        head = soup.find("head")
        if head:
            script_tag = soup.new_tag("script")
            script_tag.string = hls_script
            head.insert(0, script_tag)

        # ── (Phase 7) Lazy-Load Neutralization ──
        # data-src, data-lazy, data-original gibi nitelikleri src'ye zorla (promote)
        lazy_attrs = [
            "data-src", "data-lazy", "data-original", "data-lazy-src", 
            "data-srcset", "data-bg", "data-background-image", "data-img-url"
        ]

        # ── src, href attribute'ları ──
        for tag in soup.find_all(True):
            # Tembel yükleme iptali: data-src'yi src yap
            for l_attr in lazy_attrs:
                val = tag.get(l_attr)
                if val and isinstance(val, str):
                    if tag.name == "img":
                        tag["src"] = val
                    elif tag.name == "source":
                        tag["src"] = val
                    elif "background" in l_attr:
                        tag["style"] = (tag.get("style", "") + f"; background-image: url('{val}');").strip(" ;")

            for attr in ["src", "href", "poster"] + lazy_attrs:
                value = tag.get(attr)
                if not value or not isinstance(value, str):
                    continue

                value = value.strip()

                # Phantom/boş referansları atla (BS4 ayrıştırma artefaktları)
                if value in ('//:0', '//:', '', '#', 'javascript:void(0)', 'javascript:;'):
                    continue

                # srcset özel formatı (virgülle ayrılmış)
                if attr in ("srcset", "data-srcset"):
                    new_srcset = self._rewrite_srcset(value, base_url, depth_prefix)
                    tag[attr] = new_srcset
                    continue

                # Normal URL
                abs_url = urljoin(base_url_for_assets, value)
                local = self._find_local_path(abs_url)
                if local:
                    tag[attr] = depth_prefix + local

            # style attribute'ındaki url(...)
            style_val = tag.get("style")
            if style_val and isinstance(style_val, str) and "url(" in style_val:
                tag["style"] = self._rewrite_css_urls(style_val, base_url_for_assets, depth_prefix=depth_prefix)

        # ── <style> blokları ──
        for style_tag in soup.find_all("style"):
            if style_tag.string:
                style_tag.string = self._rewrite_css_urls(style_tag.string, base_url_for_assets, depth_prefix=depth_prefix)

        # ── <link rel="stylesheet"> -> yerel CSS'e dönüştür ──
        # Bu zaten yukarıdaki href döngüsünde yakalanıyor.

        # ── (Phase 21) Harici Script Temizleme — Sadece indirilemeyenler ──
        # Yerel yola dönüştürülemeyen (hala http/https ile başlayan) scriptleri kaldır.
        # İndirilen ve yerel yola yazılan scriptlere dokunma.
        lobotomy_count = 0
        for script in soup.find_all('script', src=True):
            src_val = script.get('src', '')
            # Hala dışarıya işaret ediyorsa (rewriting başarısız oldu) kaldır
            if src_val.startswith(('http://', 'https://', '//')):
                script.decompose()
                lobotomy_count += 1
        if lobotomy_count > 0:
            self.log_message.emit(f"   ✂️  {lobotomy_count} yerelleştirilemeyen harici script kaldırıldı")

        # ── Orijinal JavaScript'i Koru (Alive JS) & Tracking Kodlarını Sustur ──
        # Not: Yukarıdaki lobotomi framework dosyalarını sildi, bu kısım diğer yardımcı JS'leri ve casusları işler.
        tracking_domains = [
            "yandex", "google-analytics.com", "googletagmanager.com",
            "facebook", "doubleclick.net", "zendesk", "tawk.to",
            "crisp.chat", "hotjar", "clarity.ms", "smartsupp", "tidio"
        ]
        
        removed_trackers = 0
        for tag in soup.find_all(["img", "iframe", "link", "script"]):
            src = tag.get("src", "") or tag.get("href", "")
            # Harici src'li casusluk (Script Lobotomisi src scriptleri sildi ama img/iframe/link kalabilir)
            if src and any(domain in src.lower() for domain in tracking_domains):
                tag.decompose()
                removed_trackers += 1
            # Satıriçi (inline) script casusluk
            elif tag.name == "script" and tag.string and any(domain in tag.string.lower() for domain in tracking_domains):
                tag.decompose()
                removed_trackers += 1
                
        if removed_trackers > 0:
            self.log_message.emit(f"   🗑️  {removed_trackers} casus/tracking kodu yokedildi")

        # ── Inline event handler'larını BİLİNÇLİ Koru ──
        # Eskiden tüm onclick/onload siliyorduk, bu menüleri ve sekmeleri bozuyordu.
        # Artık sadece dışarıya giden veya tracking yapan tehlikeli eventleri süzüyoruz.
        inline_events = ["onclick", "onload", "onerror", "onmouseover", "onsubmit"]
        for tag in soup.find_all(True):
            for event_attr in inline_events:
                if tag.has_attr(event_attr):
                    val = tag[event_attr].lower()
                    if "yandex" in val or "google" in val or "facebook" in val or "track" in val:
                        del tag[event_attr]
                    # Mesru scriptler kalsın (örn: onmouseover="this.play()", onclick="toggleMenu()")

        # ── Eksik Favicon'u HTML'e Gömme ──
        # Eğewr sayfanın orijinalinde <link rel="icon"> yoksa ancak biz bir .ico indirdiysek, manuel ekle.
        # Bu sayede file:/// protokülünde (sunucuz) bile sekme ikonu kusursuz görünür.
        if head and not soup.find("link", rel=lambda r: r and "icon" in r.lower()):
            for orig_url, loc_path in self._url_to_local.items():
                if "favicon.ico" in orig_url.lower() or orig_url.lower().endswith(".ico"):
                    fav_tag = soup.new_tag("link", rel="icon", href=depth_prefix + loc_path)
                    head.append(fav_tag)
                    self.log_message.emit("🔖 Favicon etiketi orijinal HTML'e zorla gömüldü.")
                    break

        # ── Phase 17: Kullanıcı Adı Maskeleme (Dynamic Username Spoofer) ──
        if hide_username and len(hide_username) > 2:
            pattern = re.compile(re.escape(hide_username), re.IGNORECASE)
            masked_count = 0
            for text_node in soup.find_all(string=pattern):
                if text_node.parent and text_node.parent.name in ['script', 'style', 'title', 'meta']:
                    continue
                try:
                    new_html = pattern.sub(r'<span class="illusion-username">\g<0></span>', str(text_node))
                    if new_html != str(text_node):
                        new_soup = BeautifulSoup(new_html, 'html.parser')
                        text_node.replace_with(new_soup)
                        masked_count += 1
                except Exception:
                    continue
            if masked_count > 0:
                self.log_message.emit(f"🎭 Maskeleme: {masked_count} alandaki kaynak kullanıcı adı gizlendi.")

        # ── iFrame Sanitizasyonu (Harici Oyun Çökme Önleme) ──
        base_parsed = urlparse(base_url)
        base_domain = base_parsed.netloc
        sanitized_iframes = 0
        for iframe in soup.find_all("iframe"):
            iframe_src = iframe.get("src", "") or ""
            iframe_src = iframe_src.strip()

            # Boş, blob: veya harici domain kontrolü
            is_external = False
            if not iframe_src or iframe_src.startswith("blob:") or iframe_src.startswith("javascript:"):
                is_external = True
            elif iframe_src.startswith("http://") or iframe_src.startswith("https://"):
                iframe_parsed = urlparse(iframe_src)
                if iframe_parsed.netloc and iframe_parsed.netloc != base_domain:
                    is_external = True
            elif iframe_src.startswith("//"):
                iframe_parsed = urlparse("https:" + iframe_src)
                if iframe_parsed.netloc and iframe_parsed.netloc != base_domain:
                    is_external = True

            if is_external:
                # Harici iframe'i placeholder div ile değiştir
                placeholder = soup.new_tag("div")
                placeholder["style"] = (
                    "padding:30px; background:linear-gradient(135deg, #1a1a2e, #16213e); "
                    "color:#e2e8f0; text-align:center; border-radius:12px; "
                    "border:1px solid rgba(255,255,255,0.1); margin:10px 0; "
                    "font-family:system-ui,-apple-system,sans-serif; font-size:15px;"
                )
                inner_icon = soup.new_tag("div")
                inner_icon["style"] = "font-size:40px; margin-bottom:10px;"
                inner_icon.string = "🎮"
                placeholder.append(inner_icon)
                inner_text = soup.new_tag("span")
                inner_text.string = "Bu içerik çevrimdışı kullanımda desteklenmemektedir."
                placeholder.append(inner_text)
                iframe.replace_with(placeholder)
                sanitized_iframes += 1
            else:
                # Dahili iframe'lere sandbox ekle (güvenlik)
                iframe["sandbox"] = "allow-scripts allow-same-origin"

        if sanitized_iframes > 0:
            self.log_message.emit(f"   🛡️ {sanitized_iframes} harici iframe güvenli placeholder ile değiştirildi")

        # ════════════════════════════════════════════════════════════════
        # GLOBAL STATE INJECTOR — TÜM SAYFALARA enjekte edilir
        # localStorage'daki offlineMockUser bilgisini her sayfada uygular
        # ════════════════════════════════════════════════════════════════
        u_display_sel = ""
        if self.mocker and hasattr(self.mocker, 'username_display_selector'):
            u_display_sel = (self.mocker.username_display_selector or "").replace("\\", "\\\\").replace("'", "\\'")

        safe_depth = depth_prefix.replace("\\", "\\\\").replace("'", "\\'")

        global_state_js = f"""
(function() {{
    const DISPLAY_SEL = '{u_display_sel}';
    const LOGOUT_REDIRECT = '{safe_depth}index.html';

    const LOGOUT_KW = ['çıkış','cikis','logout','sign out','signout','exit'];
    const PROCESSED_LOGOUT = new WeakSet();

    function applyState() {{
        const user = localStorage.getItem('universalMockUser');
        if (!user) return;

        // ── İsim değiştirme (sıfır DOM eklentisi) ──
        const sels = DISPLAY_SEL ? [DISPLAY_SEL] : [
            '.user-name', '.username', '.user-info', '.account-name',
            '.profile-name', '.member-name', '[class*="username"]',
            '[class*="user-name"]', '[data-username]', '[data-user]',
        ];
        sels.forEach(s => {{
            try {{ document.querySelectorAll(s).forEach(el => {{
                if (el.tagName !== 'INPUT' && el.tagName !== 'SCRIPT') el.textContent = user;
            }}); }} catch(e) {{}}
        }});

        // ── Sanitizer Placeholder Doldurma ──
        document.querySelectorAll('.offline-dynamic-user').forEach(el => {{ el.textContent = user; }});
        const mockBalance = localStorage.getItem('universalMockBalance') || (Math.floor(Math.random()*50000)/100).toFixed(2);
        localStorage.setItem('universalMockBalance', mockBalance);
        document.querySelectorAll('.offline-dynamic-balance').forEach(el => {{ el.textContent = mockBalance; }});

        // ── Logout: Node Replacement (framework bağlarını kopar) ──
        document.querySelectorAll('a, button, [role="button"]').forEach(el => {{
            const t = (el.innerText || '').toLowerCase().trim();
            if (!LOGOUT_KW.some(k => t.includes(k))) return;
            if (PROCESSED_LOGOUT.has(el)) return;

            const clone = el.cloneNode(true);
            el.parentNode.replaceChild(clone, el);
            PROCESSED_LOGOUT.add(clone);

            clone.addEventListener('click', function(e) {{
                e.preventDefault(); e.stopImmediatePropagation();
                localStorage.removeItem('universalMockUser');
                window.location.href = LOGOUT_REDIRECT;
            }}, true);
        }});
    }}

    if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', applyState);
    else applyState();

    let t = null;
    new MutationObserver(() => {{ clearTimeout(t); t = setTimeout(applyState, 200); }}).observe(document.documentElement, {{ childList: true, subtree: true }});
}})();
"""
        gsi_block = f"<script>{global_state_js}</script>"

        # İlk soup serialize — tek seferlik, çift parse yok
        result = str(soup)

        # ── SVG bloklarını geri yükle (placeholder → orijinal SVG)
        if svg_blocks:
            for idx, svg_src in enumerate(svg_blocks):
                placeholder_tag = f'<div data-svg-placeholder="{idx}"></div>'
                result = result.replace(placeholder_tag, svg_src, 1)

        # (Phase 6) Frontend Mocking Script Injection — string üzerinde çalışır
        if self.mocker:
            try:
                result = self.mocker.inject_mock_scripts(result, depth_prefix=depth_prefix)
            except Exception as e:
                self.log_message.emit(f"⚠️ Frontend Mocker Error: {e}")

        # Global State Injector'ı </body> öncesine string olarak ekle (lxml re-parse yok)
        if "</body>" in result:
            result = result.replace("</body>", gsi_block + "\n</body>", 1)
        elif "</html>" in result:
            result = result.replace("</html>", gsi_block + "\n</html>", 1)
        else:
            result += "\n" + gsi_block

        self.log_message.emit("✅ HTML yolları güncellendi")
        return result

    async def rewrite_css_files(self, base_url: str) -> None:
        """
        Kaydedilmiş CSS dosyalarının içindeki url(...) referanslarını
        yerel yollarla değiştir ve EKSİK resim/fontları asenkron indir.
        """
        import asyncio
        import aiohttp
        
        css_dir = self.assets_dir / "css"
        if not css_dir.exists():
            return

        css_files = list(css_dir.glob("*.css"))
        self.log_message.emit(f"🎨 {len(css_files)} CSS dosyasında url() düzeltiliyor ve eksik bağımlılıklar indiriliyor...")

        missing_urls = set()
        
        # Sütun bazlı tarama ve Queue
        for css_file in css_files:
            try:
                content = css_file.read_text(encoding="utf-8", errors="replace")
                
                # Sadece linkleri toplayan özel regex
                def collect_missing(match):
                    url_val = match.group(1).strip("'\" \n\t")
                    if not url_val or url_val.startswith("data:"):
                        return match.group(0)
                        
                    abs_url = urljoin(base_url, url_val)
                    if not self._find_local_path(abs_url):
                        missing_urls.add(abs_url)
                        
                    return match.group(0)
                        
                import re
                re.sub(r"url\(([^)]+)\)", collect_missing, content)
            except Exception as e:
                pass
                
        # 1. Aşama: Eksiklikleri İndir
        if missing_urls:
            self.log_message.emit(f"   📥 CSS içerisinde Playwright'in kaçırdığı {len(missing_urls)} bağımlılık bulundu. Asenkron indiriliyor...")
            semaphore = asyncio.Semaphore(30)
            
            async def fetch_and_save(session, m_url):
                async with semaphore:
                    for attempt in range(3):
                        try:
                            async with session.get(m_url, timeout=12) as response:
                                if response.status == 200:
                                    data = await response.read()
                                    ctype = response.headers.get("Content-Type", "")
                                    # Awaitable save kullanarak kaydet
                                    local_path = await asyncio.to_thread(
                                        self._save_single_resource, m_url, data, ctype
                                    )
                                    if local_path:
                                        self._url_to_local[m_url] = local_path
                                    break
                                elif response.status in (429, 500, 502, 503, 504):
                                    await asyncio.sleep(2 ** (attempt + 1)) # Exponential backoff
                                    continue
                                else:
                                    break # 404, 403 vb.
                        except Exception as e:
                            if attempt == 2: pass
                            else: await asyncio.sleep(2 ** (attempt + 1))

            async with aiohttp.ClientSession(headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}) as session:
                tasks = [fetch_and_save(session, m_url) for m_url in missing_urls]
                await asyncio.gather(*tasks)
                
            self._lookup_initialized = False
            
        # 2. Aşama: ŞİMDİ Dosyaları Düzenle ve Sabit Yollara Yazdır
        for css_file in css_files:
            try:
                content = css_file.read_text(encoding="utf-8", errors="replace")
                new_content = self._rewrite_css_urls(content, base_url, depth_prefix="../../", from_css=True)
                if new_content != content:
                    css_file.write_text(new_content, encoding="utf-8")
            except Exception as e:
                self.log_message.emit(f"   ⚠️  CSS düzeltme hatası ({css_file.name}): {e}")

        self.log_message.emit("✅ CSS url() referansları ve eksik varlıkları güncellendi")

    def save_html(self, html_content: str, filename: str = "index.html") -> Path:
        """İşlenmiş HTML'i dosyaya kaydet."""
        output_path = self.output_dir / filename
        output_path.write_text(html_content, encoding="utf-8")
        self.log_message.emit(f"📄 HTML kaydedildi: {output_path}")
        return output_path

    # ──────────────────────────────────────────────
    #  PRIVATE HELPERS
    # ──────────────────────────────────────────────

    def _save_single_resource(self, url: str, data: bytes, content_type: str) -> str | None:
        """Tek bir kaynağı yerel dosya sistemine kaydet."""
        try:
            parsed = urlparse(url)
            # Query stringleri sök (örn: ?v=1.2.3) sadece temiz path kalsın
            clean_path = unquote(parsed.path)

            # Uzantıyı belirle
            ext = Path(clean_path).suffix.lower()
            if not ext or len(ext) > 10:
                ext = self._ext_from_content_type(content_type)
            if not ext:
                ext = ".bin"

            # Orijinal dosya adı (basename)
            basename = Path(clean_path).stem
            # Strict Slugification (Geçersiz karakterleri temizle)
            basename = re.sub(r'[^a-zA-Z0-9_\-\.]', '_', basename)
            basename = basename.strip('. _')

            # URL tabanlı benzersiz hash (Çarpışmaları %100 önlemek için)
            # Phase 3 - Perfect Offline URL Rewriting: Collision Prevention
            url_hash = hashlib.md5(url.encode()).hexdigest()[:8]
            
            if not basename or len(basename) > 80:
                final_basename = url_hash
            else:
                final_basename = f"{url_hash}_{basename}"

            # Kategoriye göre alt klasör
            subfolder = self.FOLDER_MAP.get(ext, "other")
            target_dir = self.assets_dir / subfolder
            target_dir.mkdir(parents=True, exist_ok=True)

            filename = f"{final_basename}{ext}"
            target_path = target_dir / filename

            # ── ServiceWorker Neutralizer ──
            # İndirilen JS dosyalarının içindeki ServiceWorker kayıtlarını yorum satırına al
            if ext == ".js":
                try:
                    js_text = data.decode("utf-8")
                    sw_pattern = re.compile(r'(navigator\.serviceWorker\.register\s*\()', re.IGNORECASE)
                    js_text = sw_pattern.sub(r'/* CLONER_BLOCKED_SW */ // \1', js_text)
                    # Source map yönlendirmelerini sil
                    js_text = re.sub(r'\n?//# sourceMappingURL=\S+', '', js_text)
                    # JS dosyasını sanitize et (bakiye, kripto, e-posta)
                    _san = DataSanitizer()
                    js_text = _san.sanitize_js(js_text)
                    data = js_text.encode("utf-8")
                except UnicodeDecodeError:
                    pass

            # CSS source map yönlendirmelerini sil
            if ext == ".css":
                try:
                    css_text = data.decode("utf-8")
                    css_text = re.sub(r'\n?/\*# sourceMappingURL=\S+\s*\*/', '', css_text)
                    data = css_text.encode("utf-8")
                except UnicodeDecodeError:
                    pass

            # ── B11: Görüntü Optimizasyonu (Pillow) ──
            if ext in (".jpg", ".jpeg", ".png", ".webp") and len(data) > 20_000:
                data = self._optimize_image(data, ext)

            # ── B13: Duplicate Deduplication ──
            data_hash = hashlib.md5(data).hexdigest()
            if data_hash in self._hash_to_local:
                existing_path = self._hash_to_local[data_hash]
                self._url_to_local[url] = existing_path
                self._lookup_initialized = False
                return existing_path

            # Dosyayı kaydet
            target_path.write_bytes(data)

            # Hash kaydı (deduplication için)
            self._hash_to_local[data_hash] = str(target_path.relative_to(self.output_dir)).replace("\\", "/")

            # ── B15: Integrity Kaydı ──
            import hashlib as _hl
            sha256 = _hl.sha256(data).hexdigest()
            self._integrity_map[str(target_path.relative_to(self.output_dir)).replace("\\", "/")] = sha256

            # Göreceli yol (HTML'den erişim için)
            relative = target_path.relative_to(self.output_dir)
            self._lookup_initialized = False # Cache invalidate
            return str(relative).replace("\\", "/")

        except Exception:
            return None

    @lru_cache(maxsize=10000)
    def _find_local_path(self, abs_url: str) -> str | None:
        """Verilen URL için yerel dosya yolunu (O(1) hızında) bul."""
        if not self._lookup_initialized:
            self._init_fast_lookup()

        # Tam eşleşme
        if abs_url in self._url_to_local:
            return self._url_to_local[abs_url]

        # Sorgu parametreleri olmadan dene
        parsed = urlparse(abs_url)
        clean_url = f"{parsed.scheme}://{parsed.netloc}{parsed.path}"
        if clean_url in self._url_to_local:
            return self._url_to_local[clean_url]

        # Yol tabanlı kısmi eşleşme O(1) üzerinden
        return self._fast_lookup_cache.get((parsed.netloc, parsed.path))

    def _rewrite_srcset(self, srcset_value: str, base_url: str, depth_prefix: str = "") -> str:
        """srcset attribute'ını yerel yollarla yeniden yaz."""
        parts = []
        for entry in srcset_value.split(","):
            entry = entry.strip()
            if not entry:
                continue
            tokens = entry.split()
            if tokens:
                url = tokens[0]
                abs_url = urljoin(base_url, url)
                local = self._find_local_path(abs_url)
                if local:
                    tokens[0] = depth_prefix + local
                parts.append(" ".join(tokens))
        return ", ".join(parts)

    def _rewrite_css_urls(self, css_content: str, base_url: str, depth_prefix: str = "", from_css: bool = False) -> str:
        """
        CSS içindeki tüm url(...) referanslarını yerel yollarla değiştirir.
        Gelişmiş CSS AST parse eder (cssutils). image-set ve @import destekler.
        """
        import cssutils
        import logging
        
        # cssutils loglarını sustur (parsing hataları can sıkıcı olabilir)
        cssutils.log.setLevel(logging.CRITICAL)
        
        try:
            sheet = cssutils.parseString(css_content, validate=False)
            
            # Tüm URL'leri (url() ve @import) keşfet ve değiştir
            cssutils.stylesheets.CSSStyleSheet.setSerializer(cssutils.serialize.CSSSerializer())
            
            for rule in sheet:
                if rule.type == rule.IMPORT_RULE:
                    abs_url = urljoin(base_url, rule.href)
                    local = self._find_local_path(abs_url)
                    if local:
                        # CSS içinden geliyorsa (import) derinlik ön eki önemli
                        rule.href = (depth_prefix + "/".join(local.split("/")[1:]) if from_css and "/" in local else depth_prefix + local)
                
                elif rule.type == rule.STYLE_RULE:
                    for prop in rule.style:
                        if 'url(' in prop.value:
                            def _replace_cssutils_url(match):
                                val = match.group(1).strip("'\" \n\t")
                                if not val or val.startswith("data:"): return match.group(0)
                                a_url = urljoin(base_url, val)
                                loc = self._find_local_path(a_url)
                                if loc:
                                    lp = (depth_prefix + "/".join(loc.split("/")[1:]) if from_css and "/" in loc else depth_prefix + loc)
                                    return f"url('{lp}')"
                                return match.group(0)
                            
                            prop.value = re.sub(r"url\(([^)]+)\)", _replace_cssutils_url, prop.value)

            # Serileştirme (minify edilmemiş, temiz çıktı)
            return sheet.cssText.decode("utf-8") if isinstance(sheet.cssText, bytes) else sheet.cssText

        except Exception as e:
            # Hata durumunda güvenli regex fallback (Orijinal mantık)
            def replace_url_func(match):
                url_value = match.group(1).strip("'\" \n\t")
                if not url_value or url_value.startswith("data:"):
                    return match.group(0)

                abs_url = urljoin(base_url, url_value)
                local = self._find_local_path(abs_url)

                if local:
                    local_path = (depth_prefix + "/".join(local.split("/")[1:]) if from_css and "/" in local else depth_prefix + local)
                    return f"url('{local_path}')"

                return match.group(0)

            return re.sub(r"url\(([^)]+)\)", replace_url_func, css_content)

    def _ext_from_content_type(self, content_type: str) -> str:
        """Content-Type header'ından dosya uzantısı çıkar."""
        if not content_type:
            return ""
        # Sadece mime tipini al (charset vb. kaldır)
        mime = content_type.split(";")[0].strip().lower()
        return self.CONTENT_TYPE_MAP.get(mime, "")

    # ──────────────────────────────────────────────
    #  WEBFONT BASE64 INLINE
    # ──────────────────────────────────────────────

    def inline_webfonts_as_base64(self) -> None:
        """
        Kaydedilmiş CSS dosyalarındaki yerel font referanslarını
        base64 data URI olarak inline eder. Offline görüntülemede font kayması önlenir.
        """
        import base64
        css_dir = self.assets_dir / "css"
        if not css_dir.exists():
            return
        font_exts = {".woff", ".woff2", ".ttf", ".eot", ".otf"}
        mime_map = {
            ".woff": "font/woff", ".woff2": "font/woff2",
            ".ttf": "font/ttf", ".eot": "application/vnd.ms-fontobject",
            ".otf": "font/otf",
        }
        inlined = 0
        for css_file in css_dir.glob("*.css"):
            try:
                css_text = css_file.read_text(encoding="utf-8", errors="ignore")
                changed = False

                def _replace_font_url(match):
                    nonlocal changed, inlined
                    raw = match.group(1).strip("'\" ")
                    if raw.startswith("data:"):
                        return match.group(0)
                    # Resolve relative to css file location
                    candidate = (css_file.parent / raw).resolve()
                    if not candidate.exists() or candidate.suffix.lower() not in font_exts:
                        return match.group(0)
                    mime = mime_map.get(candidate.suffix.lower(), "font/woff2")
                    b64 = base64.b64encode(candidate.read_bytes()).decode()
                    changed = True
                    inlined += 1
                    return f"url('data:{mime};base64,{b64}')"

                css_text = re.sub(r"url\(([^)]+)\)", _replace_font_url, css_text)
                if changed:
                    css_file.write_text(css_text, encoding="utf-8")
            except Exception:
                pass
        if inlined:
            self.log_message.emit(f"   🔤 {inlined} web font base64 olarak inline edildi.")

    # ──────────────────────────────────────────────
    #  JS API URL REWRITE
    # ──────────────────────────────────────────────

    def rewrite_js_api_urls(self, base_url: str) -> None:
        """
        İndirilen JS dosyalarındaki mutlak API URL'lerini göreceli yollara dönüştürür.
        Örn: 'https://example.com/api/v1' → '/api/v1'
        """
        parsed_base = urlparse(base_url)
        origin = f"{parsed_base.scheme}://{parsed_base.netloc}"
        js_dir = self.assets_dir / "js"
        if not js_dir.exists():
            return
        rewritten = 0
        for js_file in js_dir.glob("*.js"):
            try:
                js_text = js_file.read_text(encoding="utf-8", errors="ignore")
                new_text = js_text.replace(origin, "")
                if new_text != js_text:
                    js_file.write_text(new_text, encoding="utf-8")
                    rewritten += 1
            except Exception:
                pass
        if rewritten:
            self.log_message.emit(f"   🔗 {rewritten} JS dosyasında API URL'leri göreceli yola dönüştürüldü.")

    # ──────────────────────────────────────────────
    #  ZIP PAKET OLUŞTURMA
    # ──────────────────────────────────────────────

    def create_zip(self) -> Path | None:
        """
        Klonlanan site klasörünü tek bir .zip dosyasına sıkıştırır.
        Döndürülen değer zip dosyasının yolu, hata durumunda None.
        """
        import zipfile
        zip_path = self.output_dir.parent / f"{self.output_dir.name}.zip"
        try:
            with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
                for file in self.output_dir.rglob("*"):
                    if file.is_file():
                        zf.write(file, file.relative_to(self.output_dir.parent))
            self.log_message.emit(f"   📦 ZIP oluşturuldu: {zip_path.name}")
            return zip_path
        except Exception as e:
            self.log_message.emit(f"   ⚠️ ZIP oluşturma hatası: {e}")
            return None

    # ──────────────────────────────────────────────
    #  ASSET COVERAGE RAPORU
    # ──────────────────────────────────────────────

    def generate_coverage_report(self) -> None:
        """
        Hangi URL'lerin başarıyla indirildiğini, hangilerinin eksik kaldığını
        JSON + basit HTML raporu olarak output_dir'e yazar.
        """
        import json as _json
        import datetime

        total = len(self._url_to_local)
        by_type: dict[str, int] = {}
        for url, local in self._url_to_local.items():
            ext = Path(local).suffix.lower() or ".other"
            by_type[ext] = by_type.get(ext, 0) + 1

        report = {
            "generated_at": datetime.datetime.now().isoformat(),
            "total_assets": total,
            "by_extension": by_type,
            "asset_map": {url: local for url, local in list(self._url_to_local.items())[:500]},
        }

        json_path = self.output_dir / "coverage_report.json"
        json_path.write_text(_json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

        # Basit HTML raporu
        rows = "".join(
            f"<tr><td>{ext}</td><td>{count}</td></tr>"
            for ext, count in sorted(by_type.items(), key=lambda x: -x[1])
        )
        html = f"""<!DOCTYPE html><html><head><meta charset="utf-8">
<title>Coverage Report</title>
<style>body{{font-family:sans-serif;padding:20px}} table{{border-collapse:collapse;width:100%}}
th,td{{border:1px solid #ccc;padding:8px;text-align:left}} th{{background:#f0f0f0}}</style>
</head><body>
<h1>Asset Coverage Report</h1>
<p>Generated: {report['generated_at']} | Total assets: {total}</p>
<table><tr><th>Extension</th><th>Count</th></tr>{rows}</table>
</body></html>"""
        html_path = self.output_dir / "coverage_report.html"
        html_path.write_text(html, encoding="utf-8")
        self.log_message.emit(f"   📊 Coverage raporu oluşturuldu: {total} asset ({json_path.name})")

    # ──────────────────────────────────────────────
    #  B11 — GÖRÜNTÜ OPTİMİZASYONU (Pillow)
    # ──────────────────────────────────────────────

    def _optimize_image(self, data: bytes, ext: str) -> bytes:
        """
        Pillow ile görüntü sıkıştırma.
        JPEG/WEBP → %82 kalite, PNG → optimize=True.
        Pillow yüklü değilse orijinal veriyi döndürür.
        """
        try:
            from PIL import Image
            import io
            img = Image.open(io.BytesIO(data))
            out = io.BytesIO()
            if ext in (".jpg", ".jpeg"):
                img = img.convert("RGB")
                img.save(out, format="JPEG", quality=82, optimize=True, progressive=True)
            elif ext == ".webp":
                img.save(out, format="WEBP", quality=82, method=4)
            elif ext == ".png":
                img.save(out, format="PNG", optimize=True)
            else:
                return data
            optimized = out.getvalue()
            # Sadece küçüldüyse kullan
            return optimized if len(optimized) < len(data) else data
        except Exception:
            return data

    # ──────────────────────────────────────────────
    #  B12 — CSS DEAD CODE ELİMİNATİON
    # ──────────────────────────────────────────────

    def remove_dead_css(self) -> None:
        """
        Kaydedilmiş CSS dosyalarındaki selector'ları klonlanan HTML dosyalarıyla
        karşılaştırır. Hiçbir HTML'de kullanılmayan selector'ları siler.
        Basit regex tabanlı yaklaşım — tam CSS parser değil, güvenli mod.
        """
        html_files = list(self.output_dir.rglob("*.html"))
        if not html_files:
            return

        # Tüm HTML içeriğini birleştir (selector arama için)
        combined_html = ""
        for hf in html_files:
            try:
                combined_html += hf.read_text(encoding="utf-8", errors="ignore")
            except Exception:
                pass

        css_dir = self.assets_dir / "css"
        if not css_dir.exists():
            return

        removed_rules = 0
        for css_file in css_dir.glob("*.css"):
            try:
                css_text = css_file.read_text(encoding="utf-8", errors="ignore")
                # Basit kural bloğu parser: selector { ... }
                def _keep_rule(match):
                    nonlocal removed_rules
                    selector_block = match.group(1).strip()
                    # @kurallar (media, keyframes, font-face) — dokunma
                    if selector_block.startswith("@"):
                        return match.group(0)
                    # Selector'ı sınıf/ID/tag olarak parçala
                    selectors = re.split(r",", selector_block)
                    for sel in selectors:
                        sel = sel.strip()
                        # Temel class .foo, ID #bar, tag div kontrolü
                        classes = re.findall(r"\.([\w-]+)", sel)
                        ids = re.findall(r"#([\w-]+)", sel)
                        tags = re.findall(r"^([\w]+)", sel)
                        for cls in classes:
                            if cls in combined_html:
                                return match.group(0)
                        for id_ in ids:
                            if id_ in combined_html:
                                return match.group(0)
                        for tag in tags:
                            if f"<{tag}" in combined_html or f"<{tag} " in combined_html:
                                return match.group(0)
                    removed_rules += 1
                    return ""

                new_css = re.sub(
                    r"([^{}]+)\{[^{}]*\}",
                    _keep_rule,
                    css_text,
                    flags=re.DOTALL
                )
                css_file.write_text(new_css, encoding="utf-8")
            except Exception:
                pass

        if removed_rules:
            self.log_message.emit(f"   🧹 {removed_rules} kullanılmayan CSS kuralı silindi (Dead CSS Elimination).")

    # ──────────────────────────────────────────────
    #  B14 — FONT SUBSETTING (fonttools)
    # ──────────────────────────────────────────────

    def subset_fonts(self) -> None:
        """
        İndirilen woff2/ttf fontlarını klonlanan HTML dosyalarında gerçekte
        kullanılan karakter setiyle subset'ler (fonttools/pyftsubset gerekir).
        Büyük fontları dramatik şekilde küçültür.
        """
        try:
            from fontTools.subset import Subsetter, Options
            from fontTools.ttLib import TTFont
        except ImportError:
            self.log_message.emit("   ℹ️ fonttools yüklü değil, font subsetting atlandı. (pip install fonttools)")
            return

        html_files = list(self.output_dir.rglob("*.html"))
        all_text = ""
        for hf in html_files:
            try:
                all_text += hf.read_text(encoding="utf-8", errors="ignore")
            except Exception:
                pass

        # Kullanılan unique karakterler
        chars = set(all_text) - {"\n", "\r", "\t"}
        if not chars:
            return

        font_dir = self.assets_dir / "fonts"
        if not font_dir.exists():
            return

        subsetted = 0
        for font_file in list(font_dir.glob("*.ttf")) + list(font_dir.glob("*.woff2")):
            try:
                options = Options()
                options.flavor = "woff2" if font_file.suffix == ".woff2" else None
                font = TTFont(str(font_file))
                subsetter = Subsetter(options=options)
                subsetter.populate(text="".join(chars))
                subsetter.subset(font)
                font.save(str(font_file))
                subsetted += 1
            except Exception:
                pass

        if subsetted:
            self.log_message.emit(f"   ✂️ {subsetted} font dosyası subset edildi (fonttools).")

    # ──────────────────────────────────────────────
    #  B15 — INTEGRITY RAPORU (SHA-256)
    # ──────────────────────────────────────────────

    def save_integrity_report(self) -> None:
        """
        Tüm indirilen asset'lerin SHA-256 hash'ini integrity.json dosyasına yazar.
        Sonraki klonlamada değişen dosyaları tespit etmek için kullanılır.
        """
        import json as _json
        if not self._integrity_map:
            return
        integrity_path = self.output_dir / "integrity.json"
        integrity_path.write_text(
            _json.dumps(self._integrity_map, ensure_ascii=False, indent=2),
            encoding="utf-8"
        )
        self.log_message.emit(f"   🔐 integrity.json oluşturuldu ({len(self._integrity_map)} dosya).")

    def check_integrity_changes(self) -> list[str]:
        """
        Önceki integrity.json ile karşılaştırarak değişen dosyaları döndürür.
        """
        import json as _json
        integrity_path = self.output_dir / "integrity.json"
        if not integrity_path.exists():
            return []
        try:
            old_map = _json.loads(integrity_path.read_text(encoding="utf-8"))
        except Exception:
            return []
        changed = [
            path for path, sha in self._integrity_map.items()
            if old_map.get(path) and old_map[path] != sha
        ]
        return changed

    # ──────────────────────────────────────────────
    #  B16 — VİDEO THUMBNAIL ÜRETİMİ (ffmpeg)
    # ──────────────────────────────────────────────

    def generate_video_thumbnails(self) -> None:
        """
        İndirilen .mp4/.webm video dosyalarından ffmpeg ile 1. saniye thumbnail üretir.
        Thumbnail, <video> elementlerinin poster attribute'ına HTML rewrite sırasında
        integrity_map üzerinden referans alınabilir.
        ffmpeg PATH'te bulunmalıdır.
        """
        import subprocess as _sp
        media_dir = self.assets_dir / "media"
        thumb_dir = self.assets_dir / "images"
        if not media_dir.exists():
            return

        generated = 0
        for video_file in list(media_dir.glob("*.mp4")) + list(media_dir.glob("*.webm")):
            thumb_path = thumb_dir / f"{video_file.stem}_thumb.jpg"
            if thumb_path.exists():
                continue
            try:
                thumb_dir.mkdir(parents=True, exist_ok=True)
                result = _sp.run(
                    ["ffmpeg", "-y", "-ss", "00:00:01", "-i", str(video_file),
                     "-vframes", "1", "-q:v", "3", str(thumb_path)],
                    capture_output=True, timeout=15
                )
                if result.returncode == 0 and thumb_path.exists():
                    # poster path'ini integrity map'e ekle
                    rel = str(thumb_path.relative_to(self.output_dir)).replace("\\", "/")
                    generated += 1
                    # HTML'de bu video'ya ait <video poster="..."> otomatik olarak
                    # rewrite_html sırasında bulunacak (src eşleşmesiyle)
            except (FileNotFoundError, _sp.TimeoutExpired, Exception):
                pass

        if generated:
            self.log_message.emit(f"   🎬 {generated} video için thumbnail üretildi (ffmpeg).")
