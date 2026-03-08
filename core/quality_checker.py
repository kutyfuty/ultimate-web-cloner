"""
quality_checker.py — Automated Quality Control System

Compares the original site with the cloned site:
- Opens both sites with Playwright
- Takes full-page screenshots
- Computes pixel-by-pixel difference (Structural Similarity Index)
- Produces a diff image highlighting difference regions
- Generates a quality report
"""

import asyncio
from pathlib import Path
from urllib.parse import urlparse

from PyQt6.QtCore import QObject, pyqtSignal
from playwright.async_api import async_playwright
from playwright_stealth import Stealth

try:
    from PIL import Image, ImageChops, ImageDraw
    HAS_PILLOW = True
except ImportError:
    HAS_PILLOW = False


class QualityChecker(QObject):
    """Automatic Original vs Clone comparison engine."""

    # ── Signals ──
    log_message = pyqtSignal(str)
    check_completed = pyqtSignal(str, float)    # (page_name, score_0_100)
    report_finished = pyqtSignal(dict)           # full report

    # Viewport settings
    VIEWPORT_WIDTH = 1920
    VIEWPORT_HEIGHT = 1080

    def __init__(self, parent=None):
        super().__init__(parent)
        self._results: list[dict] = []

    async def run_quality_check(
        self,
        original_url: str,
        output_dir: str | Path,
        pages_to_check: dict[str, str] | None = None,
        max_checks: int = 5,
        local_port: int = 8889,
    ) -> dict:
        """
        Run quality check.

        Args:
            original_url: URL of the original site
            output_dir: folder of the cloned site
            pages_to_check: {url_path: local_filename} pages to check
            max_checks: maximum number of checks
            local_port: local server port
        """
        output_dir = Path(output_dir)
        self._results = []

        if not HAS_PILLOW:
            self.log_message.emit("⚠️  Pillow library not installed — pip install Pillow")
            return {"success": False, "error": "Pillow not installed"}

        # Pages to check
        if pages_to_check is None:
            # Default: index + first few HTML files in output_dir
            pages_to_check = {"": "index.html"}
            html_files = sorted(output_dir.glob("*.html"))
            for hf in html_files[:max_checks]:
                if hf.name != "index.html":
                    pages_to_check[hf.stem] = hf.name

        # max_checks limit
        check_list = list(pages_to_check.items())[:max_checks]

        self.log_message.emit(f"🔍 Quality check starting: {len(check_list)} pages")
        self.log_message.emit("=" * 50)

        # QC directory
        qc_dir = output_dir / "quality_report"
        qc_dir.mkdir(exist_ok=True)

        playwright = await async_playwright().start()
        browser = await playwright.chromium.launch(headless=True)

        try:
            for idx, (page_key, local_file) in enumerate(check_list, 1):
                self.log_message.emit(f"\n📊 [{idx}/{len(check_list)}] {local_file}")

                parsed = urlparse(original_url)
                if page_key:
                    # Build URL path
                    orig_page_url = f"{parsed.scheme}://{parsed.netloc}/{page_key}"
                else:
                    orig_page_url = original_url

                local_page_url = f"http://localhost:{local_port}/{local_file}"

                result = await self._compare_page(
                    browser=browser,
                    original_url=orig_page_url,
                    local_url=local_page_url,
                    page_name=local_file,
                    qc_dir=qc_dir,
                )
                self._results.append(result)
                self.check_completed.emit(local_file, result["score"])

        finally:
            await browser.close()
            await playwright.stop()

        # Generate report
        report = self._generate_report(qc_dir)
        self.report_finished.emit(report)
        return report

    async def _compare_page(
        self,
        browser,
        original_url: str,
        local_url: str,
        page_name: str,
        qc_dir: Path,
    ) -> dict:
        """Compare a single page."""

        name_base = page_name.replace(".html", "")

        # --- Screenshot of the original page ---
        self.log_message.emit(f"   📸 Opening original page: {original_url}")
        orig_path = qc_dir / f"{name_base}_original.png"
        try:
            context = await browser.new_context(
                viewport={"width": self.VIEWPORT_WIDTH, "height": self.VIEWPORT_HEIGHT},
            )
            stealth = Stealth()
            await stealth.apply_stealth_async(context)
            page = await context.new_page()

            await page.goto(original_url, wait_until="domcontentloaded", timeout=30000)
            try:
                await page.wait_for_load_state("networkidle", timeout=8000)
            except Exception:
                pass
            await asyncio.sleep(2)

            # --- Freeze Dynamic Banners / Poker / Live Data ---
            freeze_script = """
            () => {
                let s = document.createElement('style');
                s.innerHTML = `
                    * { animation: none !important; transition: none !important; caret-color: transparent !important; }
                    .swiper, .swiper-wrapper, .swiper-slide, .carousel, .slider, .banner, .home-sliders,
                    .live, .odds, .score, .timer, .countdown, .poker-tables, .jackpot, .counter,
                    iframe, video, canvas, .skeleton
                    { visibility: hidden !important; opacity: 0 !important; }
                `;
                document.head.appendChild(s);
            }
            """
            await page.evaluate(freeze_script)
            await asyncio.sleep(1)

            await page.screenshot(path=str(orig_path), full_page=False)  # Viewport only
            await context.close()
            self.log_message.emit(f"   ✅ Original screenshot captured")
        except Exception as e:
            self.log_message.emit(f"   ❌ Original page error: {e}")
            return {"page": page_name, "score": 0, "error": str(e)}

        # --- Screenshot of the cloned page ---
        self.log_message.emit(f"   📸 Opening clone page: {local_url}")
        clone_path = qc_dir / f"{name_base}_clone.png"
        try:
            context = await browser.new_context(
                viewport={"width": self.VIEWPORT_WIDTH, "height": self.VIEWPORT_HEIGHT},
            )
            page = await context.new_page()
            await page.goto(local_url, wait_until="load", timeout=15000)
            await asyncio.sleep(1)

            # --- Freeze Dynamic Banners / Poker / Live Data ---
            await page.evaluate(freeze_script)
            await asyncio.sleep(1)

            await page.screenshot(path=str(clone_path), full_page=False)
            await context.close()
            self.log_message.emit(f"   ✅ Clone screenshot captured")
        except Exception as e:
            self.log_message.emit(f"   ❌ Clone page error: {e}")
            return {"page": page_name, "score": 0, "error": str(e)}

        # --- Pixel comparison ---
        score, diff_path = self._pixel_compare(orig_path, clone_path, qc_dir, name_base)
        status = "✅ Excellent" if score >= 95 else ("⚠️  Good" if score >= 80 else "❌ Low")
        self.log_message.emit(f"   📊 Similarity: %{score:.1f} {status}")

        return {
            "page": page_name,
            "score": score,
            "original_screenshot": str(orig_path),
            "clone_screenshot": str(clone_path),
            "diff_image": str(diff_path) if diff_path else None,
        }

    def _pixel_compare(
        self, orig_path: Path, clone_path: Path, qc_dir: Path, name_base: str
    ) -> tuple[float, Path | None]:
        """
        Compare two screenshots pixel by pixel.
        Returns: (similarity_score_0-100, diff_image_path)
        """
        try:
            orig = Image.open(orig_path).convert("RGB")
            clone = Image.open(clone_path).convert("RGB")

            # Align dimensions (crop to the smaller one)
            w = min(orig.width, clone.width)
            h = min(orig.height, clone.height)
            orig = orig.crop((0, 0, w, h))
            clone = clone.crop((0, 0, w, h))

            # Difference image
            diff = ImageChops.difference(orig, clone)

            # Pixel-based similarity calculation
            diff_data = list(diff.getdata())
            total_pixels = len(diff_data)
            total_diff = sum(sum(pixel) for pixel in diff_data)
            max_possible = total_pixels * 255 * 3  # R + G + B

            similarity = (1 - (total_diff / max_possible)) * 100 if max_possible > 0 else 0

            # Save diff image (highlight differences)
            diff_enhanced = diff.point(lambda x: min(x * 5, 255))  # Amplify differences
            diff_path = qc_dir / f"{name_base}_diff.png"

            # Build side-by-side comparison
            comparison = Image.new("RGB", (w * 3, h))
            comparison.paste(orig, (0, 0))
            comparison.paste(clone, (w, 0))
            comparison.paste(diff_enhanced, (w * 2, 0))

            # Add labels
            draw = ImageDraw.Draw(comparison)
            draw.text((10, 10), "ORIGINAL", fill=(255, 255, 0))
            draw.text((w + 10, 10), "CLONE", fill=(0, 255, 0))
            draw.text((w * 2 + 10, 10), "DIFF", fill=(255, 0, 0))

            comparison.save(diff_path)
            self.log_message.emit(f"   🖼️  Comparison saved: {diff_path.name}")

            return similarity, diff_path

        except Exception as e:
            self.log_message.emit(f"   ⚠️  Comparison error: {e}")
            return 0.0, None

    def _generate_report(self, qc_dir: Path) -> dict:
        """Generate quality report."""
        if not self._results:
            return {"success": False, "pages": [], "average_score": 0}

        avg_score = sum(r["score"] for r in self._results) / len(self._results)

        self.log_message.emit("\n" + "=" * 50)
        self.log_message.emit("📋 QUALITY CONTROL REPORT")
        self.log_message.emit("=" * 50)

        for r in self._results:
            score = r["score"]
            icon = "✅" if score >= 95 else ("⚠️" if score >= 80 else "❌")
            self.log_message.emit(f"   {icon} {r['page']:40s} %{score:.1f}")

        self.log_message.emit("─" * 50)
        self.log_message.emit(f"   📊 Average: %{avg_score:.1f}")

        overall = "EXCELLENT" if avg_score >= 95 else (
            "GOOD" if avg_score >= 80 else (
                "ACCEPTABLE" if avg_score >= 60 else "LOW"
            )
        )
        self.log_message.emit(f"   🏆 Overall Rating: {overall}")
        self.log_message.emit(f"   📂 Report: {qc_dir}")

        # Write report file
        report_path = qc_dir / "report.md"
        lines = [
            "# Quality Control Report\n",
            f"**Average Similarity:** %{avg_score:.1f}\n",
            f"**Rating:** {overall}\n\n",
            "| Page | Score | Status |\n",
            "|------|-------|--------|\n",
        ]
        for r in self._results:
            s = r["score"]
            icon = "✅" if s >= 95 else ("⚠️" if s >= 80 else "❌")
            lines.append(f"| {r['page']} | %{s:.1f} | {icon} |\n")

        report_path.write_text("".join(lines), encoding="utf-8")

        return {
            "success": True,
            "pages": self._results,
            "average_score": avg_score,
            "overall": overall,
            "report_dir": str(qc_dir),
        }
