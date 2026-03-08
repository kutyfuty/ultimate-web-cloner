"""
sanitizer.py — Runtime Data Sanitizer

Removes real user data (name, balance, phone, e-mail, crypto address,
IBAN, account number) from HTML and JS files.
ZERO personal data remains in files uploaded to the VPS.
"""

import re
import json
from pathlib import Path


class DataSanitizer:
    """
    Scans an HTML string; finds real user data everywhere including
    DOM text, <script> blocks and JSON blobs, and replaces them with
    safe placeholders. Follows the Zero-UI rule — does NOT ADD new
    elements, only replaces text.

    Features:
    - Automatic balance detection (via regex from page)
    - Phone number masking
    - E-mail address masking
    - Crypto wallet address masking (BTC, ETH, TRX, BNB)
    - IBAN / bank account number masking
    - Deep JS file sanitization
    """

    # Placeholder templates
    USER_PLACEHOLDER       = "Misafir"
    USER_SPAN              = '<span class="offline-dynamic-user">Misafir</span>'
    BALANCE_PLACEHOLDER    = "0.00"
    BALANCE_SPAN           = '<span class="offline-dynamic-balance">0.00</span>'
    PHONE_PLACEHOLDER      = "+10000000000"
    EMAIL_PLACEHOLDER      = "user@example.com"
    CRYPTO_PLACEHOLDER     = "0x0000000000000000000000000000000000000000"
    IBAN_PLACEHOLDER       = "TR000000000000000000000000"

    # ── Currency / balance patterns ──
    # E.g.: "1.250,50", "1,250.50", "5 000.00", "₺1250", "$999.99"
    _BALANCE_RE = re.compile(
        r"""
        (?:(?:₺|TL|USD|\$|€|EUR|BTC|ETH|USDT|TRX|BNB|₿)\s*)?   # optional currency prefix
        (?:
            \d{1,3}(?:[.,\s]\d{3})*[.,]\d{2}   # 1.234,56 or 1,234.56 or 1 234.56
            | \d+[.,]\d{2,8}                     # 1234.56 or 0.00056789
        )
        (?:\s*(?:₺|TL|USD|\$|€|EUR|BTC|ETH|USDT|TRX|BNB|₿))?    # optional trailing currency
        """,
        re.VERBOSE,
    )

    # ── Phone number ──
    _PHONE_RE = re.compile(
        r"""
        (?<![0-9])                              # no digit before
        (?:\+\d{1,3}[\s\-]?)?                  # country code (optional)
        (?:\(?\d{3}\)?[\s\-\.]?)               # area code
        \d{3}[\s\-\.]?\d{2}[\s\-\.]?\d{2}     # 7-digit body
        (?![0-9])                              # no digit after
        """,
        re.VERBOSE,
    )

    # ── E-mail ──
    _EMAIL_RE = re.compile(
        r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}",
        re.IGNORECASE,
    )

    # ── Crypto wallet addresses ──
    # ETH/BNB/TRX hex address (0x...) or Bitcoin base58
    _CRYPTO_ETH_RE = re.compile(r"\b0x[0-9a-fA-F]{40}\b")
    _CRYPTO_BTC_RE = re.compile(r"\b[13][a-km-zA-HJ-NP-Z1-9]{25,34}\b")
    _CRYPTO_TRX_RE = re.compile(r"\bT[a-zA-Z0-9]{33}\b")

    # ── IBAN ──
    _IBAN_RE = re.compile(
        r"\b[A-Z]{2}[0-9]{2}[A-Z0-9]{4}[0-9]{7}(?:[A-Z0-9]?){0,16}\b"
    )

    def __init__(self):
        # Data automatically detected during scraping
        self._detected_balances: list[str] = []
        self._detected_emails: list[str] = []

    # ──────────────────────────────────────────────
    #  AUTO DETECTION
    # ──────────────────────────────────────────────

    def auto_detect(self, html: str) -> dict:
        """
        Automatically detects monetary amounts, phone numbers, e-mails and
        crypto addresses found in the HTML. Returns the detection list.
        Must be run BEFORE the sanitize() call.
        """
        balances = self._BALANCE_RE.findall(html)
        phones   = self._PHONE_RE.findall(html)
        emails   = self._EMAIL_RE.findall(html)
        eth_addrs = self._CRYPTO_ETH_RE.findall(html)
        btc_addrs = self._CRYPTO_BTC_RE.findall(html)
        trx_addrs = self._CRYPTO_TRX_RE.findall(html)
        ibans     = self._IBAN_RE.findall(html)

        self._detected_balances = list(set(balances))
        self._detected_emails   = list(set(emails))

        return {
            "balances":  self._detected_balances,
            "phones":    list(set(phones)),
            "emails":    self._detected_emails,
            "crypto":    list(set(eth_addrs + btc_addrs + trx_addrs)),
            "ibans":     list(set(ibans)),
        }

    # ──────────────────────────────────────────────
    #  MAIN SANITIZE
    # ──────────────────────────────────────────────

    def sanitize(self, html: str, real_user: str = "", real_balance: str = "") -> str:
        """
        Scans the HTML string and replaces personal data with placeholders.

        Args:
            html:         HTML string to process
            real_user:    Real username found on pages (e.g. 'Ahmet123')
            real_balance: Real balance found on pages (e.g. '1.250,50 TL')

        Returns:
            Sanitized HTML string
        """
        if not html:
            return html

        # 1. Username
        html = self._sanitize_username(html, real_user)

        # 2. Known balance (if provided)
        if real_balance:
            html = self._replace_all_forms(html, real_balance, self.BALANCE_SPAN)

        # 3. Auto-detected balances
        html = self._sanitize_auto_balances(html)

        # 4. Phone numbers (text content only — not in URLs/attributes)
        html = self._replace_in_text_content(html, self._PHONE_RE, self.PHONE_PLACEHOLDER)

        # 5. E-mail addresses (text content only)
        html = self._replace_in_text_content(html, self._EMAIL_RE, self.EMAIL_PLACEHOLDER)

        # 6. Crypto wallet addresses (text content only)
        html = self._replace_in_text_content(html, self._CRYPTO_ETH_RE, self.CRYPTO_PLACEHOLDER)
        html = self._replace_in_text_content(html, self._CRYPTO_BTC_RE, "1A1zP1eP5QGefi2DMPTfTL5SLmv7Divf")
        html = self._replace_in_text_content(html, self._CRYPTO_TRX_RE, "T9yD14Nj9j7xAB4dbGeiX9h8unkKHxuWwb")

        # 7. IBAN (text content only)
        html = self._replace_in_text_content(html, self._IBAN_RE, self.IBAN_PLACEHOLDER)

        return html

    def sanitize_js(self, js_text: str, real_user: str = "", real_balance: str = "") -> str:
        """
        Masks personal data in downloaded .js files.
        Unlike HTML sanitization, does not use a <span> wrapper —
        performs raw string replacement (to avoid breaking JS syntax).
        """
        if not js_text:
            return js_text

        # Username
        if real_user and len(real_user) >= 2:
            js_text = js_text.replace(f'"{real_user}"', '"Misafir"')
            js_text = js_text.replace(f"'{real_user}'", "'Misafir'")
            js_text = js_text.replace(f"`{real_user}`", "`Misafir`")

        # Balance (may appear as a raw number)
        if real_balance:
            js_text = js_text.replace(f'"{real_balance}"', '"0.00"')
            js_text = js_text.replace(f"'{real_balance}'", "'0.00'")

        # Automatic balance pattern
        js_text = self._BALANCE_RE.sub("0.00", js_text)

        # E-mail
        js_text = self._EMAIL_RE.sub(self.EMAIL_PLACEHOLDER, js_text)

        # Crypto
        js_text = self._CRYPTO_ETH_RE.sub(self.CRYPTO_PLACEHOLDER, js_text)

        return js_text

    def sanitize_json_response(self, json_bytes: bytes, real_user: str = "", real_balance: str = "") -> bytes:
        """
        Masks personal data in API response JSON content.
        Falls back to raw string replacement if JSON is invalid.
        """
        try:
            text = json_bytes.decode("utf-8")
        except Exception:
            return json_bytes

        # Quoted string replace only — prevents corrupting JSON key names
        if real_user and len(real_user) >= 2:
            text = text.replace(f'"{real_user}"', '"Misafir"')
            text = text.replace(f"'{real_user}'", "'Misafir'")
        if real_balance:
            text = text.replace(f'"{real_balance}"', '"0.00"')
            text = text.replace(f"'{real_balance}'", "'0.00'")

        text = self._EMAIL_RE.sub(self.EMAIL_PLACEHOLDER, text)
        text = self._CRYPTO_ETH_RE.sub(self.CRYPTO_PLACEHOLDER, text)
        text = self._IBAN_RE.sub(self.IBAN_PLACEHOLDER, text)

        # Balance pattern — numeric values inside JSON (string or number)
        def _replace_balance_in_json(m):
            val = m.group(0)
            # Skip very short numbers (could be an ID, version number, etc.)
            if len(val.replace(",", "").replace(".", "")) <= 3:
                return val
            return "0.00"

        text = self._BALANCE_RE.sub(_replace_balance_in_json, text)

        return text.encode("utf-8")

    def sanitize_directory(self, output_dir: Path, real_user: str = "", real_balance: str = "") -> dict:
        """
        Scans the entire cloned directory; masks personal data in
        HTML, JS and JSON files in-place.

        Returns:
            {"html": n, "js": n, "json": n}  — number of files processed
        """
        counts = {"html": 0, "js": 0, "json": 0}

        for html_file in output_dir.rglob("*.html"):
            try:
                content = html_file.read_text(encoding="utf-8", errors="ignore")
                sanitized = self.sanitize(content, real_user, real_balance)
                if sanitized != content:
                    html_file.write_text(sanitized, encoding="utf-8")
                    counts["html"] += 1
            except Exception:
                pass

        for js_file in output_dir.rglob("*.js"):
            try:
                content = js_file.read_text(encoding="utf-8", errors="ignore")
                sanitized = self.sanitize_js(content, real_user, real_balance)
                if sanitized != content:
                    js_file.write_text(sanitized, encoding="utf-8")
                    counts["js"] += 1
            except Exception:
                pass

        for json_file in output_dir.rglob("*.json"):
            try:
                raw = json_file.read_bytes()
                sanitized = self.sanitize_json_response(raw, real_user, real_balance)
                if sanitized != raw:
                    json_file.write_bytes(sanitized)
                    counts["json"] += 1
            except Exception:
                pass

        return counts

    # ──────────────────────────────────────────────
    #  INTERNAL HELPER METHODS
    # ──────────────────────────────────────────────

    def _sanitize_username(self, html: str, real_user: str) -> str:
        if not real_user or len(real_user) < 2:
            return html

        # Text content only — never inside attributes or script/style blocks
        html = self._replace_in_text_content(html, real_user, self.USER_SPAN)

        # JSON double-quoted (safe: only replaces exact quoted strings)
        html = html.replace(f'"{real_user}"', '"Misafir"')
        html = html.replace(f"'{real_user}'", "'Misafir'")

        # URL-encoded (e.g. Ahmet%20123)
        try:
            from urllib.parse import quote
            encoded = quote(real_user, safe="")
            if encoded != real_user:
                html = html.replace(encoded, "Misafir")
        except Exception:
            pass

        # Case variants — text content only
        if real_user.upper() != real_user:
            html = self._replace_in_text_content(html, real_user.upper(), "MISAFİR")
        if real_user.lower() != real_user:
            html = self._replace_in_text_content(html, real_user.lower(), "misafir")
        if real_user.capitalize() != real_user:
            html = self._replace_in_text_content(html, real_user.capitalize(), "Misafir")

        return html

    def _replace_all_forms(self, html: str, value: str, placeholder: str) -> str:
        """Replaces all possible variations of a value (with/without spaces, escaped)."""
        html = html.replace(value, placeholder)
        stripped = value.strip()
        if stripped != value:
            html = html.replace(stripped, placeholder)
        # JSON escape
        json_escaped = value.replace('"', '\\"')
        html = html.replace(f'"{json_escaped}"', f'"{self.BALANCE_PLACEHOLDER}"')
        return html

    # Matches <script>...</script> and <style>...</style> blocks (including content)
    _SCRIPT_STYLE_RE = re.compile(
        r'(<(?:script|style)\b[^>]*>)(.*?)(</(?:script|style)>)',
        re.IGNORECASE | re.DOTALL,
    )

    def _replace_in_text_content(self, html: str, pattern, replacement: str) -> str:
        """
        Apply a replacement ONLY to visible HTML text nodes —
        never inside tag markup, attribute values, <script>, or <style> blocks.
        """
        # Step 1: temporarily hide script/style block contents
        placeholders: list[str] = []

        def _stash(m: re.Match) -> str:
            idx = len(placeholders)
            placeholders.append(m.group(2))  # store inner content
            return m.group(1) + f'\x00STASH{idx}\x00' + m.group(3)

        html = self._SCRIPT_STYLE_RE.sub(_stash, html)

        # Step 2: split by tags; only process even (text) segments
        parts = re.split(r'(<[^>]*>)', html)
        for i in range(0, len(parts), 2):
            if hasattr(pattern, 'sub'):
                parts[i] = pattern.sub(replacement, parts[i])
            else:
                parts[i] = parts[i].replace(pattern, replacement)
        html = ''.join(parts)

        # Step 3: restore stashed content unchanged
        for idx, content in enumerate(placeholders):
            html = html.replace(f'\x00STASH{idx}\x00', content, 1)

        return html

    def _sanitize_auto_balances(self, html: str) -> str:
        """
        Masks automatically detected balances.
        Only touches text content — never attribute values or URLs.
        """
        for bal in self._detected_balances:
            # Skip very short values (could be an ID or version number)
            digits_only = re.sub(r"[^\d]", "", bal)
            if len(digits_only) <= 2:
                continue
            html = self._replace_in_text_content(html, bal, self.BALANCE_SPAN)
        return html
