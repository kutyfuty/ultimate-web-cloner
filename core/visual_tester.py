"""
visual_tester.py - Visual Regression Test (Pillar 5)

Compares a screenshot of the original with a screenshot of the local clone
to measure the pixel accuracy of the cloned site.
Starts a temporary HTTP server to guarantee CSS/font loading.
"""

import asyncio
import socket
import threading
import http.server
import functools
from pathlib import Path
from PIL import Image, ImageChops
import math

from PyQt6.QtCore import QObject, pyqtSignal
from playwright.async_api import async_playwright


class VisualTester(QObject):
    log_message = pyqtSignal(str)

    def __init__(self, output_dir: Path):
        super().__init__()
        self.output_dir = output_dir

    # ──────────────────────────────────────────────
    #  Temporary HTTP Server (for CSS/font loading)
    # ──────────────────────────────────────────────

    def _start_temp_server(self) -> tuple[http.server.HTTPServer, int]:
        """Starts a temporary HTTP server serving output_dir."""
        handler = functools.partial(
            http.server.SimpleHTTPRequestHandler,
            directory=str(self.output_dir)
        )

        class SilentHandler(handler):
            def log_message(self, format, *args):
                pass  # Suppress server logs

        # Get a free port from the OS
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]

        server = http.server.HTTPServer(("127.0.0.1", port), SilentHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        return server, port

    # ──────────────────────────────────────────────
    #  Main Test Flow
    # ──────────────────────────────────────────────

    async def run_test(self) -> float:
        """
        Opens the clone over HTTP with Playwright, takes a screenshot,
        and returns a similarity percentage compared to the original screenshot.
        """
        original_path = self.output_dir / "original_screenshot.png"
        cloned_path = self.output_dir / "cloned_screenshot.png"

        # Use index_auth.html (logged-in clone) if it exists, otherwise index.html
        index_path = self.output_dir / "index_auth.html"
        if not index_path.exists():
            index_path = self.output_dir / "index.html"

        if not original_path.exists() or not index_path.exists():
            self.log_message.emit("⚠️ Visual test: original screenshot or index.html not found.")
            return 0.0

        self.log_message.emit("👁️ Visual Fidelity Test Starting...")

        # Read original screenshot dimensions — clone will use the same viewport
        try:
            with Image.open(original_path) as probe:
                orig_w, orig_h = probe.size
        except Exception:
            orig_w, orig_h = 1280, 720

        # Start temporary HTTP server
        server, port = self._start_temp_server()
        base_url = f"http://127.0.0.1:{port}"
        page_url = f"{base_url}/{index_path.name}"

        try:
            playwright = await async_playwright().start()
            browser = await playwright.chromium.launch(headless=True)
            context = await browser.new_context(viewport={"width": orig_w, "height": orig_h})
            page = await context.new_page()

            await page.goto(page_url, wait_until="networkidle", timeout=30000)
            await asyncio.sleep(1)

            await page.screenshot(path=str(cloned_path), full_page=True)

            await browser.close()
            await playwright.stop()
        except Exception as e:
            self.log_message.emit(f"⚠️ Error capturing clone screenshot: {e}")
            return 0.0
        finally:
            server.shutdown()

        # Pillow comparison
        try:
            img1 = Image.open(original_path).convert('RGB')
            img2 = Image.open(cloned_path).convert('RGB')

            # Crop to the smaller dimensions
            w = min(img1.width, img2.width)
            h = min(img1.height, img2.height)
            img1 = img1.crop((0, 0, w, h))
            img2 = img2.crop((0, 0, w, h))

            diff = ImageChops.difference(img1, img2)

            h_diff = diff.histogram()
            sq = (value * ((idx % 256) ** 2) for idx, value in enumerate(h_diff))
            rms = math.sqrt(sum(sq) / float(w * h))

            # RMS → similarity (0 RMS = 100%, 255 RMS = 0%)
            similarity = max(0.0, min(100.0, 100.0 - (rms / 255.0 * 100.0)))

            # Save difference map (4x amplify for visibility)
            # Use a lookup table instead of lambda — required by Pillow 10+
            lut = [min(255, i * 4) for i in range(256)]
            num_bands = len(diff.getbands())
            diff.point(lut * num_bands).save(
                str(self.output_dir / "visual_diff.png")
            )

            self.log_message.emit(
                f"   📐 Comparison: original {orig_w}x{orig_h} "
                f"↔ clone {img2.width}x{img2.height} → common area {w}x{h}"
            )

            if similarity >= 85:
                self.log_message.emit(f"✅ Visual Test PASSED: {similarity:.1f}% match")
            elif similarity >= 60:
                self.log_message.emit(f"⚠️ Visual Test WARNING: {similarity:.1f}% match (minor shifts)")
            else:
                self.log_message.emit(f"❌ Visual Test FAILED: {similarity:.1f}% match (layout broken)")

            return similarity

        except Exception as e:
            self.log_message.emit(f"⚠️ Visual comparison error: {e}")
            return 0.0
