"""
preview_server.py — Built-in Preview Server (Phase 8)

Starts a lightweight HTTP server so the cloned site can be opened
on the local machine without CORS errors, and automatically directs
the default browser to that address.
"""

import http.server
import socketserver
import threading
import webbrowser
import mimetypes
import os
from pathlib import Path

from PyQt6.QtCore import QObject, pyqtSignal

class PreviewServer(QObject):
    log_message = pyqtSignal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.server = None
        self.server_thread = None
        self.port = 8080
        self.running = False

    def start_server(self, directory: str | Path):
        """Start the HTTP server for the specified directory."""
        directory = Path(directory)

        if self.running:
            self.stop_server()

        # Find a free port
        self.port = self._find_free_port()

        class Handler(http.server.SimpleHTTPRequestHandler):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, directory=str(directory), **kwargs)
                # Add modern MIME types
                self.extensions_map.update({
                    '.webp': 'image/webp',
                    '.avif': 'image/avif',
                    '.woff2': 'font/woff2',
                    '.woff': 'font/woff',
                    '.ttf': 'font/ttf',
                    '.svg': 'image/svg+xml',
                    '.ico': 'image/x-icon',
                    '.json': 'application/json',
                    '.mp4': 'video/mp4',
                    '.m3u8': 'application/vnd.apple.mpegurl',
                    '.ts': 'video/mp2t',
                })

            def end_headers(self):
                """Add CORS headers."""
                self.send_header('Access-Control-Allow-Origin', '*')
                self.send_header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
                self.send_header('Access-Control-Allow-Headers', 'X-Requested-With, Content-Type')
                # Disconnect external sources (prevents browser freezing)
                self.send_header('Content-Security-Policy',
                    "default-src 'self' 'unsafe-inline' 'unsafe-eval' data: blob:; "
                    "connect-src 'self'; "
                    "worker-src 'self' blob:;")
                super().end_headers()

            def log_message(self, format, *args):
                pass

            def do_GET(self):
                """Redirect to index.html (or index_auth.html) if file not found (SPA Fallback)."""
                # Check if file exists after translate_path
                path = self.translate_path(self.path)
                if not os.path.exists(path) and not self.path.split('?')[0].endswith(('.js', '.css', '.png', '.jpg', '.jpeg', '.gif', '.svg', '.woff', '.woff2')):
                    # If it's not an asset file and not found, return to the main page (SPA routing)
                    self.path = "/"

                return super().do_GET()

            def translate_path(self, path):
                """Resolve the request path. Priority: index_auth.html > index.html."""
                local_path = super().translate_path(path)
                p = Path(local_path)

                # If a directory is requested (e.g. "/")
                if p.is_dir():
                    ua = self.headers.get("User-Agent", "").lower()
                    is_mobile = any(x in ua for x in ["mobile", "android", "iphone", "ipad", "ipod"])

                    if is_mobile:
                        mobile_target = p / "index_mobile.html"
                        if mobile_target.exists():
                            return str(mobile_target)

                    # Show logged-in version if available (priority)
                    auth_target = p / "index_auth.html"
                    if auth_target.exists():
                        return str(auth_target)

                    # Normal version
                    index_target = p / "index.html"
                    if index_target.exists():
                        return str(index_target)

                # If a specific .html is requested but a mobile version exists
                elif p.suffix == ".html":
                    ua = self.headers.get("User-Agent", "").lower()
                    is_mobile = any(x in ua for x in ["mobile", "android", "iphone", "ipad", "ipod"])

                    if is_mobile:
                        mobile_target = p.with_name(f"{p.stem}_mobile.html")
                        if mobile_target.exists():
                            return str(mobile_target)

                return local_path

        class SilentTCPServer(socketserver.TCPServer):
            def handle_error(self, request, client_address):
                """Silently swallow connection abort errors."""
                import sys
                exc_type = sys.exc_info()[0]
                if exc_type in (ConnectionAbortedError, ConnectionResetError, BrokenPipeError):
                    return
                super().handle_error(request, client_address)

        try:
            self.server = SilentTCPServer(("", self.port), Handler)
            self.server_thread = threading.Thread(target=self.server.serve_forever, daemon=True)
            self.server_thread.start()
            self.running = True

            url = f"http://localhost:{self.port}"
            self.log_message.emit(f"🌐 Server started: {url}")

            # Open browser automatically
            webbrowser.open_new_tab(url)

        except Exception as e:
            self.log_message.emit(f"⚠️ Server could not be started: {e}")

    def stop_server(self):
        """Stop the running server."""
        if self.server:
            self.server.shutdown()
            self.server.server_close()
            self.server = None
            self.running = False
            self.log_message.emit("🛑 Server stopped.")

    def _find_free_port(self, start_port=8080, max_port=8099):
        """Find an available port."""
        import socket
        for port in range(start_port, max_port):
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
                if sock.connect_ex(('localhost', port)) != 0:
                    return port
        return start_port
