"""
Test — Çok sayfalı klonlama (v3) + Kalite kontrol
"""

import asyncio
import sys
import os
import io
import subprocess



sys.path.insert(0, os.path.dirname(__file__))

from core.scraper_engine import ScraperEngine
from core.asset_manager import AssetManager
from core.link_mapper import LinkMapper
from core.quality_checker import QualityChecker
from pathlib import Path


def log(msg):
    try:
        print(f"  {msg}")
    except UnicodeEncodeError:
        print(f"  {msg.encode('ascii', 'replace').decode()}")


async def main():
    url = "https://zlot.com/tr-tr/"
    output_dir = Path(__file__).parent / "output" / "zlot"

    print(f"[*] Hedef: {url}")
    print(f"[*] Cikti: {output_dir}")
    print("=" * 60)

    # -- 1. Ana sayfayi kazi --
    scraper = ScraperEngine()
    scraper.log_message.connect(log)

    result_holder = {"html": None, "resources": None}

    def on_finished(html_desktop, html_auth, html_mobile, resources):
        result_holder["html"] = html_desktop  # Use the logged-out desktop HTML for main testing
        result_holder["resources"] = resources

    def on_failed(error):
        print(f"\n[HATA] {error}")

    scraper.scraping_finished.connect(on_finished)
    scraper.scraping_failed.connect(on_failed)

    await scraper.scrape_page(url, output_dir)

    if not result_holder["html"]:
        print("\n[HATA] Ana sayfa kazilamadi.")
        return

    html = result_holder["html"]
    resources = result_holder["resources"]
    content_types = scraper.captured_content_types

    print(f"\n{'=' * 60}")
    print(f"[OK] Ana sayfa DOM: {len(html):,} karakter")
    print(f"[OK] Yakalanan kaynak: {len(resources)} dosya")

    # -- 2. Ana sayfa asset'lerini kaydet --
    asset_mgr = AssetManager(output_dir)
    asset_mgr.log_message.connect(log)
    await asset_mgr.save_resources(resources, content_types)
    rewritten = asset_mgr.rewrite_html(html, url)
    await asset_mgr.rewrite_css_files(url)
    asset_mgr.save_html(rewritten)

    # -- 3. Alt sayfalari klonla (max 100 sayfa) --
    print(f"\n{'=' * 60}")
    print("[*] Alt sayfalar klonlaniyor (max 100)...")

    mapper = LinkMapper()
    mapper.log_message.connect(log)

    await mapper.clone_all_pages(
        base_url=url,
        main_html=html,
        output_dir=output_dir,
        captured_resources=resources,
        content_types=content_types,
        max_pages=0,
    )

    # -- 4. Kalite kontrol --
    print(f"\n{'=' * 60}")
    print("[*] Kalite kontrolu basliyor...")

    # Yerel sunucu baslat
    server_proc = subprocess.Popen(
        [sys.executable, "-m", "http.server", "8889"],
        cwd=str(output_dir),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    await asyncio.sleep(2)

    try:
        checker = QualityChecker()
        checker.log_message.connect(log)

        # Kontrol edilecek sayfalar (ilk 5)
        pages = {}
        scraped = mapper.scraped_pages
        for idx, (path, filename) in enumerate(scraped.items()):
            if idx >= 5:
                break
            # URL path'i oluştur
            key = path.lstrip("/") if path != "/" else ""
            pages[key] = filename

        report = await checker.run_quality_check(
            original_url=url,
            output_dir=output_dir,
            pages_to_check=pages,
            max_checks=5,
        )

        print(f"\n{'=' * 60}")
        print(f"[TAMAM] Kalite kontrol tamamlandi!")
        print(f"   Ortalama: %{report.get('average_score', 0):.1f}")
        print(f"   Degerlendirme: {report.get('overall', 'N/A')}")
    finally:
        server_proc.terminate()

    # -- 5. Sonuc --
    total_size = sum(len(v) for v in resources.values())
    scraped = mapper.scraped_pages
    print(f"\n{'=' * 60}")
    print(f"[TAMAM] Klonlama tamamlandi!")
    print(f"   Toplam sayfa: {len(scraped)}")
    print(f"   Toplam kaynak: {len(resources)}")
    print(f"   Toplam boyut: {total_size / (1024*1024):.1f} MB")


if __name__ == "__main__":
    asyncio.run(main())
