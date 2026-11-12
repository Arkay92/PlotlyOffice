# PlotlyOffice deployment bundle

Run `python scripts/prepare_cloud.py` from the source checkout. It creates:

- `deploy/plotly-cloud/` — an upload-ready, self-contained application folder containing PlotlyOffice Drive, PlotlyWord, PlotlySheets, and PlotlySlides
- `deploy/plotlyoffice-plotly-cloud.zip` — the same folder as a ZIP archive

Use `app.py` (or `main.py`) as the application entry point with Python 3.11 or newer. The exported ASGI application is `app:server`; the Dash application is `app:app`.

Set `PLOTLYOFFICE_DATABASE`, `PLOTLYWORD_DATABASE`, `PLOTLYSHEETS_DATABASE`, and `PLOTLYSLIDES_DATABASE` to paths on persistent storage (defaulting to `data/`). Run one web worker so every request sees the same SQLite databases, and use HTTPS because workspace and share keys are bearer credentials.

For Docker, build the repository root with `docker compose up --build`. The included named volume persists all Drive folders, uploaded files, documents, workbooks, and presentations.

The deployment builder deliberately excludes local databases, virtual environments, tests, Git data, and `__pycache__`.
