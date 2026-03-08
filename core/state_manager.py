import sqlite3
import threading
from pathlib import Path

class StateManager:
    """
    SQLite-based, thread-safe state manager.
    Tracks queued and visited links (as well as assets)
    and allows resuming from where it left off after a crash.
    Phase 4: Async Crawler State Management implementation.
    """
    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._local_thread = threading.local()

    def _get_conn(self) -> sqlite3.Connection:
        if not hasattr(self._local_thread, "conn"):
            self._local_thread.conn = sqlite3.connect(
                str(self.db_path),
                check_same_thread=False,
                isolation_level=None  # Autocommit option
            )
            # Performance and Concurrency Settings
            self._local_thread.conn.execute("PRAGMA journal_mode=WAL;")
            self._local_thread.conn.execute("PRAGMA synchronous=NORMAL;")
            self._init_db(self._local_thread.conn)
        return self._local_thread.conn

    def _init_db(self, conn: sqlite3.Connection):
        """Create tables and indexes."""
        conn.execute("""
            CREATE TABLE IF NOT EXISTS pages (
                url TEXT PRIMARY KEY,
                status TEXT DEFAULT 'queued',   -- 'queued', 'processing', 'visited', 'failed'
                local_path TEXT,
                updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_status ON pages(status);")

    def add_url(self, url: str) -> bool:
        """Adds a new URL to the queue. Returns False if already present or processed (IntegrityError)."""
        try:
            conn = self._get_conn()
            conn.execute("INSERT INTO pages (url, status) VALUES (?, 'queued')", (url,))
            return True
        except sqlite3.IntegrityError:
            return False

    def get_next_url(self) -> str | None:
        """Atomically marks the first queued URL as 'processing' and returns it."""
        conn = self._get_conn()
        cursor = conn.cursor()
        # Atomic Transaction
        cursor.execute("BEGIN IMMEDIATE")
        cursor.execute("SELECT url FROM pages WHERE status = 'queued' LIMIT 1")
        row = cursor.fetchone()
        if row:
            url = row[0]
            cursor.execute("UPDATE pages SET status = 'processing', updated_at = CURRENT_TIMESTAMP WHERE url = ?", (url,))
            conn.commit()
            return url
        conn.commit()
        return None

    def mark_visited(self, url: str, local_path: str = ""):
        """Marks a URL as visited (successfully cloned)."""
        conn = self._get_conn()
        conn.execute("UPDATE pages SET status = 'visited', local_path = ?, updated_at = CURRENT_TIMESTAMP WHERE url = ?", (local_path, url))

    def mark_failed(self, url: str):
        """Marks a URL as failed (could not be retrieved due to a network error)."""
        conn = self._get_conn()
        conn.execute("UPDATE pages SET status = 'failed', updated_at = CURRENT_TIMESTAMP WHERE url = ?", (url,))

    def get_visited_count(self) -> int:
        conn = self._get_conn()
        return conn.execute("SELECT COUNT(*) FROM pages WHERE status = 'visited'").fetchone()[0]

    def get_queued_count(self) -> int:
        conn = self._get_conn()
        return conn.execute("SELECT COUNT(*) FROM pages WHERE status = 'queued'").fetchone()[0]

    def reset_processing(self):
        """Resets the status of operations interrupted by crashes back to 'queued'."""
        conn = self._get_conn()
        conn.execute("UPDATE pages SET status = 'queued' WHERE status = 'processing'")

    def is_visited(self, url: str) -> bool:
        conn = self._get_conn()
        row = conn.execute("SELECT status FROM pages WHERE url = ?", (url,)).fetchone()
        return row is not None and row[0] == 'visited'

    def get_all_visited(self) -> dict[str, str]:
        """Returns all visited URLs as a dictionary."""
        conn = self._get_conn()
        cursor = conn.execute("SELECT url, local_path FROM pages WHERE status = 'visited'")
        return {row[0]: row[1] for row in cursor.fetchall()}

    def clear(self):
        """Reset the queue."""
        conn = self._get_conn()
        conn.execute("DELETE FROM pages")

    def close(self):
        if hasattr(self._local_thread, "conn"):
            self._local_thread.conn.close()
            del self._local_thread.conn
