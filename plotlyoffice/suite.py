import base64
import io
import json
import os
import urllib.error
import urllib.request
import zipfile
from pathlib import Path
from typing import Any
from xml.sax.saxutils import escape

SUITE_APPS: dict[str, dict[str, Any]] = {
    "word": {
        "id": "word",
        "name": "PlotlyWord",
        "tagline": "Collaborative documents",
        "default_url": os.environ.get("PLOTLYWORD_URL", "/word"),
        "extensions": {"docx", "doc", "rtf", "odt", "txt", "md", "html", "htm"},
        "new_ext": "docx",
        "resource": "documents",
    },
    "sheets": {
        "id": "sheets",
        "name": "PlotlySheets",
        "tagline": "Collaborative spreadsheets",
        "default_url": os.environ.get("PLOTLYSHEETS_URL", "/sheets"),
        "extensions": {"xlsx", "xlsm", "xltx", "xls", "ods", "csv", "tsv", "plotsheet"},
        "new_ext": "xlsx",
        "resource": "workbooks",
    },
    "slides": {
        "id": "slides",
        "name": "PlotlySlides",
        "tagline": "Collaborative presentations",
        "default_url": os.environ.get("PLOTLYSLIDES_URL", "/slides"),
        "extensions": {"pptx", "plotslide"},
        "new_ext": "pptx",
        "resource": "decks",
    },
}

_LOCAL_SERVERS: dict[str, Any] = {}


def register_local_suite(app_id: str, flask_server: Any) -> None:
    """Register an in-process Flask server for zero-overhead suite bridging."""
    _LOCAL_SERVERS[app_id] = flask_server


class SuiteConnectionError(RuntimeError):
    """Raised when a target suite app is unreachable or rejects an import."""


def detect_suite_app(filename_or_ext: str) -> str:
    ext = filename_or_ext.lower().lstrip(".")
    if "." in filename_or_ext:
        ext = Path(filename_or_ext).suffix.lower().lstrip(".")
    for app_id, meta in SUITE_APPS.items():
        if ext in meta["extensions"]:
            return app_id
    return ""


def default_suite_urls() -> dict[str, str]:
    return {
        app_id: (meta["default_url"].rstrip("/") or f"/{app_id}")
        for app_id, meta in SUITE_APPS.items()
    }


def suite_deep_link(app_id: str, base_url: str, doc_id: str, share_token: str = "") -> str:
    base = base_url.rstrip("/")
    if app_id == "word":
        return f"{base}/?doc={doc_id}" + (f"#share={share_token}" if share_token else "")
    if app_id == "sheets":
        return f"{base}/studio?book={doc_id}" + (f"#share={share_token}" if share_token else "")
    if app_id == "slides":
        if share_token:
            return f"{base}/studio#deck={doc_id}&key={share_token}"
        return f"{base}/studio#deck={doc_id}"
    return base


def _http_json(
    url: str,
    method: str = "GET",
    body: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
    timeout: float = 10.0,
) -> dict[str, Any]:
    req_headers = {"Accept": "application/json", **(headers or {})}
    data = None
    if body is not None:
        req_headers["Content-Type"] = "application/json"
        data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=req_headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8")
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            err_body = exc.read().decode("utf-8", errors="replace")
            parsed = json.loads(err_body)
            detail = parsed.get("error") or parsed.get("message") or err_body[:200]
        except Exception:
            detail = str(exc)
        raise SuiteConnectionError(f"{url} returned HTTP {exc.code}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise SuiteConnectionError(
            f"Could not reach {url}. Make sure the suite app is running or update its URL in Suite Settings."
        ) from exc


def _local_json(
    flask_server: Any,
    path: str,
    method: str = "GET",
    body: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
) -> dict[str, Any]:
    client = flask_server.test_client()
    resp = client.open(path, method=method, json=body, headers=headers or {})
    raw = resp.get_data(as_text=True)
    try:
        parsed = json.loads(raw) if raw else {}
    except Exception:
        parsed = {}
    if resp.status_code >= 400:
        detail = parsed.get("error") or parsed.get("message") or raw[:200] or f"HTTP {resp.status_code}"
        raise SuiteConnectionError(f"{path} returned HTTP {resp.status_code}: {detail}")
    return parsed


def push_to_suite(
    app_id: str,
    base_url: str,
    filename: str,
    content: bytes,
    actor_name: str = "PlotlyOffice",
) -> dict[str, str]:
    """Import a file into a sibling Plotly suite app and create edit/view share links."""
    if app_id not in SUITE_APPS:
        raise ValueError("Unsupported suite application.")
    meta = SUITE_APPS[app_id]
    base = base_url.rstrip("/")
    resource = meta["resource"]
    title = Path(filename).stem[:180] or "Untitled"

    local_server = _LOCAL_SERVERS.get(app_id) if base.startswith("/") else None

    def call(path: str, method: str = "GET", body: dict[str, Any] | None = None, headers: dict[str, str] | None = None):
        if local_server is not None:
            return _local_json(local_server, path, method=method, body=body, headers=headers)
        return _http_json(f"{base}{path}", method=method, body=body, headers=headers, timeout=25.0)

    # 1. Establish a workspace identity on the target suite app
    sess = call("/api/session", method="POST", body={"name": actor_name})
    suite_key = sess.get("key", "")
    if not suite_key:
        raise SuiteConnectionError(f"{meta['name']} did not return a workspace key.")
    auth_headers = {"Authorization": f"Bearer {suite_key}"}

    # 2. Import file content via /api/import
    encoded = base64.b64encode(content).decode("ascii")
    imported = call(
        "/api/import",
        method="POST",
        body={"filename": filename, "content": encoded},
        headers=auth_headers,
    )

    # 3. Create the collaborative document/workbook/deck
    if app_id == "word":
        create_body = {
            "title": imported.get("title") or title,
            "html": imported.get("html") or "",
            "settings": imported.get("settings") or {},
        }
    elif app_id == "sheets":
        create_body = {
            "title": imported.get("title") or title,
            "settings": imported.get("settings") or {},
        }
    else:  # slides
        create_body = {
            "title": title,
            "settings": imported.get("settings") or {},
        }

    created = call(
        f"/api/{resource}",
        method="POST",
        body=create_body,
        headers=auth_headers,
    )
    doc_id = created.get("id", "")
    if not doc_id:
        raise SuiteConnectionError(f"{meta['name']} did not return a document ID.")

    # 4. Create edit & view share links so PlotlyOffice collaborators share the same live doc
    edit_share = call(
        f"/api/{resource}/{doc_id}/shares",
        method="POST",
        body={"role": "edit", "label": "PlotlyOffice collaborators (edit)"},
        headers=auth_headers,
    )
    view_share = call(
        f"/api/{resource}/{doc_id}/shares",
        method="POST",
        body={"role": "view", "label": "PlotlyOffice collaborators (view)"},
        headers=auth_headers,
    )

    return {
        "suite_app": app_id,
        "suite_doc_id": doc_id,
        "suite_url": base,
        "suite_edit_token": edit_share.get("token", ""),
        "suite_view_token": view_share.get("token", ""),
    }


def create_blank_office_file(app_id: str, title: str) -> tuple[str, bytes, str]:
    """Create a valid minimal .docx, .xlsx, or .pptx file in memory."""
    clean_title = " ".join(str(title or "Untitled").split())[:120] or "Untitled"
    safe_xml_title = escape(clean_title)
    buf = io.BytesIO()

    if app_id == "word":
        filename = f"{clean_title}.docx"
        mime = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr(
                "[Content_Types].xml",
                '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
                '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
                '<Default Extension="xml" ContentType="application/xml"/>'
                '<Override PartName="/word/document.xml" '
                'ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
                "</Types>",
            )
            zf.writestr(
                "_rels/.rels",
                '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                '<Relationship Id="rId1" '
                'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" '
                'Target="word/document.xml"/>'
                "</Relationships>",
            )
            zf.writestr(
                "word/document.xml",
                '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
                "<w:body>"
                f"<w:p><w:r><w:t>{safe_xml_title}</w:t></w:r></w:p>"
                "<w:p><w:r><w:t></w:t></w:r></w:p>"
                "<w:sectPr/>"
                "</w:body>"
                "</w:document>",
            )
        return filename, buf.getvalue(), mime

    if app_id == "sheets":
        filename = f"{clean_title}.xlsx"
        mime = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr(
                "[Content_Types].xml",
                '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
                '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
                '<Default Extension="xml" ContentType="application/xml"/>'
                '<Override PartName="/xl/workbook.xml" '
                'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
                '<Override PartName="/xl/worksheets/sheet1.xml" '
                'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
                "</Types>",
            )
            zf.writestr(
                "_rels/.rels",
                '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                '<Relationship Id="rId1" '
                'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" '
                'Target="xl/workbook.xml"/>'
                "</Relationships>",
            )
            zf.writestr(
                "xl/_rels/workbook.xml.rels",
                '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                '<Relationship Id="rId1" '
                'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" '
                'Target="worksheets/sheet1.xml"/>'
                "</Relationships>",
            )
            zf.writestr(
                "xl/workbook.xml",
                '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
                'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
                '<sheets><sheet name="Sheet1" sheetId="1" r:id="rId1"/></sheets>'
                "</workbook>",
            )
            zf.writestr(
                "xl/worksheets/sheet1.xml",
                '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
                '<sheetData><row r="1"><c r="A1" t="inlineStr">'
                f"<is><t>{safe_xml_title}</t></is>"
                "</c></row></sheetData>"
                "</worksheet>",
            )
        return filename, buf.getvalue(), mime

    if app_id == "slides":
        filename = f"{clean_title}.pptx"
        mime = "application/vnd.openxmlformats-officedocument.presentationml.presentation"
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr(
                "[Content_Types].xml",
                '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
                '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
                '<Default Extension="xml" ContentType="application/xml"/>'
                '<Override PartName="/ppt/presentation.xml" '
                'ContentType="application/vnd.openxmlformats-officedocument.presentationml.presentation.main+xml"/>'
                '<Override PartName="/ppt/slides/slide1.xml" '
                'ContentType="application/vnd.openxmlformats-officedocument.presentationml.slide+xml"/>'
                '<Override PartName="/ppt/slideLayouts/slideLayout1.xml" '
                'ContentType="application/vnd.openxmlformats-officedocument.presentationml.slideLayout+xml"/>'
                '<Override PartName="/ppt/slideMasters/slideMaster1.xml" '
                'ContentType="application/vnd.openxmlformats-officedocument.presentationml.slideMaster+xml"/>'
                "</Types>",
            )
            zf.writestr(
                "_rels/.rels",
                '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                '<Relationship Id="rId1" '
                'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" '
                'Target="ppt/presentation.xml"/>'
                "</Relationships>",
            )
            zf.writestr(
                "ppt/_rels/presentation.xml.rels",
                '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                '<Relationship Id="rId1" '
                'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/slideMaster" '
                'Target="slideMasters/slideMaster1.xml"/>'
                '<Relationship Id="rId2" '
                'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/slide" '
                'Target="slides/slide1.xml"/>'
                "</Relationships>",
            )
            zf.writestr(
                "ppt/presentation.xml",
                '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                '<p:presentation xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" '
                'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships" '
                'xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main">'
                '<p:sldMasterIdLst><p:sldMasterId id="2147483648" r:id="rId1"/></p:sldMasterIdLst>'
                '<p:sldIdLst><p:sldId id="256" r:id="rId2"/></p:sldIdLst>'
                '<p:sldSz cx="9144000" cy="5143500"/>'
                '<p:notesSz cx="6858000" cy="9144000"/>'
                "</p:presentation>",
            )
            zf.writestr(
                "ppt/slideMasters/_rels/slideMaster1.xml.rels",
                '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                '<Relationship Id="rId1" '
                'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/slideLayout" '
                'Target="../slideLayouts/slideLayout1.xml"/>'
                "</Relationships>",
            )
            zf.writestr(
                "ppt/slideMasters/slideMaster1.xml",
                '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                '<p:sldMaster xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" '
                'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships" '
                'xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main">'
                "<p:cSld><p:spTree><p:nvGrpSpPr><p:cNvPr id=\"1\" name=\"\"/><p:cNvGrpSpPr/><p:nvPr/></p:nvGrpSpPr><p:grpSpPr/></p:spTree></p:cSld>"
                '<p:sldLayoutIdLst><p:sldLayoutId id="2147483649" r:id="rId1"/></p:sldLayoutIdLst>'
                "</p:sldMaster>",
            )
            zf.writestr(
                "ppt/slideLayouts/_rels/slideLayout1.xml.rels",
                '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                '<Relationship Id="rId1" '
                'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/slideMaster" '
                'Target="../slideMasters/slideMaster1.xml"/>'
                "</Relationships>",
            )
            zf.writestr(
                "ppt/slideLayouts/slideLayout1.xml",
                '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                '<p:sldLayout xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" '
                'xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main" type="title">'
                "<p:cSld><p:spTree><p:nvGrpSpPr><p:cNvPr id=\"1\" name=\"\"/><p:cNvGrpSpPr/><p:nvPr/></p:nvGrpSpPr><p:grpSpPr/></p:spTree></p:cSld>"
                "</p:sldLayout>",
            )
            zf.writestr(
                "ppt/slides/_rels/slide1.xml.rels",
                '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                '<Relationship Id="rId1" '
                'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/slideLayout" '
                'Target="../slideLayouts/slideLayout1.xml"/>'
                "</Relationships>",
            )
            zf.writestr(
                "ppt/slides/slide1.xml",
                '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                '<p:sld xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" '
                'xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main">'
                "<p:cSld><p:spTree>"
                '<p:nvGrpSpPr><p:cNvPr id="1" name=""/><p:cNvGrpSpPr/><p:nvPr/></p:nvGrpSpPr><p:grpSpPr/>'
                "<p:sp>"
                '<p:nvSpPr><p:cNvPr id="2" name="Title 1"/><p:cNvSpPr/><p:nvPr/></p:nvSpPr>'
                '<p:spPr><a:xfrm><a:off x="914400" y="1800000"/><a:ext cx="7315200" cy="1200000"/></a:xfrm>'
                '<a:prstGeom prst="rect"><a:avLst/></a:prstGeom></p:spPr>'
                f"<p:txBody><a:bodyPr/><a:lstStyle/><a:p><a:r><a:t>{safe_xml_title}</a:t></a:r></a:p></p:txBody>"
                "</p:sp>"
                "</p:spTree></p:cSld>"
                "</p:sld>",
            )
        return filename, buf.getvalue(), mime

    raise ValueError("Unsupported blank document type.")
