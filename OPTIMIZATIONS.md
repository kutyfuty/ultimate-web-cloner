# Optimization Audit Report

### 1) Optimization Summary

* **Health Summary:** The current scraping system successfully implements complex interactions (stealth, lazy loading, modal capturing, CSS inlining). However, it relies heavily on fully sequential I/O, unbounded in-memory asset buffering, and CPU-intensive `O(N)` DOM parsing inside the `asyncio` event loop. It is highly prone to high latency and Out-Of-Memory (OOM) crashes on large, media-heavy sites.
* **Top 3 Highest-Impact Improvements:**
  1. **Parallelize Sub-Page Scraping:** Convert sequential scraping (`for url in all_links: await scrape(...)`) to bounded concurrent scraping using `asyncio.Semaphore`.
  2. **Stream Assets to Disk:** Avoid storing raw file bytes in the `_captured_resources` dict (in RAM) until the end of the run. Save them directly inside the network listener.
  3. **Optimize URL Rewriting Lookups:** Replace the `O(N)` path-matching loop in `AssetManager._find_local_path` with an `O(1)` precomputed lookup dictionary.
* **Biggest Risk:** **Memory Leaks & Out-Of-Memory Crashes**. Holding `bytes` for all videos, fonts, and images in Python memory until a 100-page scrape finishes will almost certainly crash the application on large casino or media site targets.

---

### 2) Findings (Prioritized)

#### Finding 1: Unbounded In-Memory Asset Buffering
* **Category:** Memory / Concurrency
* **Severity:** Critical
* **Impact:** Prevents Out-Of-Memory (OOM) crashes, massive reduction in RAM footprint.
* **Evidence:** `scraper_engine.py` line 469: `self._captured_resources[url] = body`. And `link_mapper.py` passing this massive dict around.
* **Why it’s inefficient:** All images, videos, fonts, and scripts are read entirely into Python memory as `bytes` before being flushed to disk. A site with a few 50MB videos or hundreds of high-res images will cause the PyQt/Python process to consume GBs of RAM and eventually crash.
* **Recommended fix:** Stream the Playwright `response.body()` directly to disk (using async file I/O or background threads) inside the network listener `on_response`. The `AssetManager` should expose an async `save_stream(url, body_bytes)` method, leaving only the `{url: local_path}` mapping in memory.
* **Tradeoffs / Risks:** Requires refactoring connection logic so `AssetManager` can incrementally save files during the scrape instead of a huge batch save at the end.
* **Expected impact estimate:** Reduces peak memory usage by 80-90% on media-heavy sites. Memory stays flat regardless of `max_pages`.
* **Removal Safety:** Needs Verification
* **Reuse Scope:** service-wide

#### Finding 2: Sequential Sub-Page Scraping Bottleneck
* **Category:** Concurrency / Latency
* **Severity:** Critical
* **Impact:** 3x - 5x faster full-site cloning time.
* **Evidence:** `link_mapper.py` line 135: `for idx, (url_path, full_url) in enumerate(all_links.items(), 1): await self._scrape_subpage(...)`
* **Why it’s inefficient:** Sub-pages are scraped sequentially. Each page involves waiting for `networkidle`, scrolling, and DOM evaluation (easily 5-10 seconds per page). 100 pages = 15+ minutes of pure idle waiting.
* **Recommended fix:** Use `asyncio.gather` combined with an `asyncio.Semaphore(5)` to scrape 5-10 sub-pages concurrently from the shared contextual `browser`.
* **Tradeoffs / Risks:** Higher CPU usage (5x browser contexts) and potential rate-limiting/IP-blocking or captchas by the target site. Needs a configurable concurrency limit in the UI.
* **Expected impact estimate:** 300% to 500% faster total multi-page cloning time.
* **Removal Safety:** Needs Verification
* **Reuse Scope:** service-wide

#### Finding 3: O(N) URL Lookup in HTML Parsing Loop
* **Category:** Algorithm / CPU
* **Severity:** High
* **Impact:** Eliminates UI freezing and excessive CPU heat during the final file rewrite.
* **Evidence:** `asset_manager.py` line 305: The loop `for original_url, local_path in self._url_to_local.items():` is called inside `_find_local_path` for *every* asset tag in the DOM across all pages.
* **Why it’s inefficient:** If a page has 500 resources, and the site has 2,000 downloaded assets, this method performs 1,000,000 `urlparse` string operations per page. Multiplied by 100 pages = 100M string parsing operations purely on the CPU blocking the main event loop.
* **Recommended fix:** When assets are finished downloading, compute a mapping: `self._path_lookup = {(urlparse(url).netloc, urlparse(url).path): local_path}`. Change `_find_local_path` to do a single `O(1)` tuple lookup: `return self._path_lookup.get((parsed.netloc, parsed.path))`.
* **Tradeoffs / Risks:** Very low risk.
* **Expected impact estimate:** HTML rewriting becomes near instantaneous instead of freezing the UI for ~2-5 seconds per page.
* **Removal Safety:** Safe
* **Reuse Scope:** module

#### Finding 4: CPU-Bound BeautifulSoup Parsing in Async Event Loop
* **Category:** CPU / UI Concurrency
* **Severity:** Medium
* **Impact:** Prevents event loop (and UI) from freezing.
* **Evidence:** `asset_manager.py` `rewrite_html` and `link_mapper.py` `_rewrite_links_in_html` both run synchronous `BeautifulSoup(html, "lxml")` logic directly in an `async` or GUI thread.
* **Why it’s inefficient:** Running CPU-heavy parsing on large HTML payloads (1-5MB) synchronously blocks the `asyncio` loop and PyQt main thread. Progress bars will stop updating, and the app will feel unresponsive ("Not Responding").
* **Recommended fix:** Offload `BeautifulSoup` parsing and HTML rewriting to a thread pool using `await asyncio.to_thread(self._rewrite_html_sync, html, base_url)` or a `QThreadPool` worker.
* **Tradeoffs / Risks:** Slight context-switching overhead, but drastically improved perceived performance.
* **Expected impact estimate:** Smooth UI progress bars, unblocked event loops.
* **Removal Safety:** Safe
* **Reuse Scope:** local file

#### Finding 5: Playwright IPC Overhead in Lazy Scroll Loop
* **Category:** I/O / Frontend
* **Severity:** Low
* **Impact:** Minor reduction in IPC (Inter-Process Communication) latency.
* **Evidence:** `scraper_engine.py` line 197 and line 222 evaluating `scrollHeight` and `scrollY + window.innerHeight` in separate `page.evaluate` calls per tick loop.
* **Why it’s inefficient:** Playwright node-to-python evaluation has a noticeable IPC latency. Doing 3 distinct IPC calls per 500ms scroll frame across 150 steps = 450 network-like calls.
* **Recommended fix:** Combine `window.scrollBy`, `scrollHeight` calculation, and `scrollY` calculation into a single `page.evaluate()` call that returns a dictionary.
* **Tradeoffs / Risks:** None.
* **Expected impact estimate:** ~30-50ms saved per scroll tick.
* **Removal Safety:** Safe
* **Reuse Scope:** local file

---

### 3) Quick Wins (Do First)
1. **O(1) Precomputed Lookup:** Add `_path_lookup` in `AssetManager` to eliminate the massive `urlparse` bottleneck. Takes 5 minutes to implement, massive ROI.
2. **IPC Scroll Consolidation:** Merge the 3 `page.evaluate` calls in `_scroll_to_bottom` (scraper_engine.py) into 1.

---

### 4) Deeper Optimizations (Do Next)
1. **Streaming Asset Save:** Decouple `_captured_resources` from RAM. Update the `request.on_response` listener to use `async with aiofiles.open(target_path, 'wb')` instead of buffering `bytes` in a dict.
2. **Concurrent Scraping Pipeline:** In `link_mapper`, use an `asyncio.Queue` or `Semaphore` to keep exactly 5 pages downloading at `_scrape_subpage` concurrently.

---

### 5) Validation Plan
* **Memory Profiling:** Run `tracemalloc` to record peak memory during a clone of a site with high-res videos (e.g., standard casino index page). Verify peak drops from > 1GB to < 200MB.
* **Profiling:** Wrap `AssetManager.rewrite_html` with `time.perf_counter()` before and after the O(1) hash map fix. Ensure processing time scales linearly, not exponentially.
* **Correctness:** Compare the visual quality (via your `quality_checker.py`) of a sequentially cloned site vs. a concurrently cloned site to ensure no missing assets or corrupted CSS files.

---

### 6) Optimized Code / Patch

#### Quick Win: AssetManager `O(1)` Lookup Fix

```python
# asset_manager.py
from functools import lru_cache

class AssetManager(QObject):
    def __init__(self, ...):
        # ...
        self._url_to_local: dict[str, str] = {}
        self._fast_lookup_cache: dict[tuple[str, str], str] = {}
        self._lookup_initialized = False

    def _init_fast_lookup(self):
        """Precompute netloc+path tuples to local paths to avoid O(N) looping."""
        if self._lookup_initialized:
            return
        for original_url, local_path in self._url_to_local.items():
            parsed = urlparse(original_url)
            self._fast_lookup_cache[(parsed.netloc, parsed.path)] = local_path
        self._lookup_initialized = True

    @lru_cache(maxsize=10000)
    def _find_local_path(self, abs_url: str) -> str | None:
        """Verilen URL için yerel dosya yolunu O(1) hızında bul."""
        if not self._lookup_initialized:
            self._init_fast_lookup()
            
        # Tam eşleşme
        if abs_url in self._url_to_local:
            return self._url_to_local[abs_url]

        parsed = urlparse(abs_url)
        clean_url = f"{parsed.scheme}://{parsed.netloc}{parsed.path}"
        if clean_url in self._url_to_local:
            return self._url_to_local[clean_url]

        # Yol tabanlı eşleşme O(1) üzerinden çalışır
        return self._fast_lookup_cache.get((parsed.netloc, parsed.path))
```
*Why this works:* It lazily builds a tuple `(netloc, path)` dictionary the first time lookups start, making `_find_local_path` an `O(1)` dictionary get. Additionally, `@lru_cache` speeds up repeated instances of the same URL references.
