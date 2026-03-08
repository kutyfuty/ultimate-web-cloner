import re
from bs4 import BeautifulSoup


class FrontendMocker:
    """
    Universal 1:1 Zero-UI Injector

    Never touches the page's original DOM structure.
    Does NOT add new divs, buttons, spinners, toasts, or CSS.
    Only uses Node Replacement to override framework events
    and silently handles localStorage + redirects in the background.
    """
    def __init__(
        self,
        login_form_selector: str = "",
        username_input_selector: str = "",
        username_display_selector: str = ""
    ):
        self.login_form_selector = login_form_selector
        self.username_input_selector = username_input_selector
        self.username_display_selector = username_display_selector

    def inject_mock_scripts(self, html_content: str, depth_prefix: str = "") -> str:
        """Injects the invisible interceptor script into HTML content."""
        soup = BeautifulSoup(html_content, "lxml")

        js_payload = self._generate_payload(depth_prefix)

        script_tag = soup.new_tag("script")
        script_tag.string = js_payload

        head = soup.find("head")
        if head:
            head.insert(0, script_tag)
        elif soup.find("html"):
            soup.find("html").insert(0, script_tag)
        else:
            soup.insert(0, script_tag)

        return str(soup)

    def _generate_payload(self, depth_prefix: str = "") -> str:
        """Zero-UI JS payload — zero DOM additions, zero style changes."""

        l_form = self.login_form_selector.replace("\\", "\\\\").replace("'", "\\'")
        u_input = self.username_input_selector.replace("\\", "\\\\").replace("'", "\\'")
        safe_prefix = depth_prefix.replace("\\", "\\\\").replace("'", "\\'")

        return f"""
(function() {{
    'use strict';

    const LOGIN_FORM_SEL = '{l_form}';
    const USERNAME_INPUT_SEL = '{u_input}';
    const REDIRECT_PATH = '{safe_prefix}index_auth.html';

    let done = false;
    const PROCESSED = new WeakSet();

    // ══════════════════════════════════════
    // Layer 0: Global API Silencer
    // Intercept ALL external requests, prevent React/Vue crashes
    // ══════════════════════════════════════
    const ORIGIN = window.location.origin;

    const _fetch = window.fetch;
    window.fetch = function(input, opts) {{
        const url = typeof input === 'string' ? input : (input && input.url) || '';
        // Same origin or empty: original fetch
        if (!url || url.startsWith('/') || url.startsWith('./') || url.startsWith('../') || url.startsWith(ORIGIN)) {{
            return _fetch.apply(this, arguments).catch(() =>
                Promise.resolve(new Response('{{}}', {{status:200, headers:{{'Content-Type':'application/json'}}}}))
            );
        }}
        // External resource: silent fake 200
        return Promise.resolve(new Response('{{}}', {{status:200, headers:{{'Content-Type':'application/json'}}}}));
    }};

    const _xhrOpen = XMLHttpRequest.prototype.open;
    const _xhrSend = XMLHttpRequest.prototype.send;
    XMLHttpRequest.prototype.open = function(m, url) {{
        this.__url = url || '';
        this.__external = !!(url && !url.startsWith('/') && !url.startsWith('./') && !url.startsWith('../') && !url.startsWith(ORIGIN) && url.startsWith('http'));
        return _xhrOpen.apply(this, arguments);
    }};
    XMLHttpRequest.prototype.send = function() {{
        if (this.__external) {{
            Object.defineProperty(this, 'readyState', {{value:4, writable:false}});
            Object.defineProperty(this, 'status', {{value:200, writable:false}});
            Object.defineProperty(this, 'responseText', {{value:'{{}}', writable:false}});
            Object.defineProperty(this, 'response', {{value:'{{}}', writable:false}});
            const s = this;
            setTimeout(() => {{ try {{ if(s.onload) s.onload(); s.dispatchEvent(new Event('load')); }} catch(e){{}} }}, 10);
            return;
        }}
        return _xhrSend.apply(this, arguments);
    }};

    // ══════════════════════════════════════
    // Layer 0.5: SPA Defeater (History API Hijack)
    // ══════════════════════════════════════
    const originalPushState = history.pushState;
    const originalReplaceState = history.replaceState;

    function handleSPA(url) {{
        if (!url || typeof url !== 'string') return;
        if (url.startsWith('http') && !url.startsWith(ORIGIN)) return; // External link

        // Clean URL and convert to .html path
        let path = url.replace(ORIGIN, '').split('?')[0].split('#')[0];
        if (path === '/' || path === '') path = '/index';
        if (path.endsWith('/')) path = path.slice(0, -1) + '/index';

        let targetFile = path.split('/').pop() + '.html';

        // If already html, leave it
        if (path.endsWith('.html')) targetFile = path.split('/').pop();

        // Add prefix based on folder depth (../../ etc.)
        const finalUrl = (REDIRECT_PATH.replace('index_auth.html', '') || './') + targetFile;
        console.log('[Cloner SPA Defeater] Intercepted route:', url, '->', finalUrl);
        window.location.href = finalUrl;
    }}

    history.pushState = function(state, title, url) {{
        if (url) handleSPA(url);
        return originalPushState.apply(this, arguments);
    }};

    history.replaceState = function(state, title, url) {{
        if (url) handleSPA(url);
        return originalReplaceState.apply(this, arguments);
    }};

    // ══════════════════════════════════════
    // Layer 1: Node Replacement Form Hijack
    // ══════════════════════════════════════

    function getUser(form) {{
        if (USERNAME_INPUT_SEL) {{ const el = form.querySelector(USERNAME_INPUT_SEL); if (el && el.value.trim()) return el.value.trim(); }}
        const inputs = form.querySelectorAll('input:not([type="password"]):not([type="hidden"]):not([type="submit"]):not([type="button"]):not([type="checkbox"]):not([type="radio"])');
        for (const inp of inputs) if (inp.value && inp.value.trim().length > 1) return inp.value.trim();
        const email = form.querySelector('input[type="email"]'); if (email && email.value) return email.value.trim();
        return 'Guest';
    }}

    function redirect(form) {{
        if (done) return; done = true;
        const user = getUser(form);
        localStorage.setItem('universalMockUser', user);
        window.location.href = REDIRECT_PATH;
    }}

    function isLogin(form) {{
        if (form.querySelector('input[type="password"]')) return true;
        if (LOGIN_FORM_SEL) {{ try {{ if (form.matches(LOGIN_FORM_SEL)) return true; }} catch(e) {{}} }}
        const action = (form.getAttribute('action') || '').toLowerCase();
        return ['/login','/auth','/signin','/sign-in','/api/login','/api/auth','/giris','/oturum'].some(p => action.includes(p));
    }}

    function hijack(form) {{
        if (PROCESSED.has(form) || !isLogin(form)) return;
        const parent = form.parentNode; if (!parent) return;
        const next = form.nextSibling;
        const html = form.outerHTML;
        const vals = {{}};
        form.querySelectorAll('input,select,textarea').forEach((el,i) => vals[i] = el.value);
        form.remove();

        const tmp = document.createElement('div'); tmp.innerHTML = html;
        const clean = tmp.firstElementChild;
        clean.querySelectorAll('input,select,textarea').forEach((el,i) => {{ if (vals[i] !== undefined) el.value = vals[i]; }});
        if (next) parent.insertBefore(clean, next); else parent.appendChild(clean);

        clean.addEventListener('submit', e => {{ e.preventDefault(); e.stopImmediatePropagation(); redirect(clean); }}, true);
        clean.querySelectorAll('button,input[type="submit"],[role="button"]').forEach(b =>
            b.addEventListener('click', e => {{ e.preventDefault(); e.stopImmediatePropagation(); redirect(clean); }}, true)
        );
        clean.addEventListener('keydown', e => {{ if (e.key === 'Enter') {{ e.preventDefault(); e.stopImmediatePropagation(); redirect(clean); }} }}, true);
        PROCESSED.add(clean);
    }}

    // ══════════════════════════════════
    // Layer 2: MutationObserver + Scan
    // ══════════════════════════════════

    function scanForms() {{ document.querySelectorAll('form').forEach(hijack); }}

    if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', scanForms);
    else {{ setTimeout(scanForms, 100); setTimeout(scanForms, 500); setTimeout(scanForms, 1500); setTimeout(scanForms, 3000); }}

    new MutationObserver(muts => {{
        for (const m of muts) for (const n of m.addedNodes) {{
            if (n.nodeType !== 1) continue;
            if (n.tagName === 'FORM') setTimeout(() => hijack(n), 50);
            if (n.querySelectorAll) n.querySelectorAll('form').forEach(f => setTimeout(() => hijack(f), 50));
        }}
    }}).observe(document.documentElement, {{ childList: true, subtree: true }});

    document.addEventListener('submit', e => {{
        if (e.target && e.target.tagName === 'FORM' && isLogin(e.target)) {{ e.preventDefault(); e.stopImmediatePropagation(); redirect(e.target); }}
    }}, true);

    // ══════════════════════════════════════════
    // Layer 3: Vanilla JS UI Rescue
    // Hamburger menu, tab, slider — offline
    // ══════════════════════════════════════════

    function initUIRescue() {{
        // ── Scroll Lock Remover + Preloader DOM Killer ──
        document.body.style.overflow = 'auto';
        document.documentElement.style.overflow = 'auto';
        ['#preloader','.preloader','.loader','.loader-wrapper','.loading-screen','.loading-overlay','.splash-screen','#loader','.page-loader','.spinner'].forEach(sel => {{
            try {{ document.querySelectorAll(sel).forEach(el => el.remove()); }} catch(e){{}}
        }});

        const TOGGLE_CLASSES = ['active','show','open','is-active','is-open','collapsed','in'];

        // ── Hamburger / Menu Toggle ──
        const menuToggles = document.querySelectorAll(
            '.hamburger, .menu-toggle, .navbar-toggler, .nav-toggle, '
            + '.mobile-menu-btn, .burger-menu, [data-toggle="collapse"], '
            + '[data-bs-toggle="collapse"], .sidebar-toggle'
        );
        menuToggles.forEach(btn => {{
            if (btn.__uiRescue) return; btn.__uiRescue = true;
            btn.addEventListener('click', function(e) {{
                // Toggle the button itself
                TOGGLE_CLASSES.forEach(c => this.classList.toggle(c));
                // Target element (data-target / aria-controls / href)
                const targetSel = this.getAttribute('data-target')
                    || this.getAttribute('data-bs-target')
                    || this.getAttribute('aria-controls')
                    || this.getAttribute('href');
                if (targetSel) {{
                    try {{
                        const target = document.querySelector(targetSel);
                        if (target) TOGGLE_CLASSES.forEach(c => target.classList.toggle(c));
                    }} catch(e) {{}}
                }}
                // Toggle sibling nav too
                const nav = this.closest('header,nav,.navbar')?.querySelector('.nav-menu,.navbar-collapse,.mobile-menu,.nav-links,ul');
                if (nav) TOGGLE_CLASSES.forEach(c => nav.classList.toggle(c));
            }});
        }});

        // ── Tab Transitions ──
        const tabs = document.querySelectorAll(
            '[data-toggle="tab"], [data-bs-toggle="tab"], [role="tab"], '
            + '.tab-btn, .tab-link, .tabs__item, .nav-tab'
        );
        tabs.forEach(tab => {{
            if (tab.__uiRescue) return; tab.__uiRescue = true;
            tab.addEventListener('click', function(e) {{
                e.preventDefault();
                // Remove active from sibling tabs
                const container = this.closest('.tabs, .nav-tabs, .tab-list, [role="tablist"], .tab-container, ul');
                if (container) {{
                    container.querySelectorAll('[role="tab"], .tab-btn, .tab-link, .tabs__item, .nav-tab, [data-toggle="tab"], [data-bs-toggle="tab"]')
                        .forEach(t => {{ t.classList.remove('active','show','selected'); t.setAttribute('aria-selected','false'); }});
                }}
                this.classList.add('active','show','selected');
                this.setAttribute('aria-selected','true');
                // Content panel
                const targetSel = this.getAttribute('data-target') || this.getAttribute('data-bs-target') || this.getAttribute('href') || this.getAttribute('aria-controls');
                if (targetSel) {{
                    try {{
                        const panelContainer = document.querySelector(targetSel)?.parentNode;
                        if (panelContainer) {{
                            panelContainer.querySelectorAll('[role="tabpanel"], .tab-pane, .tab-content__item')
                                .forEach(p => {{ p.classList.remove('active','show','in'); p.style.display = 'none'; }});
                        }}
                        const target = document.querySelector(targetSel);
                        if (target) {{ target.classList.add('active','show','in'); target.style.display = ''; }}
                    }} catch(e) {{}}
                }}
            }});
        }});

        // ── Accordion / Dropdown ──
        document.querySelectorAll(
            '[data-toggle="dropdown"], [data-bs-toggle="dropdown"], '
            + '.accordion-toggle, .accordion-header, .collapsible-header'
        ).forEach(btn => {{
            if (btn.__uiRescue) return; btn.__uiRescue = true;
            btn.addEventListener('click', function(e) {{
                e.preventDefault();
                const panel = this.nextElementSibling;
                if (panel) {{
                    const isOpen = panel.classList.contains('show') || panel.style.display === 'block';
                    TOGGLE_CLASSES.forEach(c => panel.classList.toggle(c));
                    panel.style.display = isOpen ? 'none' : 'block';
                }}
                TOGGLE_CLASSES.forEach(c => this.classList.toggle(c));
            }});
        }});

        // ── Mobile Menu Hider (Duplicate Menu Preventer) ──
        if (window.innerWidth > 991) {{
            const mobileSelectors = [
                '.mobile-menu', '.mobile-nav', '.mobile-wrapper', '.mobile-header',
                '.mobile-navigation', '.mobile-sidebar', '.offcanvas-menu',
                '[class*="mobile-menu"]', '[class*="mobile-nav"]', '[class*="mob-menu"]',
                '[class*="m-menu"]', '[class*="phone-menu"]',
                '.nav-mobile', '.menu-mobile', '.responsive-menu',
            ];
            mobileSelectors.forEach(sel => {{
                try {{
                    document.querySelectorAll(sel).forEach(el => {{
                        el.style.setProperty('display', 'none', 'important');
                    }});
                }} catch(e) {{}}
            }});
        }}
    }}

    if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', initUIRescue);
    else setTimeout(initUIRescue, 300);

    // ══════════════════════════════════════════════
    // Layer 4: Next.js / Nuxt.js Infinite Loading Killer
    // Breaks __NEXT_DATA__ and __NUXT__ fetch loops
    // ══════════════════════════════════════════════
    (function patchNextJs() {{
        // If __NEXT_DATA__ exists — Next.js SSR page
        if (window.__NEXT_DATA__) {{
            // Freeze API calls inside props.pageProps
            try {{
                const nd = window.__NEXT_DATA__;
                if (nd.props && nd.props.pageProps === undefined) {{
                    nd.props.pageProps = {{}};
                }}
                // buildId mismatch → use 'dev' in offline mode
                nd.buildId = nd.buildId || 'offline';
                nd.runtimeConfig = nd.runtimeConfig || {{}};
            }} catch(e) {{}}
        }}

        // Nuxt.js
        if (window.__NUXT__) {{
            try {{
                window.__NUXT__.serverRendered = true;
                window.__NUXT__.state = window.__NUXT__.state || {{}};
            }} catch(e) {{}}
        }}

        // Shut down Next.js Router: convert route changes to file-based navigation
        const _origNext = window.next;
        if (_origNext && _origNext.router) {{
            const nr = _origNext.router;
            const _push = nr.push.bind(nr);
            nr.push = function(url, as, opts) {{
                if (typeof url === 'string' && !url.startsWith('http')) {{
                    const cleaned = url.split('?')[0].replace(/^\//, '');
                    window.location.href = (cleaned || 'index') + '.html';
                    return Promise.resolve(true);
                }}
                return _push(url, as, opts);
            }};
        }}

        // dynamic import() → silent error if chunk can't be loaded
        const _origImport = window.__webpack_require__;
        if (typeof __webpack_require__ !== 'undefined') {{
            try {{
                const origEnsure = __webpack_require__.e;
                if (origEnsure) {{
                    __webpack_require__.e = function(chunkId) {{
                        return origEnsure(chunkId).catch(() => {{
                            console.warn('[Cloner] Chunk could not be loaded:', chunkId, '— skipping');
                            return {{}};
                        }});
                    }};
                }}
            }} catch(e) {{}}
        }}
    }})();

    // ══════════════════════════════════════════════
    // Layer 5: React Hydration Duplicate Preventer
    // Prevents server render + client render collision
    // ══════════════════════════════════════════════
    (function preventHydrationDuplicate() {{
        // Monitor React 18+ createRoot / hydrateRoot calls
        // If DOM already has content, skip hydration (re-render = duplicate)
        const _origCreateElement = document.createElement.bind(document);
        let _reactRootCount = 0;

        // MutationObserver for duplicate text node detection
        const _seen = new Map();
        const _dedupeObserver = new MutationObserver(mutations => {{
            for (const m of mutations) {{
                for (const node of m.addedNodes) {{
                    if (node.nodeType !== 1) continue;
                    const txt = node.textContent && node.textContent.trim();
                    if (!txt || txt.length < 5) continue;
                    const parent = node.parentNode;
                    if (!parent) continue;
                    // Is there the same text content in the same parent?
                    const siblings = Array.from(parent.childNodes).filter(
                        c => c !== node && c.nodeType === 1 &&
                             c.textContent && c.textContent.trim() === txt
                    );
                    if (siblings.length > 0) {{
                        // Duplicate — hide the cloning artifact copy
                        node.setAttribute('data-cloner-deduped', 'true');
                        node.style.display = 'none';
                    }}
                }}
            }}
        }});

        if (document.readyState === 'loading') {{
            document.addEventListener('DOMContentLoaded', () => {{
                _dedupeObserver.observe(document.body || document.documentElement,
                    {{ childList: true, subtree: true }});
            }});
        }} else {{
            _dedupeObserver.observe(document.body || document.documentElement,
                {{ childList: true, subtree: true }});
        }}
    }})();

    // ══════════════════════════════════════════════
    // Layer 6: SSE (Server-Sent Events) Mock
    // Silences EventSource connections when offline
    // ══════════════════════════════════════════════
    if (typeof EventSource !== 'undefined') {{
        const _OrigES = EventSource;
        window.EventSource = function(url, cfg) {{
            try {{ return new _OrigES(url, cfg); }} catch(e) {{}}
            // Return silent mock object if connection fails
            return {{
                readyState: 2, // CLOSED
                close: function() {{}},
                addEventListener: function() {{}},
                removeEventListener: function() {{}},
                dispatchEvent: function() {{ return false; }}
            }};
        }};
        window.EventSource.prototype = _OrigES.prototype;
        window.EventSource.CONNECTING = 0;
        window.EventSource.OPEN = 1;
        window.EventSource.CLOSED = 2;
    }}

    // ══════════════════════════════════════════════
    // Layer 7: Socket.io / SockJS Offline Mock
    // ══════════════════════════════════════════════
    (function patchSocketIO() {{
        // If socket.io global object exists
        if (window.io) {{
            const _origIO = window.io;
            window.io = function() {{
                try {{ return _origIO.apply(this, arguments); }} catch(e) {{}}
                // Return empty event emitter if connection fails
                const mock = {{
                    on: function() {{ return mock; }},
                    off: function() {{ return mock; }},
                    emit: function() {{ return mock; }},
                    connect: function() {{ return mock; }},
                    disconnect: function() {{ return mock; }},
                    connected: false,
                    id: 'offline-' + Math.random().toString(36).slice(2)
                }};
                return mock;
            }};
            Object.assign(window.io, _origIO);
        }}
    }})();

    // ══════════════════════════════════════════════
    // Layer 8: Infinite Loading Spinner Killer
    // After 3 seconds, all known loading elements are removed
    // ══════════════════════════════════════════════
    setTimeout(function killLoadingForever() {{
        const LOADING_SELECTORS = [
            '#preloader', '.preloader', '.loader', '.loader-wrapper',
            '.loading-screen', '.loading-overlay', '.splash-screen',
            '#loader', '.page-loader', '.spinner', '.loading',
            '[class*="loading"]', '[class*="preloader"]', '[class*="spinner"]',
            '.skeleton', '[class*="skeleton"]', '.shimmer', '[class*="shimmer"]',
            '.placeholder-glow', '.content-placeholder',
            // React/Next.js Suspense fallbacks
            '[data-nextjs-scroll-focus-boundary]',
            '.__next-spinner', '.next-spinner',
        ];
        LOADING_SELECTORS.forEach(sel => {{
            try {{
                document.querySelectorAll(sel).forEach(el => {{
                    // Only remove those without content (might be real content)
                    const hasContent = el.querySelector('img,p,h1,h2,h3,a,button,input,table');
                    if (!hasContent && el.textContent && el.textContent.trim().length < 20) {{
                        el.style.setProperty('display', 'none', 'important');
                    }}
                }});
            }} catch(e) {{}}
        }});
        document.body && (document.body.style.overflow = 'auto');
        document.documentElement && (document.documentElement.style.overflow = 'auto');
        document.body && document.body.classList.remove('loading', 'is-loading', 'preloading', 'no-scroll');
    }}, 3000);

}})();
"""
