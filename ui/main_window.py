"""
main_window.py — Main PyQt6 Window

The main window of the Web Cloner application.
URL input, progress tracking, live log, and scraping controls.
"""

import asyncio
import os
import re
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlparse

from PyQt6.QtCore import Qt, QSize, QThread, pyqtSignal, QObject
from PyQt6.QtGui import QIcon, QPixmap
from PyQt6.QtWidgets import (
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QStatusBar,
    QTabWidget,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from core.asset_manager import AssetManager
from core.link_mapper import LinkMapper
from core.scraper_engine import ScraperEngine
from core.preview_server import PreviewServer
from core.config_manager import ConfigManager
from ui.widgets.log_viewer import LogViewer
from ui.widgets.progress_panel import ProgressPanel
from ui.widgets.url_input_bar import UrlInputBar
from core.frontend_mocker import FrontendMocker
from core.visual_tester import VisualTester


class CloningWorker(QObject):
    """
    (Phase 8) Asynchronous worker class that runs cloning operations
    in a separate thread (QThread). Prevents the UI from freezing.
    """
    log_message = pyqtSignal(str)
    progress_updated = pyqtSignal(int)
    scraping_finished = pyqtSignal(str, str, str, dict, dict) # html_desktop, html_desktop_logged_in, html_mobile, captured_resources, detected_selectors
    scraping_failed = pyqtSignal(str)

    # Link Mapper signals
    page_cloned = pyqtSignal(str, str) # url, title
    all_pages_finished = pyqtSignal(int) # total_pages
    total_pages_detected = pyqtSignal(int)
    page_progress = pyqtSignal(int, int)

    # Post-processing signal — notifies UI when finished in background
    post_processing_finished = pyqtSignal(int)  # total_pages

    def __init__(self, scraper: ScraperEngine, link_mapper: LinkMapper, config_manager: ConfigManager = None):
        super().__init__()
        self.scraper = scraper
        self.link_mapper = link_mapper
        self._config_manager = config_manager

        # Connect signals (acting as a proxy)
        self.scraper.log_message.connect(self.log_message.emit)
        self.scraper.progress_updated.connect(self.progress_updated.emit)
        self.scraper.scraping_finished.connect(self.scraping_finished.emit)
        self.scraper.scraping_failed.connect(self.scraping_failed.emit)

        self.link_mapper.log_message.connect(self.log_message.emit)
        self.link_mapper.progress_updated.connect(self.progress_updated.emit)
        self.link_mapper.page_cloned.connect(self.page_cloned.emit)
        self.link_mapper.all_pages_finished.connect(self.all_pages_finished.emit)
        self.link_mapper.total_pages_detected.connect(self.total_pages_detected.emit)
        self.link_mapper.page_progress.connect(self.page_progress.emit)

        self._loop = None
        self._loop_thread = None
        self._is_running = False
        self._asset_manager: AssetManager | None = None
        self._current_output_dir: Path | None = None
        self._mocker: FrontendMocker | None = None
        self._current_max_pages: int = 0
        self._current_deep_crawl: bool = False
        self._current_max_depth: int = 0
        self._current_use_auth: bool = False
        self._current_dual_pass: bool = False
        self._current_hide_username: str = ""
        self._current_url: str = ""

    def run_flow(self, url, output_dir, use_auth, dual_pass, max_pages, deep_crawl, max_depth, hide_username, mocker):
        """Starts the asynchronous flow on this thread's event loop."""
        print(f"\n[WORKER] run_flow triggered: {url}")
        self._current_output_dir = output_dir
        self._current_max_pages = max_pages
        self._current_deep_crawl = deep_crawl
        self._current_max_depth = max_depth
        self._current_use_auth = use_auth
        self._current_dual_pass = dual_pass
        self._current_hide_username = hide_username
        self._mocker = mocker
        self._current_url = url

        # Loop check and restart if needed
        loop_needs_init = False
        if self._loop is None or self._loop.is_closed():
            loop_needs_init = True
        elif self._loop_thread and not self._loop_thread.is_alive():
            # Loop may have stopped but its object not yet closed
            loop_needs_init = True

        if loop_needs_init:
            print("[WORKER] Starting new asyncio loop...")
            self.log_message.emit("⚙️ Preparing async engine...")
            self._loop = asyncio.new_event_loop()
            import threading
            self._loop_thread = threading.Thread(target=self._loop.run_forever, daemon=True)
            self._loop_thread.start()
        else:
            print("[WORKER] Using existing asyncio loop.")

        print("[WORKER] Sending task to async channel...")
        asyncio.run_coroutine_threadsafe(
            self._async_flow(url, output_dir, use_auth, dual_pass, max_pages, deep_crawl, max_depth, hide_username),
            self._loop
        )

    async def _async_flow(self, url, output_dir, use_auth, dual_pass, max_pages, deep_crawl, max_depth, hide_username):
        try:
            # 1. Auth State
            if use_auth:
                cfg = self._config_manager.config if self._config_manager else None
                has_creds = cfg and cfg.has_credentials if cfg else False

                if has_creds:
                    # Programmatic login (using credentials from config)
                    self.log_message.emit("🔐 Config-based automatic login mode")
                    success, captured_username = await self.scraper.capture_auth_state_auto(
                        login_url=url,
                        username=cfg.login_credentials.username,
                        password=cfg.login_credentials.password,
                        output_dir=output_dir,
                        success_selector=cfg.selectors.success_indicator if cfg else "",
                    )
                else:
                    # Manual login (headed browser)
                    success, captured_username = await self.scraper.capture_auth_state_ui(url, output_dir)

                if not success:
                    self.scraping_failed.emit("Session cancelled.")
                    return
                if captured_username:
                    self._current_hide_username = captured_username
                    self.log_message.emit(f"🕵️ Captured username -> {captured_username}")
                else:
                    self.log_message.emit("⚠️ Captured username could not be fully detected.")

            # 2. Main Page Scrape
            await self.scraper.scrape_page(url, output_dir, use_auth=use_auth, dual_pass=dual_pass)
        except Exception as e:
            import traceback
            traceback.print_exc()
            self.scraping_failed.emit(str(e))

    def process_scraped_data_and_map_links(self, html_desktop: str, html_desktop_logged_in: str, html_mobile: str, captured_resources: dict, detected_selectors: dict):
        """
        Called when main page scraping is complete — saves assets and clones sub-pages.
        This is a synchronous wrapper because it arrives via a signal.
        """
        if self._loop and not self._loop.is_closed():
            asyncio.run_coroutine_threadsafe(
                self._async_process_data(html_desktop, html_desktop_logged_in, html_mobile, captured_resources, detected_selectors),
                self._loop
            )

    async def _async_process_data(self, html_desktop, html_desktop_logged_in, html_mobile, captured_resources, detected_selectors):
        url = self._current_url

        # Save auto-detected selectors to config and pass to mocker
        if detected_selectors:
            self.log_message.emit("✨ Login and Profile fields automatically detected!")

            # Write to config and save to disk
            if self._config_manager:
                self._config_manager.update_selectors(detected_selectors)
                self._config_manager.save()
                self.log_message.emit("💾 Selectors saved to target_config.json")

            # UPDATE mocker with these values (fill in empty ones)
            if self._mocker:
                if not self._mocker.login_form_selector:
                    self._mocker.login_form_selector = detected_selectors.get("login_form", "")
                if not self._mocker.username_input_selector:
                    self._mocker.username_input_selector = detected_selectors.get("username_input", "")
                if not self._mocker.username_display_selector:
                    self._mocker.username_display_selector = detected_selectors.get("username_display", "")

        self.log_message.emit("─" * 50)
        self.log_message.emit("📦 Saving main page assets...")

        # Create asset manager (Phase 6 with Mocker)
        self._asset_manager = AssetManager(self._current_output_dir, mocker=self._mocker, config=self._config_manager.config if self._config_manager else None)
        self._asset_manager.log_message.connect(self.log_message.emit)

        # Take a single snapshot of content_types — the property produces a copy on each call
        content_types_snapshot = self.scraper.captured_content_types

        # Save resources ASYNCHRONOUSLY
        await self._asset_manager.save_resources(
            captured_resources, content_types_snapshot
        )

        # Fix CSS files (and asynchronously download missing ones)
        await self._asset_manager.rewrite_css_files(url)

        # Inline web fonts as base64 (prevents offline font shift)
        await asyncio.to_thread(self._asset_manager.inline_webfonts_as_base64)

        # Convert absolute API URLs in JS files to relative paths
        await asyncio.to_thread(self._asset_manager.rewrite_js_api_urls, url)

        # ── Phase 20: Dual File Output (index.html & index_auth.html) ──
        # rewrite_html contains CPU-intensive BeautifulSoup work → use asyncio.to_thread to free event loop
        # 1. Normal index.html (Logged out, has Sign Up button)
        self.log_message.emit("🔗 Updating main page (Desktop) paths...")
        rewritten_desktop_out = await asyncio.to_thread(
            self._asset_manager.rewrite_html,
            html_desktop, url, "", self._current_hide_username, False
        )
        await asyncio.to_thread(self._asset_manager.save_html, rewritten_desktop_out, "index.html")

        # 2. Logged-in index_auth.html
        if html_desktop_logged_in:
            self.log_message.emit("🔗 Updating main page (With Cookies) paths and adding 404 Protection...")
            rewritten_desktop_in = await asyncio.to_thread(
                self._asset_manager.rewrite_html,
                html_desktop_logged_in, url, "", self._current_hide_username, True
            )
            await asyncio.to_thread(self._asset_manager.save_html, rewritten_desktop_in, "index_auth.html")

        # Save mobile version if available
        if html_mobile:
            self.log_message.emit("🔗 Updating main page (Mobile) paths...")
            rewritten_mobile = await asyncio.to_thread(
                self._asset_manager.rewrite_html,
                html_mobile, url, "", self._current_hide_username, False
            )
            await asyncio.to_thread(self._asset_manager.save_html, rewritten_mobile, "index_mobile.html")

        self.log_message.emit("─" * 50)
        self.log_message.emit("📄 Main page saved — cloning sub-pages...")
        self.progress_updated.emit(0) # Reset progress for link mapping

        # ── Start multi-page cloning ──
        if self._current_deep_crawl:
            await self.link_mapper.clone_all_pages(
                base_url=url,
                main_html=html_desktop,
                output_dir=self._current_output_dir,
                captured_resources=captured_resources, # Use the original captured_resources for link_mapper
                content_types=content_types_snapshot,
                max_pages=self._current_max_pages,
                deep_crawl=self._current_deep_crawl,
                max_depth=self._current_max_depth,
                api_mocker=self.scraper.api_mocker,
                use_auth=self._current_use_auth,
                dual_pass=self._current_dual_pass,
                hide_username=self._current_hide_username,
                mocker=self._mocker
            )
        else:
            self.log_message.emit("✅ Deep crawl disabled. Operation complete.")
            self.all_pages_finished.emit(1) # Indicate 1 page finished (the main page)

    def start_post_processing(self, total_pages: int, output_dir, hide_user: str):
        """Starts post-processing on the background loop (UI does not freeze)."""
        if self._loop and not self._loop.is_closed():
            asyncio.run_coroutine_threadsafe(
                self._run_post_processing(total_pages, output_dir, hide_user),
                self._loop
            )

    async def _run_post_processing(self, total_pages: int, output_dir, hide_user: str):
        """
        Mandatory post-processing steps — the fast ones.
        Slow optimizations (dead CSS, font subset) do not run automatically;
        they are triggered on demand via the Analyze button.
        """
        am = self._asset_manager
        if am:
            for msg, fn in [
                ("🎬 Video thumbnails...", am.generate_video_thumbnails),
                ("🔒 Integrity report...",  am.save_integrity_report),
            ]:
                self.log_message.emit(msg)
                try:
                    await asyncio.to_thread(fn)
                except Exception as e:
                    self.log_message.emit(f"⚠️ {e}")

        # Sanitize — only runs if a username was detected
        if output_dir and Path(output_dir).exists() and hide_user:
            self.log_message.emit("🔐 Sanitizing personal data...")
            try:
                from core.sanitizer import DataSanitizer
                counts = await asyncio.to_thread(
                    DataSanitizer().sanitize_directory, Path(output_dir), hide_user
                )
                total = sum(counts.values())
                if total:
                    self.log_message.emit(
                        f"🔐 Sanitize: {counts['html']} HTML + "
                        f"{counts['js']} JS + {counts['json']} JSON"
                    )
            except Exception as e:
                self.log_message.emit(f"⚠️ Sanitize error: {e}")

        # Visual quality test — original_screenshot.png vs cloned index.html
        if output_dir and Path(output_dir).exists():
            original_ss = Path(output_dir) / "original_screenshot.png"
            if original_ss.exists():
                self.log_message.emit("👁️ Starting visual quality test...")
                try:
                    vt = VisualTester(Path(output_dir))
                    vt.log_message.connect(self.log_message.emit)
                    await vt.run_test()
                except Exception as e:
                    self.log_message.emit(f"⚠️ Visual test error: {e}")

        self.post_processing_finished.emit(total_pages)

    def stop(self):
        """Stop operations."""
        print("[WORKER] Stop signal received.")
        self.link_mapper.stop()
        if self._loop and self._loop.is_running():
            print("[WORKER] Stopping loop...")
            # ScraperEngine.stop() is a coroutine, so we send it to the loop
            asyncio.run_coroutine_threadsafe(self.scraper.stop(), self._loop)
            self._loop.call_soon_threadsafe(self._loop.stop)
        else:
            print("[WORKER] Loop is not running.")


class MainWindow(QMainWindow):
    """Web Cloner main window."""
    # (Phase 8) Worker command signals
    request_run_flow = pyqtSignal(str, Path, bool, bool, int, bool, int, str, object)
    request_process_data = pyqtSignal(str, str, str, dict, dict)

    def __init__(self):
        super().__init__()
        self.setWindowTitle("Web Cloner — Site Cloning Tool")
        self.setMinimumSize(QSize(900, 700))
        self.resize(1050, 780)

        # ── Config Manager ──
        self._config_manager = ConfigManager()
        self._config_manager.load()
        cfg = self._config_manager.config

        self._scraper = ScraperEngine()
        self._link_mapper = LinkMapper()

        # (Phase 8) Worker Thread Setup
        self._worker_thread = QThread()
        self._worker = CloningWorker(self._scraper, self._link_mapper, self._config_manager)
        self._worker.moveToThread(self._worker_thread)
        self._worker_thread.start()

        self._preview_server = PreviewServer()
        self._preview_server.log_message.connect(lambda msg: self.log_viewer.append(msg))

        self._asset_manager: AssetManager | None = None
        self._current_output_dir: Path | None = None
        self._main_html_raw: str | None = None
        self._all_resources: dict[str, bytes] = {}
        self._all_content_types: dict[str, str] = {}

        # ── Load stylesheet ──
        self._load_stylesheet()

        # ── Set up UI ──
        self._setup_ui()
        self._connect_signals()
        self._connect_worker_signals()

        # ── Status bar ──
        self.statusBar().showMessage("Ready — Enter a URL to get started")

    # ──────────────────────────────────────────────
    #  UI SETUP
    # ──────────────────────────────────────────────

    def _load_stylesheet(self):
        """Load the QSS stylesheet file."""
        style_path = Path(__file__).parent / "resources" / "style.qss"
        if style_path.exists():
            self.setStyleSheet(style_path.read_text(encoding="utf-8"))

    def _setup_ui(self):
        central = QWidget()
        self.setCentralWidget(central)

        main_layout = QVBoxLayout(central)
        main_layout.setContentsMargins(24, 20, 24, 16)
        main_layout.setSpacing(16)

        # ── URL Input Bar ──
        self.url_bar = UrlInputBar()
        main_layout.addWidget(self.url_bar)

        # ── Progress Panel ──
        self.progress_panel = ProgressPanel()
        main_layout.addWidget(self.progress_panel)

        # ── Tab Panel (Log + Site Tree + Screenshots + Analysis) ──
        self.tab_widget = QTabWidget()
        self.tab_widget.setDocumentMode(True)

        # Tab 0 — Log
        self.log_viewer = LogViewer()
        self.tab_widget.addTab(self.log_viewer, "📋 Log")

        # Tab 1 — F1: Site Map Tree
        self._site_tree = QTreeWidget()
        self._site_tree.setHeaderLabels(["Page", "Local File"])
        self._site_tree.setColumnWidth(0, 380)
        self._site_tree.setAlternatingRowColors(True)
        self._site_tree.setSortingEnabled(True)
        self.tab_widget.addTab(self._site_tree, "🗺️ Site Map")

        # Tab 2 — F2: Screenshot Gallery
        self._screenshot_scroll = QScrollArea()
        self._screenshot_scroll.setWidgetResizable(True)
        self._screenshot_container = QWidget()
        self._screenshot_layout = QHBoxLayout(self._screenshot_container)
        self._screenshot_layout.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
        self._screenshot_layout.setSpacing(12)
        self._screenshot_scroll.setWidget(self._screenshot_container)
        self.tab_widget.addTab(self._screenshot_scroll, "📸 Screenshots")

        # Tab 3 — Analysis Report (C3 + C4 + C5 summary)
        self._analysis_viewer = LogViewer()
        self.tab_widget.addTab(self._analysis_viewer, "📊 Analysis")

        main_layout.addWidget(self.tab_widget, stretch=1)

        # ── Bottom buttons ──
        bottom_row = QHBoxLayout()
        bottom_row.setSpacing(10)
        bottom_row.addStretch()

        self.preview_btn = QPushButton("🌐 Preview in Browser")
        self.preview_btn.setObjectName("previewButton")
        self.preview_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.preview_btn.setEnabled(False)
        self.preview_btn.clicked.connect(self._start_preview_server)
        bottom_row.addWidget(self.preview_btn)

        self.open_folder_btn = QPushButton("📂 Open Output Folder")
        self.open_folder_btn.setObjectName("openFolderButton")
        self.open_folder_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.open_folder_btn.setEnabled(False)
        self.open_folder_btn.clicked.connect(self._open_output_folder)
        bottom_row.addWidget(self.open_folder_btn)

        self.zip_btn = QPushButton("📦 Download ZIP")
        self.zip_btn.setObjectName("zipButton")
        self.zip_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.zip_btn.setEnabled(False)
        self.zip_btn.clicked.connect(self._create_zip_package)
        bottom_row.addWidget(self.zip_btn)

        self.analyze_btn = QPushButton("📊 Analysis Report")
        self.analyze_btn.setObjectName("analyzeButton")
        self.analyze_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.analyze_btn.setEnabled(False)
        self.analyze_btn.clicked.connect(self._run_analysis)
        bottom_row.addWidget(self.analyze_btn)

        main_layout.addLayout(bottom_row)

    def _connect_signals(self):
        """Connect UI signals."""
        self.url_bar.clone_requested.connect(self._start_cloning)
        self.url_bar.stop_requested.connect(self._stop_cloning)

        self.open_folder_btn.clicked.connect(self._open_output_folder)
        self.preview_btn.clicked.connect(self._start_preview_server)

    def _connect_worker_signals(self):
        """Connect worker (background thread) signals to the UI."""
        self.request_run_flow.connect(self._worker.run_flow)
        self.request_process_data.connect(self._worker.process_scraped_data_and_map_links)

        self._worker.log_message.connect(self.log_viewer.append)
        self._worker.progress_updated.connect(self.progress_panel.set_progress)
        self._worker.scraping_finished.connect(self._on_scraping_finished)
        self._worker.scraping_failed.connect(self._on_scraping_failed)

        # Link Mapper / Phase 11 signals
        self._worker.page_cloned.connect(self._on_page_cloned)
        self._worker.total_pages_detected.connect(lambda total: self.progress_panel.set_page_info(0, total))
        self._worker.page_progress.connect(self.progress_panel.set_page_info)
        self._worker.all_pages_finished.connect(self._on_all_pages_finished)
        self._worker.post_processing_finished.connect(self._on_post_processing_done)

    # ──────────────────────────────────────────────
    #  CLONING OPERATIONS
    # ──────────────────────────────────────────────

    def _start_cloning(self, url: str, max_pages: int, deep_crawl: bool, max_depth: int, use_auth: bool = False, dual_pass: bool = False):
        """Start the cloning operation."""
        self._current_max_pages = max_pages
        self._current_deep_crawl = deep_crawl
        self._current_max_depth = max_depth
        self._current_use_auth = use_auth
        self._current_dual_pass = dual_pass
        self._current_hide_username = ""
        self._current_url = url

        # Get selectors from config and pass to FrontendMocker
        cfg = self._config_manager.config
        self._mocker = FrontendMocker(
            login_form_selector=cfg.selectors.login_form,
            username_input_selector=cfg.selectors.username_input,
            username_display_selector=cfg.selectors.logged_in_name_display,
        )

        # Determine output directory
        parsed = urlparse(url)
        domain = parsed.netloc.replace(":", "_").replace("www.", "")
        safe_domain = re.sub(r'[<>:"/\\|?*]', '_', domain)

        output_dir = Path.cwd() / "output" / safe_domain
        self._current_output_dir = output_dir

        # Update UI
        self.url_bar.set_running(True)
        self.progress_panel.reset()
        self.progress_panel.set_status("Cloning...")
        self.progress_panel.set_loading(True) # Turn on spinner
        self.log_viewer.clear()
        self._site_tree.clear()
        self._analysis_viewer.clear()
        self._clear_screenshot_gallery()
        self.open_folder_btn.setEnabled(False)
        self.preview_btn.setEnabled(False)
        self.analyze_btn.setEnabled(False)
        self._preview_server.stop_server()

        self.statusBar().showMessage(f"Cloning: {url}")

        self.log_viewer.append(f"🎯 Target: {url}", "highlight")
        self.log_viewer.append(f"📂 Output: {output_dir}", "info")

        # (Phase 8) Run in background (via QThread + Signal)
        self.request_run_flow.emit(
            url, output_dir, use_auth, dual_pass, max_pages, deep_crawl, max_depth, self._current_hide_username, self._mocker
        )

    def _stop_cloning(self):
        """Stop the cloning operation."""
        self._worker.stop()
        self.url_bar.set_running(False)
        self.progress_panel.set_status("Stopped")
        self.progress_panel.set_loading(False) # Turn off spinner
        self.statusBar().showMessage("Cloning stopped")

    def _on_scraping_finished(self, html_desktop: str, html_desktop_logged_in: str, html_mobile: str, captured_resources: dict, detected_selectors: dict):
        """Called when main page scraping is complete — hands off to the async pipeline."""

        self.progress_panel.set_status("Processing assets...")

        # Hand off asset saving and LinkMapper startup to the worker thread
        self.request_process_data.emit(
            html_desktop, html_desktop_logged_in, html_mobile, captured_resources, detected_selectors
        )

    def _on_all_pages_finished(self, total_pages: int):
        """Called when all pages have been cloned. UI is updated and heavy work is sent to background."""
        self.progress_panel.set_file_count(total_pages)

        # F1: Add main page to tree
        if self._site_tree.topLevelItemCount() == 0 and self._current_output_dir:
            item = QTreeWidgetItem([getattr(self, "_current_url", ""), "index.html"])
            self._site_tree.addTopLevelItem(item)

        self.log_viewer.append("─" * 50, "info")
        self.log_viewer.append(
            f"✅ {total_pages} pages cloned — post-processing starting in background...", "success"
        )
        self.progress_panel.set_status("Finalizing...")
        self.statusBar().showMessage(f"Finalizing: {total_pages} pages → {self._current_output_dir}")

        # Send heavy CPU work to background thread (UI does not freeze)
        hide_user = getattr(self, "_current_hide_username", "")
        self._worker.start_post_processing(total_pages, self._current_output_dir, hide_user)

    def _on_post_processing_done(self, total_pages: int):
        """UI is updated when post-processing finishes in the background."""
        self.progress_panel.set_loading(False)
        self.url_bar.set_running(False)
        self.progress_panel.set_status("Completed ✅")
        self.open_folder_btn.setEnabled(True)
        self.preview_btn.setEnabled(True)
        self.zip_btn.setEnabled(True)
        self.analyze_btn.setEnabled(True)

        # F2: Load screenshot gallery
        self._load_screenshot_gallery()

        self.statusBar().showMessage(f"Cloning complete: {total_pages} pages → {self._current_output_dir}")
        self.log_viewer.append("─" * 50, "info")
        self.log_viewer.append(
            f"🎉 Done! {total_pages} pages → {self._current_output_dir}", "success"
        )
        self.log_viewer.append("📊 Click 'Analysis Report' for analysis.", "info")

    def _on_page_cloned(self, url: str, local_filename: str):
        """F1: Each cloned page is added to the site tree."""
        self.progress_panel.set_status(f"Cloned: {local_filename}")
        item = QTreeWidgetItem([url, local_filename])
        item.setToolTip(0, url)
        self._site_tree.addTopLevelItem(item)
        # Do not switch to the tree tab on first clone (keep log visible)

    def _clear_screenshot_gallery(self):
        """F2: Reset the gallery."""
        while self._screenshot_layout.count():
            child = self._screenshot_layout.takeAt(0)
            if child.widget():
                child.widget().deleteLater()

    def _load_screenshot_gallery(self):
        """F2: Display PNGs in the output folder as thumbnails."""
        if not self._current_output_dir:
            return
        self._clear_screenshot_gallery()
        png_files = list(self._current_output_dir.rglob("*.png"))
        if not png_files:
            lbl = QLabel("No screenshots yet.")
            self._screenshot_layout.addWidget(lbl)
            return
        for png in sorted(png_files)[:30]:  # First 30
            card = QWidget()
            card_layout = QVBoxLayout(card)
            card_layout.setContentsMargins(4, 4, 4, 4)
            card_layout.setSpacing(4)

            thumb = QLabel()
            pix = QPixmap(str(png))
            if not pix.isNull():
                thumb.setPixmap(pix.scaled(200, 140, Qt.AspectRatioMode.KeepAspectRatio,
                                           Qt.TransformationMode.SmoothTransformation))
            thumb.setAlignment(Qt.AlignmentFlag.AlignCenter)

            name_lbl = QLabel(png.name[:28])
            name_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
            name_lbl.setStyleSheet("font-size:10px;color:#888")

            card_layout.addWidget(thumb)
            card_layout.addWidget(name_lbl)
            card.setFixedWidth(212)
            self._screenshot_layout.addWidget(card)

    def _run_analysis(self):
        """Run C3 + C4 + C5 analysis and display results in the Analysis tab."""
        if not self._current_output_dir or not self._current_output_dir.exists():
            return

        from core.analyzer import TechStackDetector, SEOAnalyzer, BrokenLinkDetector

        self._analysis_viewer.clear()
        self._analysis_viewer.append("🔬 Analysis starting...", "info")
        self.tab_widget.setCurrentIndex(3)  # Switch to Analysis tab

        out = self._current_output_dir

        # C3 — Technology Detection
        try:
            tech = TechStackDetector()
            found = tech.generate_report(out)
            stack = tech.detect_from_directory(out)
            self._analysis_viewer.append(f"🔬 [C3] Technology Stack ({len(stack)} detected):", "highlight")
            for t in sorted(stack):
                self._analysis_viewer.append(f"   • {t}", "info")
            self._analysis_viewer.append(f"   → Report: tech_stack.html", "success")
        except Exception as e:
            self._analysis_viewer.append(f"⚠️ Technology detection error: {e}", "warning")

        self._analysis_viewer.append("─" * 45, "info")

        # C4 — SEO Analysis
        try:
            seo = SEOAnalyzer()
            seo_path = seo.generate_report(out)
            results = seo.analyze_directory(out)
            avg = sum(r["score"] for r in results) / len(results) if results else 0
            color = "success" if avg >= 80 else "warning" if avg >= 60 else "error"
            self._analysis_viewer.append(f"📈 [C4] SEO Score: {avg:.1f}/100 ({len(results)} pages)", color)
            issues_total = sum(len(r["issues"]) for r in results)
            self._analysis_viewer.append(f"   Total issues: {issues_total}", "info")
            self._analysis_viewer.append(f"   → Report: seo_report.html", "success")
        except Exception as e:
            self._analysis_viewer.append(f"⚠️ SEO analysis error: {e}", "warning")

        self._analysis_viewer.append("─" * 45, "info")

        # C5 — Broken Link Detection
        try:
            bl = BrokenLinkDetector()
            bl_path = bl.generate_report(out)
            result = bl.check(out)
            bc = result["broken_count"]
            clr = "success" if bc == 0 else "warning" if bc < 10 else "error"
            self._analysis_viewer.append(
                f"🔗 [C5] Broken Links: {bc} / {result['total_checked']} checked", clr
            )
            for item in result["broken_links"][:5]:
                self._analysis_viewer.append(f"   ❌ {item['ref']} ← {item['from']}", "error")
            if bc > 5:
                self._analysis_viewer.append(f"   ... and {bc - 5} more (broken_links.html)", "warning")
            self._analysis_viewer.append(f"   → Report: broken_links.html", "success")
        except Exception as e:
            self._analysis_viewer.append(f"⚠️ Broken link detection error: {e}", "warning")

        self._analysis_viewer.append("─" * 45, "info")
        self._analysis_viewer.append("✅ All reports saved to the output folder.", "success")

    def _on_scraping_failed(self, error_message: str):
        """Called when scraping encounters an error."""
        self.url_bar.set_running(False)
        self.progress_panel.set_status("Error ❌")
        self.statusBar().showMessage("Cloning failed")
        self.log_viewer.append(f"❌ Operation failed: {error_message}", "error")

        QMessageBox.critical(
            self,
            "Cloning Error",
            f"An error occurred while cloning the page:\n\n{error_message}",
        )

    # ──────────────────────────────────────────────
    #  HELPERS
    # ──────────────────────────────────────────────

    def _start_preview_server(self):
        """Start the local server and open it in the browser."""
        if self._current_output_dir and self._current_output_dir.exists():
            self._preview_server.start_server(self._current_output_dir)

    def _create_zip_package(self):
        """Package the cloned site as a ZIP and open a save dialog."""
        if not (hasattr(self, '_worker') and self._worker and self._worker._asset_manager):
            return
        am = self._worker._asset_manager
        zip_path = am.create_zip()
        if zip_path:
            dest, _ = QFileDialog.getSaveFileName(
                self, "Save ZIP File", str(zip_path), "ZIP Archive (*.zip)"
            )
            if dest:
                import shutil
                try:
                    shutil.copy2(str(zip_path), dest)
                    self.log_viewer.append(f"📦 ZIP saved: {dest}", "success")
                except Exception as e:
                    self.log_viewer.append(f"⚠️ ZIP save error: {e}", "error")

    def _open_output_folder(self):
        """Open the output folder in the file manager."""
        if self._current_output_dir and self._current_output_dir.exists():
            path = str(self._current_output_dir)
            if sys.platform == "win32":
                os.startfile(path)
            elif sys.platform == "darwin":
                subprocess.Popen(["open", path])
            else:
                subprocess.Popen(["xdg-open", path])

    def closeEvent(self, event):
        """Clean up background operations when the application closes."""
        if hasattr(self, '_preview_server'):
            self._preview_server.stop_server()
        if hasattr(self, '_worker') and self._worker:
            self._worker.stop()
        if hasattr(self, '_worker_thread') and self._worker_thread:
            self._worker_thread.quit()
            self._worker_thread.wait(5000)
        event.accept()
