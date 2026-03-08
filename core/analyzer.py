"""
analyzer.py — Site Analysis Module

C3: Technology Detection  (TechStackDetector)
C4: SEO Report            (SEOAnalyzer)
C5: Broken Link Detector  (BrokenLinkDetector)
"""

import json
import re
from pathlib import Path

from bs4 import BeautifulSoup


# ══════════════════════════════════════════════════════════════
# C3 — TECHNOLOGY STACK DETECTION
# ══════════════════════════════════════════════════════════════

class TechStackDetector:
    """
    Detects the technology stack used from HTML content.
    Signature-matching (string match) based; requires no network request.
    """

    SIGNATURES: dict[str, list[str]] = {
        "React": [
            "__REACT_DEVTOOLS_GLOBAL_HOOK__", "data-reactroot", "data-reactid",
            "react.production.min.js", "react.development.js", "_reactFiber",
            "ReactDOM.render(", "createRoot(",
        ],
        "Vue.js": [
            "window.Vue", "__vue__", "data-v-", "vue.min.js",
            "vue.runtime", "$nuxt", "createApp(",
        ],
        "Angular": [
            "ng-version", "window.ng", "angular.version", "ng-app",
            "ng-controller", "angular.min.js", "zone.js",
        ],
        "Next.js": [
            "__NEXT_DATA__", "_next/static", "next/router",
            "__NEXT_LOADED_PAGES__",
        ],
        "Nuxt.js": [
            "__NUXT__", "_nuxt/", "nuxt-link", "$nuxt",
        ],
        "jQuery": [
            "window.jQuery", "jquery.min.js", "jquery-",
            "$.ajax(", "$(document).ready(",
        ],
        "Bootstrap": [
            "bootstrap.min.css", "bootstrap.bundle",
            "class=\"container\"", "btn-primary", "navbar-toggler",
        ],
        "Tailwind CSS": [
            "tailwind.css", "tailwindcss", "class=\"flex ",
            "class=\"grid ", "class=\"text-", "class=\"bg-",
        ],
        "WordPress": [
            "wp-content/", "wp-includes/", "wp-json/",
            "WordPress", "wp-embed.min.js",
        ],
        "Shopify": [
            "Shopify.", "cdn.shopify.com", "shopify_analytics",
            "myshopify.com",
        ],
        "GSAP": [
            "gsap.min.js", "TweenMax", "TimelineMax", "gsap.to(",
        ],
        "Swiper": [
            "swiper.min.js", "swiper-bundle", "class=\"swiper",
        ],
        "Axios": [
            "axios.min.js", "axios.get(", "axios.post(",
        ],
        "Lodash": [
            "lodash.min.js", "_.debounce(", "_.throttle(",
        ],
        "Chart.js": [
            "chart.min.js", "chart.js", "new Chart(",
        ],
        "Socket.io": [
            "socket.io.min.js", "socket.io.js", "io.connect(",
        ],
        "Alpine.js": [
            "alpine.js", "x-data=", "x-bind:", "x-on:",
        ],
        "Vite": [
            "/@vite/", "vite.config", "__vite__",
        ],
        "Webpack": [
            "__webpack_require__", "webpackChunk", "webpack.config",
        ],
    }

    def detect(self, html: str) -> dict[str, list[str]]:
        """Returns technologies detected and their evidence from an HTML string."""
        found: dict[str, list[str]] = {}
        for tech, sigs in self.SIGNATURES.items():
            hits = [s for s in sigs if s in html]
            if hits:
                found[tech] = hits
        return found

    def detect_from_directory(self, output_dir: Path) -> dict[str, list[str]]:
        """Scans all HTML files to produce a consolidated technology detection result."""
        all_found: dict[str, list[str]] = {}
        for html_file in output_dir.rglob("*.html"):
            try:
                content = html_file.read_text(encoding="utf-8", errors="ignore")
                for tech, hits in self.detect(content).items():
                    if tech not in all_found:
                        all_found[tech] = hits
            except Exception:
                pass
        return all_found

    def generate_report(self, output_dir: Path) -> Path:
        """Saves the technology report as JSON and returns the HTML report path."""
        found = self.detect_from_directory(output_dir)

        # JSON
        json_path = output_dir / "tech_stack.json"
        json_path.write_text(json.dumps(found, ensure_ascii=False, indent=2), encoding="utf-8")

        # HTML report
        rows = ""
        for tech, hits in sorted(found.items()):
            hits_html = ", ".join(f"<code>{h}</code>" for h in hits[:3])
            rows += f"<tr><td><b>{tech}</b></td><td>{hits_html}</td></tr>"

        badges = "".join(f'<span class="badge">{t}</span>' for t in sorted(found))

        html_report = f"""<!DOCTYPE html>
<html lang="en">
<head><meta charset="UTF-8"><title>Technology Report</title><style>
body{{font-family:Arial,sans-serif;padding:20px;background:#f5f5f5}}
h1{{color:#2c3e50}}
table{{border-collapse:collapse;width:100%;background:#fff;border-radius:8px;overflow:hidden;box-shadow:0 2px 8px rgba(0,0,0,.1)}}
th{{background:#8e44ad;color:#fff;padding:12px 8px;text-align:left}}
td{{padding:10px 8px;border-bottom:1px solid #eee}}
code{{background:#f0f0f0;padding:2px 6px;border-radius:3px;font-size:12px}}
.badge{{display:inline-block;background:#8e44ad;color:#fff;padding:4px 12px;border-radius:20px;margin:4px;font-size:14px}}
</style></head>
<body>
<h1>🔬 Technology Stack Report</h1>
<p>Number of technologies detected: <b>{len(found)}</b></p>
<div>{badges}</div>
<br>
<table>
<tr><th>Technology</th><th>Detected Signatures</th></tr>
{rows}
</table>
</body></html>"""

        html_path = output_dir / "tech_stack.html"
        html_path.write_text(html_report, encoding="utf-8")
        return html_path


# ══════════════════════════════════════════════════════════════
# C4 — SEO ANALYZER
# ══════════════════════════════════════════════════════════════

class SEOAnalyzer:
    """Analyzes SEO metrics in HTML files."""

    def analyze(self, html: str, url: str = "") -> dict:
        """SEO analysis for a single page. Returns a 0-100 score."""
        try:
            soup = BeautifulSoup(html, "lxml")
        except Exception:
            soup = BeautifulSoup(html, "html.parser")

        result: dict = {
            "url": url,
            "title": None,
            "title_length": 0,
            "meta_description": None,
            "meta_description_length": 0,
            "og_title": None,
            "og_description": None,
            "canonical": None,
            "h1_count": 0,
            "h1_texts": [],
            "h2_count": 0,
            "images_total": 0,
            "images_without_alt": 0,
            "issues": [],
            "score": 100,
        }

        # ── Title ──
        title_tag = soup.find("title")
        if title_tag:
            result["title"] = title_tag.get_text(strip=True)
            result["title_length"] = len(result["title"])
        else:
            result["issues"].append("❌ Title tag missing")
            result["score"] -= 20

        if result["title_length"] > 60:
            result["issues"].append(f"⚠️ Title too long ({result['title_length']} chars, recommended <60)")
            result["score"] -= 5
        elif result["title_length"] < 10 and result["title"]:
            result["issues"].append(f"⚠️ Title too short ({result['title_length']} characters)")
            result["score"] -= 5

        # ── Meta Description ──
        meta_desc = soup.find("meta", attrs={"name": "description"})
        if meta_desc:
            result["meta_description"] = meta_desc.get("content", "")
            result["meta_description_length"] = len(result["meta_description"])
        else:
            result["issues"].append("❌ Meta description missing")
            result["score"] -= 15

        if result["meta_description_length"] > 160:
            result["issues"].append(f"⚠️ Meta desc too long ({result['meta_description_length']} chars)")
            result["score"] -= 5

        # ── Open Graph ──
        og_title = soup.find("meta", property="og:title")
        if og_title:
            result["og_title"] = og_title.get("content", "")
        else:
            result["issues"].append("⚠️ og:title missing")
            result["score"] -= 5

        og_desc = soup.find("meta", property="og:description")
        if og_desc:
            result["og_description"] = og_desc.get("content", "")

        # ── Canonical ──
        canonical = soup.find("link", rel="canonical")
        if canonical:
            result["canonical"] = canonical.get("href", "")

        # ── Headings ──
        h1_tags = soup.find_all("h1")
        result["h1_count"] = len(h1_tags)
        result["h1_texts"] = [h.get_text(strip=True)[:80] for h in h1_tags[:5]]
        result["h2_count"] = len(soup.find_all("h2"))

        if result["h1_count"] == 0:
            result["issues"].append("❌ H1 tag missing")
            result["score"] -= 10
        elif result["h1_count"] > 1:
            result["issues"].append(f"⚠️ Multiple H1 tags ({result['h1_count']} found)")
            result["score"] -= 5

        # ── Images ──
        images = soup.find_all("img")
        result["images_total"] = len(images)
        no_alt = [img for img in images if not img.get("alt")]
        result["images_without_alt"] = len(no_alt)

        if result["images_without_alt"] > 0:
            result["issues"].append(f"⚠️ {result['images_without_alt']} images missing alt attribute")
            result["score"] -= min(10, result["images_without_alt"] * 2)

        result["score"] = max(0, result["score"])
        return result

    def analyze_directory(self, output_dir: Path) -> list[dict]:
        """Analyzes all HTML files."""
        results = []
        for html_file in output_dir.rglob("*.html"):
            try:
                content = html_file.read_text(encoding="utf-8", errors="ignore")
                r = self.analyze(content, url=html_file.name)
                r["file"] = str(html_file.relative_to(output_dir))
                results.append(r)
            except Exception:
                pass
        return results

    def generate_report(self, output_dir: Path) -> Path:
        """Saves SEO report as JSON + HTML, returns HTML path."""
        results = self.analyze_directory(output_dir)

        # JSON
        json_path = output_dir / "seo_report.json"
        json_path.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")

        avg_score = sum(r["score"] for r in results) / len(results) if results else 0
        rows = ""
        for r in results:
            color = "#27ae60" if r["score"] >= 80 else "#f39c12" if r["score"] >= 60 else "#e74c3c"
            issues_html = "<br>".join(r["issues"]) if r["issues"] else "✅ No issues"
            title_short = (r.get("title") or "—")[:45]
            rows += f"""<tr>
<td style="font-size:12px">{r.get('file', r.get('url', ''))}</td>
<td>{title_short}</td>
<td style="text-align:center">{r.get('h1_count', 0)}</td>
<td style="text-align:center">{r.get('images_without_alt', 0)}/{r.get('images_total', 0)}</td>
<td style="text-align:center;color:{color}"><b>{r['score']}/100</b></td>
<td style="font-size:11px">{issues_html}</td>
</tr>"""

        html_report = f"""<!DOCTYPE html>
<html lang="en">
<head><meta charset="UTF-8"><title>SEO Report</title><style>
body{{font-family:Arial,sans-serif;padding:20px;background:#f5f5f5}}
h1{{color:#2c3e50}}
table{{border-collapse:collapse;width:100%;background:#fff;border-radius:8px;overflow:hidden;box-shadow:0 2px 8px rgba(0,0,0,.1)}}
th{{background:#2c3e50;color:#fff;padding:12px 8px;text-align:left}}
td{{padding:9px 8px;border-bottom:1px solid #eee;vertical-align:top}}
tr:hover{{background:#f9f9f9}}
.score-box{{background:#2c3e50;color:#fff;padding:15px 20px;border-radius:8px;margin-bottom:20px;display:inline-block;font-size:18px}}
</style></head>
<body>
<h1>🔍 SEO Analysis Report</h1>
<div class="score-box">Average Score: <b>{avg_score:.1f}/100</b> &nbsp;|&nbsp; {len(results)} pages analyzed</div>
<table>
<tr><th>File</th><th>Title</th><th>H1</th><th>Alt Missing</th><th>Score</th><th>Issues</th></tr>
{rows}
</table></body></html>"""

        html_path = output_dir / "seo_report.html"
        html_path.write_text(html_report, encoding="utf-8")
        return html_path


# ══════════════════════════════════════════════════════════════
# C5 — BROKEN LINK DETECTOR
# ══════════════════════════════════════════════════════════════

class BrokenLinkDetector:
    """Detects local 404 / broken references in the cloned site."""

    def check(self, output_dir: Path) -> dict:
        """
        Checks href and src attributes in all HTML files.
        Lists references that have no corresponding local file.
        """
        broken: list[dict] = []
        checked = 0

        for html_file in output_dir.rglob("*.html"):
            try:
                content = html_file.read_text(encoding="utf-8", errors="ignore")
                soup = BeautifulSoup(content, "html.parser")

                for tag in soup.find_all(href=True):
                    self._check_ref(tag["href"], html_file, output_dir, broken)
                    checked += 1

                for tag in soup.find_all(src=True):
                    self._check_ref(tag["src"], html_file, output_dir, broken)
                    checked += 1

            except Exception:
                pass

        return {
            "total_checked": checked,
            "broken_count": len(broken),
            "broken_links": broken[:200],
        }

    def _check_ref(self, ref: str, from_file: Path, output_dir: Path, broken: list) -> None:
        if not ref:
            return
        # External URL, anchor, data URI, javascript, mailto, tel → skip
        if ref.startswith(("http://", "https://", "//", "#", "data:", "javascript:", "mailto:", "tel:")):
            return

        ref_clean = ref.split("?")[0].split("#")[0].strip()
        if not ref_clean:
            return

        try:
            resolved = (from_file.parent / ref_clean).resolve()
            if not resolved.exists():
                broken.append({
                    "from": str(from_file.relative_to(output_dir)),
                    "ref": ref,
                })
        except Exception:
            pass

    def generate_report(self, output_dir: Path) -> Path:
        """Saves the broken link report as JSON + HTML."""
        result = self.check(output_dir)

        # JSON
        json_path = output_dir / "broken_links.json"
        json_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")

        # HTML report
        rows = ""
        for item in result["broken_links"]:
            rows += f"<tr><td style='font-size:12px'>{item['from']}</td><td style='font-size:12px;color:#e74c3c'>{item['ref']}</td></tr>"

        summary_color = "#27ae60" if result["broken_count"] == 0 else "#e74c3c"
        html_report = f"""<!DOCTYPE html>
<html lang="en">
<head><meta charset="UTF-8"><title>Broken Link Report</title><style>
body{{font-family:Arial,sans-serif;padding:20px;background:#f5f5f5}}
h1{{color:#2c3e50}}
table{{border-collapse:collapse;width:100%;background:#fff;border-radius:8px;overflow:hidden;box-shadow:0 2px 8px rgba(0,0,0,.1)}}
th{{background:#c0392b;color:#fff;padding:12px 8px;text-align:left}}
td{{padding:9px 8px;border-bottom:1px solid #eee}}
.summary{{padding:15px 20px;border-radius:8px;background:#fff;margin-bottom:20px;box-shadow:0 2px 8px rgba(0,0,0,.1);display:inline-block}}
</style></head>
<body>
<h1>🔗 Broken Link Report</h1>
<div class="summary">
  Checked: <b>{result['total_checked']}</b> &nbsp;|&nbsp;
  Broken: <b style="color:{summary_color}">{result['broken_count']}</b>
</div>
{"<p style='color:#27ae60;font-size:18px'>✅ No broken links found!</p>" if result['broken_count'] == 0 else f"<table><tr><th>Source File</th><th>Broken Reference</th></tr>{rows}</table>"}
</body></html>"""

        html_path = output_dir / "broken_links.html"
        html_path.write_text(html_report, encoding="utf-8")
        return html_path
