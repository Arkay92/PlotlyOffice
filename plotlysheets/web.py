"""Permission-checked HTTP API for the collaborative spreadsheet studio."""
import base64
import binascii
import gzip
import json
import re
from pathlib import Path
from urllib.parse import urlsplit

from flask import Response, jsonify, request
from werkzeug.exceptions import HTTPException

from .formats import MAX_BYTES, checked, export_workbook, import_workbook
from .merge import MergeConflict
from .release import asset_version, release_id
from .storage import ConflictError, Store


def configure(server, database):
    # Flask 2+ provides method-specific decorators. Keep the app usable on
    # older managed Dash images that still expose only ``route``.
    if not hasattr(server, "get"):
        server.get = lambda rule, **options: server.route(rule, methods=["GET"], **options)
        server.post = lambda rule, **options: server.route(rule, methods=["POST"], **options)
        server.put = lambda rule, **options: server.route(rule, methods=["PUT"], **options)
        server.delete = lambda rule, **options: server.route(rule, methods=["DELETE"], **options)
    store = Store(database)
    build = release_id()
    assets = Path(__file__).resolve().parents[1] / "apps" / "sheets" / "assets"
    studio_html = (assets / "studio.html").read_text(encoding="utf-8")
    studio_html = re.sub(
        r'((?:src|href)="/assets/)([^"?]+)(")',
        lambda m: m[1] + m[2] + "?v=" + asset_version(m[2]) + m[3],
        studio_html,
    ).replace("</head>", f'<meta name="plotlysheets-build" content="{build}"></head>')
    server.config["MAX_CONTENT_LENGTH"] = 38 * 1024 * 1024

    @server.before_request
    def origin_check():
        if request.path.startswith("/api/") and request.method not in ("GET", "HEAD", "OPTIONS"):
            origin = request.headers.get("Origin")
            if (origin and urlsplit(origin).netloc != request.host) or request.headers.get("Sec-Fetch-Site") == "cross-site":
                return jsonify(error="Cross-origin changes are not allowed."), 403

    @server.after_request
    def headers(response):
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-PlotlySheets-Build"] = build
        if response.status_code == 200 and request.path.startswith(("/assets/", "/_dash-component-suites/")):
            if request.path.endswith(".js"): response.mimetype = "application/javascript"
            if request.path.endswith(".css"): response.mimetype = "text/css"
        if request.path.startswith("/api/") or request.path in ("/", "/studio", "/_dash-layout", "/_dash-dependencies"):
            response.headers["Cache-Control"] = "no-store, max-age=0"
        if request.path.startswith("/api/") and response.is_json and response.status_code in (200, 201):
            raw = response.get_data()
            if len(raw) > 4096 and request.accept_encodings["gzip"] > 0:
                response.set_data(gzip.compress(raw, compresslevel=5))
                response.headers["Content-Encoding"] = "gzip"
                response.vary.add("Accept-Encoding")
        return response

    @server.errorhandler(Exception)
    def error(exc):
        if not request.path.startswith("/api/"):
            if isinstance(exc, HTTPException): return exc
            server.logger.exception("Application error")
            return "Application error", 500
        if isinstance(exc, (ConflictError, MergeConflict)): return jsonify(error=str(exc), code="conflict"), 409
        if isinstance(exc, PermissionError): return jsonify(error=str(exc)), 403
        if isinstance(exc, KeyError): return jsonify(error="Workbook not found."), 404
        if isinstance(exc, ValueError): return jsonify(error=str(exc)), 400
        if isinstance(exc, HTTPException): return jsonify(error=exc.description), exc.code
        server.logger.exception("Workbook operation failed")
        return jsonify(error="The workbook could not be processed."), 422

    def key():
        header = request.headers.get("Authorization", "")
        return header[7:] if header.startswith("Bearer ") else ""

    def link(): return request.headers.get("X-Document-Key")
    def access(book_id, require="view"): return store.access(book_id, key(), link(), require)

    def payload():
        value = request.get_json()
        if not isinstance(value, dict): raise ValueError("Expected a JSON object.")
        return value

    def fields(data):
        if not isinstance(data.get("title"), str): raise ValueError("Enter a workbook title.")
        return data["title"].strip()[:180] or "Untitled workbook", checked(data.get("settings"))

    def present(value, role):
        return {k: v for k, v in value.items() if k != "owner_id"} | {"role": role}

    @server.route("/studio", methods=["GET"])
    def studio(): return Response(studio_html, mimetype="text/html")

    @server.route("/api/health", methods=["GET"])
    def health(): return jsonify(ok=True, build=build, import_transport="json-base64")

    @server.route("/api/session", methods=["POST"])
    def session():
        name = payload().get("name", "")
        if not isinstance(name, str) or not name.strip(): raise ValueError("Enter your display name to continue.")
        name = " ".join(name.split())[:40]
        if key():
            try: store.rename_identity(key(), name)
            except PermissionError: return jsonify(valid=False, reason="workspace_unavailable")
            return jsonify(valid=True, name=name)
        return jsonify(valid=True, **store.new_identity(name))

    @server.route("/api/workbooks", methods=["GET"])
    def workbooks(): return jsonify(store.list(store.identity(key())["key_hash"]))

    @server.route("/api/workbooks", methods=["POST"])
    def create():
        title, settings = fields(payload())
        return jsonify(present(store.save(title, settings, owner=store.identity(key())["key_hash"]), "owner")), 201

    @server.route("/api/workbooks/<book_id>", methods=["GET"])
    def get_workbook(book_id):
        role = access(book_id)["role"]
        value = store.get(book_id)
        if request.args.get("version") == str(value["version"]):
            return jsonify(unchanged=True, version=value["version"], role=role)
        return jsonify(present(value, role))

    @server.route("/api/workbooks/<book_id>", methods=["PUT"])
    def save(book_id):
        grant = access(book_id, "edit")
        data = payload(); base = None
        if data.get("base") is not None:
            btitle, bsettings = fields(data["base"]); base = {"title": btitle, "settings": bsettings}
        title, settings = fields(data)
        value = store.save(title, settings, workbook_id=book_id, version=data.get("version"), base=base,
                           credentials=(key(), link()))
        return jsonify(present(value, grant["role"]))

    @server.route("/api/workbooks/<book_id>", methods=["DELETE"])
    def delete(book_id):
        access(book_id, "owner"); store.delete(book_id); return "", 204

    @server.route("/api/workbooks/<book_id>/shares", methods=["GET", "POST"])
    def shares(book_id):
        access(book_id, "owner")
        if request.method == "GET": return jsonify(store.links(book_id))
        data = payload(); role = data.get("role")
        if role not in ("view", "edit"): raise ValueError("Select view or edit.")
        return jsonify(store.create_link(book_id, role, str(data.get("label", "Shared link"))[:100]))

    @server.route("/api/workbooks/<book_id>/shares/<share_id>", methods=["PUT", "DELETE"])
    def share(book_id, share_id):
        access(book_id, "owner")
        role = None if request.method == "DELETE" else payload().get("role")
        if request.method == "PUT" and role not in ("view", "edit"): raise ValueError("Invalid permission.")
        store.update_link(book_id, share_id, role); return jsonify(ok=True)

    @server.route("/api/workbooks/<book_id>/history", methods=["GET"])
    def history(book_id): access(book_id, "edit"); return jsonify(store.history(book_id))

    @server.route("/api/workbooks/<book_id>/history/<int:version>", methods=["GET"])
    def revision(book_id, version): access(book_id, "edit"); return jsonify(present(store.revision(book_id, version), "edit"))

    @server.route("/api/workbooks/<book_id>/comments", methods=["GET", "POST"])
    def comments(book_id):
        if request.method == "GET": return jsonify(store.comments(book_id, key(), link()))
        data = payload()
        return jsonify(id=store.add_comment(book_id, key(), link(), data.get("body"), data.get("cell", ""), data.get("sheet", "")))

    @server.route("/api/workbooks/<book_id>/comments/<comment_id>", methods=["PUT", "DELETE"])
    def comment(book_id, comment_id):
        data = payload() if request.method == "PUT" else {}
        store.change_comment(book_id, comment_id, key(), link(), data.get("resolved"), request.method == "DELETE")
        return jsonify(ok=True)

    @server.route("/api/workbooks/<book_id>/presence", methods=["POST"])
    def presence(book_id):
        grant = access(book_id); data = payload(); client = data.get("client")
        if not isinstance(client, str) or len(client) > 100: raise ValueError("Invalid client session.")
        cursor = data.get("cursor", {})
        if not isinstance(cursor, dict) or len(json.dumps(cursor)) > 1000: raise ValueError("Invalid cursor.")
        store.touch_presence(book_id, client, grant, cursor)
        return jsonify(store.peers(book_id))

    @server.route("/api/import", methods=["POST"])
    def import_file():
        store.identity(key()); upload = payload(); filename = upload.get("filename"); encoded = upload.get("content")
        if not isinstance(filename, str): raise ValueError("Choose a workbook file.")
        if not isinstance(encoded, str) or len(encoded) > 4 * ((MAX_BYTES + 2) // 3): raise ValueError("Upload limit is 25 MB.")
        try: raw = base64.b64decode(encoded, validate=True)
        except (binascii.Error, ValueError) as exc: raise ValueError("Invalid file encoding.") from exc
        settings, warnings = import_workbook(filename, raw)
        return jsonify(title=Path(filename).stem[:180], settings=settings, warnings=warnings)

    @server.route("/api/export", methods=["POST"])
    def export_file():
        store.identity(key()); data = payload(); title, settings = fields(data)
        extension = str(data.get("extension", "xlsx")).lower()
        raw, mimetype = export_workbook(settings, title, extension)
        safe = re.sub(r"[^A-Za-z0-9._ -]+", "_", title).strip() or "workbook"
        return Response(raw, mimetype=mimetype, headers={
            "Content-Disposition": f'attachment; filename="{safe[:120]}.{extension}"'
        })

    return store
