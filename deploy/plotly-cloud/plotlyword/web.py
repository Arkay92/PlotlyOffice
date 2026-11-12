"""Local-only document API. No document content is sent to external services."""
import io
import inspect
import re
import base64
import binascii
from urllib.parse import urlsplit

from flask import jsonify, request, send_file
from werkzeug.exceptions import HTTPException, RequestEntityTooLarge

from .formats import (MAX_BYTES, export_document, import_document, office_path,
                      sanitize, settings_checked)
from .storage import ConflictError, Store
from .merge import MergeConflict


def configure(server, database):
    store = Store(database)
    # Allow both the base and pending document in merge requests, plus the
    # base64 upload envelope. Decoded files retain their separate 20 MB limit.
    server.config['MAX_CONTENT_LENGTH'] = 2 * MAX_BYTES + 1024 * 1024

    @server.before_request
    def same_origin():
        if request.path.startswith('/api/') and request.method not in ('GET','HEAD','OPTIONS'):
            origin = request.headers.get('Origin')
            if (origin and urlsplit(origin).netloc != request.host) or request.headers.get('Sec-Fetch-Site') == 'cross-site':
                return jsonify(error='Cross-origin changes are not allowed.'), 403

    @server.errorhandler(Exception)
    def error(exc):
        if not request.path.startswith('/api/'):
            if isinstance(exc, HTTPException): return exc
            server.logger.exception('Application error')
            return 'Application error', 500
        if isinstance(exc, (ConflictError, MergeConflict)): return jsonify(error=str(exc)), 409
        if isinstance(exc, PermissionError): return jsonify(error=str(exc)), 403
        if isinstance(exc, KeyError): return jsonify(error='Document not found.'), 404
        if isinstance(exc, ValueError): return jsonify(error=str(exc)), 400
        if isinstance(exc, HTTPException): return jsonify(error=exc.description), exc.code
        server.logger.exception('Document operation failed')
        return jsonify(error='The document could not be processed. It may be damaged or unsupported.'), 422

    def payload():
        value = request.get_json()
        if not isinstance(value, dict): raise ValueError('Expected a JSON object.')
        return value

    def fields(data):
        title = data.get('title','Untitled document')
        if not isinstance(title,str): raise ValueError('Title must be text.')
        return (title.strip()[:180] or 'Untitled document', sanitize(data.get('html','')),
                settings_checked(data.get('settings')))

    def key():
        header=request.headers.get('Authorization','')
        return header[7:] if header.startswith('Bearer ') else ''

    def access(doc_id, require='view'):
        return store.access(doc_id,key(),request.headers.get('X-Document-Key'),require)

    def present(doc, role):
        return {k:v for k,v in doc.items() if k != 'owner_id'} | {'role':role}

    @server.route('/api/session', methods=['POST'])
    def session():
        if key():
            try:
                user=store.identity(key())
            except PermissionError:
                # Session validation is a probe, not permission to read a
                # document. Cloud hosts may replace 403 bodies with HTML.
                return jsonify(valid=False, reason='workspace_unavailable')
            return jsonify(valid=True, name=user['name'])
        return jsonify(valid=True, **store.new_identity())

    @server.route('/api/profile', methods=['PUT'])
    def profile():
        return jsonify(store.rename_identity(key(),payload().get('name','Guest')))

    @server.route('/api/capabilities', methods=['GET'])
    def capabilities():
        return jsonify(libreoffice=bool(office_path()), max_upload_mb=20, collaboration=True)

    @server.route('/api/documents', methods=['GET'])
    def documents(): return jsonify(store.list(store.identity(key())['key_hash']))

    @server.route('/api/documents', methods=['POST'])
    def create():
        owner=store.identity(key())['key_hash']
        return jsonify(present(store.save(*fields(payload()),owner=owner),'owner')), 201

    @server.route('/api/documents/<doc_id>', methods=['GET'])
    def get(doc_id):
        grant=access(doc_id)
        return jsonify(present(store.get(doc_id),grant['role']))

    @server.route('/api/documents/<doc_id>', methods=['PUT'])
    def save(doc_id):
        grant=access(doc_id,'edit')
        data = payload()
        base=data.get('base')
        if base is not None:
            if not isinstance(base,dict): raise ValueError('Invalid edit base.')
            title,markup,settings=fields(base)
            base={'title':title,'html':markup,'settings':settings}
        result=store.save(*fields(data), doc_id=doc_id, version=data.get('version'),base=base,
                          credentials=(key(),request.headers.get('X-Document-Key')))
        return jsonify(present(result,grant['role']))

    @server.route('/api/documents/<doc_id>', methods=['DELETE'])
    def delete(doc_id):
        access(doc_id,'owner')
        store.delete(doc_id)
        return '', 204

    @server.route('/api/documents/<doc_id>/history', methods=['GET'])
    def history(doc_id):
        access(doc_id,'edit')
        return jsonify(store.history(doc_id))

    @server.route('/api/documents/<doc_id>/history/<int:version>', methods=['GET'])
    def revision(doc_id, version):
        access(doc_id,'edit')
        return jsonify(store.revision(doc_id,version))

    @server.route('/api/documents/<doc_id>/shares', methods=['GET','POST'])
    def shares(doc_id):
        access(doc_id,'owner')
        if request.method == 'GET': return jsonify(store.links(doc_id))
        data=payload(); role=data.get('role')
        if role not in ('view','edit'): raise ValueError('Choose view or edit permission.')
        label=' '.join(str(data.get('label','Shared link')).split())[:60] or 'Shared link'
        return jsonify(store.create_link(doc_id,role,label)),201

    @server.route('/api/documents/<doc_id>/shares/<link_id>', methods=['PATCH','DELETE'])
    def share(doc_id,link_id):
        access(doc_id,'owner')
        role=payload().get('role') if request.method == 'PATCH' else None
        if request.method == 'PATCH' and role not in ('view','edit'): raise ValueError('Choose view or edit permission.')
        store.update_link(doc_id,link_id,role)
        return '',204

    @server.route('/api/documents/<doc_id>/comments', methods=['GET','POST'])
    def comments(doc_id):
        link=request.headers.get('X-Document-Key')
        if request.method=='GET':return jsonify(store.comments(doc_id,key(),link))
        data=payload()
        comment_id=store.add_comment(doc_id,key(),link,data.get('body'),data.get('quote',''),data.get('anchor',0))
        return jsonify(id=comment_id),201

    @server.route('/api/documents/<doc_id>/comments/<comment_id>', methods=['PATCH','DELETE'])
    def comment(doc_id,comment_id):
        store.change_comment(doc_id,comment_id,key(),request.headers.get('X-Document-Key'),
                             resolved=payload().get('resolved') if request.method=='PATCH' else None,
                             delete=request.method=='DELETE')
        return '',204

    @server.route('/api/import', methods=['POST'])
    def upload():
        if request.is_json:
            data = payload()
            filename, encoded = data.get('filename'), data.get('content')
            if not isinstance(filename, str) or not filename.strip() or len(filename) > 255:
                raise ValueError('Choose a file with a valid filename (up to 255 characters).')
            if not isinstance(encoded, str) or not encoded:
                raise ValueError('The upload is empty or missing its encoded file content.')
            if len(encoded) > 4 * ((MAX_BYTES + 2) // 3):
                raise RequestEntityTooLarge('Choose a file smaller than 20 MB.')
            try:
                content = base64.b64decode(encoded, validate=True)
            except (binascii.Error, ValueError) as exc:
                raise ValueError('The uploaded file encoding is invalid. Please select the file again.') from exc
            if len(content) > MAX_BYTES:
                raise RequestEntityTooLarge('Choose a file smaller than 20 MB.')
            return jsonify(import_document(filename, content))
        # Retain multipart support for existing clients and local API users.
        file = request.files.get('file')
        if not file: raise ValueError('Choose a document to import.')
        return jsonify(import_document(file.filename,file.read(MAX_BYTES + 1)))

    @server.route('/api/sanitize', methods=['POST'])
    def clean(): return jsonify(html=sanitize(payload().get('html','')))

    @server.route('/api/export/<extension>', methods=['POST'])
    def export(extension):
        title, markup, settings = fields(payload())
        data, mime = export_document(markup,title,settings,extension)
        name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', title).strip('. ') or 'document'
        filename_key = 'download_name' if 'download_name' in inspect.signature(send_file).parameters else 'attachment_filename'
        return send_file(io.BytesIO(data), mimetype=mime, as_attachment=True,
                         **{filename_key: name + '.' + extension})

    return store
