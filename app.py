import gzip
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if (ROOT / ".deps").is_dir():
    sys.path.insert(0, str(ROOT / ".deps"))

import plotly.graph_objects as go
from dash import Dash, Input, Output, html
from fastapi import FastAPI, Response
from flask import Flask
from plotly.offline import get_plotlyjs
from starlette.middleware.wsgi import WSGIMiddleware

from plotlyoffice.release import release_id
from plotlyoffice.suite import register_local_suite
from plotlyoffice.web import configure as configure_office
from plotlysheets.web import configure as configure_sheets
from plotlyslides.web import configure as configure_slides
from plotlyword.collaboration import attach as attach_word_collab
from plotlyword.ui import layout as word_layout
from plotlyword.web import configure as configure_word

DATA_DIR = ROOT / "data"

# 1. PlotlyOffice (Drive & Suite Hub — Dash FastAPI so app.server is ASGI)
app = Dash(
    __name__,
    title="PlotlyOffice · Collaborative workspace & drive",
    backend="fastapi",
    assets_folder=str(ROOT / "assets"),
    assets_ignore=r"office\.js",
    update_title=None,
)
app.layout = html.Div(
    html.Iframe(
        src="/drive?v=" + release_id(),
        title="PlotlyOffice drive",
        style={"border": 0, "width": "100%", "height": "100vh", "display": "block"},
    )
)
server = app.server

office_flask = Flask("plotlyoffice_api")
office_store = configure_office(
    office_flask,
    os.environ.get("PLOTLYOFFICE_DATABASE", str(DATA_DIR / "office.sqlite3")),
)

# 2. PlotlySheets (mounted at /sheets)
sheets_flask = Flask(
    "plotlysheets_app",
    static_folder=str(ROOT / "apps" / "sheets" / "assets"),
    static_url_path="/assets",
)
configure_sheets(
    sheets_flask,
    os.environ.get("PLOTLYSHEETS_DATABASE", str(DATA_DIR / "sheets.sqlite3")),
)
register_local_suite("sheets", sheets_flask)

# 3. PlotlySlides (mounted at /slides)
slides_flask = Flask(
    "plotlyslides_app",
    static_folder=str(ROOT / "apps" / "slides" / "assets"),
    static_url_path="/assets",
)
configure_slides(
    slides_flask,
    os.environ.get("PLOTLYSLIDES_DATABASE", str(DATA_DIR / "slides.sqlite3")),
)
register_local_suite("slides", slides_flask)

# 4. PlotlyWord (Dash FastAPI + WebSocket collab, mounted at /word)
word_app = Dash(
    "plotlyword_app",
    title="PlotlyWord · A space for your words",
    backend="fastapi",
    requests_pathname_prefix="/word/",
    routes_pathname_prefix="/",
    assets_folder=str(ROOT / "apps" / "word" / "assets"),
    update_title=None,
    external_scripts=["/word/plotlyword-plotly.js"],
)
word_app.layout = word_layout
word_flask = Flask("plotlyword_api")
word_store = configure_word(
    word_flask,
    os.environ.get("PLOTLYWORD_DATABASE", str(DATA_DIR / "documents.sqlite3")),
)
attach_word_collab(word_app, word_store)
register_local_suite("word", word_flask)

word_app.clientside_callback(
    """function(packet) {
    if (window.PlotlyWordCollab) {
        if (packet) window.PlotlyWordCollab.receive(packet);
    }
    return window.dash_clientside.no_update;
}""",
    Output("collab-drain", "children"),
    Input("collab-incoming", "data"),
)

_plotly_js = gzip.compress(get_plotlyjs().encode("utf-8"))


@word_app.server.get("/plotlyword-plotly.js")
def word_plotly_runtime():
    return Response(
        _plotly_js,
        media_type="application/javascript",
        headers={"Content-Encoding": "gzip", "Cache-Control": "public, max-age=3600"},
    )


@word_app.callback(Output("insights-graph", "figure"), Input("editor-metrics", "data"))
def word_insights(data):
    sections = (data or {}).get("sections", [])[:30]
    fig = go.Figure(
        go.Bar(
            x=[s["words"] for s in sections],
            y=[s["title"][:28] for s in sections],
            orientation="h",
            marker_color="#779481",
            hovertemplate="%{x} words<extra></extra>",
        )
    )
    fig.update_layout(
        template="plotly_white",
        margin=dict(l=10, r=15, t=15, b=30),
        paper_bgcolor="#fafbf8",
        plot_bgcolor="#fafbf8",
        font=dict(family="Segoe UI", size=11, color="#52675a"),
        xaxis=dict(title="Words", gridcolor="#e6ece2"),
        yaxis=dict(autorange="reversed"),
        height=max(200, len(sections) * 26 + 70),
    )
    return fig


word_app.server.mount("/", WSGIMiddleware(word_flask))

# 5. Mount sub-apps and API fallback onto app.server (FastAPI ASGI)
server.mount("/sheets", WSGIMiddleware(sheets_flask))
server.mount("/slides", WSGIMiddleware(slides_flask))
server.mount("/word", word_app.server)
server.mount("/", WSGIMiddleware(office_flask))


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        server,
        host=os.environ.get("HOST", "127.0.0.1"),
        port=int(os.environ.get("PORT", "8050")),
        log_level="info",
    )
