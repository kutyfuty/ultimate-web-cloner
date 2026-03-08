"""
api_mocker.py — Dynamic API Recorder and Replayer (Phase 9)

Captures all fetch/XHR (JSON) requests made by the site in the background
during scraping and saves them. By embedding a `mock_api.js` script into
pages, it enables answering these requests from local JSON files without
an internet connection (API Replay).
"""

import hashlib
import json
import re
from pathlib import Path
from urllib.parse import urlparse

from PyQt6.QtCore import QObject, pyqtSignal

class ApiMocker(QObject):
    log_message = pyqtSignal(str)

    def __init__(self, output_dir: str | Path):
        super().__init__()
        self.output_dir = Path(output_dir)
        self.api_dir = self.output_dir / "api"
        self.api_dir.mkdir(parents=True, exist_ok=True)

        # Original URL -> Local file path map
        self._url_map: dict[str, str] = {}

    def save_api_response(self, url: str, body: bytes) -> None:
        """Save a captured JSON response to disk."""
        # Hash the cleaned URL
        url_hash = hashlib.md5(url.encode('utf-8')).hexdigest()[:12]

        # Build a simple filename
        parsed = urlparse(url)
        path_name = parsed.path.split('/')[-1]
        if not path_name or len(path_name) > 20:
            path_name = "endpoint"

        filename = f"{path_name}_{url_hash}.json"
        local_path = self.api_dir / filename

        try:
            local_path.write_bytes(body)
            # Save path as relative (accessible from HTML as: api/...)
            self._url_map[url] = f"api/{filename}"
        except Exception as e:
            self.log_message.emit(f"⚠️ API save error ({filename}): {e}")

    def generate_mock_script(self) -> str:
        """Returns the fake API interceptor script to be embedded into HTML pages."""
        if not self._url_map:
            return ""

        # Embed the map as a JSON string
        map_json = json.dumps(self._url_map)

        return f"""
<!-- CLONER MOCK API ENGINE -->
<script id="cloner-mock-api">
(function() {{
    const apiMap = {map_json};

    // 1. window.fetch Override
    const originalFetch = window.fetch;
    window.fetch = async function() {{
        const urlArgs = arguments[0];
        const urlString = typeof urlArgs === 'string' ? urlArgs : (urlArgs instanceof Request ? urlArgs.url : '');

        // If request URL is in our captured map
        for (const [originalUrl, localPath] of Object.entries(apiMap)) {{
            if (urlString.includes(originalUrl) || originalUrl.includes(urlString)) {{
                console.log('[Cloner Mock API] Fetch Intercepted:', urlString, '->', localPath);
                // Fetch static JSON from local file
                return originalFetch(localPath).then(res => {{
                    // Mimic the JSON response expected by the original fetch
                    return new Response(res.body, {{
                        status: 200,
                        statusText: 'OK',
                        headers: new Headers({{
                            'Content-Type': 'application/json'
                        }})
                    }});
                }});
            }}
        }}
        return originalFetch.apply(this, arguments);
    }};

    // 2. XMLHttpRequest Override
    const originalXhrOpen = XMLHttpRequest.prototype.open;
    XMLHttpRequest.prototype.open = function(method, url) {{
        this._mockMatchedUrl = null;
        for (const [originalUrl, localPath] of Object.entries(apiMap)) {{
            if (url.includes(originalUrl) || originalUrl.includes(url)) {{
                console.log('[Cloner Mock API] XHR Intercepted:', url, '->', localPath);
                this._mockMatchedUrl = localPath; // Save the replaced path
                url = localPath; // Override URL to fetch from file
                method = 'GET'; // Local file is always fetched with GET (even if originally POST)
                break;
            }}
        }}
        return originalXhrOpen.apply(this, [method, url, arguments[2], arguments[3], arguments[4]]);
    }};
}})();
</script>
"""

    def inject_mock_script(self, html_files: list[Path]) -> None:
        """Inject the Mock script into all cloned pages."""
        script = self.generate_mock_script()
        if not script:
            return

        for html_file in html_files:
            try:
                content = html_file.read_text(encoding='utf-8', errors='replace')
                if "CLONER MOCK API ENGINE" not in content:
                    # Place right after <head> tag so the interceptor activates before the page loads
                    if "<head>" in content.lower():
                        content = re.sub(r'(<head[^>]*>)', r'\1\n' + script, content, flags=re.IGNORECASE)
                    else:
                        content = script + "\n" + content

                    html_file.write_text(content, encoding='utf-8')
            except Exception as e:
                self.log_message.emit(f"⚠️ Mock script injection error ({html_file.name}): {e}")

    @property
    def captured_count(self) -> int:
        return len(self._url_map)
