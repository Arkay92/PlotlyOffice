import hashlib
import json
import mimetypes
import os
import secrets
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any

MAX_UPLOAD_BYTES = 25 * 1024 * 1024  # 25 MB
MAX_SHARES_PER_ITEM = 30
PRESENCE_TTL_SECONDS = 20

ROLE_RANK = {"view": 1, "edit": 2, "owner": 3}

PALETTE = [
    "#176b5a",
    "#2f6fed",
    "#b45309",
    "#9333ea",
    "#be185d",
    "#0f766e",
    "#4338ca",
    "#c2410c",
]


def _now() -> int:
    return int(time.time())


def _safe_name(name: str, fallback: str = "Untitled") -> str:
    cleaned = " ".join(str(name or "").replace("\x00", "").split()).strip()
    for ch in ("/", "\\", ":", "*", "?", '"', "<", ">", "|"):
        cleaned = cleaned.replace(ch, "-")
    return cleaned[:180] or fallback


def _ext_of(filename: str) -> str:
    suffix = Path(filename).suffix.lower()
    return suffix[1:] if suffix.startswith(".") else ""


class Store:
    """SQLite-backed storage for PlotlyOffice folders, files, sharing, presence, and comments."""

    def __init__(self, db_path: str):
        self.db_path = db_path
        if db_path != ":memory:":
            Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(db_path, check_same_thread=False, timeout=15.0)
        self._conn.row_factory = sqlite3.Row
        with self._conn:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA foreign_keys=ON")
        self._init_schema()

    def _init_schema(self) -> None:
        with self._conn:
            self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS identities (
                    key_hash TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    created INTEGER NOT NULL
                );

                CREATE TABLE IF NOT EXISTS items (
                    id TEXT PRIMARY KEY,
                    parent_id TEXT,
                    owner_id TEXT NOT NULL,
                    kind TEXT NOT NULL CHECK (kind IN ('folder', 'file')),
                    name TEXT NOT NULL,
                    ext TEXT NOT NULL DEFAULT '',
                    mime_type TEXT NOT NULL DEFAULT 'application/octet-stream',
                    size_bytes INTEGER NOT NULL DEFAULT 0,
                    sha256 TEXT NOT NULL DEFAULT '',
                    starred INTEGER NOT NULL DEFAULT 0,
                    trashed INTEGER NOT NULL DEFAULT 0,
                    suite_app TEXT NOT NULL DEFAULT '',
                    suite_doc_id TEXT NOT NULL DEFAULT '',
                    suite_url TEXT NOT NULL DEFAULT '',
                    suite_edit_token TEXT NOT NULL DEFAULT '',
                    suite_view_token TEXT NOT NULL DEFAULT '',
                    created INTEGER NOT NULL,
                    updated INTEGER NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_items_owner_parent
                    ON items (owner_id, parent_id, trashed);

                CREATE INDEX IF NOT EXISTS idx_items_parent
                    ON items (parent_id, trashed);

                CREATE TABLE IF NOT EXISTS file_blobs (
                    item_id TEXT PRIMARY KEY REFERENCES items(id) ON DELETE CASCADE,
                    content BLOB NOT NULL
                );

                CREATE TABLE IF NOT EXISTS share_links (
                    id TEXT PRIMARY KEY,
                    item_id TEXT NOT NULL REFERENCES items(id) ON DELETE CASCADE,
                    token TEXT UNIQUE NOT NULL,
                    label TEXT NOT NULL,
                    role TEXT NOT NULL CHECK (role IN ('view', 'edit')),
                    created INTEGER NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_share_links_item
                    ON share_links (item_id);

                CREATE TABLE IF NOT EXISTS presence (
                    client_id TEXT NOT NULL,
                    item_id TEXT NOT NULL,
                    identity TEXT NOT NULL,
                    link_id TEXT NOT NULL DEFAULT '',
                    name TEXT NOT NULL,
                    color TEXT NOT NULL,
                    cursor_json TEXT NOT NULL DEFAULT '{}',
                    updated INTEGER NOT NULL,
                    PRIMARY KEY (client_id, item_id)
                );

                CREATE TABLE IF NOT EXISTS comments (
                    id TEXT PRIMARY KEY,
                    item_id TEXT NOT NULL REFERENCES items(id) ON DELETE CASCADE,
                    author_id TEXT NOT NULL,
                    author_name TEXT NOT NULL,
                    author_color TEXT NOT NULL,
                    body TEXT NOT NULL,
                    resolved INTEGER NOT NULL DEFAULT 0,
                    created INTEGER NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_comments_item
                    ON comments (item_id, created);

                CREATE TABLE IF NOT EXISTS activity (
                    id TEXT PRIMARY KEY,
                    item_id TEXT NOT NULL REFERENCES items(id) ON DELETE CASCADE,
                    actor_name TEXT NOT NULL,
                    actor_color TEXT NOT NULL,
                    action TEXT NOT NULL,
                    detail TEXT NOT NULL DEFAULT '',
                    created INTEGER NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_activity_item
                    ON activity (item_id, created);
                """
            )

    @contextmanager
    def _tx(self):
        with self._conn:
            yield self._conn

    @staticmethod
    def key_hash(raw_key: str) -> str:
        return hashlib.sha256(raw_key.encode("utf-8")).hexdigest()

    @staticmethod
    def user_color(seed: str) -> str:
        digest = hashlib.sha256(seed.encode("utf-8")).digest()
        return PALETTE[digest[0] % len(PALETTE)]

    def new_identity(self, name: str = "Workspace User") -> dict[str, str]:
        clean_name = " ".join(str(name or "").split())[:40] or "Workspace User"
        raw_key = secrets.token_urlsafe(32)
        k_hash = self.key_hash(raw_key)
        with self._tx() as conn:
            conn.execute(
                "INSERT INTO identities (key_hash, name, created) VALUES (?, ?, ?)",
                (k_hash, clean_name, _now()),
            )
        return {"key": raw_key, "name": clean_name, "color": self.user_color(k_hash)}

    def identity(self, raw_key: str) -> dict[str, str]:
        if not raw_key:
            raise PermissionError("Workspace key required.")
        k_hash = self.key_hash(raw_key)
        row = self._conn.execute(
            "SELECT key_hash, name, created FROM identities WHERE key_hash = ?",
            (k_hash,),
        ).fetchone()
        if not row:
            raise PermissionError("Workspace unavailable.")
        return {
            "key_hash": row["key_hash"],
            "name": row["name"],
            "color": self.user_color(row["key_hash"]),
        }

    def rename_identity(self, raw_key: str, name: str) -> dict[str, str]:
        ident = self.identity(raw_key)
        clean_name = " ".join(str(name or "").split())[:40]
        if not clean_name:
            raise ValueError("Enter your display name to continue.")
        with self._tx() as conn:
            conn.execute(
                "UPDATE identities SET name = ? WHERE key_hash = ?",
                (clean_name, ident["key_hash"]),
            )
        ident["name"] = clean_name
        return ident

    def _row_to_item(self, row: sqlite3.Row, role: str | None = None) -> dict[str, Any]:
        item = {
            "id": row["id"],
            "parent_id": row["parent_id"],
            "kind": row["kind"],
            "name": row["name"],
            "ext": row["ext"],
            "mime_type": row["mime_type"],
            "size_bytes": row["size_bytes"],
            "sha256": row["sha256"],
            "starred": bool(row["starred"]),
            "trashed": bool(row["trashed"]),
            "suite_app": row["suite_app"],
            "suite_doc_id": row["suite_doc_id"],
            "suite_url": row["suite_url"],
            "created": row["created"],
            "updated": row["updated"],
            "shared": self._has_shares(row["id"]),
        }
        if role is not None:
            item["role"] = role
        return item

    def _has_shares(self, item_id: str) -> bool:
        row = self._conn.execute(
            "SELECT 1 FROM share_links WHERE item_id = ? LIMIT 1", (item_id,)
        ).fetchone()
        return row is not None

    def get_raw_item(self, item_id: str) -> sqlite3.Row:
        row = self._conn.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()
        if not row:
            raise KeyError("Item not found.")
        return row

    def access(
        self,
        item_id: str,
        raw_key: str = "",
        share_token: str = "",
        require: str = "view",
    ) -> dict[str, Any]:
        item = self.get_raw_item(item_id)
        ident: dict[str, str] | None = None
        if raw_key:
            try:
                ident = self.identity(raw_key)
            except PermissionError:
                ident = None

        grant: dict[str, Any] | None = None
        if ident and item["owner_id"] == ident["key_hash"]:
            grant = {
                "role": "owner",
                "identity": ident,
                "link_id": "",
                "grant_item_id": item_id,
                "item": item,
            }
        elif share_token:
            # Walk up folder hierarchy to support inherited folder share links
            curr: sqlite3.Row | None = item
            visited = set()
            for _ in range(32):
                if curr is None or curr["id"] in visited:
                    break
                visited.add(curr["id"])
                if curr["trashed"]:
                    break
                link = self._conn.execute(
                    "SELECT id, item_id, role, label FROM share_links WHERE item_id = ? AND token = ?",
                    (curr["id"], share_token),
                ).fetchone()
                if link:
                    guest = ident or {
                        "key_hash": f"link:{link['id']}",
                        "name": "Guest",
                        "color": self.user_color(link["id"]),
                    }
                    grant = {
                        "role": link["role"],
                        "identity": guest,
                        "link_id": link["id"],
                        "grant_item_id": curr["id"],
                        "item": item,
                    }
                    break
                parent_id = curr["parent_id"]
                curr = (
                    self._conn.execute("SELECT * FROM items WHERE id = ?", (parent_id,)).fetchone()
                    if parent_id
                    else None
                )

        if not grant:
            raise PermissionError("You do not have access to this item.")
        if ROLE_RANK[grant["role"]] < ROLE_RANK[require]:
            raise PermissionError("You do not have permission to modify this item.")
        return grant

    def breadcrumbs(
        self, item_id: str, raw_key: str = "", share_token: str = ""
    ) -> list[dict[str, str]]:
        grant = self.access(item_id, raw_key, share_token, "view")
        stop_id = None if grant["role"] == "owner" else grant["grant_item_id"]
        crumbs: list[dict[str, str]] = []
        curr_id: str | None = item_id
        visited = set()
        for _ in range(32):
            if not curr_id or curr_id in visited:
                break
            visited.add(curr_id)
            row = self._conn.execute(
                "SELECT id, parent_id, name, kind FROM items WHERE id = ?", (curr_id,)
            ).fetchone()
            if not row:
                break
            crumbs.append({"id": row["id"], "name": row["name"], "kind": row["kind"]})
            if curr_id == stop_id:
                break
            curr_id = row["parent_id"]
        crumbs.reverse()
        return crumbs

    def list_items(
        self,
        raw_key: str = "",
        share_token: str = "",
        parent_id: str | None = None,
        view: str = "folder",
        query: str = "",
    ) -> dict[str, Any]:
        """List items in a folder or smart view ('folder', 'recent', 'starred', 'shared', 'trash')."""
        if parent_id and parent_id != "root":
            grant = self.access(parent_id, raw_key, share_token, "view")
            parent_row = grant["item"]
            if parent_row["kind"] != "folder":
                raise ValueError("Target is not a folder.")
            rows = self._conn.execute(
                """
                SELECT * FROM items
                WHERE parent_id = ? AND trashed = 0
                ORDER BY CASE WHEN kind = 'folder' THEN 0 ELSE 1 END, LOWER(name) ASC
                """,
                (parent_id,),
            ).fetchall()
            items = [self._row_to_item(r, grant["role"]) for r in rows]
            return {
                "folder": self._row_to_item(parent_row, grant["role"]),
                "breadcrumbs": self.breadcrumbs(parent_id, raw_key, share_token),
                "role": grant["role"],
                "items": items,
            }

        # Root / smart views require an owned workspace identity
        ident = self.identity(raw_key)
        owner_id = ident["key_hash"]
        q_clean = query.strip().lower()

        if q_clean:
            rows = self._conn.execute(
                """
                SELECT * FROM items
                WHERE owner_id = ? AND trashed = 0 AND LOWER(name) LIKE ?
                ORDER BY CASE WHEN kind = 'folder' THEN 0 ELSE 1 END, updated DESC
                """,
                (owner_id, f"%{q_clean}%"),
            ).fetchall()
        elif view == "recent":
            rows = self._conn.execute(
                """
                SELECT * FROM items
                WHERE owner_id = ? AND trashed = 0 AND kind = 'file'
                ORDER BY updated DESC LIMIT 100
                """,
                (owner_id,),
            ).fetchall()
        elif view == "starred":
            rows = self._conn.execute(
                """
                SELECT * FROM items
                WHERE owner_id = ? AND trashed = 0 AND starred = 1
                ORDER BY CASE WHEN kind = 'folder' THEN 0 ELSE 1 END, LOWER(name) ASC
                """,
                (owner_id,),
            ).fetchall()
        elif view == "shared":
            rows = self._conn.execute(
                """
                SELECT DISTINCT i.* FROM items i
                JOIN share_links s ON s.item_id = i.id
                WHERE i.owner_id = ? AND i.trashed = 0
                ORDER BY CASE WHEN i.kind = 'folder' THEN 0 ELSE 1 END, i.updated DESC
                """,
                (owner_id,),
            ).fetchall()
        elif view == "trash":
            rows = self._conn.execute(
                """
                SELECT * FROM items
                WHERE owner_id = ? AND trashed = 1
                ORDER BY updated DESC
                """,
                (owner_id,),
            ).fetchall()
        else:
            rows = self._conn.execute(
                """
                SELECT * FROM items
                WHERE owner_id = ? AND parent_id IS NULL AND trashed = 0
                ORDER BY CASE WHEN kind = 'folder' THEN 0 ELSE 1 END, LOWER(name) ASC
                """,
                (owner_id,),
            ).fetchall()

        return {
            "folder": None,
            "breadcrumbs": [],
            "role": "owner",
            "items": [self._row_to_item(r, "owner") for r in rows],
        }

    def create_folder(
        self,
        name: str,
        parent_id: str | None = None,
        raw_key: str = "",
        share_token: str = "",
    ) -> dict[str, Any]:
        clean = _safe_name(name, "New Folder")
        if parent_id and parent_id != "root":
            grant = self.access(parent_id, raw_key, share_token, "edit")
            if grant["item"]["kind"] != "folder":
                raise ValueError("Cannot create folder inside a file.")
            owner_id = grant["item"]["owner_id"]
            actor = grant["identity"]
            actual_parent = parent_id
            role = grant["role"]
        else:
            ident = self.identity(raw_key)
            owner_id = ident["key_hash"]
            actor = ident
            actual_parent = None
            role = "owner"

        folder_id = secrets.token_urlsafe(10)
        now = _now()
        with self._tx() as conn:
            conn.execute(
                """
                INSERT INTO items (id, parent_id, owner_id, kind, name, created, updated)
                VALUES (?, ?, ?, 'folder', ?, ?, ?)
                """,
                (folder_id, actual_parent, owner_id, clean, now, now),
            )
            self._log_activity(conn, folder_id, actor, "created folder", clean)
            if actual_parent:
                self._log_activity(conn, actual_parent, actor, "added folder", clean)

        return self._row_to_item(self.get_raw_item(folder_id), role)

    def save_file(
        self,
        filename: str,
        content: bytes,
        parent_id: str | None = None,
        raw_key: str = "",
        share_token: str = "",
        suite_app: str = "",
        mime_type: str = "",
    ) -> dict[str, Any]:
        if len(content) > MAX_UPLOAD_BYTES:
            raise ValueError("File exceeds the 25 MB upload limit.")

        clean = _safe_name(filename, "Untitled")
        ext = _ext_of(clean)
        guessed_mime = mime_type or mimetypes.guess_type(clean)[0] or "application/octet-stream"
        digest = hashlib.sha256(content).hexdigest()

        if parent_id and parent_id != "root":
            grant = self.access(parent_id, raw_key, share_token, "edit")
            if grant["item"]["kind"] != "folder":
                raise ValueError("Upload target is not a folder.")
            owner_id = grant["item"]["owner_id"]
            actor = grant["identity"]
            actual_parent = parent_id
            role = grant["role"]
        else:
            ident = self.identity(raw_key)
            owner_id = ident["key_hash"]
            actor = ident
            actual_parent = None
            role = "owner"

        file_id = secrets.token_urlsafe(10)
        now = _now()
        with self._tx() as conn:
            conn.execute(
                """
                INSERT INTO items (
                    id, parent_id, owner_id, kind, name, ext, mime_type,
                    size_bytes, sha256, suite_app, created, updated
                ) VALUES (?, ?, ?, 'file', ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    file_id,
                    actual_parent,
                    owner_id,
                    clean,
                    ext,
                    guessed_mime,
                    len(content),
                    digest,
                    suite_app,
                    now,
                    now,
                ),
            )
            conn.execute(
                "INSERT INTO file_blobs (item_id, content) VALUES (?, ?)",
                (file_id, sqlite3.Binary(content)),
            )
            self._log_activity(conn, file_id, actor, "uploaded", clean)
            if actual_parent:
                self._log_activity(conn, actual_parent, actor, "uploaded file", clean)

        return self._row_to_item(self.get_raw_item(file_id), role)

    def get_file_bytes(
        self, item_id: str, raw_key: str = "", share_token: str = ""
    ) -> tuple[dict[str, Any], bytes]:
        grant = self.access(item_id, raw_key, share_token, "view")
        item = grant["item"]
        if item["kind"] != "file":
            raise ValueError("Cannot download a folder.")
        blob = self._conn.execute(
            "SELECT content FROM file_blobs WHERE item_id = ?", (item_id,)
        ).fetchone()
        if not blob:
            raise KeyError("File content missing.")
        return self._row_to_item(item, grant["role"]), bytes(blob["content"])

    def update_item(
        self,
        item_id: str,
        raw_key: str = "",
        share_token: str = "",
        name: str | None = None,
        parent_id: str | None = ...,
        starred: bool | None = None,
        trashed: bool | None = None,
    ) -> dict[str, Any]:
        grant = self.access(item_id, raw_key, share_token, "edit")
        item = grant["item"]
        actor = grant["identity"]
        updates: list[str] = []
        params: list[Any] = []
        actions: list[tuple[str, str]] = []

        if name is not None:
            new_name = _safe_name(name, item["name"])
            if new_name != item["name"]:
                updates.append("name = ?")
                params.append(new_name)
                if item["kind"] == "file":
                    updates.append("ext = ?")
                    params.append(_ext_of(new_name))
                actions.append(("renamed", f"{item['name']} -> {new_name}"))

        if parent_id is not ...:
            target_parent = None if (parent_id is None or parent_id == "root") else parent_id
            if target_parent == item_id:
                raise ValueError("Cannot move a folder into itself.")
            if target_parent is not None:
                p_grant = self.access(target_parent, raw_key, share_token, "edit")
                if p_grant["item"]["kind"] != "folder":
                    raise ValueError("Destination must be a folder.")
                # Cycle guard
                curr = target_parent
                for _ in range(32):
                    if curr == item_id:
                        raise ValueError("Cannot move a folder into its own subfolder.")
                    row = self._conn.execute(
                        "SELECT parent_id FROM items WHERE id = ?", (curr,)
                    ).fetchone()
                    if not row or not row["parent_id"]:
                        break
                    curr = row["parent_id"]
            else:
                # Moving to root requires owner
                if grant["role"] != "owner":
                    raise PermissionError("Only the workspace owner can move items to root.")
            updates.append("parent_id = ?")
            params.append(target_parent)
            actions.append(("moved", item["name"]))

        if starred is not None:
            updates.append("starred = ?")
            params.append(1 if starred else 0)

        if trashed is not None:
            updates.append("trashed = ?")
            params.append(1 if trashed else 0)
            actions.append(("trashed" if trashed else "restored", item["name"]))

        if updates:
            updates.append("updated = ?")
            params.append(_now())
            params.append(item_id)
            with self._tx() as conn:
                conn.execute(f"UPDATE items SET {', '.join(updates)} WHERE id = ?", params)
                for act, detail in actions:
                    self._log_activity(conn, item_id, actor, act, detail)

        return self._row_to_item(self.get_raw_item(item_id), grant["role"])

    def set_suite_link(
        self,
        item_id: str,
        suite_app: str,
        suite_doc_id: str,
        suite_url: str,
        suite_edit_token: str,
        suite_view_token: str,
    ) -> None:
        with self._tx() as conn:
            conn.execute(
                """
                UPDATE items
                SET suite_app = ?, suite_doc_id = ?, suite_url = ?,
                    suite_edit_token = ?, suite_view_token = ?, updated = ?
                WHERE id = ?
                """,
                (
                    suite_app,
                    suite_doc_id,
                    suite_url,
                    suite_edit_token,
                    suite_view_token,
                    _now(),
                    item_id,
                ),
            )

    def get_suite_link(self, item_id: str) -> dict[str, str]:
        row = self.get_raw_item(item_id)
        return {
            "suite_app": row["suite_app"],
            "suite_doc_id": row["suite_doc_id"],
            "suite_url": row["suite_url"],
            "suite_edit_token": row["suite_edit_token"],
            "suite_view_token": row["suite_view_token"],
        }

    def delete_forever(self, item_id: str, raw_key: str) -> None:
        self.access(item_id, raw_key, "", "owner")
        # Collect descendants for recursive cleanup
        to_delete = [item_id]
        queue = [item_id]
        while queue:
            curr = queue.pop(0)
            children = self._conn.execute(
                "SELECT id FROM items WHERE parent_id = ?", (curr,)
            ).fetchall()
            for child in children:
                to_delete.append(child["id"])
                queue.append(child["id"])
        with self._tx() as conn:
            for target in reversed(to_delete):
                conn.execute("DELETE FROM presence WHERE item_id = ?", (target,))
                conn.execute("DELETE FROM items WHERE id = ?", (target,))

    # --- Share links ---
    def links(self, item_id: str) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT id, token, label, role, created FROM share_links WHERE item_id = ? ORDER BY created DESC",
            (item_id,),
        ).fetchall()
        return [dict(r) for r in rows]

    def create_link(self, item_id: str, role: str, label: str = "Shared link") -> dict[str, Any]:
        if role not in ("view", "edit"):
            raise ValueError("Select view or edit.")
        count = self._conn.execute(
            "SELECT COUNT(*) AS c FROM share_links WHERE item_id = ?", (item_id,)
        ).fetchone()["c"]
        if count >= MAX_SHARES_PER_ITEM:
            raise ValueError(f"Maximum {MAX_SHARES_PER_ITEM} active share links per item.")
        link_id = secrets.token_urlsafe(8)
        token = secrets.token_urlsafe(24)
        clean_label = " ".join(str(label or "Shared link").split())[:80] or "Shared link"
        now = _now()
        with self._tx() as conn:
            conn.execute(
                "INSERT INTO share_links (id, item_id, token, label, role, created) VALUES (?, ?, ?, ?, ?, ?)",
                (link_id, item_id, token, clean_label, role, now),
            )
        return {"id": link_id, "token": token, "label": clean_label, "role": role, "created": now}

    def update_link(self, item_id: str, share_id: str, role: str | None) -> None:
        with self._tx() as conn:
            if role is None:
                conn.execute(
                    "DELETE FROM share_links WHERE id = ? AND item_id = ?", (share_id, item_id)
                )
            else:
                if role not in ("view", "edit"):
                    raise ValueError("Invalid permission.")
                conn.execute(
                    "UPDATE share_links SET role = ? WHERE id = ? AND item_id = ?",
                    (role, share_id, item_id),
                )

    # --- Presence ---
    def touch_presence(
        self,
        item_id: str,
        client_id: str,
        grant: dict[str, Any],
        cursor: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        ident = grant["identity"]
        now = _now()
        with self._tx() as conn:
            conn.execute("DELETE FROM presence WHERE updated < ?", (now - PRESENCE_TTL_SECONDS,))
            conn.execute(
                """
                INSERT INTO presence (client_id, item_id, identity, link_id, name, color, cursor_json, updated)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (client_id, item_id) DO UPDATE SET
                    identity = excluded.identity,
                    name = excluded.name,
                    color = excluded.color,
                    cursor_json = excluded.cursor_json,
                    updated = excluded.updated
                """,
                (
                    client_id,
                    item_id,
                    ident["key_hash"],
                    grant["link_id"],
                    ident["name"],
                    ident["color"],
                    json.dumps(cursor or {}),
                    now,
                ),
            )
        return self.peers(item_id)

    def peers(self, item_id: str) -> list[dict[str, Any]]:
        cutoff = _now() - PRESENCE_TTL_SECONDS
        rows = self._conn.execute(
            """
            SELECT client_id, name, color, cursor_json, updated
            FROM presence
            WHERE item_id = ? AND updated >= ?
            ORDER BY updated DESC
            """,
            (item_id, cutoff),
        ).fetchall()
        result = []
        for r in rows:
            try:
                cursor = json.loads(r["cursor_json"])
            except Exception:
                cursor = {}
            result.append(
                {
                    "client_id": r["client_id"],
                    "name": r["name"],
                    "color": r["color"],
                    "cursor": cursor,
                    "updated": r["updated"],
                }
            )
        return result

    # --- Comments ---
    def comments(self, item_id: str, raw_key: str = "", share_token: str = "") -> list[dict[str, Any]]:
        self.access(item_id, raw_key, share_token, "view")
        rows = self._conn.execute(
            "SELECT * FROM comments WHERE item_id = ? ORDER BY created ASC", (item_id,)
        ).fetchall()
        return [
            {
                "id": r["id"],
                "item_id": r["item_id"],
                "author_name": r["author_name"],
                "author_color": r["author_color"],
                "body": r["body"],
                "resolved": bool(r["resolved"]),
                "created": r["created"],
            }
            for r in rows
        ]

    def add_comment(
        self,
        item_id: str,
        raw_key: str = "",
        share_token: str = "",
        body: str = "",
    ) -> dict[str, Any]:
        grant = self.access(item_id, raw_key, share_token, "view")
        clean_body = " ".join(str(body or "").split())[:2000]
        if not clean_body:
            raise ValueError("Enter a comment.")
        actor = grant["identity"]
        cid = secrets.token_urlsafe(8)
        now = _now()
        with self._tx() as conn:
            conn.execute(
                """
                INSERT INTO comments (id, item_id, author_id, author_name, author_color, body, resolved, created)
                VALUES (?, ?, ?, ?, ?, ?, 0, ?)
                """,
                (cid, item_id, actor["key_hash"], actor["name"], actor["color"], clean_body, now),
            )
            self._log_activity(conn, item_id, actor, "commented", clean_body[:80])
        return {
            "id": cid,
            "item_id": item_id,
            "author_name": actor["name"],
            "author_color": actor["color"],
            "body": clean_body,
            "resolved": False,
            "created": now,
        }

    def change_comment(
        self,
        item_id: str,
        comment_id: str,
        raw_key: str = "",
        share_token: str = "",
        resolved: bool | None = None,
        delete: bool = False,
    ) -> None:
        grant = self.access(item_id, raw_key, share_token, "view")
        row = self._conn.execute(
            "SELECT * FROM comments WHERE id = ? AND item_id = ?", (comment_id, item_id)
        ).fetchone()
        if not row:
            raise KeyError("Comment not found.")
        is_author = row["author_id"] == grant["identity"]["key_hash"]
        if grant["role"] == "view" and not is_author:
            raise PermissionError("You can only modify your own comments.")
        with self._tx() as conn:
            if delete:
                conn.execute("DELETE FROM comments WHERE id = ?", (comment_id,))
            elif resolved is not None:
                conn.execute(
                    "UPDATE comments SET resolved = ? WHERE id = ?",
                    (1 if resolved else 0, comment_id),
                )

    # --- Activity ---
    def _log_activity(
        self,
        conn: sqlite3.Connection,
        item_id: str,
        actor: dict[str, str],
        action: str,
        detail: str = "",
    ) -> None:
        conn.execute(
            """
            INSERT INTO activity (id, item_id, actor_name, actor_color, action, detail, created)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                secrets.token_urlsafe(8),
                item_id,
                actor["name"],
                actor["color"],
                action,
                detail[:180],
                _now(),
            ),
        )

    def activity(self, item_id: str, raw_key: str = "", share_token: str = "") -> list[dict[str, Any]]:
        self.access(item_id, raw_key, share_token, "view")
        rows = self._conn.execute(
            "SELECT * FROM activity WHERE item_id = ? ORDER BY created DESC LIMIT 50",
            (item_id,),
        ).fetchall()
        return [dict(r) for r in rows]
