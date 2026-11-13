import json
import os
import secrets
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from functools import lru_cache
from urllib.parse import urlsplit
import pandas as pd
from flask import request, session, jsonify, Response
from plotly.offline import get_plotlyjs
from .store import Store
from .data import parse_file, pack, unpack, profile, relationships
from .connectors import fetch
from .dashboard import demo_plan, demo_tables, render
from .ai import generate

EXECUTOR = ThreadPoolExecutor(max_workers=4, thread_name_prefix='genie')
CAPACITY = threading.BoundedSemaphore(8)

GENIE_HTML = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Data Genie · PlotlyOffice</title>
  <link rel="stylesheet" href="assets/styles.css?v=2">
</head>
<body>
  <div id="genie-root"></div>
  <script src="assets/genie.js?v=2" defer></script>
</body>
</html>
"""

@lru_cache(maxsize=1)
def plotly_bundle(): return get_plotlyjs()

def configure(server, office_store=None):
    store = Store()
    server.secret_key = store.secret
    server.config.update(MAX_CONTENT_LENGTH=25*1024*1024, SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE='Lax')

    @server.route('/', methods=['GET'])
    def index():
        return Response(GENIE_HTML, mimetype='text/html; charset=utf-8')

    @server.before_request
    def protect():
        if not request.path.startswith('/api/'): return
        if request.method != 'GET':
            origin = request.headers.get('Origin')
            if origin and urlsplit(origin).netloc != request.host:
                return jsonify(error='Cross-origin requests are not allowed.'),403
            if request.headers.get('X-Genie') != '1': return jsonify(error='Missing request header.'),403
        if 'owner' not in session: session['owner'] = secrets.token_urlsafe(32)

    @server.after_request
    def privacy(response):
        # Windows installations can associate .js with text/plain. Keep nosniff
        # and explicitly identify application-owned static resources instead.
        if request.path.endswith('.js') and request.path.startswith(('/assets/','/api/')):
            response.headers['Content-Type'] = 'application/javascript; charset=utf-8'
        elif request.path.endswith('.css') and request.path.startswith('/assets/'):
            response.headers['Content-Type'] = 'text/css; charset=utf-8'
        if request.path.startswith('/api/') and not request.path.endswith('.js'):
            response.headers['Cache-Control'] = 'no-store'
        response.headers['X-Content-Type-Options'] = 'nosniff'
        return response

    def owner(): return session['owner']

    def resolve_office_identity(create_if_missing=False):
        if office_store is None:
            return None, None
        raw_key = request.headers.get('X-Office-Key', '').strip()
        if not raw_key:
            auth = request.headers.get('Authorization', '')
            if auth.startswith('Bearer '):
                raw_key = auth[7:].strip()
        if raw_key:
            try:
                return raw_key, office_store.identity(raw_key)
            except PermissionError:
                pass
        if create_if_missing:
            created = office_store.new_identity('Workspace User')
            raw_key = created['key']
            return raw_key, office_store.identity(raw_key)
        return None, None

    def folder_choices(owner_id):
        if office_store is None:
            return []
        rows = office_store._conn.execute(
            "SELECT id, parent_id, name FROM items WHERE owner_id = ? AND kind = 'folder' AND trashed = 0 ORDER BY name COLLATE NOCASE",
            (owner_id,),
        ).fetchall()
        by_id = {r['id']: {'id': r['id'], 'parent_id': r['parent_id'], 'name': r['name']} for r in rows}
        def path_of(fid):
            parts = []
            cur = fid
            seen = set()
            while cur and cur in by_id and cur not in seen:
                seen.add(cur)
                parts.append(by_id[cur]['name'])
                cur = by_id[cur]['parent_id']
            return ' / '.join(reversed(parts))
        out = [{'id': '', 'name': 'My Files (Root)', 'path': 'My Files'}]
        for fid in sorted(by_id, key=lambda k: path_of(k).lower()):
            out.append({'id': fid, 'name': by_id[fid]['name'], 'path': 'My Files / ' + path_of(fid)})
        return out
    def sources(who): return store.list(who,'source:')
    def tables(who): return {k.removeprefix('source:'):unpack(v['table']) for k,v in sources(who).items()}
    def add_sources(who, parsed, kind, fixed_id=None):
        existing = sources(who)
        size = sum(len(json.dumps(v)) for v in existing.values()) + sum(len(df.to_json()) for df in parsed.values())
        if size > 100*1024*1024: raise ValueError('Workspace limit is 100 MB of parsed data. Remove unused sources first.')
        for name, df in parsed.items():
            key = fixed_id or secrets.token_hex(6)
            store.put(who,'source:'+key,{'name':name,'kind':kind,'table':pack(df)})

    def launch(who, operation):
        if not CAPACITY.acquire(blocking=False): raise ValueError('The server is busy. Please retry shortly.')
        job = secrets.token_hex(16)
        def update(message): store.put(who,'job:'+job,{'status':'running','message':message,'started':started})
        started=time.time()
        try: update('Preparing your data…')
        except Exception:
            CAPACITY.release(); raise
        def run():
            try:
                result = operation(update)
                store.put(who,'job:'+job,{'status':'complete','message':'Ready','result':result,'started':started})
            except Exception as exc:
                message = str(exc) if isinstance(exc, ValueError) else 'The operation failed. Check the input format or endpoint and retry.'
                store.put(who,'job:'+job,{'status':'error','message':message,'started':started})
            finally: CAPACITY.release()
        EXECUTOR.submit(run)
        return {'job':job}

    def api(path, methods=['GET']):
        def decorator(fn):
            def wrapped(**kwargs):
                try: return jsonify(fn(**kwargs))
                except (ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
                    return jsonify(error=str(exc)[:400] or 'Invalid input.'),400
                except Exception:
                    return jsonify(error='Unable to process this input. Check its format and try again.'),500
            server.add_url_rule('/api/'+path,fn.__name__,wrapped,methods=methods)
            return fn
        return decorator

    @server.route('/api/plotly.js', methods=['GET'])
    def plotly_js():
        response=Response(plotly_bundle(),mimetype='application/javascript')
        response.headers['Cache-Control']='public,max-age=86400'
        return response

    @api('state')
    def state():
        src = sources(owner())
        return {'sources':[{'id':k.removeprefix('source:'),'kind':v['kind'],**profile(v['name'],unpack(v['table']))} for k,v in src.items()], 'connectors':[{'id':k.removeprefix('connector:'),'name':v['name'],'method':v['method'],'host':urlsplit(v['url']).hostname} for k,v in store.list(owner(),'connector:').items()], 'configured':bool(os.getenv('NVIDIA_API_KEY')), 'has_dashboard':store.get(owner(),'dashboard') is not None}

    def _index_feed_to_kg(owner_id, spec, df):
        if office_store is None or not owner_id:
            return None
        sample_csv = df.head(25).to_csv(index=False) if len(df) else ""
        return office_store.kg.index_api_feed(
            owner_id=owner_id,
            feed_name=spec.get('name', 'API Feed'),
            url=spec.get('url', ''),
            method=spec.get('method', 'GET'),
            columns=[str(c) for c in df.columns],
            sample_text=sample_csv,
            row_count=len(df),
        )

    @api('office/context')
    def office_context():
        raw_key, ident = resolve_office_identity(create_if_missing=True)
        if not ident:
            return {'enabled': False, 'folders': [], 'documents': []}
        owner_id = ident['key_hash']
        q = (request.args.get('q') or '').strip()
        g = office_store.kg.query(owner_id, q, limit=30) if q else office_store.kg.graph(owner_id, limit=160)
        concepts_by_doc = {}
        for edge in g.get('edges', []):
            if edge.get('rel') == 'MENTIONS_CONCEPT' and edge.get('dst', '').startswith('concept:'):
                concepts_by_doc.setdefault(edge['src'], []).append(edge['dst'].replace('concept:', ''))
        docs = []
        for node in g.get('nodes', []):
            if node.get('kind') == 'Document':
                meta = node.get('meta') or {}
                item_id = meta.get('item_id') or node['id'].removeprefix('doc:')
                docs.append({
                    'id': item_id,
                    'node_id': node['id'],
                    'name': node['label'],
                    'ext': meta.get('ext', ''),
                    'suite_app': meta.get('suite_app', ''),
                    'snippet': meta.get('snippet', ''),
                    'concepts': concepts_by_doc.get(node['id'], [])[:6],
                })
        return {
            'enabled': True,
            'office_key': raw_key,
            'folders': folder_choices(owner_id),
            'documents': docs,
            'stats': g.get('stats', {}),
        }

    @api('office/folders', ['POST'])
    def office_create_folder():
        raw_key, ident = resolve_office_identity(create_if_missing=True)
        if not ident:
            raise ValueError('PlotlyOffice integration is not enabled.')
        body = request.get_json() or {}
        name = (body.get('name') or '').strip()
        if not name:
            raise ValueError('Folder name is required.')
        parent_id = (body.get('parent_id') or '').strip() or None
        folder = office_store.create_folder(name=name, parent_id=parent_id, raw_key=raw_key)
        return {'folder': folder, 'folders': folder_choices(ident['key_hash'])}

    @api('office/kg-import', ['POST'])
    def office_kg_import():
        raw_key, ident = resolve_office_identity(create_if_missing=False)
        if not ident:
            raise ValueError('Workspace key required to load from Knowledge Graph.')
        body = request.get_json() or {}
        item_ids = body.get('item_ids') or ([] if not body.get('item_id') else [body.get('item_id')])
        if not item_ids:
            raise ValueError('Select at least one Knowledge Graph document.')
        from plotlyoffice.kg import extract_file_text
        parsed = {}
        loaded_names = []
        for item_id in item_ids:
            item, raw = office_store.get_file_bytes(str(item_id), raw_key=raw_key)
            try:
                tables_dict = parse_file(item['name'], raw)
                parsed.update(tables_dict)
            except ValueError:
                extracted = extract_file_text(item['name'], item.get('ext', ''), raw)
                lines = [ln.strip() for ln in (extracted.get('text') or '').splitlines() if ln.strip()]
                if not lines:
                    lines = [f"(No extractable text in {item['name']})"]
                df = pd.DataFrame({
                    'document': [item['name']] * len(lines),
                    'line_no': list(range(1, len(lines) + 1)),
                    'content': lines,
                })
                parsed[item['name']] = df
            loaded_names.append(item['name'])
        add_sources(owner(), parsed, 'kg')
        return {'added': len(parsed), 'documents': loaded_names}

    @api('upload',['POST'])
    def upload():
        raw_key, ident = resolve_office_identity(create_if_missing=True)
        save_to_office = request.form.get('save_to_office', '1') != '0'
        folder_id = (request.form.get('office_folder_id') or '').strip() or None
        parsed={}
        office_saved=[]
        for file in request.files.getlist('files'):
            fname = file.filename or 'data.csv'
            raw_bytes = file.read()
            parsed.update(parse_file(fname, raw_bytes))
            if office_store is not None and save_to_office and raw_key:
                saved_item = office_store.save_file(fname, raw_bytes, parent_id=folder_id, raw_key=raw_key)
                office_saved.append(saved_item['name'])
        if not parsed: raise ValueError('Choose at least one supported file.')
        add_sources(owner(),parsed,'file')
        return {'added':len(parsed), 'office_saved': office_saved, 'office_key': raw_key or ''}

    @api('paste',['POST'])
    def paste():
        raw_key, ident = resolve_office_identity(create_if_missing=True)
        body=request.get_json() or {}
        name = body.get('name') or 'pasted.csv'
        raw_bytes = body.get('text','').encode()
        add_sources(owner(),parse_file(name,raw_bytes),'paste')
        office_saved = []
        if office_store is not None and body.get('save_to_office', True) and raw_key:
            folder_id = (body.get('office_folder_id') or '').strip() or None
            saved_item = office_store.save_file(name, raw_bytes, parent_id=folder_id, raw_key=raw_key)
            office_saved.append(saved_item['name'])
        return {'ok':True, 'office_saved': office_saved, 'office_key': raw_key or ''}

    @api('sources/<sid>',['DELETE'])
    def remove_source(sid):
        store.delete(owner(),'source:'+sid)
        return {'ok':True}

    @api('sources/<sid>')
    def preview(sid):
        item=store.get(owner(),'source:'+sid)
        if not item: raise ValueError('Source has expired or was removed.')
        df=unpack(item['table'])
        return {'name':item['name'],'columns':list(df.columns),'rows':json.loads(df.head(30).to_json(orient='records'))}

    @api('connectors',['POST'])
    def connector():
        spec=request.get_json()
        if not spec.get('name') or not spec.get('url'): raise ValueError('Give this connection a name and URL.')
        cid=secrets.token_hex(6); who=owner()
        _, ident = resolve_office_identity(create_if_missing=True)
        office_owner_id = ident['key_hash'] if ident else None
        def work(progress):
            progress('Connecting and reading response…')
            df=fetch(spec)
            add_sources(who,{spec['name']:df},'api',cid)
            store.put(who,'connector:'+cid,spec)
            kg_info = _index_feed_to_kg(office_owner_id, spec, df)
            return {'kind':'source','rows':len(df),'kg_indexed':bool(kg_info)}
        return launch(who,work)

    @api('connectors/<cid>/refresh',['POST'])
    def refresh(cid):
        spec=store.get(owner(),'connector:'+cid)
        if not spec: raise ValueError('Connection has expired or was removed.')
        who=owner()
        _, ident = resolve_office_identity(create_if_missing=True)
        office_owner_id = ident['key_hash'] if ident else None
        def work(progress):
            progress('Refreshing the API source…')
            df=fetch(spec); add_sources(who,{spec['name']:df},'api',cid)
            kg_info = _index_feed_to_kg(office_owner_id, spec, df)
            return {'kind':'source','rows':len(df),'kg_indexed':bool(kg_info)}
        return launch(who,work)

    @api('connectors/<cid>',['DELETE'])
    def remove_connector(cid):
        store.delete(owner(),'connector:'+cid); store.delete(owner(),'source:'+cid)
        return {'ok':True}

    @api('relationships')
    def links(): return {'relationships':relationships(tables(owner()))}

    @api('demo',['POST'])
    def demo():
        data=demo_tables()
        for name,df in data.items(): store.put(owner(),'source:'+name,{'name':name,'kind':'sample','table':pack(df)})
        dashboard={'plan':demo_plan(),'tables':{k:pack(v) for k,v in data.items()},'mode':'Sample workspace'}
        store.put(owner(),'dashboard',dashboard)
        return {'ok':True}

    @api('generate',['POST'])
    def create_dashboard():
        who=owner(); source_items=sources(who); body=request.get_json()
        data={k.removeprefix('source:'):unpack(v['table']) for k,v in source_items.items()}
        names={k.removeprefix('source:'):v['name'] for k,v in source_items.items()}
        if not data: raise ValueError('Add a data source first, or explore the sample workspace.')
        if not os.getenv('NVIDIA_API_KEY'): raise ValueError('Set NVIDIA_API_KEY in the server environment and restart to enable AI generation.')
        def work(progress):
            plan=generate(data,body.get('prompt',''),bool(body.get('samples')),progress,names)
            progress('Checking measures and rendering charts…')
            render(plan,data)
            store.put(who,'dashboard',{'plan':plan,'tables':{k:pack(v) for k,v in data.items()},'mode':'Generated with MiniMax'})
            return {'kind':'dashboard'}
        return launch(who,work)

    @api('jobs/<job>')
    def job_state(job):
        job=store.get(owner(),'job:'+job)
        if not job: raise ValueError('This job is no longer available.')
        if job['status']=='running' and time.time()-job['started']>600:
            return {'status':'error','message':'The job timed out or the server restarted. Please retry.'}
        return job

    @api('dashboard',['POST'])
    def dashboard():
        saved=store.get(owner(),'dashboard')
        if not saved: return {'empty':True}
        output=render(saved['plan'],{k:unpack(v) for k,v in saved['tables'].items()},request.get_json(silent=True) or {})
        return {**output,'mode':saved['mode']}

    @api('export')
    def export():
        saved=store.get(owner(),'dashboard')
        if not saved: raise ValueError('Generate a dashboard first.')
        return {'plan':saved['plan'],'dashboard':render(saved['plan'],{k:unpack(v) for k,v in saved['tables'].items()})}

    @api('clear',['POST'])
    def clear():
        for key in store.list(owner(),''): store.delete(owner(),key)
        # A running worker writes only to the old owner; the browser gets a fresh workspace.
        session['owner']=secrets.token_urlsafe(32)
        return {'ok':True}
