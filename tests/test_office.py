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
