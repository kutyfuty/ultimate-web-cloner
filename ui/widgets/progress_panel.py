"""
progress_panel.py — Progress and Status Widget

Progress bar + status label.
"""

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QProgressBar,
    QVBoxLayout,
    QWidget,
)


class ProgressPanel(QWidget):
    """Progress bar and status information panel."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._setup_ui()

    def _setup_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)

        # ── Top info row ──
        info_row = QHBoxLayout()
        info_row.setSpacing(12)

        self.spinner_label = QLabel("")
        self.spinner_label.setObjectName("spinnerLabel")
        self.spinner_label.setStyleSheet("color: #58a6ff; font-family: 'Consolas', monospace; font-size: 16px; font-weight: bold;")
        info_row.addWidget(self.spinner_label)

        section_label = QLabel("📊 LIVE TRACKING")
        section_label.setObjectName("sectionLabel")
        info_row.addWidget(section_label)

        info_row.addStretch()

        self.status_label = QLabel("Ready")
        self.status_label.setObjectName("statusLabel")
        info_row.addWidget(self.status_label)

        layout.addLayout(info_row)

        # ── Progress bar ──
        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        self.progress_bar.setFormat("%p% [%v/%m]")
        self.progress_bar.setTextVisible(True)
        self.progress_bar.setFixedHeight(12)
        layout.addWidget(self.progress_bar)

        # ── Stats row ──
        stats_row = QHBoxLayout()
        stats_row.setSpacing(20)

        self.files_label = QLabel("📁 0 Files")
        self.files_label.setStyleSheet("color: #8b949e; font-size: 11px;")
        stats_row.addWidget(self.files_label)

        self.size_label = QLabel("💾 0 KB")
        self.size_label.setStyleSheet("color: #8b949e; font-size: 11px;")
        stats_row.addWidget(self.size_label)

        self.page_label = QLabel("📄 Page: 0/0")
        self.page_label.setStyleSheet("color: #8b949e; font-size: 11px;")
        stats_row.addWidget(self.page_label)

        stats_row.addStretch()
        layout.addLayout(stats_row)

        # ── Animation settings ──
        from PyQt6.QtCore import QTimer
        self._spinner_timer = QTimer(self)
        self._spinner_timer.timeout.connect(self._rotate_spinner)
        self._spinner_chars = ["|", "/", "-", "\\"]
        self._spinner_idx = 0

    def _rotate_spinner(self):
        """Node.js-style spinning character animation."""
        self.spinner_label.setText(self._spinner_chars[self._spinner_idx])
        self._spinner_idx = (self._spinner_idx + 1) % len(self._spinner_chars)

    def set_progress(self, value: int):
        """Update the progress bar (0-100)."""
        self.progress_bar.setValue(min(max(value, 0), 100))

    def set_status(self, text: str):
        """Update the status label."""
        self.status_label.setText(text)

    def set_file_count(self, count: int):
        """Display the number of downloaded files."""
        self.files_label.setText(f"📁 {count} Files")

    def set_total_size(self, size_bytes: int):
        """Display the total size."""
        if size_bytes < 1024:
            text = f"💾 {size_bytes} B"
        elif size_bytes < 1024 * 1024:
            text = f"💾 {size_bytes / 1024:.1f} KB"
        else:
            text = f"💾 {size_bytes / (1024*1024):.1f} MB"
        self.size_label.setText(text)

    def set_page_info(self, current: int, total: int):
        """Update the page information."""
        self.page_label.setText(f"📄 Page: {current}/{total}")
        if total > 0:
            self.set_progress(int((current / total) * 100))

    def set_loading(self, active: bool):
        """Start/stop the loading animation."""
        if active:
            self._spinner_timer.start(100) # spin at 100ms intervals
        else:
            self._spinner_timer.stop()
            self.spinner_label.setText("")

    def reset(self):
        """Reset the panel."""
        self.progress_bar.setValue(0)
        self.status_label.setText("Ready")
        self.files_label.setText("📁 0 Files")
        self.size_label.setText("💾 0 KB")
        self.page_label.setText("📄 Page: 0/0")
        self.set_loading(False)
