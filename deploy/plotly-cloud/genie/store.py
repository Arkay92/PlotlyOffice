"""SQLite-backed, expiring per-browser workspace shared across local WSGI workers."""
import json
import os
import secrets
import sqlite3
import time
import zlib
from pathlib import Path

class Store:
    def __init__(self):
        root = Path(os.getenv('GENIE_STORAGE_DIR', '.runtime')).resolve()
        root.mkdir(parents=True, exist_ok=True)
        self.path = root / 'genie.sqlite3'
        with self.connect() as db:
            db.execute('PRAGMA journal_mode=WAL')
            db.execute('CREATE TABLE IF NOT EXISTS items (owner TEXT, key TEXT, value BLOB, updated REAL, PRIMARY KEY(owner,key))')
            db.execute('CREATE TABLE IF NOT EXISTS config (key TEXT PRIMARY KEY, value TEXT)')
            db.execute('INSERT OR IGNORE INTO config VALUES (?,?)', ('secret', secrets.token_hex(32)))
            self.secret = db.execute('SELECT value FROM config WHERE key=?', ('secret',)).fetchone()[0]

    def connect(self):
        return sqlite3.connect(self.path, timeout=15)

    def put(self, owner, key, value):
        with self.connect() as db:
            db.execute('DELETE FROM items WHERE updated < ?', (time.time()-86400,))
            db.execute('INSERT OR REPLACE INTO items VALUES (?,?,?,?)', (owner, key, zlib.compress(json.dumps(value).encode()), time.time()))

    def get(self, owner, key, default=None):
        with self.connect() as db:
            row = db.execute('SELECT value FROM items WHERE owner=? AND key=? AND updated>?', (owner, key, time.time()-86400)).fetchone()
        return json.loads(zlib.decompress(row[0])) if row else default

    def delete(self, owner, key):
        with self.connect() as db:
            db.execute('DELETE FROM items WHERE owner=? AND key=?', (owner, key))

    def list(self, owner, prefix):
        with self.connect() as db:
            rows = db.execute('SELECT key,value FROM items WHERE owner=? AND key LIKE ? AND updated>?', (owner, prefix+'%', time.time()-86400)).fetchall()
        return {k:json.loads(zlib.decompress(v)) for k,v in rows}
