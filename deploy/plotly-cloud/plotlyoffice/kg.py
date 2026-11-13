"""Neurosymbolic Knowledge Graph for PlotlyOffice.

Combines:
1. Symbolic extraction of Documents, Folders, Concepts, Metrics, People, and SuiteApps.
2. Forward-chaining symbolic inference rules (shared concepts, folder topic propagation, expertise).
3. Local 128-d dense n-gram semantic embeddings + cosine similarity edges.
4. Hybrid neurosymbolic querying with provenance and human-readable explanations.
"""
import csv
import hashlib
import io
import json
import math
import re
import sqlite3
import time
import xml.etree.ElementTree as ET
import zipfile
from collections import Counter
from typing import Any

EMBED_DIM = 128

STOPWORDS = {
    "the", "and", "for", "that", "with", "this", "from", "are", "was", "were",
    "have", "has", "had", "not", "but", "you", "your", "our", "all", "can",
    "will", "just", "into", "about", "more", "than", "then", "also", "been",
    "being", "which", "when", "what", "where", "how", "who", "why", "its",
    "their", "there", "they", "them", "these", "those", "such", "each", "other",
    "new", "file", "document", "sheet", "slide", "untitled", "none", "null",
}

METRIC_RE = re.compile(
    r"(?:[$€£¥]\s?\d[\d,]*(?:\.\d+)?[kKmMbB]?|\b\d+(?:\.\d+)?%|\bQ[1-4]\b|\b20\d{2}\b)"
)
WORD_RE = re.compile(r"[A-Za-z][A-Za-z0-9_-]{2,32}")


def _now() -> int:
    return int(time.time())


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:64] or "item"


def extract_file_text(filename: str, ext: str, content: bytes) -> dict[str, Any]:
    """Extract readable text and structural hints from supported Office & text files."""
    ext = (ext or "").lower().lstrip(".")
    title = filename.rsplit(".", 1)[0] if "." in filename else filename
    chunks: list[str] = [title]
    sections: list[str] = []

    try:
        if ext == "docx":
            with zipfile.ZipFile(io.BytesIO(content)) as zf:
                if "word/document.xml" in zf.namelist():
                    root = ET.fromstring(zf.read("word/document.xml"))
                    texts = [el.text for el in root.iter() if el.tag.endswith("}t") and el.text]
                    chunks.append(" ".join(texts))
        elif ext in ("xlsx", "xlsm"):
            try:
                import openpyxl

                wb = openpyxl.load_workbook(io.BytesIO(content), data_only=True, read_only=True)
                for sname in wb.sheetnames[:10]:
                    sections.append(sname)
                    chunks.append(sname)
                    ws = wb[sname]
                    for r_idx, row in enumerate(ws.iter_rows(values_only=True)):
                        if r_idx > 120:
                            break
                        vals = [str(v) for v in row if v is not None]
                        if vals:
                            chunks.append(" ".join(vals))
                wb.close()
            except Exception:
                with zipfile.ZipFile(io.BytesIO(content)) as zf:
                    for name in zf.namelist():
                        if name.startswith("xl/worksheets/") and name.endswith(".xml"):
                            root = ET.fromstring(zf.read(name))
                            texts = [el.text for el in root.iter() if el.tag.endswith("}t") and el.text]
                            chunks.append(" ".join(texts))
        elif ext == "pptx":
            with zipfile.ZipFile(io.BytesIO(content)) as zf:
                slide_files = sorted(
                    n for n in zf.namelist() if n.startswith("ppt/slides/slide") and n.endswith(".xml")
                )
                for sf in slide_files[:30]:
                    root = ET.fromstring(zf.read(sf))
                    texts = [el.text for el in root.iter() if el.tag.endswith("}t") and el.text]
                    if texts:
                        sections.append(texts[0][:60])
                        chunks.append(" ".join(texts))
        elif ext in ("csv", "tsv"):
            delim = "\t" if ext == "tsv" else ","
            text = content[:200000].decode("utf-8", errors="replace")
            reader = csv.reader(io.StringIO(text), delimiter=delim)
            for idx, row in enumerate(reader):
                if idx > 150:
                    break
                if idx == 0:
                    sections.extend([c.strip() for c in row if c.strip()][:12])
                chunks.append(" ".join(row))
        elif ext in ("txt", "md", "html", "htm", "json", "py", "js", "css", "xml"):
            text = content[:200000].decode("utf-8", errors="replace")
            if ext in ("html", "htm"):
                text = re.sub(r"<[^>]+>", " ", text)
            elif ext == "md":
                for m in re.finditer(r"^#{1,3}\s+(.+)$", text, flags=re.MULTILINE):
                    sections.append(m.group(1).strip()[:60])
            chunks.append(text)
    except Exception:
        pass

    full_text = " ".join(c for c in chunks if c).strip()
    return {"text": full_text[:100000], "sections": sections[:12]}


def extract_symbolic_features(text: str) -> dict[str, Any]:
    """Extract top symbolic concepts and quantitative metrics from text."""
    tokens = [w.lower() for w in WORD_RE.findall(text)]
    filtered = [t for t in tokens if t not in STOPWORDS and not t.isdigit()]
    counts = Counter(filtered)

    # Also capture capitalized multi-word phrases as high-signal concepts
    phrases = re.findall(r"\b([A-Z][a-z]+(?:\s+[A-Z][a-z]+){1,2})\b", text)
    for ph in phrases:
        low = ph.lower()
        if not any(w in STOPWORDS for w in low.split()):
            counts[low] += 2

    top_concepts = [
        (term, round(min(1.0, 0.35 + freq * 0.12), 3))
        for term, freq in counts.most_common(12)
    ]
    metrics = list(dict.fromkeys(METRIC_RE.findall(text)))[:10]
    return {"concepts": top_concepts, "metrics": metrics}


def embed_text(text: str) -> list[float]:
    """Compute a deterministic 128-d L2-normalized semantic vector over word & char n-grams."""
    vec = [0.0] * EMBED_DIM
    clean = re.sub(r"\s+", " ", text.lower()).strip()
    if not clean:
        return vec

    words = [w for w in WORD_RE.findall(clean) if w not in STOPWORDS]
    features: list[tuple[str, float]] = []

    for w in words[:400]:
        features.append((f"w:{w}", 1.5))
        # Character 3-grams for morphological similarity (budget ~ budgets ~ budgeting)
        if len(w) >= 4:
            for i in range(len(w) - 2):
                features.append((f"c:{w[i:i+3]}", 0.45))

    for i in range(len(words) - 1):
        features.append((f"b:{words[i]}_{words[i+1]}", 1.8))

    for feat, weight in features:
        digest = hashlib.blake2b(feat.encode("utf-8"), digest_size=8).digest()
        idx = int.from_bytes(digest[:4], "little") % EMBED_DIM
        sign = 1.0 if (digest[4] & 1) == 0 else -1.0
        vec[idx] += sign * weight

    norm = math.sqrt(sum(v * v for v in vec))
    if norm > 1e-9:
        vec = [round(v / norm, 5) for v in vec]
    return vec


def cosine_sim(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    return sum(x * y for x, y in zip(a, b))


class KnowledgeGraph:
    """SQLite-persisted Neurosymbolic Knowledge Graph over PlotlyOffice items."""

    def __init__(self, conn: sqlite3.Connection):
        self._conn = conn
        self._init_schema()

    def _init_schema(self) -> None:
        with self._conn:
            self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS kg_nodes (
                    id TEXT PRIMARY KEY,
                    owner_id TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    label TEXT NOT NULL,
                    meta_json TEXT NOT NULL DEFAULT '{}',
                    updated INTEGER NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_kg_nodes_owner ON kg_nodes (owner_id, kind);

                CREATE TABLE IF NOT EXISTS kg_edges (
                    id TEXT PRIMARY KEY,
                    owner_id TEXT NOT NULL,
                    src TEXT NOT NULL,
                    rel TEXT NOT NULL,
                    dst TEXT NOT NULL,
                    weight REAL NOT NULL DEFAULT 1.0,
                    inferred INTEGER NOT NULL DEFAULT 0,
                    rule TEXT NOT NULL DEFAULT '',
                    explanation TEXT NOT NULL DEFAULT '',
                    updated INTEGER NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_kg_edges_owner ON kg_edges (owner_id, src, dst);

                CREATE TABLE IF NOT EXISTS kg_embeddings (
                    node_id TEXT PRIMARY KEY,
                    owner_id TEXT NOT NULL,
                    vec_json TEXT NOT NULL
                );
                """
            )

    def _upsert_node(
        self, owner_id: str, node_id: str, kind: str, label: str, meta: dict[str, Any] | None = None
    ) -> None:
        self._conn.execute(
            """
            INSERT INTO kg_nodes (id, owner_id, kind, label, meta_json, updated)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                label = excluded.label,
                meta_json = excluded.meta_json,
                updated = excluded.updated
            """,
            (node_id, owner_id, kind, label[:160], json.dumps(meta or {}), _now()),
        )

    def _upsert_edge(
        self,
        owner_id: str,
        src: str,
        rel: str,
        dst: str,
        weight: float = 1.0,
        inferred: bool = False,
        rule: str = "",
        explanation: str = "",
    ) -> None:
        eid = hashlib.sha1(f"{owner_id}|{src}|{rel}|{dst}".encode("utf-8")).hexdigest()[:20]
        self._conn.execute(
            """
            INSERT INTO kg_edges (id, owner_id, src, rel, dst, weight, inferred, rule, explanation, updated)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                weight = excluded.weight,
                inferred = excluded.inferred,
                rule = excluded.rule,
                explanation = excluded.explanation,
                updated = excluded.updated
            """,
            (eid, owner_id, src, rel, dst, float(weight), 1 if inferred else 0, rule, explanation[:240], _now()),
        )

    def index_item(self, item_id: str) -> None:
        """Symbolically and neurally index a single Drive item, then refresh inferred rules."""
        row = self._conn.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()
        if not row or row["trashed"]:
            return
        owner_id = row["owner_id"]
        node_id = f"{'folder' if row['kind'] == 'folder' else 'doc'}:{item_id}"

        with self._conn:
            # Clear previous outgoing explicit edges from this node so updates stay clean
            self._conn.execute(
                "DELETE FROM kg_edges WHERE owner_id = ? AND src = ? AND inferred = 0",
                (owner_id, node_id),
            )

            if row["kind"] == "folder":
                self._upsert_node(owner_id, node_id, "Folder", row["name"], {"item_id": item_id})
                vec = embed_text(row["name"])
                self._conn.execute(
                    "INSERT OR REPLACE INTO kg_embeddings (node_id, owner_id, vec_json) VALUES (?, ?, ?)",
                    (node_id, owner_id, json.dumps(vec)),
                )
                if row["parent_id"]:
                    parent_node = f"folder:{row['parent_id']}"
                    self._upsert_edge(owner_id, node_id, "LOCATED_IN", parent_node, 1.0)
            else:
                blob = self._conn.execute(
                    "SELECT content FROM file_blobs WHERE item_id = ?", (item_id,)
                ).fetchone()
                raw = bytes(blob["content"]) if blob else b""
                extracted = extract_file_text(row["name"], row["ext"], raw)
                sym = extract_symbolic_features(extracted["text"])

                self._upsert_node(
                    owner_id,
                    node_id,
                    "Document",
                    row["name"],
                    {
                        "item_id": item_id,
                        "ext": row["ext"],
                        "suite_app": row["suite_app"],
                        "sections": extracted["sections"],
                        "snippet": extracted["text"][:220],
                    },
                )
                vec = embed_text(extracted["text"] or row["name"])
                self._conn.execute(
                    "INSERT OR REPLACE INTO kg_embeddings (node_id, owner_id, vec_json) VALUES (?, ?, ?)",
                    (node_id, owner_id, json.dumps(vec)),
                )

                if row["parent_id"]:
                    self._upsert_edge(owner_id, node_id, "LOCATED_IN", f"folder:{row['parent_id']}", 1.0)

                if row["suite_app"]:
                    app_node = f"suite:{row['suite_app']}"
                    app_label = {
                        "word": "PlotlyWord",
                        "sheets": "PlotlySheets",
                        "slides": "PlotlySlides",
                        "genie": "Data Genie",
                    }.get(row["suite_app"], row["suite_app"])
                    self._upsert_node(owner_id, app_node, "SuiteApp", app_label)
                    self._upsert_edge(owner_id, node_id, "USES_APP", app_node, 1.0)

                self._link_owner_and_features(owner_id, node_id, sym)

        self.run_inference(owner_id)

    def _link_owner_and_features(self, owner_id: str, node_id: str, sym: dict[str, Any]) -> None:
        owner_ident = self._conn.execute(
            "SELECT name FROM identities WHERE key_hash = ?", (owner_id,)
        ).fetchone()
        if owner_ident:
            p_node = f"person:{_slug(owner_ident['name'])}"
            self._upsert_node(owner_id, p_node, "Person", owner_ident["name"])
            self._upsert_edge(owner_id, node_id, "COLLABORATED_BY", p_node, 1.0)

        for term, weight in sym["concepts"]:
            c_node = f"concept:{_slug(term)}"
            self._upsert_node(owner_id, c_node, "Concept", term)
            self._upsert_edge(owner_id, node_id, "MENTIONS_CONCEPT", c_node, weight)
            c_vec = embed_text(term)
            self._conn.execute(
                "INSERT OR REPLACE INTO kg_embeddings (node_id, owner_id, vec_json) VALUES (?, ?, ?)",
                (c_node, owner_id, json.dumps(c_vec)),
            )

        for metric in sym["metrics"]:
            m_node = f"metric:{_slug(metric)}"
            self._upsert_node(owner_id, m_node, "Metric", metric)
            self._upsert_edge(owner_id, node_id, "HAS_METRIC", m_node, 0.9)

    def index_api_feed(
        self,
        owner_id: str,
        feed_name: str,
        url: str,
        method: str = "GET",
        columns: list[str] | None = None,
        sample_text: str = "",
        row_count: int = 0,
    ) -> dict[str, Any]:
        """Symbolically and neurally index an external API feed response into the KG."""
        clean_name = str(feed_name or "API Feed")[:120]
        feed_hash = hashlib.sha1(f"{owner_id}|{clean_name}|{url}".encode("utf-8")).hexdigest()[:14]
        node_id = f"api:{feed_hash}"
        cols = [str(c) for c in (columns or [])][:60]
        full_text = "\n".join(filter(None, [clean_name, url, " ".join(cols), sample_text]))
        sym = extract_symbolic_features(full_text)

        with self._conn:
            self._conn.execute(
                "DELETE FROM kg_edges WHERE owner_id = ? AND src = ? AND inferred = 0",
                (owner_id, node_id),
            )
            self._upsert_node(
                owner_id,
                node_id,
                "APIFeed",
                clean_name,
                {
                    "url": str(url)[:300],
                    "method": str(method or "GET").upper(),
                    "row_count": int(row_count),
                    "columns": cols,
                    "snippet": full_text[:220],
                },
            )
            vec = embed_text(full_text)
            self._conn.execute(
                "INSERT OR REPLACE INTO kg_embeddings (node_id, owner_id, vec_json) VALUES (?, ?, ?)",
                (node_id, owner_id, json.dumps(vec)),
            )
            self._upsert_node(owner_id, "suite:genie", "SuiteApp", "Data Genie")
            self._upsert_edge(owner_id, node_id, "USES_APP", "suite:genie", 1.0)
            self._link_owner_and_features(owner_id, node_id, sym)

        self.run_inference(owner_id)
        return {
            "node_id": node_id,
            "concepts": [t for t, _ in sym["concepts"]],
            "metrics": sym["metrics"],
        }

    def remove_item(self, owner_id: str, item_id: str) -> None:
        with self._conn:
            for prefix in ("doc:", "folder:"):
                nid = f"{prefix}{item_id}"
                self._conn.execute("DELETE FROM kg_edges WHERE owner_id = ? AND (src = ? OR dst = ?)", (owner_id, nid, nid))
                self._conn.execute("DELETE FROM kg_nodes WHERE owner_id = ? AND id = ?", (owner_id, nid))
                self._conn.execute("DELETE FROM kg_embeddings WHERE owner_id = ? AND node_id = ?", (owner_id, nid))
        self.run_inference(owner_id)

    def rebuild_for_owner(self, owner_id: str) -> dict[str, int]:
        """Re-index every non-trashed folder and file for an owner."""
        with self._conn:
            self._conn.execute("DELETE FROM kg_edges WHERE owner_id = ?", (owner_id,))
            self._conn.execute("DELETE FROM kg_nodes WHERE owner_id = ?", (owner_id,))
            self._conn.execute("DELETE FROM kg_embeddings WHERE owner_id = ?", (owner_id,))

        rows = self._conn.execute(
            "SELECT id FROM items WHERE owner_id = ? AND trashed = 0 ORDER BY CASE WHEN kind='folder' THEN 0 ELSE 1 END",
            (owner_id,),
        ).fetchall()
        for r in rows:
            self.index_item(r["id"])
        return self.stats(owner_id)

    def run_inference(self, owner_id: str) -> None:
        """Execute symbolic forward-chaining rules + neural semantic similarity links."""
        with self._conn:
            self._conn.execute("DELETE FROM kg_edges WHERE owner_id = ? AND inferred = 1", (owner_id,))

            # Rule 1: shared_concept_link — docs sharing >= 2 concepts
            doc_concepts = self._conn.execute(
                "SELECT src, dst FROM kg_edges WHERE owner_id = ? AND rel = 'MENTIONS_CONCEPT'",
                (owner_id,),
            ).fetchall()
            by_doc: dict[str, set[str]] = {}
            for r in doc_concepts:
                by_doc.setdefault(r["src"], set()).add(r["dst"])

            doc_ids = sorted(by_doc.keys())
            for i in range(len(doc_ids)):
                for j in range(i + 1, len(doc_ids)):
                    d1, d2 = doc_ids[i], doc_ids[j]
                    shared = sorted(by_doc[d1] & by_doc[d2])
                    if len(shared) >= 2:
                        labels = [s.replace("concept:", "") for s in shared[:4]]
                        w = round(min(0.98, 0.45 + 0.15 * len(shared)), 3)
                        self._upsert_edge(
                            owner_id,
                            d1,
                            "RELATED_TO",
                            d2,
                            weight=w,
                            inferred=True,
                            rule="shared_concept_link",
                            explanation=f"Shares concepts: {', '.join(labels)}",
                        )

            # Rule 2: folder_topic_propagation — Folder -> Concept via contained Documents
            folder_topics = self._conn.execute(
                """
                SELECT e1.dst AS folder_id, e2.dst AS concept_id, COUNT(*) AS cnt
                FROM kg_edges e1
                JOIN kg_edges e2 ON e1.src = e2.src
                WHERE e1.owner_id = ? AND e1.rel = 'LOCATED_IN' AND e2.rel = 'MENTIONS_CONCEPT'
                GROUP BY e1.dst, e2.dst
                """,
                (owner_id,),
            ).fetchall()
            for r in folder_topics:
                self._upsert_edge(
                    owner_id,
                    r["folder_id"],
                    "COVERS_TOPIC",
                    r["concept_id"],
                    weight=round(min(1.0, 0.5 + 0.2 * r["cnt"]), 3),
                    inferred=True,
                    rule="folder_topic_propagation",
                    explanation=f"Inferred from {r['cnt']} file(s) inside this folder",
                )

            # Rule 3: person_expertise — Person -> Concept via authored/collaborated Documents
            expertise = self._conn.execute(
                """
                SELECT e1.dst AS person_id, e2.dst AS concept_id, COUNT(*) AS cnt
                FROM kg_edges e1
                JOIN kg_edges e2 ON e1.src = e2.src
                WHERE e1.owner_id = ? AND e1.rel = 'COLLABORATED_BY' AND e2.rel = 'MENTIONS_CONCEPT'
                GROUP BY e1.dst, e2.dst
                """,
                (owner_id,),
            ).fetchall()
            for r in expertise:
                self._upsert_edge(
                    owner_id,
                    r["person_id"],
                    "EXPERT_IN",
                    r["concept_id"],
                    weight=round(min(1.0, 0.4 + 0.2 * r["cnt"]), 3),
                    inferred=True,
                    rule="person_expertise",
                    explanation=f"Collaborated on {r['cnt']} document(s) about this topic",
                )

            # Rule 4: neural_semantic_link — cosine similarity >= 0.28 between Document & APIFeed embeddings
            emb_rows = self._conn.execute(
                """
                SELECT e.node_id, e.vec_json
                FROM kg_embeddings e
                JOIN kg_nodes n ON n.id = e.node_id
                WHERE e.owner_id = ? AND n.kind IN ('Document', 'APIFeed')
                """,
                (owner_id,),
            ).fetchall()
            vecs = [(r["node_id"], json.loads(r["vec_json"])) for r in emb_rows]
            for i in range(len(vecs)):
                for j in range(i + 1, len(vecs)):
                    n1, v1 = vecs[i]
                    n2, v2 = vecs[j]
                    sim = cosine_sim(v1, v2)
                    if sim >= 0.28:
                        self._upsert_edge(
                            owner_id,
                            n1,
                            "SEMANTICALLY_SIMILAR",
                            n2,
                            weight=round(sim, 3),
                            inferred=True,
                            rule="neural_semantic_link",
                            explanation=f"Neural cosine similarity {sim:.2f}",
                        )

    def stats(self, owner_id: str) -> dict[str, int]:
        nodes = self._conn.execute(
            "SELECT COUNT(*) AS c FROM kg_nodes WHERE owner_id = ?", (owner_id,)
        ).fetchone()["c"]
        edges = self._conn.execute(
            "SELECT COUNT(*) AS c FROM kg_edges WHERE owner_id = ?", (owner_id,)
        ).fetchone()["c"]
        inferred = self._conn.execute(
            "SELECT COUNT(*) AS c FROM kg_edges WHERE owner_id = ? AND inferred = 1", (owner_id,)
        ).fetchone()["c"]
        return {"nodes": nodes, "edges": edges, "inferred_edges": inferred}

    def graph(self, owner_id: str, limit: int = 120) -> dict[str, Any]:
        nodes_rows = self._conn.execute(
            "SELECT id, kind, label, meta_json, updated FROM kg_nodes WHERE owner_id = ? ORDER BY updated DESC LIMIT ?",
            (owner_id, limit),
        ).fetchall()
        node_ids = {r["id"] for r in nodes_rows}
        edges_rows = self._conn.execute(
            "SELECT id, src, rel, dst, weight, inferred, rule, explanation FROM kg_edges WHERE owner_id = ?",
            (owner_id,),
        ).fetchall()
        nodes = [
            {
                "id": r["id"],
                "kind": r["kind"],
                "label": r["label"],
                "meta": json.loads(r["meta_json"] or "{}"),
            }
            for r in nodes_rows
        ]
        edges = [
            {
                "id": r["id"],
                "src": r["src"],
                "rel": r["rel"],
                "dst": r["dst"],
                "weight": r["weight"],
                "inferred": bool(r["inferred"]),
                "rule": r["rule"],
                "explanation": r["explanation"],
            }
            for r in edges_rows
            if r["src"] in node_ids and r["dst"] in node_ids
        ]
        return {"nodes": nodes, "edges": edges, "stats": self.stats(owner_id)}

    def query(self, owner_id: str, query_text: str, limit: int = 12) -> dict[str, Any]:
        """Hybrid neurosymbolic query combining neural vector similarity and symbolic graph reasoning."""
        q = (query_text or "").strip()
        if not q:
            return {"query": "", "matches": [], **self.graph(owner_id, limit=80)}

        q_vec = embed_text(q)
        q_tokens = {t.lower() for t in WORD_RE.findall(q) if t.lower() not in STOPWORDS}
        if not q_tokens:
            q_tokens = {q.lower()}

        nodes = self._conn.execute(
            "SELECT id, kind, label, meta_json FROM kg_nodes WHERE owner_id = ?", (owner_id,)
        ).fetchall()
        embs = {
            r["node_id"]: json.loads(r["vec_json"])
            for r in self._conn.execute(
                "SELECT node_id, vec_json FROM kg_embeddings WHERE owner_id = ?", (owner_id,)
            ).fetchall()
        }
        edges = self._conn.execute(
            "SELECT id, src, rel, dst, weight, inferred, rule, explanation FROM kg_edges WHERE owner_id = ?",
            (owner_id,),
        ).fetchall()

        adj: dict[str, list[sqlite3.Row]] = {}
        for e in edges:
            adj.setdefault(e["src"], []).append(e)
            adj.setdefault(e["dst"], []).append(e)

        scored: list[dict[str, Any]] = []
        for n in nodes:
            nid = n["id"]
            label_low = n["label"].lower()
            meta = json.loads(n["meta_json"] or "{}")

            neural_score = max(0.0, cosine_sim(q_vec, embs.get(nid, [])))
            sym_score = 0.0
            reasons: list[str] = []

            if any(tok in label_low for tok in q_tokens):
                sym_score += 0.65
                reasons.append(f"Symbolic label match on '{n['label']}'")

            # 1-hop symbolic & inferred neighbor boost
            for e in adj.get(nid, []):
                other = e["dst"] if e["src"] == nid else e["src"]
                other_slug = other.split(":", 1)[-1].replace("-", " ")
                if any(tok in other_slug for tok in q_tokens):
                    boost = 0.35 * float(e["weight"])
                    sym_score += boost
                    if e["inferred"]:
                        reasons.append(f"Inferred via {e['rule']}: {e['explanation']}")
                    else:
                        reasons.append(f"Connected via {e['rel']} -> {other_slug}")

            if neural_score >= 0.15:
                reasons.insert(0, f"Neural semantic similarity: {neural_score:.2f}")

            total = round(0.55 * neural_score + 0.45 * min(1.5, sym_score), 4)
            if total > 0.12:
                scored.append(
                    {
                        "id": nid,
                        "kind": n["kind"],
                        "label": n["label"],
                        "meta": meta,
                        "score": total,
                        "neural_score": round(neural_score, 3),
                        "symbolic_score": round(sym_score, 3),
                        "explanations": reasons[:4],
                    }
                )

        scored.sort(key=lambda x: x["score"], reverse=True)
        top = scored[:limit]
        top_ids = {m["id"] for m in top}
        # Include 1-hop neighbors of top matches in the returned subgraph
        sub_ids = set(top_ids)
        for e in edges:
            if e["src"] in top_ids or e["dst"] in top_ids:
                sub_ids.add(e["src"])
                sub_ids.add(e["dst"])

        node_map = {
            n["id"]: {
                "id": n["id"],
                "kind": n["kind"],
                "label": n["label"],
                "meta": json.loads(n["meta_json"] or "{}"),
            }
            for n in nodes
            if n["id"] in sub_ids
        }
        sub_edges = [
            {
                "id": e["id"],
                "src": e["src"],
                "rel": e["rel"],
                "dst": e["dst"],
                "weight": e["weight"],
                "inferred": bool(e["inferred"]),
                "rule": e["rule"],
                "explanation": e["explanation"],
            }
            for e in edges
            if e["src"] in sub_ids and e["dst"] in sub_ids
        ]
        return {
            "query": q,
            "matches": top,
            "nodes": list(node_map.values()),
            "edges": sub_edges,
            "stats": self.stats(owner_id),
        }
