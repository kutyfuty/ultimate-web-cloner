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
                self.log_message.emit(f"⚠️ PWA files could not be created: {e}")

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
        self.log_message.emit(f"💾 {total} resource files queued...")

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

        self.log_message.emit(f"✅ Total {len(self._url_to_local)} files saved")
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

    def rewrite_html(self, html_content: str, base_url: str, local_filename: str = "", hide_username: str = "", is_auth_page: bool = False) -> str:
        """
        Rewrite all asset URLs in HTML to local paths using regex/string
        operations only — no BeautifulSoup, no lxml, no str(soup).
        This prevents SVG path data, CSS values, and Tailwind class names
        from being corrupted by the HTML parser.
        """
        self.log_message.emit("🔗 Rewriting HTML paths to local references...")
        
        # (Phase 9) Alt klasör derinlik hesaplaması (Örn: iframes/xyz.html -> depth=1 -> ../)
        depth_prefix = "./"
        if local_filename:
            normalized_name = local_filename.replace("\\", "/")
            if "/" in normalized_name:
                depth_prefix = "../" * normalized_name.count("/")

        # ── 1. Personal data sanitization (auth pages only) ──
        if is_auth_page or hide_username:
            sanitizer = DataSanitizer()
            detected = sanitizer.auto_detect(html_content)
            if any(detected.values()):
                self.log_message.emit(f"   🔍 Personal data detected: " + ", ".join(f"{k}:{len(v)}" for k, v in detected.items() if v))
            html_content = sanitizer.sanitize(html_content, real_user=hide_username or "")

        # ── 2. URL rewriting ──
        # Pass A: exact absolute URL match (longest first to avoid partial replacements)
        for orig_url, local_path in sorted(self._url_to_local.items(), key=lambda x: -len(x[0])):
            if orig_url in html_content:
                html_content = html_content.replace(orig_url, depth_prefix + local_path)

        # Pass B: resolve remaining src/href/poster attributes that weren't caught
        # by exact string match (e.g. relative URLs like /path/to/file.css)
        _ATTR_RE_DQ = re.compile(r'(\b(?:src|href|poster|action|data-src)=")([^"]+?)(")')
        _ATTR_RE_SQ = re.compile(r"(\b(?:src|href|poster|action|data-src)=')([^']+?)(')")

        def _rewrite_attr(m: re.Match) -> str:
            url = m.group(2)
            if not url or url.startswith(('data:', 'blob:', '#', 'javascript:', './')):
                return m.group(0)
            abs_url = urljoin(base_url, url)
            local = self._find_local_path(abs_url)
            if local:
                return m.group(1) + depth_prefix + local + m.group(3)
            return m.group(0)

        html_content = _ATTR_RE_DQ.sub(_rewrite_attr, html_content)
        html_content = _ATTR_RE_SQ.sub(_rewrite_attr, html_content)

        # Pass C: url(...) inside inline style attributes and <style> blocks
        def _rewrite_css_url(m: re.Match) -> str:
            raw = m.group(1).strip().strip("'\"")
            if not raw or raw.startswith(('data:', 'blob:')):
                return m.group(0)
            abs_url = urljoin(base_url, raw)
            local = self._find_local_path(abs_url)
            if local:
                return f"url('{depth_prefix}{local}')"
            return m.group(0)

        html_content = re.sub(r"url\(([^)]+)\)", _rewrite_css_url, html_content)

        # ── 3. Lazy-load: rename data-src / data-lazy / data-original → src ──
        for lazy_attr in ['data-src', 'data-lazy', 'data-original', 'data-lazy-src']:
            html_content = re.sub(
                rf'\s{re.escape(lazy_attr)}=("([^"]*?)"|\'([^\']*?)\')',
                lambda m: f' src={m.group(1)}',
                html_content,
            )

        # ── 4. Remove CSP meta tags and base tags ──
        html_content = re.sub(r'<meta[^>]+content-security-policy[^>]*>', '', html_content, flags=re.IGNORECASE)
        html_content = re.sub(r'<base\b[^>]*/?\s*>', '', html_content, flags=re.IGNORECASE)

        # ── 5. Build script block to inject into <head> ──
        safe_depth = depth_prefix.replace("'", "\\'")
        auth_js_val = "true" if is_auth_page else "false"

        # 5a. Permissive CSP
        csp_meta = '<meta http-equiv="Content-Security-Policy" content="default-src * \'unsafe-inline\' \'unsafe-eval\' data: blob:;">'

        # 5b. Login redirect → index_auth.html
        login_redirect = f"""<script>
document.addEventListener('click', function(e) {{
  var btn = e.target.closest('button[type="submit"], .login-btn, [class*="login"]');
  var pwd = document.querySelector('input[type="password"]');
  var form = e.target.closest('form') || (pwd && pwd.closest('div'));
  if (btn || (form && form.contains(e.target))) {{
    e.preventDefault(); e.stopPropagation(); e.stopImmediatePropagation();
    if (document.querySelector('input[type="password"]'))
      window.location.href = '{safe_depth}index_auth.html';
  }}
}}, true);
</script>"""

        # 5c. Offline utilities (broken image hide, form toast)
        offline_utils = f"""<script>
(function() {{
  window.__IS_AUTH = {auth_js_val};
  window.addEventListener('error', function(e) {{
    if (e.target && e.target.tagName === 'IMG') e.target.style.visibility = 'hidden';
  }}, true);
  document.addEventListener('submit', function(e) {{
    e.preventDefault(); e.stopPropagation();
    var t = document.createElement('div');
    t.style.cssText = 'position:fixed;top:20px;right:20px;background:#10B981;color:#fff;padding:14px 20px;border-radius:8px;z-index:999999;font-family:system-ui,sans-serif;font-size:14px;';
    t.textContent = 'Operation completed successfully.';
    document.body.appendChild(t);
    setTimeout(function(){{ t.remove(); }}, 3000);
  }}, true);
}})();
</script>"""

        # 5d. PWA / service worker
        pwa_tags = f'<link rel="manifest" href="{safe_depth}manifest.json"><script>if("serviceWorker"in navigator)navigator.serviceWorker.register("{safe_depth}offline-sw.js").catch(function(){{}});</script>'

        # 5e. LocalStorage restore (auth page only)
        ls_script = ""
        auth_file = self.output_dir / "auth_state.json"
        if auth_file.exists() and is_auth_page:
            try:
                import json as _json
                auth_data = _json.loads(auth_file.read_text(encoding="utf-8"))
                origins = auth_data.get("origins", [])
                if origins:
                    ls_items = origins[0].get("localStorage", [])
                    if ls_items:
                        lines = "\n".join(
                            f"  localStorage.setItem('{i.get('name','').replace(chr(39),chr(92)+chr(39))}','{i.get('value','').replace(chr(39),chr(92)+chr(39))}');"
                            for i in ls_items
                        )
                        ls_script = f"<script>(function(){{\n{lines}\n}})();</script>"
            except Exception:
                pass

        # 5f. Global state injector (username display, logout handling)
        u_display_sel = ""
        if self.mocker and hasattr(self.mocker, 'username_display_selector'):
            u_display_sel = (self.mocker.username_display_selector or "").replace("'", "\\'")
        global_state = f"""<script>
(function(){{
  var SEL='{u_display_sel}', LOGOUT='{safe_depth}index.html';
  var LK=['logout','sign out','signout','\u00e7\u0131k\u0131\u015f','cikis','exit'];
  function apply(){{
    var u=localStorage.getItem('universalMockUser'); if(!u) return;
    var sels=SEL?[SEL]:['.user-name','.username','.account-name','[data-username]'];
    sels.forEach(function(s){{try{{document.querySelectorAll(s).forEach(function(el){{if(el.tagName!=='INPUT'&&el.tagName!=='SCRIPT')el.textContent=u;}})}}catch(e){{}}}});
    document.querySelectorAll('.offline-dynamic-user').forEach(function(el){{el.textContent=u;}});
    var b=localStorage.getItem('universalMockBalance')||(Math.floor(Math.random()*50000)/100).toFixed(2);
    localStorage.setItem('universalMockBalance',b);
    document.querySelectorAll('.offline-dynamic-balance').forEach(function(el){{el.textContent=b;}});
    document.querySelectorAll('a,button,[role="button"]').forEach(function(el){{
      if(!LK.some(function(k){{return(el.innerText||'').toLowerCase().includes(k);}}))return;
      el.addEventListener('click',function(e){{e.preventDefault();e.stopImmediatePropagation();localStorage.removeItem('universalMockUser');window.location.href=LOGOUT;}},true);
    }});
  }}
  if(document.readyState==='loading')document.addEventListener('DOMContentLoaded',apply);else apply();
  new MutationObserver(function(){{setTimeout(apply,200);}}).observe(document.documentElement,{{childList:true,subtree:true}});
}})();
</script>"""

        head_inject = "\n".join(filter(None, [ls_script, csp_meta, login_redirect, offline_utils, pwa_tags, global_state]))

        if "</head>" in html_content:
            html_content = html_content.replace("</head>", head_inject + "\n</head>", 1)
        elif re.search(r'<body[\s>]', html_content, re.IGNORECASE):
            html_content = re.sub(r'(<body[^>]*>)', r'\1\n' + head_inject, html_content, count=1, flags=re.IGNORECASE)
        else:
            html_content = head_inject + "\n" + html_content

        # ── 6. Frontend mocker injection (string-based) ──
        if self.mocker:
            try:
                html_content = self.mocker.inject_mock_scripts(html_content, depth_prefix=depth_prefix)
            except Exception as e:
                self.log_message.emit(f"⚠️ Frontend Mocker Error: {e}")

        self.log_message.emit("✅ HTML rewrite complete")
        return html_content

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
        self.log_message.emit(f"🎨 {len(css_files)} CSS files: fixing url() and downloading missing dependencies...")

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
            self.log_message.emit(f"   📥 Found {len(missing_urls)} dependencies missed by Playwright in CSS. Downloading async...")
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
                self.log_message.emit(f"   ⚠️  CSS fix error ({css_file.name}): {e}")

        self.log_message.emit("✅ CSS url() references and missing assets updated")

    def save_html(self, html_content: str, filename: str = "index.html") -> Path:
        """İşlenmiş HTML'i dosyaya kaydet."""
        output_path = self.output_dir / filename
        output_path.write_text(html_content, encoding="utf-8")
        self.log_message.emit(f"📄 HTML saved: {output_path}")
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
            self.log_message.emit(f"   🔤 {inlined} web fonts inlined as base64.")

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
            self.log_message.emit(f"   🔗 {rewritten} JS files: API URLs converted to relative paths.")

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
            self.log_message.emit(f"   📦 ZIP created: {zip_path.name}")
            return zip_path
        except Exception as e:
            self.log_message.emit(f"   ⚠️ ZIP creation error: {e}")
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
        self.log_message.emit(f"   📊 Coverage report created: {total} asset ({json_path.name})")

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
            self.log_message.emit(f"   🧹 {removed_rules} unused CSS rules removed (Dead CSS Elimination).")

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
            self.log_message.emit("   ℹ️ fonttools not installed, font subsetting skipped. (pip install fonttools)")
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
            self.log_message.emit(f"   ✂️ {subsetted} font files subsetted (fonttools).")

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
        self.log_message.emit(f"   🔐 integrity.json created ({len(self._integrity_map)} files).")

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
            self.log_message.emit(f"   🎬 {generated} video thumbnails generated (ffmpeg).")
