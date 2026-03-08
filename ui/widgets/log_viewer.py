"""
log_viewer.py — Live Log Viewer Widget

A scrollable panel displaying coloured log messages.
"""

from datetime import datetime

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QTextCursor
from PyQt6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QPushButton,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)


class LogViewer(QWidget):
    """Live log viewer."""

    # ── Colors by log level ──
    LEVEL_COLORS = {
        "info": "#8b949e",
        "success": "#3fb950",
        "warning": "#d29922",
        "error": "#f85149",
        "highlight": "#58a6ff",
    }

    def __init__(self, parent=None):
        super().__init__(parent)
        self._auto_scroll = True
        self._setup_ui()

    def _setup_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)

        # ── Header row ──
        header_row = QHBoxLayout()
        header_row.setSpacing(10)

        section_label = QLabel("📋 Live Log")
        section_label.setObjectName("sectionLabel")
        header_row.addWidget(section_label)

        header_row.addStretch()

        self.clear_btn = QPushButton("🗑 Clear")
        self.clear_btn.setObjectName("browseButton")
        self.clear_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.clear_btn.clicked.connect(self.clear)
        self.clear_btn.setFixedHeight(30)
        header_row.addWidget(self.clear_btn)

        layout.addLayout(header_row)

        # ── Log area ──
        self.text_edit = QTextEdit()
        self.text_edit.setObjectName("logViewer")
        self.text_edit.setReadOnly(True)
        self.text_edit.setMinimumHeight(200)
        layout.addWidget(self.text_edit)

    def append(self, message: str, level: str = "info"):
        """Add a new log message."""
        timestamp = datetime.now().strftime("%H:%M:%S")
        color = self.LEVEL_COLORS.get(level, self.LEVEL_COLORS["info"])

        # Emoji check — if the message starts with an emoji, auto-detect the level
        if not level or level == "info":
            if message.startswith("✅") or message.startswith("🎉"):
                color = self.LEVEL_COLORS["success"]
            elif message.startswith("⚠️"):
                color = self.LEVEL_COLORS["warning"]
            elif message.startswith("❌"):
                color = self.LEVEL_COLORS["error"]
            elif message.startswith(("🚀", "🌐", "📡", "⚡", "📜", "🔄", "📦", "💾", "📸", "🔗", "🎨")):
                color = self.LEVEL_COLORS["highlight"]

        html_line = (
            f'<span style="color: #484f58;">[{timestamp}]</span> '
            f'<span style="color: {color};">{message}</span>'
        )

        self.text_edit.append(html_line)

        # Auto-scroll
        if self._auto_scroll:
            cursor = self.text_edit.textCursor()
            cursor.movePosition(QTextCursor.MoveOperation.End)
            self.text_edit.setTextCursor(cursor)

    def clear(self):
        """Clear the log."""
        self.text_edit.clear()
