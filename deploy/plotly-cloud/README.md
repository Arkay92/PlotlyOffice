# PlotlyOffice

A self-hosted, collaborative OneDrive-style document & folder hub for the **Plotly Office Suite**, built with Python, Flask, and Dash.

## Features

- **Folder & File Management**: Create nested folders, drag-and-drop or multi-file upload (up to 25 MB), search, filter by suite app, sort, star, preview (images, PDF, text/CSV/Markdown), download, and trash/restore.
- **Office Suite Deep-Linking**:
  - **PlotlyWord**: `.docx`, `.doc`, `.rtf`, `.odt`, `.txt`, `.md`, `.html`, `.htm`
  - **PlotlySheets**: `.xlsx`, `.xlsm`, `.xltx`, `.xls`, `.ods`, `.csv`, `.tsv`, `.plotsheet`
  - **PlotlySlides**: `.pptx`, `.plotslide`
  - Create blank `.docx`, `.xlsx`, and `.pptx` files directly from **+ New** or upload existing files and click **Open in Word / Sheets / Slides** to import & launch them in the corresponding app.
- **Sharing & Co-Working**:
  - Capability-link sharing (`Can edit` / `Can view`) on both folders and individual files, with inherited permissions for subfolders and files.
  - Real-time collaborator presence avatars, threaded comments, and activity logs.
  - Collaborators opening a linked Office file from a shared folder land in the **same live collaborative document** in PlotlyWord, PlotlySheets, or PlotlySlides with matching `edit`/`view` permissions.

## Quick Start

```bash
pip install -r requirements.txt
python app.py
```

By default `PlotlyOffice` runs on `http://127.0.0.1:8050` and stores data in `data/office.sqlite3`.

### Configuring Suite App URLs

Set environment variables before starting `app.py` (or edit them anytime in the **⚙ Suite** dialog in the UI):

- `PLOTLYWORD_URL` (default: `http://127.0.0.1:8052`)
- `PLOTLYSHEETS_URL` (default: `http://127.0.0.1:8053`)
- `PLOTLYSLIDES_URL` (default: `http://127.0.0.1:8054`)
- `PLOTLYOFFICE_DATABASE` (default: `data/office.sqlite3`)

## Docker

```bash
docker compose up --build
```

## Tests

```bash
pytest -v
```
