"""
url_input_bar.py — URL Input Widget

Target URL input field + Clone / Stop buttons.
"""

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import (
    QCheckBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)


class UrlInputBar(QWidget):
    """URL input bar and advanced settings widget."""

    # signal arguments: (url, max_pages, deep_crawl, max_depth, use_auth, dual_pass)
    clone_requested = pyqtSignal(str, int, bool, int, bool, bool)
    stop_requested = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._is_running = False
        self._setup_ui()

    def _setup_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(12)

        # ── Title ──
        title = QLabel("🌐 Web Cloner")
        title.setObjectName("titleLabel")
        title.setAlignment(Qt.AlignmentFlag.AlignLeft)
        layout.addWidget(title)

        subtitle = QLabel("Clone the target website — all assets are downloaded locally")
        subtitle.setObjectName("subtitleLabel")
        layout.addWidget(subtitle)

        # ── URL row ──
        input_row = QHBoxLayout()
        input_row.setSpacing(10)

        self.url_input = QLineEdit()
        self.url_input.setObjectName("urlInput")
        self.url_input.setPlaceholderText("https://example-site.com")
        self.url_input.setClearButtonEnabled(True)
        self.url_input.returnPressed.connect(self._on_clone_clicked)
        input_row.addWidget(self.url_input, stretch=1)

        self.clone_btn = QPushButton("⚡ Clone")
        self.clone_btn.setObjectName("cloneButton")
        self.clone_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.clone_btn.clicked.connect(self._on_clone_clicked)
        input_row.addWidget(self.clone_btn)

        self.stop_btn = QPushButton("■ Stop")
        self.stop_btn.setObjectName("stopButton")
        self.stop_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.stop_btn.setVisible(False)
        self.stop_btn.clicked.connect(self._on_stop_clicked)
        input_row.addWidget(self.stop_btn)

        layout.addLayout(input_row)

        # ── Advanced Settings ──
        settings_row = QHBoxLayout()
        settings_row.setSpacing(15)

        self.deep_crawl_cb = QCheckBox("Map Mode (Deep Crawl)")
        self.deep_crawl_cb.setToolTip("Crawls and maps the entire site before starting the download.")
        settings_row.addWidget(self.deep_crawl_cb)

        self.max_depth_spinner = QSpinBox()
        self.max_depth_spinner.setRange(1, 10)
        self.max_depth_spinner.setValue(3)
        self.max_depth_spinner.setPrefix("Depth: ")
        settings_row.addWidget(self.max_depth_spinner)

        self.max_pages_spinner = QSpinBox()
        self.max_pages_spinner.setRange(0, 50000)
        self.max_pages_spinner.setValue(100)
        self.max_pages_spinner.setPrefix("Max Pages: ")
        self.max_pages_spinner.setToolTip("0 = Unlimited")
        settings_row.addWidget(self.max_pages_spinner)

        self.auth_clone_cb = QCheckBox("🔐 Start with Session")
        self.auth_clone_cb.setToolTip("When checked, a visible browser opens first so you can log in, then close it. All members-only pages will be cloned.")
        self.auth_clone_cb.setStyleSheet("color: #FACC15; font-weight: bold;")
        settings_row.addWidget(self.auth_clone_cb)

        self.dual_pass_cb = QCheckBox("Dual Pass (PC + Mobile)")
        self.dual_pass_cb.setToolTip("Crawls the site twice — once in Desktop view and once in iPhone view.")
        self.dual_pass_cb.setChecked(False)
        settings_row.addWidget(self.dual_pass_cb)

        settings_row.addStretch()
        layout.addLayout(settings_row)

    def _on_clone_clicked(self):
        url = self.url_input.text().strip()
        if not url:
            return
        if not url.startswith(("http://", "https://")):
            url = "https://" + url
            self.url_input.setText(url)

        max_pages = self.max_pages_spinner.value()
        deep_crawl = self.deep_crawl_cb.isChecked()
        max_depth = self.max_depth_spinner.value()
        use_auth = self.auth_clone_cb.isChecked()
        dual_pass = self.dual_pass_cb.isChecked()

        self.clone_requested.emit(url, max_pages, deep_crawl, max_depth, use_auth, dual_pass)

    def _on_stop_clicked(self):
        self.stop_requested.emit()

    def set_running(self, running: bool):
        """Update buttons according to the running state."""
        self._is_running = running
        self.clone_btn.setVisible(not running)
        self.clone_btn.setEnabled(not running)
        self.stop_btn.setVisible(running)
        self.url_input.setReadOnly(running)
        self.deep_crawl_cb.setEnabled(not running)
        self.max_depth_spinner.setEnabled(not running)
        self.max_pages_spinner.setEnabled(not running)
        self.auth_clone_cb.setEnabled(not running)
        self.dual_pass_cb.setEnabled(not running)

    @property
    def current_url(self) -> str:
        return self.url_input.text().strip()
