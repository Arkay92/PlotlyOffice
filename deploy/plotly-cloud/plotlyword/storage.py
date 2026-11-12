"""Transactional document storage with optimistic concurrency and version history."""
import json
import sqlite3
import hashlib
import secrets
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4
from .merge import merge_text, merge_value


class ConflictError(Exception):
    pass


class Store:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS documents (
                    id TEXT PRIMARY KEY, title TEXT NOT NULL, html TEXT NOT NULL,
                    settings TEXT NOT NULL, version INTEGER NOT NULL, updated TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS revisions (
                    document_id TEXT NOT NULL, version INTEGER NOT NULL, title TEXT NOT NULL,
                    html TEXT NOT NULL, settings TEXT NOT NULL, updated TEXT NOT NULL,
                    PRIMARY KEY(document_id, version));
                CREATE TABLE IF NOT EXISTS identities (
                    key_hash TEXT PRIMARY KEY, name TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS share_links (
                    id TEXT PRIMARY KEY, document_id TEXT NOT NULL, token TEXT NOT NULL UNIQUE,
                    label TEXT NOT NULL, role TEXT NOT NULL, created TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS links_document ON share_links(document_id);
                CREATE TABLE IF NOT EXISTS presence (
                    client_id TEXT PRIMARY KEY, document_id TEXT NOT NULL, identity TEXT NOT NULL,
                    link_id TEXT, name TEXT NOT NULL, cursor TEXT NOT NULL, updated REAL NOT NULL);
                CREATE INDEX IF NOT EXISTS presence_document ON presence(document_id);
                CREATE TABLE IF NOT EXISTS comments (
                    id TEXT PRIMARY KEY, document_id TEXT NOT NULL, author_id TEXT NOT NULL,
                    author_name TEXT NOT NULL, color TEXT NOT NULL, body TEXT NOT NULL,
                    quote TEXT NOT NULL, anchor INTEGER NOT NULL, resolved INTEGER NOT NULL DEFAULT 0,
                    created TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS comments_document ON comments(document_id);
            ''')
            db.execute('BEGIN IMMEDIATE')
            if 'owner_id' not in {r['name'] for r in db.execute('PRAGMA table_info(documents)')}:
                db.execute("ALTER TABLE documents ADD COLUMN owner_id TEXT NOT NULL DEFAULT 'legacy'")
            revision_columns={r['name'] for r in db.execute('PRAGMA table_info(revisions)')}
            for field,default in [('author_name','Unknown (older version)'),('author_color','#647568')]:
                if field not in revision_columns:
                    db.execute(f"ALTER TABLE revisions ADD COLUMN {field} TEXT NOT NULL DEFAULT '{default}'")

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
            raise KeyError('Document not found.')
        result = dict(row)
        if 'settings' in result:
            result['settings'] = json.loads(result['settings'])
        return result

    def list(self, owner=None):
        with self.connect() as db:
            query='SELECT id,title,version,updated FROM documents'
            return [dict(r) for r in db.execute(query + (' WHERE owner_id=?' if owner else '') + ' ORDER BY updated DESC', (owner,) if owner else ())]

    def get(self, doc_id):
        with self.connect() as db:
            return self.decode(db.execute('SELECT * FROM documents WHERE id=?', (doc_id,)).fetchone())

    def save(self, title, html, settings, doc_id=None, version=None, owner='legacy', base=None, credentials=None):
        now = datetime.now(timezone.utc).isoformat()
        encoded = json.dumps(settings)
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            author=None
            if doc_id:
                if credentials is not None:
                    author=self.access(doc_id,*credentials,require='edit')
                old = db.execute('SELECT * FROM documents WHERE id=?', (doc_id,)).fetchone()
                if old is None:
                    raise KeyError('Document not found.')
                if base is not None:
                    from .formats import sanitize
                    html = sanitize(merge_text(base['html'],old['html'],html))
                    title = merge_value(base['title'],old['title'],title,'the document title')
                    settings = {key:merge_value(base['settings'].get(key),json.loads(old['settings']).get(key),value,'page '+key) for key,value in settings.items()}
                    encoded=json.dumps(settings)
                elif version != old['version']:
                    raise ConflictError('This document changed in another tab. Export your edits, then reload to continue.')
                next_version = old['version'] + 1
                if (title,html,encoded) == (old['title'],old['html'],old['settings']):
                    return self.decode(old)
                db.execute('UPDATE documents SET title=?,html=?,settings=?,version=?,updated=? WHERE id=?',
                           (title, html, encoded, next_version, now, doc_id))
            else:
                author=db.execute('SELECT * FROM identities WHERE key_hash=?',(owner,)).fetchone()
                doc_id, next_version = uuid4().hex, 1
                db.execute('INSERT INTO documents (id,title,html,settings,version,updated,owner_id) VALUES (?,?,?,?,?,?,?)',
                           (doc_id, title, html, encoded, next_version, now, owner))
            db.execute('INSERT INTO revisions (document_id,version,title,html,settings,updated,author_name,author_color) VALUES (?,?,?,?,?,?,?,?)',
                       (doc_id,next_version,title,html,encoded,now,author['name'] if author else 'Unknown (older version)',self.user_color(author['key_hash']) if author else '#647568'))
            # Keep the last 50 revisions for each document.
            db.execute('DELETE FROM revisions WHERE document_id=? AND version<=?',
                       (doc_id, next_version - 50))
        return self.get(doc_id)

    def history(self, doc_id):
        self.get(doc_id)
        with self.connect() as db:
            return [dict(r) for r in db.execute(
                'SELECT version,title,updated,author_name,author_color FROM revisions WHERE document_id=? ORDER BY version DESC', (doc_id,))]

    def revision(self, doc_id, version):
        with self.connect() as db:
            return self.decode(db.execute('SELECT * FROM revisions WHERE document_id=? AND version=?',
                                         (doc_id, version)).fetchone())

    def delete(self, doc_id):
        with self.connect() as db:
            db.execute('DELETE FROM comments WHERE document_id=?', (doc_id,))
            db.execute('DELETE FROM share_links WHERE document_id=?', (doc_id,))
            db.execute('DELETE FROM presence WHERE document_id=?', (doc_id,))
            db.execute('DELETE FROM revisions WHERE document_id=?', (doc_id,))
            db.execute('DELETE FROM documents WHERE id=?', (doc_id,))

    @staticmethod
    def key_hash(key):
        return hashlib.sha256(key.encode()).hexdigest() if isinstance(key,str) and 20 <= len(key) <= 200 else ''

    def identity(self, key):
        with self.connect() as db:
            row=db.execute('SELECT * FROM identities WHERE key_hash=?',(self.key_hash(key),)).fetchone()
            if row is None:
                raise PermissionError('Open your workspace first, or restore it with your workspace key.')
            return dict(row)

    def new_identity(self, name='Guest'):
        key=secrets.token_urlsafe(32)
        with self.connect() as db:
            db.execute('INSERT INTO identities VALUES (?,?)',(self.key_hash(key),name))
        return {'key':key,'name':name}

    def rename_identity(self, key, name):
        identity=self.identity(key)
        name=' '.join(str(name).split())[:40] or 'Guest'
        with self.connect() as db:
            db.execute('UPDATE identities SET name=? WHERE key_hash=?',(name,identity['key_hash']))
        return {'name':name}

    def access(self, doc_id, key, link=None, require='view'):
        identity=self.identity(key)
        with self.connect() as db:
            doc=db.execute('SELECT owner_id,version FROM documents WHERE id=?',(doc_id,)).fetchone()
            if doc is None: raise KeyError(doc_id)
            grant=None
            if doc['owner_id'] == identity['key_hash']:
                role='owner'
            else:
                if isinstance(link,str) and 20 <= len(link) <= 200:
                    grant=db.execute('SELECT id,role FROM share_links WHERE document_id=? AND token=?',(doc_id,link)).fetchone()
                if not grant:
                    raise PermissionError('This document is private or its share link has been revoked.')
                role=grant['role']
            if {'view':0,'edit':1,'owner':2}[role] < {'view':0,'edit':1,'owner':2}[require]:
                raise PermissionError('You do not have permission to make this change.')
            return {**identity,'role':role,'version':doc['version'],'link_id':grant['id'] if grant else None}

    def links(self, doc_id):
        with self.connect() as db:
            return [dict(r) for r in db.execute('SELECT * FROM share_links WHERE document_id=? ORDER BY created',(doc_id,))]

    def create_link(self, doc_id, role, label):
        row={'id':uuid4().hex,'document_id':doc_id,'token':secrets.token_urlsafe(32),'label':label,'role':role,'created':datetime.now(timezone.utc).isoformat()}
        with self.connect() as db:
            if db.execute('SELECT count(*) FROM share_links WHERE document_id=?',(doc_id,)).fetchone()[0]>=30:
                raise ValueError('A document can have up to 30 active links. Revoke an old link first.')
            db.execute('INSERT INTO share_links VALUES (:id,:document_id,:token,:label,:role,:created)',row)
        return row

    def update_link(self, doc_id, link_id, role):
        with self.connect() as db:
            if role is None:
                db.execute('DELETE FROM share_links WHERE document_id=? AND id=?',(doc_id,link_id))
                db.execute('DELETE FROM presence WHERE document_id=? AND link_id=?',(doc_id,link_id))
            else:
                db.execute('UPDATE share_links SET role=? WHERE document_id=? AND id=?',(role,doc_id,link_id))

    def touch_presence(self, doc_id, client_id, access, cursor=None):
        now=time.time()
        with self.connect() as db:
            db.execute('DELETE FROM presence WHERE updated<?',(now-15,))
            existing=db.execute('SELECT identity FROM presence WHERE client_id=?',(client_id,)).fetchone()
            if existing and existing['identity'] != access['key_hash']:
                raise PermissionError('This cursor belongs to another session.')
            old=db.execute('SELECT cursor FROM presence WHERE client_id=? AND document_id=?',(client_id,doc_id)).fetchone()
            value=json.dumps(cursor) if cursor is not None else (old['cursor'] if old else '{}')
            db.execute('INSERT OR REPLACE INTO presence VALUES (?,?,?,?,?,?,?)',
                       (client_id,doc_id,access['key_hash'],access['link_id'],access['name'],value,now))

    def peers(self, doc_id):
        with self.connect() as db:
            rows=db.execute('''SELECT p.* FROM presence p LEFT JOIN share_links l ON p.link_id=l.id
                WHERE p.document_id=? AND p.updated>? AND (p.link_id IS NULL OR l.id IS NOT NULL)''',(doc_id,time.time()-12)).fetchall()
            return [{'id':r['client_id'],'name':r['name'],'color':self.user_color(r['identity']),'cursor':json.loads(r['cursor'])} for r in rows]

    def leave(self, client_id, identity):
        with self.connect() as db:
            db.execute('DELETE FROM presence WHERE client_id=? AND identity=?',(client_id,identity))


    @staticmethod
    def user_color(identity):
        colors=['#397b6b','#a05d37','#6f64ad','#ba5977','#387cad','#8b8538']
        return colors[int(identity[:8],16)%len(colors)]

    def comments(self,doc_id,key,link=None):
        grant=self.access(doc_id,key,link)
        with self.connect() as db:
            rows=db.execute('SELECT * FROM comments WHERE document_id=? ORDER BY created',(doc_id,)).fetchall()
            return [{k:r[k] for k in r.keys() if k!='author_id'} | {
                'can_delete':grant['role'] in ('owner','edit') and (grant['role']=='owner' or r['author_id']==grant['key_hash'])
            } for r in rows]

    def add_comment(self,doc_id,key,link,body,quote='',anchor=0):
        if not isinstance(body,str) or not body.strip() or len(body)>3000:
            raise ValueError('Enter a comment between 1 and 3,000 characters.')
        if not isinstance(quote,str) or len(quote)>2000:
            raise ValueError('Select up to 2,000 characters to comment on.')
        if not isinstance(anchor,int) or isinstance(anchor,bool) or not 0<=anchor<=40*1024*1024:
            raise ValueError('Invalid comment position.')
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            grant=self.access(doc_id,key,link,'edit')
            if db.execute('SELECT count(*) FROM comments WHERE document_id=?',(doc_id,)).fetchone()[0]>=500:
                raise ValueError('This document has reached its 500-comment limit.')
            comment_id=uuid4().hex
            db.execute('INSERT INTO comments VALUES (?,?,?,?,?,?,?,?,?,?)',(
                comment_id,doc_id,grant['key_hash'],grant['name'],self.user_color(grant['key_hash']),
                body.strip(),quote,anchor,0,datetime.now(timezone.utc).isoformat()))
        return comment_id

    def change_comment(self,doc_id,comment_id,key,link,resolved=None,delete=False):
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            grant=self.access(doc_id,key,link,'edit')
            row=db.execute('SELECT * FROM comments WHERE document_id=? AND id=?',(doc_id,comment_id)).fetchone()
            if row is None:raise KeyError('Comment not found.')
            if delete:
                if grant['role']!='owner' and row['author_id']!=grant['key_hash']:
                    raise PermissionError('Only the comment author or document owner can delete it.')
                db.execute('DELETE FROM comments WHERE id=?',(comment_id,))
            else:
                if not isinstance(resolved,bool):raise ValueError('Choose resolved or open.')
                db.execute('UPDATE comments SET resolved=? WHERE id=?',(int(resolved),comment_id))
