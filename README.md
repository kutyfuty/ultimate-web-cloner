# Ultimate Web Cloner

> Capture any website as a fully offline, self-contained static snapshot — including authenticated sessions, dynamic content, and SPA frameworks.

![Python](https://img.shields.io/badge/Python-3.11+-blue?logo=python&logoColor=white)
![PyQt6](https://img.shields.io/badge/UI-PyQt6-green?logo=qt)
![Playwright](https://img.shields.io/badge/Browser-Playwright-orange?logo=playwright)
![License](https://img.shields.io/badge/License-MIT-lightgrey)

---

## What it does

Ultimate Web Cloner downloads a website — HTML, CSS, JavaScript, fonts, images, everything — and rewrites all links so the result works 100% offline in any browser. It goes beyond simple `wget` by executing JavaScript, handling login sessions, and capturing dynamically rendered content.

---

## Features

| Category | Details |
|---|---|
| **Cloning** | Full-page HTML/CSS/JS + all assets (fonts, images, video) |
| **Auth sessions** | Logs in with your credentials, clones the authenticated view |
| **Deep crawl** | Follows internal links up to configurable depth & page count |
| **SPA support** | Intercepts History API for React/Vue/Angular routing |
| **Stealth** | Canvas/WebGL fingerprint normalization, human-like scroll |
| **Sanitizer** | Strips usernames, balances, emails, IBANs, crypto addresses from output |
| **Visual QA** | Pixel-level screenshot diff — reports % similarity to original |
| **Preview** | Built-in local HTTP server for instant in-app preview |
| **Offline PWA** | Injects Service Worker so clone loads without internet |
| **Quality report** | Auto-scores the clone across 5 pillars (HTML, CSS, assets, JS, visual) |

---

## Requirements

- Python 3.11+
- Chromium (installed via Playwright)

```bash
pip install -r requirements.txt
playwright install chromium
```

---

## Installation & Usage

```bash
git clone https://github.com/kutyfuty/ultimate-web-cloner.git
cd ultimate-web-cloner

pip install -r requirements.txt
playwright install chromium

python main.py
```

1. Paste the target URL into the input bar
2. (Optional) Fill in login credentials and CSS selectors in `target_config.json`
3. Set crawl depth and max pages
4. Click **Clone** — output lands in `output/<domain>/`

---

## Project Structure

```
ultimate-web-cloner/
├── main.py                  # Entry point
├── core/
│   ├── scraper_engine.py    # Playwright-based crawler & session manager
│   ├── asset_manager.py     # Asset download, URL rewriting, HTML reconstruction
│   ├── link_mapper.py       # Multi-page crawl coordinator
│   ├── sanitizer.py         # Personal data removal
│   ├── frontend_mocker.py   # JS state injection for offline SPA behaviour
│   ├── api_mocker.py        # Intercepts XHR/fetch, serves cached responses
│   ├── visual_tester.py     # Screenshot diff & similarity score
│   ├── quality_checker.py   # Multi-pillar quality audit
│   ├── preview_server.py    # Local HTTP preview server
│   └── ...
├── ui/
│   ├── main_window.py       # Main PyQt6 window
│   └── widgets/             # Log viewer, progress panel, URL bar
├── requirements.txt
└── build_exe.bat            # One-click PyInstaller EXE builder (Windows)
```

---

## Configuration (`target_config.json`)

```json
{
  "start_url": "https://example.com",
  "login_credentials": {
    "username": "your_username",
    "password": "your_password"
  },
  "selectors": {
    "username_input": "input[name='username']",
    "password_input": "input[type='password']",
    "submit_button": "button[type='submit']",
    "logged_in_name_display": ".user-display-name"
  },
  "crawl_settings": {
    "max_pages": 100,
    "max_depth": 3,
    "use_auth": true
  }
}
```

> `target_config.json` is excluded from git (`.gitignore`) — your credentials never leave your machine.

---

## Build EXE (Windows)

Double-click `build_exe.bat` to produce a standalone `dist/WebCloner_GodMode/WebCloner_GodMode.exe` using PyInstaller. No Python installation needed on the target machine.

---

## ⚠️ Legal Disclaimer

**This tool is for personal archiving, UI research, and authorized testing only.**

- Only clone websites you own or have **explicit written permission** to clone.
- Do **not** use this tool to create fraudulent copies, phishing pages, or to deceive users in any way.
- Cloning third-party websites without authorization may violate copyright law, GDPR, CFAA, the Computer Misuse Act, and the target site's Terms of Service.
- The authors accept **no liability** for misuse. You are solely responsible for ensuring your use is lawful.

By using this software you agree to these terms.

---

## License

[MIT License](LICENSE) — free to use, modify, and distribute with attribution.
