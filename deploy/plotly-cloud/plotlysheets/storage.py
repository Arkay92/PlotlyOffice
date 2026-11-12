"""SQLite workspaces, capability links, presence, comments and history."""
import hashlib
import json
import secrets
import sqlite3
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from .merge import merge_value


class ConflictError(Exception):
    pass


class Store:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS workbooks (
                    id TEXT PRIMARY KEY, title TEXT NOT NULL, settings TEXT NOT NULL,
                    version INTEGER NOT NULL, updated TEXT NOT NULL, owner_id TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS revisions (
                    workbook_id TEXT NOT NULL, version INTEGER NOT NULL, title TEXT NOT NULL,
                    settings TEXT NOT NULL, updated TEXT NOT NULL, author_name TEXT NOT NULL,
                    author_color TEXT NOT NULL, PRIMARY KEY(workbook_id, version));
                CREATE TABLE IF NOT EXISTS identities (key_hash TEXT PRIMARY KEY, name TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS share_links (
                    id TEXT PRIMARY KEY, workbook_id TEXT NOT NULL, token TEXT NOT NULL UNIQUE,
                    label TEXT NOT NULL, role TEXT NOT NULL, created TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS links_workbook ON share_links(workbook_id);
                CREATE TABLE IF NOT EXISTS presence (
                    client_id TEXT PRIMARY KEY, workbook_id TEXT NOT NULL, identity TEXT NOT NULL,
                    link_id TEXT, name TEXT NOT NULL, cursor TEXT NOT NULL, updated REAL NOT NULL);
                CREATE INDEX IF NOT EXISTS presence_workbook ON presence(workbook_id);
                CREATE TABLE IF NOT EXISTS comments (
                    id TEXT PRIMARY KEY, workbook_id TEXT NOT NULL, author_id TEXT NOT NULL,
                    author_name TEXT NOT NULL, color TEXT NOT NULL, body TEXT NOT NULL,
                    cell TEXT NOT NULL, sheet_id TEXT NOT NULL, resolved INTEGER NOT NULL DEFAULT 0,
                    created TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS comments_workbook ON comments(workbook_id);
                """
            )

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=15)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    @staticmethod
    def decode(row):
        if row is None:
            raise KeyError("Workbook not found.")
        value = dict(row)
        if "settings" in value:
            value["settings"] = json.loads(value["settings"])
        return value

    @staticmethod
    def key_hash(key):
        return hashlib.sha256(key.encode()).hexdigest() if isinstance(key, str) and 20 <= len(key) <= 200 else ""

    @staticmethod
    def user_color(identity):
        colors = ["#0f7b6c", "#9c5a32", "#6657b8", "#b74c76", "#2878a6", "#827c27"]
        return colors[int(identity[:8], 16) % len(colors)]

    def identity(self, key):
        with self.connect() as db:
            row = db.execute("SELECT * FROM identities WHERE key_hash=?", (self.key_hash(key),)).fetchone()
            if row is None:
                raise PermissionError("Open your workspace first, or restore it with your workspace key.")
            return dict(row)

    def new_identity(self, name):
        key = secrets.token_urlsafe(32)
        with self.connect() as db:
            db.execute("INSERT INTO identities VALUES (?,?)", (self.key_hash(key), name))
        return {"key": key, "name": name}

    def rename_identity(self, key, name):
        identity = self.identity(key)
        with self.connect() as db:
            db.execute("UPDATE identities SET name=? WHERE key_hash=?", (name, identity["key_hash"]))
        return {"name": name}

    def list(self, owner):
        with self.connect() as db:
            return [dict(r) for r in db.execute(
                "SELECT id,title,version,updated FROM workbooks WHERE owner_id=? ORDER BY updated DESC", (owner,)
            )]

    def get(self, workbook_id):
        with self.connect() as db:
            return self.decode(db.execute("SELECT * FROM workbooks WHERE id=?", (workbook_id,)).fetchone())

    def access(self, workbook_id, key, link=None, require="view"):
        identity = self.identity(key)
        with self.connect() as db:
            book = db.execute("SELECT owner_id,version FROM workbooks WHERE id=?", (workbook_id,)).fetchone()
            if book is None:
                raise KeyError("Workbook not found.")
            grant = None
            if book["owner_id"] == identity["key_hash"]:
                role = "owner"
            else:
                if isinstance(link, str) and 20 <= len(link) <= 200:
                    grant = db.execute(
                        "SELECT id,role FROM share_links WHERE workbook_id=? AND token=?", (workbook_id, link)
                    ).fetchone()
                if not grant:
                    raise PermissionError("This workbook is private or its share link has been revoked.")
                role = grant["role"]
            if {"view": 0, "edit": 1, "owner": 2}[role] < {"view": 0, "edit": 1, "owner": 2}[require]:
                raise PermissionError("You do not have permission to make this change.")
            return {**identity, "role": role, "version": book["version"], "link_id": grant["id"] if grant else None}

    def save(self, title, settings, workbook_id=None, version=None, owner=None, base=None, credentials=None):
        now = datetime.now(timezone.utc).isoformat()
        encoded = json.dumps(settings, separators=(",", ":"))
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if workbook_id:
                author = self.access(workbook_id, *credentials, require="edit") if credentials else None
                old = db.execute("SELECT * FROM workbooks WHERE id=?", (workbook_id,)).fetchone()
                if old is None:
                    raise KeyError("Workbook not found.")
                if base is not None:
                    title = merge_value(base["title"], old["title"], title, "the workbook title")
                    settings = merge_value(base["settings"], json.loads(old["settings"]), settings)
                    encoded = json.dumps(settings, separators=(",", ":"))
                elif version != old["version"]:
                    raise ConflictError("This workbook changed in another tab. Your local draft has been preserved.")
                if (title, encoded) == (old["title"], old["settings"]):
                    return self.decode(old)
                next_version = old["version"] + 1
                db.execute(
                    "UPDATE workbooks SET title=?,settings=?,version=?,updated=? WHERE id=?",
                    (title, encoded, next_version, now, workbook_id),
                )
            else:
                author = db.execute("SELECT * FROM identities WHERE key_hash=?", (owner,)).fetchone()
                workbook_id, next_version = uuid4().hex, 1
                db.execute(
                    "INSERT INTO workbooks VALUES (?,?,?,?,?,?)",
                    (workbook_id, title, encoded, next_version, now, owner),
                )
            author_name = author["name"] if author else "Unknown"
            author_id = author["key_hash"] if author else "0" * 64
            db.execute(
                "INSERT INTO revisions VALUES (?,?,?,?,?,?,?)",
                (workbook_id, next_version, title, encoded, now, author_name, self.user_color(author_id)),
            )
            db.execute("DELETE FROM revisions WHERE workbook_id=? AND version<=?", (workbook_id, next_version - 50))
        return self.get(workbook_id)

    def delete(self, workbook_id):
        with self.connect() as db:
            for table in ("comments", "share_links", "presence", "revisions"):
                db.execute(f"DELETE FROM {table} WHERE workbook_id=?", (workbook_id,))
            db.execute("DELETE FROM workbooks WHERE id=?", (workbook_id,))

    def history(self, workbook_id):
        self.get(workbook_id)
        with self.connect() as db:
            return [dict(r) for r in db.execute(
                "SELECT version,title,updated,author_name,author_color FROM revisions WHERE workbook_id=? ORDER BY version DESC",
                (workbook_id,),
            )]

    def revision(self, workbook_id, version):
        with self.connect() as db:
            return self.decode(db.execute(
                "SELECT workbook_id AS id,title,settings,version,updated FROM revisions WHERE workbook_id=? AND version=?",
                (workbook_id, version),
            ).fetchone())

    def links(self, workbook_id):
        with self.connect() as db:
            return [dict(r) for r in db.execute(
                "SELECT * FROM share_links WHERE workbook_id=? ORDER BY created", (workbook_id,)
            )]

    def create_link(self, workbook_id, role, label):
        row = {
            "id": uuid4().hex, "workbook_id": workbook_id, "token": secrets.token_urlsafe(32),
            "label": label, "role": role, "created": datetime.now(timezone.utc).isoformat(),
        }
        with self.connect() as db:
            if db.execute("SELECT count(*) FROM share_links WHERE workbook_id=?", (workbook_id,)).fetchone()[0] >= 30:
                raise ValueError("A workbook can have up to 30 active links.")
            db.execute("INSERT INTO share_links VALUES (:id,:workbook_id,:token,:label,:role,:created)", row)
        return row

    def update_link(self, workbook_id, link_id, role):
        with self.connect() as db:
            if role is None:
                db.execute("DELETE FROM share_links WHERE workbook_id=? AND id=?", (workbook_id, link_id))
                db.execute("DELETE FROM presence WHERE workbook_id=? AND link_id=?", (workbook_id, link_id))
            else:
                db.execute("UPDATE share_links SET role=? WHERE workbook_id=? AND id=?", (role, workbook_id, link_id))

    def touch_presence(self, workbook_id, client_id, access, cursor):
        now = time.time()
        with self.connect() as db:
            db.execute("DELETE FROM presence WHERE updated<?", (now - 20,))
            old = db.execute("SELECT identity FROM presence WHERE client_id=?", (client_id,)).fetchone()
            if old and old["identity"] != access["key_hash"]:
                raise PermissionError("This cursor belongs to another session.")
            db.execute(
                "INSERT OR REPLACE INTO presence VALUES (?,?,?,?,?,?,?)",
                (client_id, workbook_id, access["key_hash"], access["link_id"], access["name"], json.dumps(cursor), now),
            )

    def peers(self, workbook_id):
        with self.connect() as db:
            rows = db.execute(
                """SELECT p.* FROM presence p LEFT JOIN share_links l ON p.link_id=l.id
                WHERE p.workbook_id=? AND p.updated>? AND (p.link_id IS NULL OR l.id IS NOT NULL)""",
                (workbook_id, time.time() - 15),
            ).fetchall()
            return [{
                "id": r["client_id"], "name": r["name"], "color": self.user_color(r["identity"]),
                "cursor": json.loads(r["cursor"]),
            } for r in rows]

    def comments(self, workbook_id, key, link=None):
        grant = self.access(workbook_id, key, link)
        with self.connect() as db:
            rows = db.execute("SELECT * FROM comments WHERE workbook_id=? ORDER BY created", (workbook_id,)).fetchall()
            return [{k: r[k] for k in r.keys() if k != "author_id"} | {
                "can_delete": grant["role"] == "owner" or r["author_id"] == grant["key_hash"]
            } for r in rows]

    def add_comment(self, workbook_id, key, link, body, cell, sheet_id):
        if not isinstance(body, str) or not body.strip() or len(body) > 3000:
            raise ValueError("Enter a comment between 1 and 3,000 characters.")
        grant = self.access(workbook_id, key, link, "edit")
        with self.connect() as db:
            if db.execute("SELECT count(*) FROM comments WHERE workbook_id=?", (workbook_id,)).fetchone()[0] >= 500:
                raise ValueError("This workbook has reached its 500-comment limit.")
            comment_id = uuid4().hex
            db.execute("INSERT INTO comments VALUES (?,?,?,?,?,?,?,?,?,?)", (
                comment_id, workbook_id, grant["key_hash"], grant["name"], self.user_color(grant["key_hash"]),
                body.strip(), str(cell)[:20], str(sheet_id)[:100], 0, datetime.now(timezone.utc).isoformat(),
            ))
        return comment_id

    def change_comment(self, workbook_id, comment_id, key, link, resolved=None, delete=False):
        grant = self.access(workbook_id, key, link, "edit")
        with self.connect() as db:
            row = db.execute("SELECT * FROM comments WHERE workbook_id=? AND id=?", (workbook_id, comment_id)).fetchone()
            if row is None:
                raise KeyError("Comment not found.")
            if delete:
                if grant["role"] != "owner" and row["author_id"] != grant["key_hash"]:
                    raise PermissionError("Only the comment author or workbook owner can delete it.")
                db.execute("DELETE FROM comments WHERE id=?", (comment_id,))
            else:
                if not isinstance(resolved, bool):
                    raise ValueError("Choose resolved or open.")
                db.execute("UPDATE comments SET resolved=? WHERE id=?", (int(resolved), comment_id))
