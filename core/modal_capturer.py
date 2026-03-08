"""
modal_capturer.py — Automatic Login/Register Modal Capture

Automatically detects and clones login and registration forms on any site:
- Searches the DOM for Login/Register buttons
- Clicks the button to open the modal
- Inlines all CSS
- Downloads images inside the modal
- Saves as a standalone HTML page
"""

import asyncio
import re
from pathlib import Path
from urllib.parse import urljoin, urlparse

from PyQt6.QtCore import QObject, pyqtSignal
from core.asset_manager import AssetManager

# Login button search selectors
LOGIN_SELECTORS = [
    # Text-based
    "a:has-text('Giriş')",
    "button:has-text('Giriş')",
    "a:has-text('Giriş Yap')",
    "button:has-text('Giriş Yap')",
    "a:has-text('Login')",
    "button:has-text('Login')",
    "a:has-text('Sign In')",
    "button:has-text('Sign In')",
    "a:has-text('Log In')",
    "button:has-text('Log In')",
    # Class-based
    "[class*='login']",
    "[class*='Login']",
    "[class*='signin']",
    "[class*='SignIn']",
    "[class*='giris']",
    "[class*='header-buttons-login']",
    # Data-based
    "[data-testid*='login']",
    "[data-action*='login']",
]

# Register button search selectors
REGISTER_SELECTORS = [
    "a:has-text('Üye Ol')",
    "button:has-text('Üye Ol')",
    "a:has-text('Kayıt Ol')",
    "button:has-text('Kayıt Ol')",
    "a:has-text('Kayıt')",
    "button:has-text('Kayıt')",
    "a:has-text('Register')",
    "button:has-text('Register')",
    "a:has-text('Sign Up')",
    "button:has-text('Sign Up')",
    "[class*='register']",
    "[class*='Register']",
    "[class*='signup']",
    "[class*='SignUp']",
    "[class*='kayit']",
    "[class*='header-buttons-register']",
    "[data-testid*='register']",
    "[data-action*='register']",
]


class ModalCapturer(QObject):
    """Login/Register modal capture engine."""

    log_message = pyqtSignal(str)

    # CSS inline JS
    INLINE_CSS_JS = """() => {
        const rules = [];
        for (const sheet of document.styleSheets) {
            try {
                for (const rule of sheet.cssRules) {
                    rules.push(rule.cssText);
                }
            } catch(e) {}
        }
        if (rules.length > 0) {
            const style = document.createElement('style');
            style.setAttribute('data-inlined', 'modal-capture');
            style.textContent = rules.join('\\n');
            document.head.appendChild(style);
        }
        return rules.length;
    }"""

    # Modal DOM capture JS — find modal/dialog/overlay and extract with full parent chain
    CAPTURE_MODAL_JS = """() => {
        // Detect modal/dialog/overlay
        const selectors = [
            '.p-dialog', '.modal', '.dialog', '[role="dialog"]',
            '.overlay-content', '.popup', '.p-dialog-mask',
            '[class*="modal"]', '[class*="dialog"]', '[class*="popup"]',
            '[class*="overlay"]',
        ];

        let modal = null;
        for (const sel of selectors) {
            const el = document.querySelector(sel);
            if (el && el.offsetWidth > 200 && el.offsetHeight > 200) {
                modal = el;
                break;
            }
        }

        if (!modal) {
            // Fallback: find the last opened large overlay
            const allElements = document.querySelectorAll('*');
            for (let i = allElements.length - 1; i >= 0; i--) {
                const el = allElements[i];
                const style = window.getComputedStyle(el);
                if (style.position === 'fixed' && el.offsetWidth > 300 && el.offsetHeight > 300) {
                    modal = el;
                    break;
                }
            }
        }

        if (!modal) return null;

        // Find the topmost overlay parent of the modal (including backdrop)
        let topModal = modal;
        let parent = modal.parentElement;
        while (parent && parent !== document.body) {
            const style = window.getComputedStyle(parent);
            if (style.position === 'fixed' || style.position === 'absolute') {
                if (parent.offsetWidth > window.innerWidth * 0.5) {
                    topModal = parent;
                }
            }
            parent = parent.parentElement;
        }

        // --- Inline Styles (Make independent from the site's main CSS) ---
        // Copy only visible or important element styles inline to avoid being too heavy
        function inlineComputedStyles(root) {
            const elements = root.querySelectorAll('*');
            for(let i=0; i<elements.length; i++) {
                const el = elements[i];
                if(el.tagName === 'SCRIPT' || el.tagName === 'STYLE') continue;
                const cs = window.getComputedStyle(el);
                let cssText = '';
                const props = [
                    'color', 'background-color', 'background-image', 'background-position', 'background-size',
                    'font-family', 'font-size', 'font-weight', 'text-align', 'border-radius', 'border', 'box-shadow',
                    'padding', 'margin', 'display', 'flex-direction', 'justify-content', 'align-items',
                    'width', 'height', 'max-width', 'max-height', 'min-width', 'min-height',
                    'position', 'top', 'left', 'right', 'bottom', 'z-index',
                    'opacity', 'transform', 'transform-origin', 'transition', 'animation', 'filter', 'backdrop-filter'
                ];
                for(let p of props) {
                   if(cs[p] && cs[p] !== 'none' && cs[p] !== 'auto' && cs[p] !== '0px') {
                       cssText += `${p}:${cs[p]} !important;`;
                   }
                }
                if(cssText) {
                    // Be careful not to overwrite existing style (can be appended)
                    el.setAttribute('style', (el.getAttribute('style') || '') + ';' + cssText);
                }
            }
        }

        // Work on a clone to avoid corrupting the actual body
        const clone = topModal.cloneNode(true);
        // Inlining directly on the DOM is more reliable (since getComputedStyle doesn't work on clones)
        // But if we corrupt the actual page it will crash? That's fine, the page will close after scraping
        inlineComputedStyles(topModal);

        return topModal.outerHTML;
    }"""

    # Capture page background styles and fonts
    CAPTURE_PAGE_STYLES_JS = """() => {
        const bodyStyle = window.getComputedStyle(document.body);
        return {
            bgColor: bodyStyle.backgroundColor,
            fontFamily: bodyStyle.fontFamily,
            color: bodyStyle.color,
        };
    }"""

    # Find all images
    FIND_MODAL_IMAGES_JS = """() => {
        const images = [];
        const modal = document.querySelector(
            '.p-dialog, .modal, .dialog, [role="dialog"], [class*="modal"], [class*="dialog"]'
        );
        if (!modal) return images;

        // img tags
        modal.querySelectorAll('img').forEach(img => {
            if (img.src) images.push(img.src);
            if (img.dataset.src) images.push(img.dataset.src);
        });

        // Background images
        modal.querySelectorAll('*').forEach(el => {
            const bg = window.getComputedStyle(el).backgroundImage;
            if (bg && bg !== 'none') {
                const urls = bg.match(/url\\(["']?([^"')]+)["']?\\)/g);
                if (urls) {
                    urls.forEach(u => {
                        const clean = u.replace(/url\\(["']?/, '').replace(/["']?\\)$/, '');
                        if (clean.startsWith('http')) images.push(clean);
                    });
                }
            }
        });

        return [...new Set(images)];
    }"""

    def __init__(self, parent=None):
        super().__init__(parent)

    async def capture_modals(
        self,
        page,
        base_url: str,
        output_dir: Path,
        shared_asset_mgr: AssetManager,
    ) -> dict:
        """
        Capture login and register modals on the page.
        Page must already be open (shared browser context).

        Returns:
            {
              'files': ['login.html', 'register.html'],
              'fragments': {
                  'login': {'html': '...', 'is_popup': True, 'button_texts': [...]},
                  'register': {'html': '...', 'is_popup': True, 'button_texts': [...]},
              }
            }
        """
        output_dir = Path(output_dir)
        result = {'files': [], 'fragments': {}}

        parsed = urlparse(base_url)
        base_domain = parsed.netloc

        # 1. LOGIN MODAL
        login_data = await self._capture_single_modal(
            page=page,
            base_url=base_url,
            output_dir=output_dir,
            button_selectors=LOGIN_SELECTORS,
            modal_name="login",
            title="Login",
            shared_asset_mgr=shared_asset_mgr,
        )
        if login_data:
            result['files'].append(login_data['filename'])
            result['fragments']['login'] = login_data['fragment']

        # 2. REGISTER MODAL
        register_data = await self._capture_single_modal(
            page=page,
            base_url=base_url,
            output_dir=output_dir,
            button_selectors=REGISTER_SELECTORS,
            modal_name="register",
            title="Register",
            shared_asset_mgr=shared_asset_mgr,
        )
        if register_data:
            result['files'].append(register_data['filename'])
            result['fragments']['register'] = register_data['fragment']

        return result

    async def _detect_button_type(self, button) -> dict:
        """Detect whether the button opens a popup or navigates to a separate page."""
        try:
            tag_name = await button.evaluate("el => el.tagName.toLowerCase()")
            href = await button.evaluate("el => el.getAttribute('href') || ''")
            text = (await button.inner_text()).strip()

            # Separate page indicators: full URL or /login, /register path
            is_separate_page = False
            if tag_name == 'a' and href:
                href_lower = href.lower().strip()
                if href_lower.startswith('http') or (
                    href_lower.startswith('/') and
                    not href_lower.startswith('/#') and
                    href_lower not in ('/', '')
                ):
                    is_separate_page = True
                # # or javascript: → popup
                if href_lower in ('#', '', 'javascript:void(0)', 'javascript:;'):
                    is_separate_page = False

            return {
                'tag': tag_name,
                'href': href,
                'text': text,
                'is_popup': not is_separate_page,
            }
        except Exception:
            return {'tag': 'button', 'href': '', 'text': '', 'is_popup': True}

    async def _capture_single_modal(
        self,
        page,
        base_url: str,
        output_dir: Path,
        button_selectors: list[str],
        modal_name: str,
        title: str,
        shared_asset_mgr: AssetManager,
    ) -> dict | None:
        """Capture a single modal. Returns fragment + standalone file."""

        self.log_message.emit(f"🔍 Searching for {title} button...")

        # Find button
        button = None
        matched_selector = None
        for selector in button_selectors:
            try:
                # Get full list of locators
                elements = await page.locator(selector).all()
                for el in elements:
                    if await el.is_visible(timeout=500):
                        text = (await el.inner_text()).strip().lower()
                        # Strict mode: if modal is "login", text must contain giriş/login etc.
                        if modal_name == "login" and not any(kw in text for kw in ["giriş", "login", "sign in"]):
                            continue
                        if modal_name == "register" and not any(kw in text for kw in ["üye", "kayıt", "register", "sign up"]):
                            continue

                        button = el
                        matched_selector = selector
                        self.log_message.emit(f"   ✅ Button found: {selector} (Text: {text})")
                        break
                if button:
                    break
            except Exception:
                continue

        if not button:
            self.log_message.emit(f"   ⚠️  {title} button not found, skipping")
            return None

        # Detect button type (popup or separate page?)
        btn_info = await self._detect_button_type(button)
        self.log_message.emit(
            f"   📋 Type: {'Popup Modal' if btn_info['is_popup'] else 'Separate Page'}"
            f" | Button: <{btn_info['tag']}> '{btn_info['text']}'"
        )

        try:
            # Click the button
            self.log_message.emit(f"   🖱️  Clicking {title} button...")

            # Add wait to detect separate page navigation
            old_url = page.url
            await button.click()

            # Wait a short time (for Navigation or Modal)
            await asyncio.sleep(2)

            # 1. Did navigation occur? (Separate Page Case)
            if page.url != old_url and not page.url.endswith("#"):
                self.log_message.emit(f"   🌐 Navigated to new page: {page.url}")
                try:
                    await page.wait_for_load_state("domcontentloaded", timeout=10000)
                except Exception: pass

                # In this case, take the whole page as 'modal' (or the main 'form' section)
                modal_html = await page.evaluate("""() => {
                    const main = document.querySelector('main, form, .login-container, #login-form, .auth-box');
                    return main ? main.outerHTML : document.body.innerHTML;
                }""")
            else:
                # 2. Did a Modal/Popup open? (Check via JS)
                # Wait for form to render
                try:
                    # Wait until input or form is visible
                    await page.wait_for_selector('input, [role="dialog"], .modal', timeout=5000, state="visible")
                    await asyncio.sleep(1)
                except Exception:
                    self.log_message.emit("   ⚠️  <input> or overlay inside modal not found, form may be incomplete")

                modal_html = await page.evaluate(self.CAPTURE_MODAL_JS)

            if not modal_html:
                self.log_message.emit(f"   ⚠️  Modal did not open or could not be detected")
                # If navigation occurred, go back
                if page.url != old_url:
                    await page.goto(old_url, wait_until="domcontentloaded")
                else:
                    await page.keyboard.press("Escape")
                await asyncio.sleep(0.5)
                return None

            self.log_message.emit(f"   ✅ Modal captured ({len(modal_html):,} characters)")

            # Inline CSS
            css_count = await page.evaluate(self.INLINE_CSS_JS)
            self.log_message.emit(f"   🎨 {css_count} CSS rules inlined")

            # Find and download modal images
            image_urls = await page.evaluate(self.FIND_MODAL_IMAGES_JS)

            # Also check all img tags (after DOM render)
            all_img_srcs = await page.evaluate("""() => {
                const srcs = [];
                document.querySelectorAll('img').forEach(img => {
                    let src = img.getAttribute('src');
                    if (src) srcs.push(src);
                    let dsrc = img.getAttribute('data-src');
                    if (dsrc) srcs.push(dsrc);
                    if (img.srcset) {
                        img.srcset.split(',').forEach(s => {
                            const url = s.trim().split(' ')[0];
                            if (url) srcs.push(url);
                        });
                    }
                });
                // Also scan CSS background-image URLs (flags, icons, etc.)
                document.querySelectorAll('[style*="background"], [class*="flag"], [class*="country"], .p-dropdown-item, .p-dropdown-trigger').forEach(el => {
                    const cs = getComputedStyle(el);
                    const bg = cs.backgroundImage;
                    if (bg && bg !== 'none') {
                        const urls = bg.match(/url\\(["']?([^"')]+)["']?\\)/g);
                        if (urls) {
                            urls.forEach(u => {
                                const clean = u.replace(/url\\(["']?/, '').replace(/["']?\\)/, '');
                                if (clean && !clean.startsWith('data:')) srcs.push(clean);
                            });
                        }
                    }
                });
                return [...new Set(srcs)];
            }""")

            all_images = set()
            for raw_url in (image_urls + all_img_srcs):
                if raw_url and not raw_url.startswith('data:'):
                    full = urljoin(base_url, raw_url)
                    all_images.add(full)
            all_images = list(all_images)
            self.log_message.emit(f"   🖼️  {len(all_images)} image(s) found")

            for img_url in all_images:
                if shared_asset_mgr._find_local_path(img_url):
                    continue
                try:
                    response = await page.context.request.get(img_url, timeout=10000)
                    if response.ok:
                        body = await response.body()
                        ct = response.headers.get("content-type", "image/png")
                        await shared_asset_mgr.save_resources({img_url: body}, {img_url: ct})
                        self.log_message.emit(f"   💾 Downloaded: {img_url.split('/')[-1][:50]}")
                except Exception as e:
                    self.log_message.emit(f"   ⚠️  Could not download: {str(e)[:60]}")

            # data-src → src conversion
            await page.evaluate("""() => {
                document.querySelectorAll('[data-src]').forEach(el => {
                    if (el.dataset.src) {
                        el.src = el.dataset.src;
                        el.removeAttribute('data-src');
                    }
                });
            }""")

            # Get FULL PAGE HTML (modal open + CSS inlined)
            full_html = await page.content()
            self.log_message.emit(f"   📝 Full page HTML: {len(full_html):,} characters")

            # Re-capture modal fragment (updated after CSS inline)
            updated_fragment = await page.evaluate(self.CAPTURE_MODAL_JS)
            if not updated_fragment:
                updated_fragment = modal_html

            # Close modal
            await page.keyboard.press("Escape")
            await asyncio.sleep(0.5)
            try:
                close_btn = page.locator(
                    "button.p-dialog-header-close, .modal-close, [class*='close'], button:has-text('✕'), button:has-text('×')"
                ).first
                if await close_btn.is_visible(timeout=500):
                    await close_btn.click()
                    await asyncio.sleep(0.5)
            except Exception:
                pass

            # Save resources and rewrite URLs
            rewritten_html = await asyncio.to_thread(shared_asset_mgr.rewrite_html, full_html, base_url)

            # Save standalone file
            output_file = output_dir / f"{modal_name}.html"
            output_file.write_text(rewritten_html, encoding="utf-8")
            self.log_message.emit(f"   📄 Saved: {output_file.name}")

            # Rewrite URLs in fragment HTML using regex — no BeautifulSoup to avoid corruption
            import re as _re

            def _rewrite_frag_attr(m: re.Match) -> str:
                attr_eq_q = m.group(1)   # e.g. src="
                url       = m.group(2)
                close_q   = m.group(3)
                if not url or url.startswith(('data:', '#', 'javascript:', 'blob:')):
                    return m.group(0)
                abs_url = urljoin(base_url, url)
                local = shared_asset_mgr._find_local_path(abs_url)
                if local:
                    return attr_eq_q + local + close_q
                return m.group(0)

            # Double-quoted attributes
            rewritten_fragment = _re.sub(
                r'(\b(?:src|href|data-src|poster)=")([^"]+?)(")',
                _rewrite_frag_attr, updated_fragment)
            # Single-quoted attributes
            rewritten_fragment = _re.sub(
                r"(\b(?:src|href|data-src|poster)=')([^']+?)(')",
                _rewrite_frag_attr, rewritten_fragment)

            def _rewrite_frag_css_url(m: re.Match) -> str:
                raw = m.group(1).strip().strip('\'"')
                if not raw or raw.startswith(('data:', 'blob:')):
                    return m.group(0)
                abs_u = urljoin(base_url, raw)
                loc = shared_asset_mgr._find_local_path(abs_u)
                return f"url('{loc}')" if loc else m.group(0)

            rewritten_fragment = _re.sub(r'url\(([^)]+)\)', _rewrite_frag_css_url, rewritten_fragment)

            return {
                'filename': output_file.name,
                'fragment': {
                    'html': rewritten_fragment,
                    'is_popup': btn_info['is_popup'],
                    'button_text': btn_info['text'],
                    'button_tag': btn_info['tag'],
                    'matched_selector': matched_selector,
                },
            }

        except Exception as e:
            self.log_message.emit(f"   ❌ Modal capture error: {e}")
            try:
                await page.keyboard.press("Escape")
                await asyncio.sleep(0.5)
            except Exception:
                pass
            return None

    async def _create_standalone_page(
        self,
        modal_html: str,
        full_html: str,
        page_styles: dict,
        image_urls: list[str],
        shared_resources: dict[str, bytes],
        shared_content_types: dict[str, str],
        base_url: str,
        output_dir: Path,
        output_file: Path,
        title: str,
    ) -> None:
        """Save the modal as a standalone HTML page."""

        # Save resources — let asset_mgr build the _url_to_local mapping
        asset_mgr = AssetManager(output_dir)
        await asset_mgr.save_resources(shared_resources, shared_content_types)

        # Extract <style> and <link> tags from <head> using regex — no BeautifulSoup
        head_content = ""
        head_match = re.search(r'<head\b[^>]*>(.*?)</head>', full_html, re.IGNORECASE | re.DOTALL)
        if head_match:
            head_html = head_match.group(1)
            for style_m in re.finditer(r'<style\b[^>]*>.*?</style>', head_html, re.IGNORECASE | re.DOTALL):
                head_content += style_m.group(0) + "\n"
            for link_m in re.finditer(r'<link\b[^>]*/?\s*>', head_html, re.IGNORECASE):
                tag_str = link_m.group(0)
                if 'stylesheet' in tag_str or ('font' in tag_str and 'href' in tag_str):
                    head_content += tag_str + "\n"

        # Convert URLs in modal HTML to local paths
        processed_modal = modal_html

        # Convert all URLs using _find_local_path
        for img_url in image_urls:
            local_path = asset_mgr._find_local_path(img_url)
            if local_path:
                processed_modal = processed_modal.replace(img_url, local_path)

        # src/href URLs
        url_pattern = re.compile(r'(src|href|data-src)=["\']?(https?://[^"\'>\s]+)["\']?')
        for match in url_pattern.finditer(processed_modal):
            url = match.group(2)
            local_path = asset_mgr._find_local_path(url)
            if local_path:
                processed_modal = processed_modal.replace(url, local_path)

        # CSS url() references
        css_url_pattern = re.compile(r'url\(["\']?(https?://[^"\')\s]+)["\']?\)')
        for match in css_url_pattern.finditer(processed_modal):
            url = match.group(1)
            local_path = asset_mgr._find_local_path(url)
            if local_path:
                processed_modal = processed_modal.replace(url, local_path)

        # Convert URLs in head_content too
        for match in css_url_pattern.finditer(head_content):
            url = match.group(1)
            local_path = asset_mgr._find_local_path(url)
            if local_path:
                head_content = head_content.replace(url, local_path)

        # Background color
        bg_color = page_styles.get("bgColor", "#0a1128")
        font_family = page_styles.get("fontFamily", "'Urbanist', sans-serif")

        standalone_html = f"""<!DOCTYPE html>
<html lang="tr">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>{title}</title>
    {head_content}
    <style>
        body {{
            margin: 0;
            padding: 0;
            background: {bg_color};
            font-family: {font_family};
            min-height: 100vh;
            display: flex;
            align-items: center;
            justify-content: center;
            overflow: hidden;
        }}
        /* Modal overlay */
        .modal-backdrop {{
            position: fixed;
            inset: 0;
            background: rgba(0, 0, 0, 0.6);
            backdrop-filter: blur(8px);
            display: flex;
            align-items: center;
            justify-content: center;
            z-index: 9999;
        }}
    </style>
</head>
<body>
    <div class="modal-backdrop">
        {processed_modal}
    </div>
</body>
</html>"""

        output_file.write_text(standalone_html, encoding="utf-8")
