import base64
import io
import zipfile
from unittest.mock import patch

import pytest
from flask import Flask

from plotlyoffice.suite import create_blank_office_file, detect_suite_app, suite_deep_link
from plotlyoffice.web import configure


@pytest.fixture()
def client(tmp_path):
    server = Flask(__name__)
    configure(server, str(tmp_path / "office-test.sqlite3"))
    server.config["TESTING"] = True
    return server.test_client()


def _new_user(client, name="Alice"):
    res = client.post("/api/session", json={"name": name})
    assert res.status_code == 200
    data = res.get_json()
    assert data["valid"] is True
    return data["key"]


def test_detect_suite_app_and_deep_links():
    assert detect_suite_app("Quarterly Report.docx") == "word"
    assert detect_suite_app("notes.md") == "word"
    assert detect_suite_app("budget.xlsx") == "sheets"
    assert detect_suite_app("data.csv") == "sheets"
    assert detect_suite_app("roadmap.pptx") == "slides"
    assert detect_suite_app("photo.png") == ""

    assert suite_deep_link("word", "/word", "d1", "tok") == "/word/?doc=d1#share=tok"
    assert suite_deep_link("sheets", "/sheets", "b1", "tok") == "/sheets/studio?book=b1#share=tok"
    assert suite_deep_link("slides", "/slides", "s1", "tok") == "/slides/studio#deck=s1&key=tok"


def test_blank_office_files_are_valid_zips():
    for app_id, expected_ext, member in (
        ("word", ".docx", "word/document.xml"),
        ("sheets", ".xlsx", "xl/workbook.xml"),
        ("slides", ".pptx", "ppt/presentation.xml"),
    ):
        filename, raw, _ = create_blank_office_file(app_id, "Team Plan")
        assert filename.endswith(expected_ext)
        with zipfile.ZipFile(io.BytesIO(raw), "r") as zf:
            assert member in zf.namelist()


def test_folders_upload_preview_and_trash(client):
    key = _new_user(client, "Alice")
    auth = {"Authorization": f"Bearer {key}"}

    # Create parent and nested folder
    f1 = client.post("/api/folders", json={"name": "Projects"}, headers=auth).get_json()
    f2 = client.post("/api/folders", json={"name": "Q4", "parent_id": f1["id"]}, headers=auth).get_json()

    # Multipart upload into nested folder
    resp = client.post(
        "/api/upload",
        data={"parent_id": f2["id"], "files": (io.BytesIO(b"col1,col2\n1,2\n"), "metrics.csv")},
        headers=auth,
        content_type="multipart/form-data",
    )
    assert resp.status_code == 201
    uploaded = resp.get_json()["items"][0]
    assert uploaded["suite_app"] == "sheets"
    assert uploaded["name"] == "metrics.csv"

    # List folder & check breadcrumbs
    listing = client.get(f"/api/items?parent_id={f2['id']}", headers=auth).get_json()
    assert [c["name"] for c in listing["breadcrumbs"]] == ["Projects", "Q4"]
    assert len(listing["items"]) == 1

    # Preview & download
    prev = client.get(f"/api/items/{uploaded['id']}/preview", headers=auth).get_json()
    assert prev["kind"] == "text"
    assert "col1,col2" in prev["text"]

    dl = client.get(f"/api/items/{uploaded['id']}/download", headers=auth)
    assert dl.status_code == 200
    assert dl.data == b"col1,col2\n1,2\n"

    # Trash & restore
    client.delete(f"/api/items/{uploaded['id']}", headers=auth)
    trash_list = client.get("/api/items?view=trash", headers=auth).get_json()["items"]
    assert len(trash_list) == 1
    client.patch(f"/api/items/{uploaded['id']}", json={"trashed": False}, headers=auth)
    assert len(client.get("/api/items?view=trash", headers=auth).get_json()["items"]) == 0


def test_inherited_folder_sharing_presence_and_comments(client):
    alice_key = _new_user(client, "Alice")
    bob_key = _new_user(client, "Bob")
    alice_hdr = {"Authorization": f"Bearer {alice_key}"}

    folder = client.post("/api/folders", json={"name": "Shared Hub"}, headers=alice_hdr).get_json()
    sub = client.post("/api/folders", json={"name": "Sub", "parent_id": folder["id"]}, headers=alice_hdr).get_json()

    # Create view-only and edit share links on the parent folder
    view_link = client.post(
        f"/api/items/{folder['id']}/shares",
        json={"role": "view", "label": "Read only"},
        headers=alice_hdr,
    ).get_json()
    edit_link = client.post(
        f"/api/items/{folder['id']}/shares",
        json={"role": "edit", "label": "Editors"},
        headers=alice_hdr,
    ).get_json()

    # Bob with view link can browse subfolder, but cannot create inside it
    bob_view = {"Authorization": f"Bearer {bob_key}", "X-Document-Key": view_link["token"]}
    res_list = client.get(f"/api/items?parent_id={sub['id']}", headers=bob_view)
    assert res_list.status_code == 200
    assert res_list.get_json()["role"] == "view"

    res_deny = client.post("/api/folders", json={"name": "Nope", "parent_id": sub["id"]}, headers=bob_view)
    assert res_deny.status_code == 403

    # Bob with edit link CAN upload into subfolder
    bob_edit = {"Authorization": f"Bearer {bob_key}", "X-Document-Key": edit_link["token"]}
    encoded = base64.b64encode(b"# Hello").decode("ascii")
    res_up = client.post(
        "/api/upload",
        json={"filename": "notes.md", "content": encoded, "parent_id": sub["id"]},
        headers=bob_edit,
    )
    assert res_up.status_code == 201
    assert res_up.get_json()["items"][0]["suite_app"] == "word"

    # Presence & comments
    peers = client.post(
        f"/api/items/{folder['id']}/presence",
        json={"client": "bob-1", "cursor": {"view": "grid"}},
        headers=bob_edit,
    ).get_json()
    assert any(p["name"] == "Bob" for p in peers)

    c = client.post(
        f"/api/items/{folder['id']}/comments",
        json={"body": "Uploaded the markdown notes!"},
        headers=bob_edit,
    )
    assert c.status_code == 201
    comments = client.get(f"/api/items/{folder['id']}/comments", headers=alice_hdr).get_json()
    assert len(comments) == 1
    assert comments[0]["author_name"] == "Bob"


def test_open_in_suite_bridge_reuses_collaborative_doc(client):
    alice_key = _new_user(client, "Alice")
    bob_key = _new_user(client, "Bob")
    alice_hdr = {"Authorization": f"Bearer {alice_key}"}

    folder = client.post("/api/folders", json={"name": "Suite Docs"}, headers=alice_hdr).get_json()
    blank = client.post(
        "/api/files/blank",
        json={"app_id": "word", "title": "Spec", "parent_id": folder["id"]},
        headers=alice_hdr,
    ).get_json()

    view_link = client.post(
        f"/api/items/{folder['id']}/shares",
        json={"role": "view", "label": "Viewers"},
        headers=alice_hdr,
    ).get_json()

    fake_linked = {
        "suite_app": "word",
        "suite_doc_id": "doc-xyz",
        "suite_url": "/word",
        "suite_edit_token": "edit-tok",
        "suite_view_token": "view-tok",
    }

    with patch("plotlyoffice.web.push_to_suite", return_value=fake_linked) as mock_push:
        r1 = client.post(f"/api/items/{blank['id']}/open-suite", json={}, headers=alice_hdr)
        assert r1.status_code == 200
        d1 = r1.get_json()
        assert d1["reused"] is False
        assert d1["url"] == "/word/?doc=doc-xyz#share=edit-tok"
        assert mock_push.call_count == 1

        # Collaborator with view role reuses the same linked doc with the view token
        bob_view = {"Authorization": f"Bearer {bob_key}", "X-Document-Key": view_link["token"]}
        r2 = client.post(f"/api/items/{blank['id']}/open-suite", json={}, headers=bob_view)
        assert r2.status_code == 200
        d2 = r2.get_json()
        assert d2["reused"] is True
        assert d2["url"] == "/word/?doc=doc-xyz#share=view-tok"
        assert mock_push.call_count == 1


def test_in_process_sheets_and_slides_bridge(tmp_path):
    from plotlyoffice.suite import register_local_suite
    from plotlysheets.web import configure as configure_sheets
    from plotlyslides.web import configure as configure_slides

    office_app = Flask(__name__)
    configure(office_app, str(tmp_path / "office.sqlite3"))
    office_app.config["TESTING"] = True
    c = office_app.test_client()

    sheets_app = Flask("test_sheets")
    configure_sheets(sheets_app, str(tmp_path / "sheets.sqlite3"))
    register_local_suite("sheets", sheets_app)

    slides_app = Flask("test_slides")
    configure_slides(slides_app, str(tmp_path / "slides.sqlite3"))
    register_local_suite("slides", slides_app)

    key = _new_user(c, "Alice")
    hdr = {"Authorization": f"Bearer {key}"}

    xlsx = c.post("/api/files/blank", json={"app_id": "sheets", "title": "Budget"}, headers=hdr).get_json()
    res_sheets = c.post(f"/api/items/{xlsx['id']}/open-suite", json={}, headers=hdr)
    assert res_sheets.status_code == 200
    assert res_sheets.get_json()["url"].startswith("/sheets/studio?book=")

    pptx = c.post("/api/files/blank", json={"app_id": "slides", "title": "Deck"}, headers=hdr).get_json()
    res_slides = c.post(f"/api/items/{pptx['id']}/open-suite", json={}, headers=hdr)
    assert res_slides.status_code == 200
    assert res_slides.get_json()["url"].startswith("/slides/studio#deck=")


def test_neurosymbolic_knowledge_graph_and_inferences(client):
    key = _new_user(client, "Alice")
    hdr = {"Authorization": f"Bearer {key}"}

    folder = client.post("/api/folders", json={"name": "Finance"}, headers=hdr).get_json()

    # Upload two files that share domain concepts & metrics
    f1 = base64.b64encode(b"# Q4 Revenue Forecast\nRevenue growth target is 24% and EBITDA margin $195,000.\n").decode("ascii")
    f2 = base64.b64encode(b"Quarter,Revenue,Margin\nQ4,195000,24%\nForecast growth remains strong.\n").decode("ascii")
    client.post("/api/upload", json={"filename": "Q4_Plan.md", "content": f1, "parent_id": folder["id"]}, headers=hdr)
    client.post("/api/upload", json={"filename": "Q4_Numbers.csv", "content": f2, "parent_id": folder["id"]}, headers=hdr)

    kg = client.get("/api/kg", headers=hdr).get_json()
    assert kg["stats"]["nodes"] >= 5
    assert kg["stats"]["inferred_edges"] >= 1
    rels = {e["rel"] for e in kg["edges"]}
    assert "MENTIONS_CONCEPT" in rels
    assert "COVERS_TOPIC" in rels

    q = client.post("/api/kg/query", json={"query": "Q4 revenue growth"}, headers=hdr).get_json()
    assert len(q["matches"]) >= 1
    assert any(m["explanations"] for m in q["matches"])


def test_mcp_jsonrpc_suite_and_kg_tools(client):
    key = _new_user(client, "Alice")
    hdr = {"Authorization": f"Bearer {key}"}

    manifest = client.get("/mcp").get_json()
    assert manifest["name"] == "plotlyoffice-mcp"
    assert len(manifest["tools"]) >= 10

    init_resp = client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "initialize"}).get_json()
    assert init_resp["result"]["serverInfo"]["name"] == "plotlyoffice-mcp"

    tools_resp = client.post("/mcp", json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"}).get_json()
    tool_names = {t["name"] for t in tools_resp["result"]["tools"]}
    assert {"office_create_folder", "office_create_document", "office_read_file", "kg_query"}.issubset(tool_names)

    # Create folder and blank Word doc via MCP tools/call
    f_resp = client.post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {"name": "office_create_folder", "arguments": {"name": "MCP Folder"}},
        },
        headers=hdr,
    ).get_json()
    assert f_resp["result"]["isError"] is False

    # Upload a markdown note and query KG via MCP
    client.post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 4,
            "method": "tools/call",
            "params": {
                "name": "office_upload_text_file",
                "arguments": {"filename": "Roadmap.md", "content": "# AI Roadmap\nBuild neurosymbolic graph and MCP server."},
            },
        },
        headers=hdr,
    )

    kg_resp = client.post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 5,
            "method": "tools/call",
            "params": {"name": "kg_query", "arguments": {"query": "neurosymbolic MCP"}},
        },
        headers=hdr,
    ).get_json()
    assert "Roadmap.md" in kg_resp["result"]["content"][0]["text"]


def test_data_genie_office_folder_upload_kg_import_and_api_feed(tmp_path, monkeypatch):
    import io
    from flask import Flask
    from plotlyoffice.web import configure
    from genie.web import configure as configure_genie

    monkeypatch.setenv("GENIE_STORAGE_DIR", str(tmp_path / "genie_runtime"))

    office_app = Flask("test_office_genie")
    office_store = configure(office_app, str(tmp_path / "office.sqlite3"))
    office_app.config["TESTING"] = True
    oc = office_app.test_client()

    genie_app = Flask("test_genie")
    configure_genie(genie_app, office_store=office_store)
    genie_app.config["TESTING"] = True
    gc = genie_app.test_client()

    key = _new_user(oc, "Alice")
    ohdr = {"Authorization": f"Bearer {key}"}
    ghdr = {"X-Genie": "1", "X-Office-Key": key}

    # 1. Create an Office folder and upload a Markdown strategy doc so it is in the KG
    folder = oc.post("/api/folders", json={"name": "GenieAnalytics"}, headers=ohdr).get_json()
    doc_b64 = base64.b64encode(b"# Revenue Expansion Strategy\nEnterprise revenue growth and customer retention.\n").decode("ascii")
    doc_item = oc.post(
        "/api/upload",
        json={"filename": "Expansion_Strategy.md", "content": doc_b64, "parent_id": folder["id"]},
        headers=ohdr,
    ).get_json()["items"][0]

    # 2. Data Genie context returns the folder and the KG document
    ctx = gc.get("/api/office/context", headers=ghdr).get_json()
    assert ctx["enabled"] is True
    assert any(f["id"] == folder["id"] for f in ctx["folders"])
    assert any(d["id"] == doc_item["id"] for d in ctx["documents"])

    # 3. Upload a CSV in Data Genie targeting the PlotlyOffice folder
    csv_bytes = b"region,revenue,retention\nNorth America,145000,0.94\nEurope,98000,0.91\n"
    up_res = gc.post(
        "/api/upload",
        data={
            "files": (io.BytesIO(csv_bytes), "regional_revenue.csv"),
            "office_folder_id": folder["id"],
            "save_to_office": "1",
        },
        headers=ghdr,
        content_type="multipart/form-data",
    )
    assert up_res.status_code == 200
    assert "regional_revenue.csv" in up_res.get_json()["office_saved"]

    # Confirm the uploaded CSV is inside that PlotlyOffice folder
    folder_items = oc.get(f"/api/items?parent_id={folder['id']}", headers=ohdr).get_json()["items"]
    assert any(i["name"] == "regional_revenue.csv" for i in folder_items)

    # 4. Select the Markdown document from Knowledge Graph and add it to Data Genie workspace
    kg_imp = gc.post("/api/office/kg-import", json={"item_ids": [doc_item["id"]]}, headers=ghdr)
    assert kg_imp.status_code == 200
    assert "Expansion_Strategy.md" in kg_imp.get_json()["documents"]

    gstate = gc.get("/api/state", headers=ghdr).get_json()
    source_names = {s["name"] for s in gstate["sources"]}
    assert {"regional_revenue.csv", "Expansion_Strategy.md"}.issubset(source_names)

    # 5. Index an API feed response into the KG and verify APIFeed node & links
    ident = office_store.identity(key)
    feed_res = office_store.kg.index_api_feed(
        owner_id=ident["key_hash"],
        feed_name="Enterprise Revenue Feed",
        url="https://api.example.com/v1/revenue",
        method="GET",
        columns=["region", "revenue", "retention"],
        sample_text="Enterprise revenue growth and customer retention across regions",
        row_count=25,
    )
    assert feed_res["node_id"].startswith("api:")
    kg = oc.get("/api/kg", headers=ohdr).get_json()
    assert any(n["kind"] == "APIFeed" and n["label"] == "Enterprise Revenue Feed" for n in kg["nodes"])
    assert any(e["src"] == feed_res["node_id"] and e["rel"] in ("RELATED_TO", "SEMANTICALLY_SIMILAR") for e in kg["edges"])


def test_unified_asgi_mounts_genie():
    from starlette.testclient import TestClient
    import app as unified_app

    tc = TestClient(unified_app.app.server)
    assert tc.get("/genie/").status_code == 200
    assert tc.get("/genie/api/state").status_code == 200
    assert tc.get("/genie/assets/styles.css").status_code == 200
    assert tc.get("/genie/assets/genie.js").status_code == 200
