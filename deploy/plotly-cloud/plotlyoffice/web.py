import base64
import binascii
import json
from pathlib import Path
from typing import Any

from flask import Flask, Response, jsonify, request
from werkzeug.exceptions import RequestEntityTooLarge

from plotlyoffice.release import release_id
from plotlyoffice.storage import MAX_UPLOAD_BYTES, Store
from plotlyoffice.suite import (
    SUITE_APPS,
    SuiteConnectionError,
    create_blank_office_file,
    default_suite_urls,
    detect_suite_app,
    push_to_suite,
    suite_deep_link,
)

DRIVE_HTML = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>PlotlyOffice · Collaborative workspace &amp; drive</title>
  <link rel="stylesheet" href="/assets/styles.css?v=__RELEASE__">
</head>
<body>
  <div id="app" class="office-shell"></div>
  <script src="/assets/office.js?v=__RELEASE__" defer></script>
</body>
</html>
"""


def configure(server: Flask, db_path: str) -> Store:
    store = Store(db_path)
    build = release_id()
    drive_html = DRIVE_HTML.replace("__RELEASE__", build)

    def key() -> str:
        header = request.headers.get("Authorization", "")
        return header[7:].strip() if header.startswith("Bearer ") else ""

    def link() -> str:
        return request.headers.get("X-Document-Key", "").strip()

    def payload() -> dict[str, Any]:
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            raise ValueError("Expected a JSON object.")
        return data

    @server.errorhandler(ValueError)
    def handle_value_error(exc):
        return jsonify(error=str(exc)), 400

    @server.errorhandler(PermissionError)
    def handle_perm_error(exc):
        return jsonify(error=str(exc)), 403

    @server.errorhandler(KeyError)
    def handle_key_error(exc):
        return jsonify(error=str(exc).strip("'") or "Not found."), 404

    @server.errorhandler(RequestEntityTooLarge)
    def handle_too_large(_exc):
        return jsonify(error="Upload exceeds the 25 MB limit."), 413

    @server.errorhandler(SuiteConnectionError)
    def handle_suite_error(exc):
        return jsonify(error=str(exc)), 502

    @server.route("/drive", methods=["GET"])
    def drive():
        return Response(drive_html, mimetype="text/html")

    @server.route("/api/health", methods=["GET"])
    def health():
        return jsonify(ok=True, build=build, suite_urls=default_suite_urls())

    @server.route("/api/suite/config", methods=["GET"])
    def suite_config():
        apps = {
            app_id: {
                "id": meta["id"],
                "name": meta["name"],
                "tagline": meta["tagline"],
                "default_url": meta["default_url"].rstrip("/"),
                "extensions": sorted(meta["extensions"]),
            }
            for app_id, meta in SUITE_APPS.items()
        }
        return jsonify(apps=apps, urls=default_suite_urls())

    @server.route("/api/session", methods=["POST"])
    def session():
        data = request.get_json(silent=True) or {}
        name = data.get("name", "")
        if key():
            if isinstance(name, str) and name.strip():
                try:
                    ident = store.rename_identity(key(), name)
                    return jsonify(valid=True, name=ident["name"], color=ident["color"])
                except PermissionError:
                    return jsonify(valid=False, reason="workspace_unavailable")
            try:
                ident = store.identity(key())
                return jsonify(valid=True, name=ident["name"], color=ident["color"])
            except PermissionError:
                return jsonify(valid=False, reason="workspace_unavailable")
        if not isinstance(name, str) or not name.strip():
            name = "Workspace User"
        return jsonify(valid=True, **store.new_identity(name))

    @server.route("/api/items", methods=["GET"])
    def list_items():
        parent_id = request.args.get("parent_id") or None
        view = request.args.get("view", "folder")
        query = request.args.get("q", "")
        return jsonify(
            store.list_items(
                raw_key=key(),
                share_token=link(),
                parent_id=parent_id,
                view=view,
                query=query,
            )
        )

    @server.route("/api/folders", methods=["POST"])
    def create_folder():
        data = payload()
        folder = store.create_folder(
            name=data.get("name", "New Folder"),
            parent_id=data.get("parent_id") or None,
            raw_key=key(),
            share_token=link(),
        )
        return jsonify(folder), 201

    @server.route("/api/files/blank", methods=["POST"])
    def create_blank():
        data = payload()
        app_id = data.get("app_id", "")
        title = data.get("title", "Untitled")
        parent_id = data.get("parent_id") or None
        filename, content, mime = create_blank_office_file(app_id, title)
        item = store.save_file(
            filename=filename,
            content=content,
            parent_id=parent_id,
            raw_key=key(),
            share_token=link(),
            suite_app=app_id,
            mime_type=mime,
        )
        return jsonify(item), 201

    @server.route("/api/upload", methods=["POST"])
    def upload():
        parent_id = request.form.get("parent_id") or request.args.get("parent_id") or None
        if request.is_json:
            data = payload()
            parent_id = data.get("parent_id") or parent_id
            filename = data.get("filename")
            encoded = data.get("content")
            if not isinstance(filename, str) or not filename.strip():
                raise ValueError("Choose a file with a valid filename.")
            if not isinstance(encoded, str) or len(encoded) > 4 * ((MAX_UPLOAD_BYTES + 2) // 3):
                raise ValueError("Upload exceeds the 25 MB limit.")
            try:
                raw = base64.b64decode(encoded, validate=True)
            except (binascii.Error, ValueError) as exc:
                raise ValueError("Invalid file encoding.") from exc
            item = store.save_file(
                filename=filename,
                content=raw,
                parent_id=parent_id,
                raw_key=key(),
                share_token=link(),
                suite_app=detect_suite_app(filename),
            )
            return jsonify(items=[item]), 201

        uploaded = request.files.getlist("files") or request.files.getlist("file")
        if not uploaded:
            raise ValueError("Choose at least one file to upload.")
        created = []
        for f in uploaded:
            if not f or not f.filename:
                continue
            raw = f.read(MAX_UPLOAD_BYTES + 1)
            item = store.save_file(
                filename=f.filename,
                content=raw,
                parent_id=parent_id,
                raw_key=key(),
                share_token=link(),
                suite_app=detect_suite_app(f.filename),
                mime_type=f.mimetype or "",
            )
            created.append(item)
        if not created:
            raise ValueError("No valid files were uploaded.")
        return jsonify(items=created), 201

    @server.route("/api/items/<item_id>", methods=["GET"])
    def get_item(item_id):
        grant = store.access(item_id, key(), link(), "view")
        return jsonify(
            item=store._row_to_item(grant["item"], grant["role"]),
            breadcrumbs=store.breadcrumbs(item_id, key(), link()),
        )

    @server.route("/api/items/<item_id>", methods=["PATCH", "PUT"])
    def update_item(item_id):
        data = payload()
        kwargs: dict[str, Any] = {}
        if "name" in data:
            kwargs["name"] = data["name"]
        if "parent_id" in data:
            kwargs["parent_id"] = data["parent_id"]
        if "starred" in data:
            kwargs["starred"] = bool(data["starred"])
        if "trashed" in data:
            kwargs["trashed"] = bool(data["trashed"])
        updated = store.update_item(item_id, raw_key=key(), share_token=link(), **kwargs)
        return jsonify(updated)

    @server.route("/api/items/<item_id>", methods=["DELETE"])
    def delete_item(item_id):
        if request.args.get("permanent") == "true":
            store.delete_forever(item_id, key())
            return jsonify(ok=True, deleted=True)
        updated = store.update_item(item_id, raw_key=key(), share_token=link(), trashed=True)
        return jsonify(updated)

    @server.route("/api/items/<item_id>/download", methods=["GET"])
    def download_item(item_id):
        item, raw = store.get_file_bytes(item_id, key(), link())
        disposition = "inline" if request.args.get("inline") == "1" else "attachment"
        safe_fn = item["name"].replace('"', "'")
        return Response(
            raw,
            mimetype=item["mime_type"] or "application/octet-stream",
            headers={
                "Content-Disposition": f'{disposition}; filename="{safe_fn}"',
                "Content-Length": str(len(raw)),
            },
        )

    @server.route("/api/items/<item_id>/preview", methods=["GET"])
    def preview_item(item_id):
        item, raw = store.get_file_bytes(item_id, key(), link())
        ext = item["ext"].lower()
        mime = item["mime_type"] or ""
        if mime.startswith("image/"):
            return jsonify(kind="image", item=item)
        if ext == "pdf" or mime == "application/pdf":
            return jsonify(kind="pdf", item=item)
        text_exts = {"txt", "md", "csv", "tsv", "json", "html", "htm", "py", "js", "css", "xml", "log"}
        if ext in text_exts or mime.startswith("text/"):
            snippet = raw[:131072].decode("utf-8", errors="replace")
            return jsonify(kind="text", text=snippet, truncated=len(raw) > 131072, item=item)
        return jsonify(kind="unsupported", item=item)

    @server.route("/api/items/<item_id>/open-suite", methods=["POST"])
    def open_suite(item_id):
        grant = store.access(item_id, key(), link(), "view")
        item = grant["item"]
        if item["kind"] != "file":
            raise ValueError("Folders cannot be opened in an editor app.")

        app_id = item["suite_app"] or detect_suite_app(item["name"])
        if not app_id or app_id not in SUITE_APPS:
            raise ValueError("This file type is not supported by PlotlyWord, PlotlySheets, or PlotlySlides.")

        data = request.get_json(silent=True) or {}
        urls = default_suite_urls()
        custom_urls = data.get("suite_urls")
        if isinstance(custom_urls, dict) and isinstance(custom_urls.get(app_id), str):
            candidate = custom_urls[app_id].strip().rstrip("/")
            if candidate.startswith(("http://", "https://", "/")):
                urls[app_id] = candidate

        target_url = urls[app_id]
        force_new = bool(data.get("force_new"))
        existing = store.get_suite_link(item_id)

        if (
            not force_new
            and existing["suite_doc_id"]
            and existing["suite_app"] == app_id
            and existing["suite_url"].rstrip("/") == target_url
        ):
            token = (
                existing["suite_edit_token"]
                if grant["role"] in ("owner", "edit")
                else existing["suite_view_token"]
            )
            deep_url = suite_deep_link(app_id, target_url, existing["suite_doc_id"], token)
            return jsonify(
                url=deep_url,
                app=app_id,
                app_name=SUITE_APPS[app_id]["name"],
                doc_id=existing["suite_doc_id"],
                reused=True,
            )

        _, raw = store.get_file_bytes(item_id, key(), link())
        linked = push_to_suite(
            app_id=app_id,
            base_url=target_url,
            filename=item["name"],
            content=raw,
            actor_name=grant["identity"]["name"],
        )
        store.set_suite_link(
            item_id=item_id,
            suite_app=linked["suite_app"],
            suite_doc_id=linked["suite_doc_id"],
            suite_url=linked["suite_url"],
            suite_edit_token=linked["suite_edit_token"],
            suite_view_token=linked["suite_view_token"],
        )
        token = (
            linked["suite_edit_token"]
            if grant["role"] in ("owner", "edit")
            else linked["suite_view_token"]
        )
        deep_url = suite_deep_link(app_id, target_url, linked["suite_doc_id"], token)
        return jsonify(
            url=deep_url,
            app=app_id,
            app_name=SUITE_APPS[app_id]["name"],
            doc_id=linked["suite_doc_id"],
            reused=False,
        )

    # --- Share links ---
    @server.route("/api/items/<item_id>/shares", methods=["GET", "POST"])
    def shares(item_id):
        store.access(item_id, key(), link(), "owner")
        if request.method == "GET":
            return jsonify(store.links(item_id))
        data = payload()
        role = data.get("role", "view")
        label = data.get("label", "Shared link")
        return jsonify(store.create_link(item_id, role, label)), 201

    @server.route("/api/items/<item_id>/shares/<share_id>", methods=["PUT", "PATCH", "DELETE"])
    def share(item_id, share_id):
        store.access(item_id, key(), link(), "owner")
        role = None if request.method == "DELETE" else payload().get("role")
        store.update_link(item_id, share_id, role)
        return jsonify(ok=True)

    # --- Comments ---
    @server.route("/api/items/<item_id>/comments", methods=["GET", "POST"])
    def comments(item_id):
        if request.method == "GET":
            return jsonify(store.comments(item_id, key(), link()))
        data = payload()
        return jsonify(store.add_comment(item_id, key(), link(), data.get("body", ""))), 201

    @server.route("/api/items/<item_id>/comments/<comment_id>", methods=["PUT", "PATCH", "DELETE"])
    def comment(item_id, comment_id):
        data = payload() if request.method in ("PUT", "PATCH") else {}
        store.change_comment(
            item_id,
            comment_id,
            key(),
            link(),
            resolved=data.get("resolved"),
            delete=request.method == "DELETE",
        )
        return jsonify(ok=True)

    # --- Presence & Activity ---
    @server.route("/api/items/<item_id>/presence", methods=["POST"])
    def presence(item_id):
        grant = store.access(item_id, key(), link(), "view")
        data = payload()
        client = data.get("client")
        if not isinstance(client, str) or not client or len(client) > 100:
            raise ValueError("Invalid client session.")
        cursor = data.get("cursor", {})
        if not isinstance(cursor, dict) or len(json.dumps(cursor)) > 1000:
            raise ValueError("Invalid cursor.")
        return jsonify(store.touch_presence(item_id, client, grant, cursor))

    @server.route("/api/items/<item_id>/activity", methods=["GET"])
    def item_activity(item_id):
        return jsonify(store.activity(item_id, key(), link()))

    return store
