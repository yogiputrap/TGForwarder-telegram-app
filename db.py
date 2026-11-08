import os
import sqlite3

SCHEMA = """
CREATE TABLE IF NOT EXISTS rules (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id INTEGER NOT NULL,
    target_id INTEGER NOT NULL,
    source_title TEXT NOT NULL DEFAULT '',
    target_title TEXT NOT NULL DEFAULT '',
    UNIQUE (source_id, target_id)
);
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


class Database:
    """Small SQLite store for forwarding rules and runtime settings."""

    def __init__(self, path):
        os.makedirs(os.path.dirname(path) or '.', exist_ok=True)
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def list_rules(self):
        return self.conn.execute("SELECT * FROM rules ORDER BY id").fetchall()

    def get_rule(self, rule_id):
        return self.conn.execute("SELECT * FROM rules WHERE id = ?", (rule_id,)).fetchone()

    def add_rule(self, source_id, target_id, source_title='', target_title=''):
        """Add a rule. Returns False if the same source/target pair already exists."""
        try:
            self.conn.execute(
                "INSERT INTO rules (source_id, target_id, source_title, target_title) VALUES (?, ?, ?, ?)",
                (source_id, target_id, source_title, target_title),
            )
            self.conn.commit()
            return True
        except sqlite3.IntegrityError:
            return False

    def delete_rule(self, rule_id):
        self.conn.execute("DELETE FROM rules WHERE id = ?", (rule_id,))
        self.conn.commit()

    def forwarding_map(self):
        """Return {source_id: [target_id, ...]} for the event handlers."""
        forwarding_map = {}
        for rule in self.list_rules():
            forwarding_map.setdefault(rule['source_id'], []).append(rule['target_id'])
        return forwarding_map

    def get_setting(self, key, default=None):
        row = self.conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        return row['value'] if row else default

    def set_setting(self, key, value):
        self.conn.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, str(value)),
        )
        self.conn.commit()
