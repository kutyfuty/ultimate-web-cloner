"""
config_manager.py — Universal Configuration Manager

Reads, validates and populates target_config.json with default values.
Provides a single config object to all modules (Scraper, Mocker, Injector).
"""

import json
from pathlib import Path
from dataclasses import dataclass, field


@dataclass
class LoginCredentials:
    username: str = ""
    password: str = ""


@dataclass
class Selectors:
    login_form: str = ""
    username_input: str = ""
    password_input: str = "input[type=\"password\"]"
    submit_button: str = ""
    success_indicator: str = ""
    logged_in_name_display: str = ""
    balance_display: str = ""
    login_button: str = ""
    register_button: str = ""
    preserve_nav: str = ""


@dataclass
class CrawlSettings:
    max_pages: int = 100
    max_depth: int = 3
    deep_crawl: bool = True
    use_auth: bool = False
    dual_pass: bool = False


@dataclass
class TargetConfig:
    """Central configuration used by all modules."""
    start_url: str = ""
    login_credentials: LoginCredentials = field(default_factory=LoginCredentials)
    selectors: Selectors = field(default_factory=Selectors)
    crawl_settings: CrawlSettings = field(default_factory=CrawlSettings)

    @property
    def has_credentials(self) -> bool:
        """Are login credentials filled in?"""
        return bool(self.login_credentials.username and self.login_credentials.password)

    @property
    def has_login_selectors(self) -> bool:
        """Is at least one login selector defined?"""
        return bool(self.selectors.login_form or self.selectors.submit_button)


class ConfigManager:
    """Reads target_config.json and converts it to a TargetConfig object."""

    DEFAULT_CONFIG_NAME = "target_config.json"

    def __init__(self, config_path: str | Path | None = None):
        if config_path:
            self._path = Path(config_path)
        else:
            self._path = Path.cwd() / self.DEFAULT_CONFIG_NAME

        self.config = TargetConfig()

    def load(self) -> TargetConfig:
        """Read config file and return TargetConfig."""
        if not self._path.exists():
            return self.config  # Continue with defaults

        try:
            with open(self._path, "r", encoding="utf-8") as f:
                raw = json.load(f)
        except (json.JSONDecodeError, OSError) as e:
            print(f"⚠️ Config read error: {e}")
            return self.config

        # ── Start URL ──
        self.config.start_url = raw.get("start_url", "")

        # ── Login Credentials ──
        creds = raw.get("login_credentials", {})
        self.config.login_credentials = LoginCredentials(
            username=creds.get("username", ""),
            password=creds.get("password", ""),
        )

        # ── Selectors ──
        sels = raw.get("selectors", {})
        self.config.selectors = Selectors(
            login_form=sels.get("login_form", ""),
            username_input=sels.get("username_input", ""),
            password_input=sels.get("password_input", "input[type=\"password\"]"),
            submit_button=sels.get("submit_button", ""),
            success_indicator=sels.get("success_indicator", ""),
            logged_in_name_display=sels.get("logged_in_name_display", ""),
            balance_display=sels.get("balance_display", ""),
            login_button=sels.get("login_button", ""),
            register_button=sels.get("register_button", ""),
            preserve_nav=sels.get("preserve_nav", ""),
        )

        # ── Crawl Settings ──
        cs = raw.get("crawl_settings", {})
        self.config.crawl_settings = CrawlSettings(
            max_pages=cs.get("max_pages", 100),
            max_depth=cs.get("max_depth", 3),
            deep_crawl=cs.get("deep_crawl", True),
            use_auth=cs.get("use_auth", False),
            dual_pass=cs.get("dual_pass", False),
        )

        return self.config

    def update_selectors(self, detected: dict) -> None:
        """
        Writes auto-detected selectors to config (only fills empty fields).
        Called by ScraperEngine.
        """
        s = self.config.selectors
        if not s.login_form and detected.get("login_form"):
            s.login_form = detected["login_form"]
        if not s.username_input and detected.get("username_input"):
            s.username_input = detected["username_input"]
        if not s.logged_in_name_display and detected.get("username_display"):
            s.logged_in_name_display = detected["username_display"]

    def save(self) -> None:
        """Writes the current config to disk (with auto-detected values)."""
        data = {
            "start_url": self.config.start_url,
            "login_credentials": {
                "username": self.config.login_credentials.username,
                "password": self.config.login_credentials.password,
            },
            "selectors": {
                "login_form": self.config.selectors.login_form,
                "username_input": self.config.selectors.username_input,
                "password_input": self.config.selectors.password_input,
                "submit_button": self.config.selectors.submit_button,
                "success_indicator": self.config.selectors.success_indicator,
                "logged_in_name_display": self.config.selectors.logged_in_name_display,
                "balance_display": self.config.selectors.balance_display,
                "login_button": self.config.selectors.login_button,
                "register_button": self.config.selectors.register_button,
                "preserve_nav": self.config.selectors.preserve_nav,
            },
            "crawl_settings": {
                "max_pages": self.config.crawl_settings.max_pages,
                "max_depth": self.config.crawl_settings.max_depth,
                "deep_crawl": self.config.crawl_settings.deep_crawl,
                "use_auth": self.config.crawl_settings.use_auth,
                "dual_pass": self.config.crawl_settings.dual_pass,
            },
        }
        try:
            with open(self._path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=4)
        except OSError as e:
            print(f"⚠️ Config save error: {e}")
