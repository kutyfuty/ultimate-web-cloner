import asyncio
import random
from pathlib import Path

class InteractionEngine:
    """
    Triggers sliders, tabs and interactive elements on the page to capture
    hidden content (lazy-load) and dynamic network requests.
    """
    def __init__(self, page, log_callback=None):
        self.page = page
        self.log_callback = log_callback
        self._is_running = True

    def log(self, msg):
        if self.log_callback:
            self.log_callback(f"   🖱️  {msg}")

    async def run_all(self):
        """Runs all interaction scenarios in sequence."""
        self.log("Starting interactive asset discovery...")

        # 1. Slider and Carousel triggering
        await self.trigger_sliders()

        # 2. Tab transitions
        await self.trigger_tabs()

        # 3. Hover effects (for menus, etc.)
        await self.trigger_hovers()

        self.log("Interactive discovery complete.")

    async def trigger_sliders(self):
        """Clicks slider arrows and dots."""
        self.log("Triggering sliders...")
        selectors = [
            ".swiper-button-next", ".swiper-button-prev",
            ".slick-next", ".slick-prev",
            ".owl-next", ".owl-prev",
            ".carousel-control-next", ".carousel-control-prev",
            "button[aria-label*='next']", "button[aria-label*='Next']",
            ".next-button", ".prev-button"
        ]

        for selector in selectors:
            try:
                elements = await self.page.query_selector_all(selector)
                if elements:
                    self.log(f"Slider button found: {selector} ({len(elements)} items)")
                    # Click each button type at most 3 times (to prevent infinite loops)
                    for i, el in enumerate(elements[:3]):
                        if await el.is_visible():
                            await el.click()
                            await asyncio.sleep(0.5)
            except Exception:
                continue

        # Swiper pagination dots
        try:
            dots = await self.page.query_selector_all(".swiper-pagination-bullet, .slick-dots li")
            if dots:
                for dot in dots[:5]: # Click only the first 5 dots
                    await dot.click()
                    await asyncio.sleep(0.3)
        except Exception:
            pass

    async def trigger_tabs(self):
        """Navigates tab structures."""
        self.log("Scanning tabs...")
        tab_selectors = [
            "[role='tab']", ".nav-link", ".tab-item",
            ".category-item", ".game-category"
        ]

        for selector in tab_selectors:
            try:
                tabs = await self.page.query_selector_all(selector)
                if tabs:
                    for tab in tabs[:8]: # Navigate only the first 8 if there are too many
                        if await tab.is_visible():
                            await tab.click()
                            # Wait for content to load
                            await asyncio.sleep(0.8)
            except Exception:
                continue

    async def trigger_hovers(self):
        """Hovers the mouse over menus and cards."""
        self.log("Simulating hover effects...")
        hover_selectors = [
            ".menu-item", ".nav-item", ".dropdown",
            ".game-card", ".product-item", "a.has-dropdown"
        ]

        for selector in hover_selectors:
            try:
                elements = await self.page.query_selector_all(selector)
                if elements:
                    for el in elements[:10]:
                        if await el.is_visible():
                            await el.hover()
                            await asyncio.sleep(0.2)
            except Exception:
                continue

    @staticmethod
    def inject_into_html(html_files: list[Path]) -> None:
        """Injects the offline interaction engine into all cloned pages."""
        script_content = """
        <!-- CLONER INTERACTION ENGINE (ALIVE JS) -->
        <script id="cloner-interaction-engine">
        (() => {
            console.log('[Cloner] Etkileşim Motoru Aktif...');

            // 1. OTOMATİK SLIDER DÖNGÜSÜ
            const triggerSliders = () => {
                const nextButtons = document.querySelectorAll('.swiper-button-next, .slick-next, .owl-next, .carousel-control-next');
                nextButtons.forEach(btn => {
                    setInterval(() => {
                        if (document.visibilityState === 'visible') btn.click();
                    }, 5000 + Math.random() * 3000);
                });
            };

            // 2. TABS & DROPDOWNS (JS-less fix)
            const fixInteractions = () => {
                document.querySelectorAll('.nav-link, .tab-item, [role="tab"], .dropdown-toggle').forEach(el => {
                    el.addEventListener('click', function(e) {
                         // Eğer link değilse veya empty link ise etkileşimi simüle et
                         const href = this.getAttribute('href');
                         if (!href || href === '#' || href.startsWith('javascript:')) {
                             e.preventDefault();
                             // Kardeş tabları/mönüleri bul ve aktiflik sınıflarını değiştir (Heuristic)
                             const parent = this.parentElement;
                             if (parent) {
                                 Array.from(parent.children).forEach(c => c.classList.remove('active', 'show', 'selected'));
                             }
                             this.classList.add('active', 'show');
                             console.log('[Cloner] Interaction Simulated:', this.innerText);
                         }
                    });
                });
            };

            // 3. LAZY-LOAD GÖRSEL TETİKLEYİCİ (Scroll-free)
            const forceImages = () => {
                document.querySelectorAll('img[data-src], img[data-lazy]').forEach(img => {
                    const src = img.getAttribute('data-src') || img.getAttribute('data-lazy');
                    if (src) img.src = src;
                });
            };

            window.addEventListener('DOMContentLoaded', () => {
                triggerSliders();
                fixInteractions();
                forceImages();
            });
        })();
        </script>
        """
        import re
        for html_file in html_files:
            try:
                content = html_file.read_text(encoding='utf-8', errors='replace')
                if "CLONER INTERACTION ENGINE" not in content:
                    if "</head>" in content.lower():
                        content = content.replace("</head>", script_content + "\n</head>")
                    else:
                        content = script_content + "\n" + content
                    html_file.write_text(content, encoding='utf-8')
            except Exception:
                continue

    def stop(self):
        self._is_running = False
