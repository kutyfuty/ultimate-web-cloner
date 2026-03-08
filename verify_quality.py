"""
Doğrudan Playwright ile kalite doğrulama.
HTTP server'ı kendi içinde başlatır.
"""
import asyncio, sys, os, io, threading
from http.server import SimpleHTTPRequestHandler
from socketserver import TCPServer
from pathlib import Path

if sys.platform == "win32":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace')

sys.path.insert(0, os.path.dirname(__file__))

from playwright.async_api import async_playwright
from playwright_stealth import Stealth

OUTPUT_DIR = Path(__file__).parent / "output" / "lidyabet"
QC_DIR = OUTPUT_DIR / "qc_screenshots"
PORT = 9292

PAGES = [
    ("index", "index.html", "/"),
    ("casino", "tr_game_casino.html", "/tr/game/casino"),
    ("sport", "tr_sport_bet.html", "/tr/sport/bet"),
    ("promotions", "tr_contents_promotions.html", "/tr/contents/promotions"),
    ("poker", "tr_game_poker.html", "/tr/game/poker"),
]

def start_server():
    os.chdir(str(OUTPUT_DIR))
    handler = SimpleHTTPRequestHandler
    handler.log_message = lambda *a: None  # sessiz
    httpd = TCPServer(("127.0.0.1", PORT), handler)
    httpd.serve_forever()

async def main():
    QC_DIR.mkdir(exist_ok=True)

    # HTTP sunucu başlat
    print("[*] HTTP sunucu baslatiliyor...")
    t = threading.Thread(target=start_server, daemon=True)
    t.start()
    await asyncio.sleep(1)

    pw = await async_playwright().start()
    browser = await pw.chromium.launch(headless=True)

    for name, local_file, orig_path in PAGES:
        print(f"\n{'='*50}")
        print(f"[*] {name}: {local_file}")

        local_path = OUTPUT_DIR / local_file
        if not local_path.exists():
            print(f"   [ATLA] Dosya mevcut degil")
            continue

        # -- Klon screenshot --
        print(f"   [1] Klon sayfasi aciliyor...")
        ctx = await browser.new_context(viewport={"width": 1920, "height": 1080})
        page = await ctx.new_page()
        try:
            await page.goto(f"http://127.0.0.1:{PORT}/{local_file}", wait_until="load", timeout=15000)
            await asyncio.sleep(2)
            clone_path = QC_DIR / f"{name}_clone.png"
            await page.screenshot(path=str(clone_path), full_page=False)
            print(f"   [OK] Klon screenshot: {clone_path.name}")
        except Exception as e:
            print(f"   [HATA] Klon: {e}")
            clone_path = None
        await ctx.close()

        # -- Orijinal screenshot --
        print(f"   [2] Orijinal sayfa aciliyor...")
        ctx = await browser.new_context(viewport={"width": 1920, "height": 1080})
        stealth = Stealth()
        await stealth.apply_stealth_async(ctx)
        page = await ctx.new_page()
        orig_url = f"https://lidyabet704.com{orig_path}"
        try:
            await page.goto(orig_url, wait_until="domcontentloaded", timeout=30000)
            try:
                await page.wait_for_load_state("networkidle", timeout=8000)
            except:
                pass
            await asyncio.sleep(3)
            orig_sc = QC_DIR / f"{name}_original.png"
            await page.screenshot(path=str(orig_sc), full_page=False)
            print(f"   [OK] Orijinal screenshot: {orig_sc.name}")
        except Exception as e:
            print(f"   [HATA] Orijinal: {e}")
            orig_sc = None
        await ctx.close()

        # -- Karsilastirma --
        if clone_path and orig_sc and clone_path.exists() and orig_sc.exists():
            try:
                from PIL import Image, ImageChops, ImageDraw
                orig = Image.open(orig_sc).convert("RGB")
                clone = Image.open(clone_path).convert("RGB")
                w, h = min(orig.width, clone.width), min(orig.height, clone.height)
                orig = orig.crop((0, 0, w, h))
                clone = clone.crop((0, 0, w, h))

                diff = ImageChops.difference(orig, clone)
                diff_data = list(diff.getdata())
                total_diff = sum(sum(p) for p in diff_data)
                max_possible = len(diff_data) * 255 * 3
                score = (1 - total_diff / max_possible) * 100 if max_possible > 0 else 0

                # Yan yana kaydet
                comp = Image.new("RGB", (w * 3, h))
                comp.paste(orig, (0, 0))
                comp.paste(clone, (w, 0))
                diff_enhanced = diff.point(lambda x: min(x * 5, 255))
                comp.paste(diff_enhanced, (w * 2, 0))
                draw = ImageDraw.Draw(comp)
                draw.text((10, 10), "ORIGINAL", fill=(255, 255, 0))
                draw.text((w + 10, 10), "CLONE", fill=(0, 255, 0))
                draw.text((w * 2 + 10, 10), "DIFF", fill=(255, 0, 0))
                comp_path = QC_DIR / f"{name}_comparison.png"
                comp.save(comp_path)

                icon = "OK" if score >= 90 else ("ORTA" if score >= 70 else "DUSUK")
                print(f"   [SKOR] %{score:.1f} — {icon}")
                print(f"   [KAYIT] {comp_path.name}")
            except Exception as e:
                print(f"   [HATA] Karsilastirma: {e}")

    await browser.close()
    await pw.stop()
    print(f"\n{'='*50}")
    print(f"[TAMAM] Tum ekran goruntuleri: {QC_DIR}")

if __name__ == "__main__":
    asyncio.run(main())
