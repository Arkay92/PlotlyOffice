"""Permission-checked API. Reuses PlotlyWord's workspace, sharing and review store."""
import json
import gzip
import base64
import binascii
import re
import secrets
import time
from pathlib import Path
from urllib.parse import urlsplit
from flask import jsonify, request, Response
from werkzeug.exceptions import HTTPException
from .storage import Store, ConflictError
from .merge import MergeConflict
from .formats import checked, import_pptx, MAX_BYTES
from .release import asset_version, release_id


def configure(server,database):
    store=Store(database)
    build=release_id()
    assets=Path(__file__).resolve().parents[1]/'apps'/'slides'/'assets'
    studio_html=(assets/'studio.html').read_text(encoding='utf-8')
    studio_html=re.sub(r'((?:src|href)="/assets/)([^"?]+)(")',
                       lambda m:m[1]+m[2]+'?v='+asset_version(m[2])+m[3],studio_html)
    studio_html=studio_html.replace('</head>',f'<meta name="plotlyslides-build" content="{build}"></head>')
    server.config['MAX_CONTENT_LENGTH']=45*1024*1024
    with store.connect() as db:
        db.execute('CREATE TABLE IF NOT EXISTS broadcasts (document_id TEXT PRIMARY KEY, controller TEXT NOT NULL, state TEXT NOT NULL)')

    @server.before_request
    def origin_check():
        if request.path.startswith('/api/') and request.method not in ('GET','HEAD','OPTIONS'):
            origin=request.headers.get('Origin')
            if (origin and urlsplit(origin).netloc!=request.host) or request.headers.get('Sec-Fetch-Site')=='cross-site':
                return jsonify(error='Cross-origin changes are not allowed.'),403

    @server.after_request
    def headers(response):
        response.headers['Referrer-Policy']='no-referrer'
        response.headers['X-Content-Type-Options']='nosniff'
        # Windows registry MIME overrides can label JavaScript as text/plain.
        if response.status_code==200 and request.path.startswith(('/assets/','/_dash-component-suites/')):
            if request.path.endswith('.js'):response.mimetype='application/javascript'
            elif request.path.endswith('.css'):response.mimetype='text/css'
        if request.path.startswith('/api/') or request.path in ('/','/studio','/_dash-layout','/_dash-dependencies'):
            response.headers['Cache-Control']='no-store, max-age=0'
        if request.path.startswith('/assets/') and not request.args.get('v'):
            response.headers['Cache-Control']='no-cache, max-age=0, must-revalidate'
        response.headers['X-PlotlySlides-Build']=build
        # Opt-in wire format: send each embedded image once, including across slides.
        # The stored model and downloaded JSON remain self-contained and unchanged.
        if (request.headers.get('X-PlotlySlides-Packed')=='1' and response.is_json
                and response.status_code in (200,201) and response.content_length
                and response.content_length>100000):
            data=response.get_json();images=[];seen={}
            def pack(value):
                if isinstance(value,list):
                    for item in value:pack(item)
                elif isinstance(value,dict):
                    src=value.get('src')
                    if value.get('type')=='image' and isinstance(src,str) and src.startswith('data:image/'):
                        if src not in seen:seen[src]=len(images);images.append(src)
                        value['src']='@image:'+str(seen[src])
                    for item in value.values():pack(item)
            pack(data)
            if images:
                response.set_data(json.dumps({'data':data,'images':images}))
                response.headers['X-PlotlySlides-Packed']='1'
        if (request.path.startswith('/api/') and response.is_json
                and response.status_code in (200,201) and not response.headers.get('Content-Encoding')
                and request.accept_encodings['gzip']>0):
            raw=response.get_data()
            if len(raw)>4096:
                response.set_data(gzip.compress(raw,compresslevel=5))
                response.headers['Content-Encoding']='gzip'
                response.vary.add('Accept-Encoding')
        return response

    @server.errorhandler(Exception)
    def error(exc):
        if not request.path.startswith('/api/'):
            if isinstance(exc,HTTPException):return exc
            server.logger.exception('Application error'); return 'Application error',500
        if isinstance(exc,(ConflictError,MergeConflict)):return jsonify(error=str(exc)),409
        if isinstance(exc,PermissionError):return jsonify(error=str(exc)),403
        if isinstance(exc,KeyError):return jsonify(error='Presentation not found.'),404
        if isinstance(exc,ValueError):return jsonify(error=str(exc)),400
        if isinstance(exc,HTTPException):return jsonify(error=exc.description),exc.code
        server.logger.exception('Presentation operation failed')
        return jsonify(error='The presentation could not be processed.'),422

    def key():
        header=request.headers.get('Authorization','')
        return header[7:] if header.startswith('Bearer ') else ''
    def link():return request.headers.get('X-Document-Key')
    def access(d,require='view'):return store.access(d,key(),link(),require)
    def payload():
        value=request.get_json()
        if not isinstance(value,dict):raise ValueError('Expected a JSON object.')
        if request.headers.get('X-PlotlySlides-Packed')=='1' and isinstance(value.get('data'),dict) and isinstance(value.get('images'),list):
            images=value['images']
            def unpack(node):
                if isinstance(node,list):
                    for item in node:unpack(item)
                elif isinstance(node,dict):
                    src=node.get('src')
                    if node.get('type')=='image' and isinstance(src,str) and src.startswith('@image:'):
                        index=src[7:]
                        if not index.isascii() or not index.isdigit() or len(index)>8 or int(index)>=len(images):raise ValueError('Invalid image reference.')
                        node['src']=images[int(index)]
                    for item in node.values():unpack(item)
            unpack(value['data']);value=value['data']
        return value
    def fields(d):
        if not isinstance(d.get('title'),str):raise ValueError('Enter a title.')
        return d['title'].strip()[:180] or 'Untitled presentation','',checked(d.get('settings'))
    def present(d,role):return {k:v for k,v in d.items() if k!='owner_id'}|{'role':role}

    @server.route('/studio', methods=["GET"])
    def studio():return Response(studio_html,mimetype='text/html')

    @server.route('/api/health', methods=["GET"])
    def health():return jsonify(ok=True,build=build,import_transport='json-base64')

    @server.route('/api/session', methods=["POST"])
    def session():
        name=payload().get('name','')
        if not isinstance(name,str) or not name.strip():raise ValueError('Enter your display name to continue.')
        name=' '.join(name.split())[:40]
        if key():
            try:
                store.rename_identity(key(),name)
            except PermissionError:
                # Cloud ingress replaces 403 bodies with HTML. This is only an
                # identity probe; document endpoints still enforce permissions.
                return jsonify(valid=False, reason='workspace_unavailable')
            return jsonify(valid=True, name=name)
        return jsonify(valid=True, **store.new_identity(name))

    @server.route('/api/decks', methods=["GET"])
    def decks():return jsonify(store.list(store.identity(key())['key_hash']))
    @server.route('/api/decks', methods=["POST"])
    def create():return jsonify(present(store.save(*fields(payload()),owner=store.identity(key())['key_hash']),'owner')),201
    @server.route('/api/decks/<d>', methods=["GET"])
    def get(d):
        role=access(d)['role']
        document=store.get(d)
        if request.args.get('version')==str(document['version']):
            return jsonify(unchanged=True,version=document['version'],role=role)
        return jsonify(present(document,role))
    @server.route('/api/decks/<d>', methods=["PUT"])
    def save(d):
        grant=access(d,'edit');data=payload();base=None
        if data.get('base') is not None:
            title,markup,settings=fields(data['base']);base={'title':title,'html':markup,'settings':settings}
        result=store.save(*fields(data),doc_id=d,version=data.get('version'),base=base,credentials=(key(),link()))
        return jsonify(present(result,grant['role']))
    @server.route('/api/decks/<d>', methods=["DELETE"])
    def delete(d):
        access(d,'owner');store.delete(d)
        with store.connect() as db:db.execute('DELETE FROM broadcasts WHERE document_id=?',(d,))
        return '',204
    @server.route('/api/decks/<d>/shares',methods=['GET','POST'])
    def shares(d):
        access(d,'owner')
        if request.method=='GET':return jsonify(store.links(d))
        data=payload();role=data.get('role')
        if role not in ('view','edit'):raise ValueError('Select view or edit.')
        return jsonify(store.create_link(d,role,str(data.get('label','Shared link'))[:100]))
    @server.route('/api/decks/<d>/shares/<sid>',methods=['PUT','DELETE'])
    def share(d,sid):
        access(d,'owner');role=None if request.method=='DELETE' else payload().get('role')
        if request.method=='PUT' and role not in ('view','edit'):raise ValueError('Invalid permission.')
        store.update_link(d,sid,role);return jsonify(ok=True)
    @server.route('/api/decks/<d>/history', methods=["GET"])
    def history(d):access(d,'edit');return jsonify(store.history(d))
    @server.route('/api/decks/<d>/history/<int:v>', methods=["GET"])
    def revision(d,v):access(d,'edit');return jsonify(present(store.revision(d,v),'edit'))
    @server.route('/api/decks/<d>/comments',methods=['GET','POST'])
    def comments(d):
        if request.method=='GET':return jsonify(store.comments(d,key(),link()))
        data=payload();return jsonify(id=store.add_comment(d,key(),link(),data.get('body'),str(data.get('slide',''))[:2000]))
    @server.route('/api/decks/<d>/comments/<cid>',methods=['PUT','DELETE'])
    def comment(d,cid):
        store.change_comment(d,cid,key(),link(),resolved=payload().get('resolved') if request.method=='PUT' else None,delete=request.method=='DELETE');return jsonify(ok=True)
    @server.route('/api/decks/<d>/presence', methods=["POST"])
    def presence(d):
        grant=access(d);data=payload();client=data.get('client')
        if not isinstance(client,str) or len(client)>100:raise ValueError('Invalid client.')
        cursor=data.get('cursor',{})
        if not isinstance(cursor,dict) or len(json.dumps(cursor))>1000:raise ValueError('Invalid cursor.')
        store.touch_presence(d,client,grant,cursor);return jsonify(store.peers(d))
    @server.route('/api/import', methods=["POST"])
    def import_file():
        store.identity(key())
        if request.is_json:
            upload=payload();filename=upload.get('filename');encoded=upload.get('content')
            if not isinstance(filename,str) or not filename.lower().endswith('.pptx'):
                raise ValueError('Choose a .pptx presentation.')
            if not isinstance(encoded,str) or len(encoded)>4*((MAX_BYTES+2)//3):
                raise ValueError('Upload limit is 20 MB.')
            try:raw=base64.b64decode(encoded,validate=True)
            except (binascii.Error,ValueError) as exc:raise ValueError('Invalid file encoding.') from exc
        else:
            # Retained for local integrations; the browser uses JSON for cloud hosts.
            f=request.files.get('file')
            if not f or not f.filename.lower().endswith('.pptx'):raise ValueError('Choose a .pptx presentation.')
            raw=f.read(MAX_BYTES+1)
        data,warnings=import_pptx(raw)
        return jsonify(settings=data,warnings=warnings)

    @server.route('/api/decks/<d>/broadcast',methods=['GET','POST','PUT','DELETE'])
    def broadcast(d):
        if request.method=='POST':
            access(d,'owner');control=secrets.token_urlsafe(32)
            state={'slide':store.get(d)['settings']['order'][0],'active':True,'blackout':False,'updated':time.time()}
            with store.connect() as db:
                db.execute('INSERT OR REPLACE INTO broadcasts VALUES (?,?,?)',(d,store.key_hash(control),json.dumps(state)))
            return jsonify(controller=control,**state)
        if request.method=='DELETE':
            access(d,'owner')
            with store.connect() as db:db.execute('DELETE FROM broadcasts WHERE document_id=?',(d,))
            return jsonify(active=False)
        # All viewers and controllers also need a currently valid deck grant.
        grant=access(d)
        with store.connect() as db:
            if request.method=='PUT':db.execute('BEGIN IMMEDIATE')
            row=db.execute('SELECT * FROM broadcasts WHERE document_id=?',(d,)).fetchone()
            if request.method=='GET':return jsonify(json.loads(row['state']) if row else {'active':False})
            if not row:raise ValueError('Start a live session first.')
            if grant['role']!='owner' and not secrets.compare_digest(row['controller'],store.key_hash(request.headers.get('X-Controller-Key',''))):
                raise PermissionError('A controller link is required to drive this presentation.')
            data=payload();state=json.loads(row['state']);order=store.get(d)['settings']['order']
            if data.get('slide') not in order:raise ValueError('Select a slide in this presentation.')
            state.update(slide=data['slide'],blackout=data.get('blackout') is True,updated=time.time())
            db.execute('UPDATE broadcasts SET state=? WHERE document_id=?',(json.dumps(state),d))
        return jsonify(state)
    return store
