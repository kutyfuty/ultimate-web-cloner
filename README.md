# Ultimate Web Cloner

A desktop tool for creating **offline static snapshots** of websites — for personal archiving, UI review, and development reference.

Built with Python, PyQt6, and Playwright.

---

## Features

- Full-page HTML/CSS/JS/asset download
- Authenticated session cloning (login-required pages)
- Deep multi-page crawl with configurable depth
- Offline-ready output (works without internet)
- Personal data sanitizer (removes usernames, balances, emails, IBANs)
- Local HTTP preview server
- Visual quality comparison (pixel diff)
- PWA / Service Worker injection for offline use
- SPA (React/Vue) compatibility via History API interception

## Requirements

```
Python 3.11+
pip install -r requirements.txt
playwright install chromium
```

## Usage

```bash
python main.py
```

Enter the target URL, configure crawl depth, and click **Clone**.

## ⚠️ Legal Disclaimer

**This tool is intended for personal, educational, and authorized use only.**

- Only clone websites you own or have explicit written permission to clone.
- Do **not** use this tool to create fraudulent copies, phishing pages, or to deceive users.
- The authors are not responsible for any misuse. You are solely responsible for ensuring your use complies with applicable laws (CFAA, Computer Misuse Act, GDPR, etc.) and the target website's Terms of Service.
- Cloning third-party websites without authorization may violate copyright law and website ToS.

By using this software, you agree to use it only for lawful purposes.

## License

MIT License — see [LICENSE](LICENSE)
