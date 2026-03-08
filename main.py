"""
Web Cloner — Desktop Website Cloning Application
=================================================

Entry point. PyQt6 + asyncio event loop integration.
"""

import sys
import asyncio

from PyQt6.QtWidgets import QApplication

# qasync — combines the PyQt6 event loop with asyncio
try:
    from qasync import QEventLoop
except ImportError:
    print("ERROR: 'qasync' package not found.")
    print("Please run: pip install qasync")
    sys.exit(1)

from ui.main_window import MainWindow


def main():
    # ── Qt Application ──
    app = QApplication(sys.argv)
    app.setApplicationName("Web Cloner")
    app.setOrganizationName("WebCloner")

    # ── Asyncio Event Loop (qasync) ──
    loop = QEventLoop(app)
    asyncio.set_event_loop(loop)

    # ── Main Window ──
    window = MainWindow()
    window.show()

    # ── Run Event Loop ──
    with loop:
        loop.run_forever()


if __name__ == "__main__":
    main()
