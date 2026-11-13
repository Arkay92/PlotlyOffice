"""Model Context Protocol (MCP) server for the PlotlyOffice Suite & Knowledge Graph.

Supports:
1. HTTP JSON-RPC 2.0 endpoint (`POST /mcp` in `plotlyoffice/web.py`).
2. Stdio transport (`python -m plotlyoffice.mcp_server`) for local MCP clients.
"""
import json
import os
import sys
from pathlib import Path
from typing import Any

from plotlyoffice.kg import extract_file_text
from plotlyoffice.storage import Store
from plotlyoffice.suite import (
    SUITE_APPS,
    create_blank_office_file,
    default_suite_urls,
    detect_suite_app,
    push_to_suite,
    suite_deep_link,
)

PROTOCOL_VERSION = "2024-11-05"

MCP_TOOLS: list[dict[str, Any]] = [
    {
        "name": "office_list_items",
        "description": "List folders and files in PlotlyOffice Drive (by parent_id, smart view, or search query).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "parent_id": {"type": "string", "description": "Folder ID (omit for root)"},
                "view": {"type": "string", "enum": ["folder", "recent", "starred", "shared", "trash"]},
                "query": {"type": "string", "description": "Optional filename search filter"},
            },
        },
    },
    {
        "name": "office_create_folder",
        "description": "Create a new folder in PlotlyOffice Drive.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "parent_id": {"type": "string"},
            },
            "required": ["name"],
        },
    },
    {
        "name": "office_create_document",
        "description": "Create a new blank Word (.docx), Sheets (.xlsx), or Slides (.pptx) document in Drive.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "app_id": {"type": "string", "enum": ["word", "sheets", "slides"]},
                "title": {"type": "string"},
                "parent_id": {"type": "string"},
            },
            "required": ["app_id", "title"],
        },
    },
    {
        "name": "office_upload_text_file",
        "description": "Upload or create a text-based file (.md, .txt, .csv, .tsv, .html, .json) in Drive and index it into the Knowledge Graph.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "filename": {"type": "string"},
                "content": {"type": "string"},
                "parent_id": {"type": "string"},
            },
            "required": ["filename", "content"],
        },
    },
    {
        "name": "office_read_file",
        "description": "Extract readable text and structural sections from a file (.docx, .xlsx, .pptx, .csv, .md, .txt, .html).",
        "inputSchema": {
            "type": "object",
            "properties": {"item_id": {"type": "string"}},
            "required": ["item_id"],
        },
    },
    {
        "name": "office_open_in_suite",
        "description": "Bridge a Drive file into PlotlyWord, PlotlySheets, or PlotlySlides and return its live collaborative URL.",
        "inputSchema": {
            "type": "object",
            "properties": {"item_id": {"type": "string"}},
            "required": ["item_id"],
        },
    },
    {
        "name": "office_share_item",
        "description": "Create a capability share link (view or edit) for a folder or file.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "item_id": {"type": "string"},
                "role": {"type": "string", "enum": ["view", "edit"]},
                "label": {"type": "string"},
            },
            "required": ["item_id", "role"],
        },
    },
    {
        "name": "office_add_comment",
        "description": "Add a collaborator comment to a folder or file.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "item_id": {"type": "string"},
                "body": {"type": "string"},
            },
            "required": ["item_id", "body"],
        },
    },
    {
        "name": "kg_query",
        "description": "Run a hybrid neurosymbolic query (neural semantic similarity + symbolic rule reasoning) over the Knowledge Graph.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "limit": {"type": "integer", "default": 10},
            },
            "required": ["query"],
        },
    },
    {
        "name": "kg_get_graph",
        "description": "Retrieve Knowledge Graph nodes, explicit/inferred edges, and summary statistics.",
        "inputSchema": {
            "type": "object",
            "properties": {"limit": {"type": "integer", "default": 100}},
        },
    },
    {
        "name": "kg_rebuild",
        "description": "Re-index all workspace files and re-run symbolic and neural inference rules.",
        "inputSchema": {"type": "object", "properties": {}},
    },
]


class MCPHandler:
    """Stateless JSON-RPC 2.0 MCP handler backed by a PlotlyOffice Store."""

    def __init__(self, store: Store):
        self.store = store

    def handle(
        self,
        rpc: dict[str, Any],
        raw_key: str = "",
        share_token: str = "",
    ) -> dict[str, Any] | None:
        if not isinstance(rpc, dict):
            return self._err(None, -32600, "Invalid Request")
        req_id = rpc.get("id")
        method = rpc.get("method", "")
        params = rpc.get("params") or {}

        if method == "notifications/initialized":
            return None

        try:
            if method == "initialize":
                return self._ok(
                    req_id,
                    {
                        "protocolVersion": PROTOCOL_VERSION,
                        "capabilities": {"tools": {}, "resources": {}},
                        "serverInfo": {"name": "plotlyoffice-mcp", "version": "0.1.0"},
                    },
                )
            if method == "ping":
                return self._ok(req_id, {})
            if method == "tools/list":
                return self._ok(req_id, {"tools": MCP_TOOLS})
            if method == "tools/call":
                name = params.get("name", "")
                args = params.get("arguments") or {}
                result = self._call_tool(name, args, raw_key, share_token)
                return self._ok(
                    req_id,
                    {
                        "content": [{"type": "text", "text": json.dumps(result, indent=2)}],
                        "isError": False,
                    },
                )
            if method == "resources/list":
                return self._ok(
                    req_id,
                    {
                        "resources": [
                            {
                                "uri": "office://kg/summary",
                                "name": "Knowledge Graph Summary",
                                "mimeType": "application/json",
                            },
                            {
                                "uri": "office://drive/root",
                                "name": "Drive Root Listing",
                                "mimeType": "application/json",
                            },
                        ]
                    },
                )
            if method == "resources/read":
                uri = params.get("uri", "")
                ident = self.store.identity(raw_key)
                if uri == "office://kg/summary":
                    data = self.store.kg.graph(ident["key_hash"], limit=60)
                elif uri == "office://drive/root":
                    data = self.store.list_items(raw_key=raw_key, share_token=share_token)
                else:
                    raise KeyError(f"Unknown resource URI: {uri}")
                return self._ok(
                    req_id,
                    {
                        "contents": [
                            {
                                "uri": uri,
                                "mimeType": "application/json",
                                "text": json.dumps(data, indent=2),
                            }
                        ]
                    },
                )
            return self._err(req_id, -32601, f"Method not found: {method}")
        except PermissionError as exc:
            return self._err(req_id, -32001, str(exc))
        except (ValueError, KeyError) as exc:
            return self._err(req_id, -32602, str(exc))
        except Exception as exc:
            return self._err(req_id, -32603, str(exc))

    def _call_tool(
        self, name: str, args: dict[str, Any], raw_key: str, share_token: str
    ) -> Any:
        if name == "office_list_items":
            return self.store.list_items(
                raw_key=raw_key,
                share_token=share_token,
                parent_id=args.get("parent_id") or None,
                view=args.get("view", "folder"),
                query=args.get("query", ""),
            )
        if name == "office_create_folder":
            return self.store.create_folder(
                name=args["name"],
                parent_id=args.get("parent_id") or None,
                raw_key=raw_key,
                share_token=share_token,
            )
        if name == "office_create_document":
            app_id = args["app_id"]
            filename, content, mime = create_blank_office_file(app_id, args["title"])
            return self.store.save_file(
                filename=filename,
                content=content,
                parent_id=args.get("parent_id") or None,
                raw_key=raw_key,
                share_token=share_token,
                suite_app=app_id,
                mime_type=mime,
            )
        if name == "office_upload_text_file":
            fn = args["filename"]
            raw = str(args["content"]).encode("utf-8")
            return self.store.save_file(
                filename=fn,
                content=raw,
                parent_id=args.get("parent_id") or None,
                raw_key=raw_key,
                share_token=share_token,
                suite_app=detect_suite_app(fn),
            )
        if name == "office_read_file":
            item, raw = self.store.get_file_bytes(args["item_id"], raw_key, share_token)
            extracted = extract_file_text(item["name"], item["ext"], raw)
            return {"item": item, **extracted}
        if name == "office_open_in_suite":
            item_id = args["item_id"]
            grant = self.store.access(item_id, raw_key, share_token, "view")
            item = grant["item"]
            app_id = item["suite_app"] or detect_suite_app(item["name"])
            if not app_id or app_id not in SUITE_APPS:
                raise ValueError("File type is not supported by Word, Sheets, or Slides.")
            target_url = default_suite_urls()[app_id]
            existing = self.store.get_suite_link(item_id)
            if existing["suite_doc_id"] and existing["suite_app"] == app_id:
                tok = (
                    existing["suite_edit_token"]
                    if grant["role"] in ("owner", "edit")
                    else existing["suite_view_token"]
                )
                return {
                    "url": suite_deep_link(app_id, target_url, existing["suite_doc_id"], tok),
                    "app": app_id,
                    "doc_id": existing["suite_doc_id"],
                    "reused": True,
                }
            _, raw = self.store.get_file_bytes(item_id, raw_key, share_token)
            linked = push_to_suite(
                app_id=app_id,
                base_url=target_url,
                filename=item["name"],
                content=raw,
                actor_name=grant["identity"]["name"],
            )
            self.store.set_suite_link(
                item_id,
                linked["suite_app"],
                linked["suite_doc_id"],
                linked["suite_url"],
                linked["suite_edit_token"],
                linked["suite_view_token"],
            )
            tok = (
                linked["suite_edit_token"]
                if grant["role"] in ("owner", "edit")
                else linked["suite_view_token"]
            )
            return {
                "url": suite_deep_link(app_id, target_url, linked["suite_doc_id"], tok),
                "app": app_id,
                "doc_id": linked["suite_doc_id"],
                "reused": False,
            }
        if name == "office_share_item":
            self.store.access(args["item_id"], raw_key, share_token, "owner")
            return self.store.create_link(
                args["item_id"], args["role"], args.get("label", "MCP shared link")
            )
        if name == "office_add_comment":
            return self.store.add_comment(args["item_id"], raw_key, share_token, args["body"])
        if name == "kg_query":
            ident = self.store.identity(raw_key)
            return self.store.kg.query(
                ident["key_hash"], args["query"], limit=int(args.get("limit", 10))
            )
        if name == "kg_get_graph":
            ident = self.store.identity(raw_key)
            return self.store.kg.graph(ident["key_hash"], limit=int(args.get("limit", 100)))
        if name == "kg_rebuild":
            ident = self.store.identity(raw_key)
            return self.store.kg.rebuild_for_owner(ident["key_hash"])
        raise ValueError(f"Unknown tool: {name}")

    @staticmethod
    def _ok(req_id: Any, result: Any) -> dict[str, Any]:
        return {"jsonrpc": "2.0", "id": req_id, "result": result}

    @staticmethod
    def _err(req_id: Any, code: int, message: str) -> dict[str, Any]:
        return {"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": message}}


def run_stdio() -> None:
    """Run PlotlyOffice MCP server over stdio."""
    root = Path(__file__).resolve().parent.parent
    db_path = os.environ.get("PLOTLYOFFICE_DATABASE", str(root / "data" / "office.sqlite3"))
    store = Store(db_path)
    raw_key = os.environ.get("PLOTLYOFFICE_KEY", "")
    if not raw_key:
        raw_key = store.new_identity("MCP Client")["key"]
    handler = MCPHandler(store)
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            rpc = json.loads(line)
        except json.JSONDecodeError:
            resp = MCPHandler._err(None, -32700, "Parse error")
        else:
            resp = handler.handle(rpc, raw_key=raw_key)
        if resp is not None:
            sys.stdout.write(json.dumps(resp) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    run_stdio()
