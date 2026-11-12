"""Record a walkthrough GIF of PlotlyOffice at https://plotly-office.plotly.app."""
import argparse
import io
import os
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent.parent
ARTIFACTS = ROOT / "artifacts"
ARTIFACTS.mkdir(exist_ok=True)
GIF_PATH = ARTIFACTS / "plotlyoffice-walkthrough.gif"
POSTER_PATH = ARTIFACTS / "plotlyoffice-walkthrough-poster.png"
DEFAULT_URL = "https://plotly-office.plotly.app"


def add_caption(img: Image.Image, step: str, subtitle: str) -> Image.Image:
    out = img.convert("RGBA")
    overlay = Image.new("RGBA", out.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    w, h = out.size
    bar_h = 56
    draw.rectangle([0, h - bar_h, w, h], fill=(24, 55, 47, 232))
    draw.rectangle([0, h - bar_h, w, h - bar_h + 3], fill=(215, 238, 133, 255))
    try:
        font_title = ImageFont.truetype("segoeui.ttf", 18)
        font_sub = ImageFont.truetype("segoeui.ttf", 14)
    except Exception:
        font_title = ImageFont.load_default()
        font_sub = ImageFont.load_default()
    draw.text((22, h - bar_h + 9), step, fill=(215, 238, 133, 255), font=font_title)
    draw.text((22, h - bar_h + 31), subtitle, fill=(230, 239, 233, 255), font=font_sub)
    return Image.alpha_composite(out, overlay).convert("RGB")


def snap(page, frames: list[tuple[Image.Image, int]], step: str, subtitle: str, duration_ms: int = 1600):
    raw = page.screenshot(type="png")
    img = Image.open(io.BytesIO(raw))
    frames.append((add_caption(img, step, subtitle), duration_ms))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default=DEFAULT_URL)
    parser.add_argument("--local", action="store_true", help="Run a temporary local PlotlyOffice server")
    args = parser.parse_args()

    proc = None
    tmpdir = None
    target_url = args.url

    if args.local:
        import socket
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        tmpdir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        env = os.environ.copy()
        env["HOST"] = "127.0.0.1"
        env["PORT"] = str(port)
        env["PLOTLYOFFICE_DATABASE"] = str(Path(tmpdir.name) / "office.sqlite3")
        env["PLOTLYSHEETS_DATABASE"] = str(Path(tmpdir.name) / "sheets.sqlite3")
        env["PLOTLYSLIDES_DATABASE"] = str(Path(tmpdir.name) / "slides.sqlite3")
        env["PLOTLYWORD_DATABASE"] = str(Path(tmpdir.name) / "documents.sqlite3")
        proc = subprocess.Popen(
            [sys.executable, "app.py"],
            cwd=str(ROOT),
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        target_url = f"http://127.0.0.1:{port}"
        ready = False
        for _ in range(90):
            try:
                if urllib.request.urlopen(f"{target_url}/api/health", timeout=2).status == 200:
                    ready = True
                    break
            except Exception:
                time.sleep(0.5)
        if not ready:
            raise RuntimeError("Local PlotlyOffice server did not become healthy in time.")

    frames: list[tuple[Image.Image, int]] = []
    try:
        with sync_playwright() as p:
            browser = None
            for kwargs in ({"headless": True, "channel": "msedge"}, {"headless": True, "channel": "chrome"}, {"headless": True}):
                try:
                    browser = p.chromium.launch(**kwargs)
                    break
                except Exception:
                    continue
            if browser is None:
                raise RuntimeError("No Chromium/Chrome/Edge browser available for Playwright.")
            context = browser.new_context(viewport={"width": 1280, "height": 800})
            page = context.new_page()
            page.on("pageerror", lambda err: print("PAGEERROR:", err))
            page.on("console", lambda msg: print("CONSOLE:", msg.type, msg.text) if msg.type == "error" else None)

            print(f"Opening {target_url} ...")
            page.goto(target_url, wait_until="networkidle", timeout=60000)

            # Wait for Plotly Cloud wake-up page (if sleeping) and iframe /drive to render
            deadline = time.time() + 90
            drive_frame = None
            while time.time() < deadline:
                for fr in page.frames:
                    if "/drive" in fr.url:
                        try:
                            if fr.locator("#btn-new").count() > 0:
                                drive_frame = fr
                                break
                        except Exception:
                            pass
                if drive_frame is not None:
                    break
                time.sleep(1.5)

            if drive_frame is None:
                raise RuntimeError(f"Timed out waiting for PlotlyOffice /drive to load. Frames: {[f.url for f in page.frames]}")

            time.sleep(0.8)
            snap(page, frames, "1. OneDrive-style Folder View", "PlotlyOffice loads directly into your collaborative Drive workspace.", 1800)

            # Create a folder via + New -> New Folder
            drive_frame.locator("#btn-new").click()
            time.sleep(0.3)
            snap(page, frames, "2. Create Folders & Office Files", "Click + New to create folders or blank Word, Sheets, and Slides files.", 1500)

            page.once("dialog", lambda d: d.accept("Q4 Launch"))
            drive_frame.locator('#new-menu button[data-new="folder"]').click()
            drive_frame.locator(".item-name", has_text="Q4 Launch").wait_for(timeout=10000)
            time.sleep(0.5)
            snap(page, frames, "3. Folder Created", "New folder 'Q4 Launch' appears in My Files.", 1400)

            # Open the folder
            drive_frame.locator(".item-name", has_text="Q4 Launch").click()
            drive_frame.locator(".view-heading", has_text="Q4 Launch").wait_for(timeout=10000)
            time.sleep(0.5)

            # Upload a CSV file
            sample_csv = b"Quarter,Revenue,Target\nQ1,120000,110000\nQ2,145000,130000\nQ3,168000,155000\nQ4,195000,180000\n"
            drive_frame.locator("#file-input").set_input_files(
                [{"name": "Q4_Metrics.csv", "mimeType": "text/csv", "buffer": sample_csv}]
            )
            drive_frame.locator(".item-name", has_text="Q4_Metrics.csv").wait_for(timeout=15000)

            # Create a blank Word doc and Slides deck inside the folder
            page.once("dialog", lambda d: d.accept("Launch_Brief"))
            drive_frame.locator("#btn-new").click()
            drive_frame.locator('#new-menu button[data-new="word"]').click()
            drive_frame.locator(".item-name", has_text="Launch_Brief.docx").wait_for(timeout=15000)

            page.once("dialog", lambda d: d.accept("Board_Deck"))
            drive_frame.locator("#btn-new").click()
            drive_frame.locator('#new-menu button[data-new="slides"]').click()
            drive_frame.locator(".item-name", has_text="Board_Deck.pptx").wait_for(timeout=15000)
            time.sleep(0.6)
            snap(page, frames, "4. Upload & Manage Suite Files", "Uploaded Q4_Metrics.csv and created Word & Slides files inside Q4 Launch.", 1800)

            # Toggle List view
            drive_frame.locator('.view-toggle button[data-layout="list"]').click()
            time.sleep(0.4)
            snap(page, frames, "5. Grid & List Views", "Switch between Grid cards and detailed List view with suite badges.", 1500)
            drive_frame.locator('.view-toggle button[data-layout="grid"]').click()
            time.sleep(0.3)

            # Share folder modal
            drive_frame.locator("#btn-folder-share").click()
            drive_frame.locator("#share-dialog[open]").wait_for(timeout=5000)
            drive_frame.locator('.create-share-btn[data-role="edit"]').click()
            drive_frame.locator(".share-link-row").first.wait_for(timeout=10000)
            time.sleep(0.4)
            snap(page, frames, "6. Capability-Link Folder Sharing", "Create Can edit / Can view links with inherited access to all files inside.", 1900)
            drive_frame.locator("#share-dialog button", has_text="✕").click()
            time.sleep(0.3)

            # Comments drawer
            drive_frame.locator("#btn-folder-comments").click()
            drive_frame.locator("#comment-input").wait_for(timeout=5000)
            drive_frame.locator("#comment-input").fill("Ready for Q4 review — please check the metrics sheet!")
            drive_frame.locator('#comment-form button[type="submit"]').click()
            drive_frame.locator(".comment-card").first.wait_for(timeout=10000)
            time.sleep(0.4)
            snap(page, frames, "7. Co-Working Comments & Presence", "Collaborators see live presence avatars, comments, and activity logs.", 1800)
            drive_frame.locator("#close-drawer").click()
            time.sleep(0.3)

            # Open Q4_Metrics.csv in PlotlySheets
            csv_card = drive_frame.locator(".item-card", has_text="Q4_Metrics.csv")
            with context.expect_page(timeout=30000) as new_page_info:
                csv_card.locator(".suite-open-btn").click()
            sheets_page = new_page_info.value
            sheets_page.wait_for_load_state("domcontentloaded", timeout=30000)
            # Wait for spreadsheet grid / title to render
            sheets_page.wait_for_load_state("networkidle", timeout=30000)
            if sheets_page.locator("#session-name").is_visible():
                sheets_page.locator("#session-name").fill("Alex Morgan")
                sheets_page.locator("#session-continue").click()
            sheets_page.locator("#grid").wait_for(state="visible", timeout=30000)
            time.sleep(1.2)
            snap(sheets_page, frames, "8. Opens Directly in PlotlySheets", "Supported files import & open in PlotlySheets with live co-working links.", 2400)

            # Save poster from step 4 or step 8
            frames[3][0].save(POSTER_PATH)

            # Quantize & save animated GIF
            pal_frames = [f.quantize(colors=256, method=Image.Quantize.MEDIANCUT) for f, _ in frames]
            durations = [d for _, d in frames]
            pal_frames[0].save(
                GIF_PATH,
                save_all=True,
                append_images=pal_frames[1:],
                duration=durations,
                loop=0,
                optimize=True,
            )
        print(f"Saved walkthrough GIF: {GIF_PATH} ({GIF_PATH.stat().st_size // 1024} KB)")
        print(f"Saved poster PNG: {POSTER_PATH}")
        browser.close()
    finally:
        if proc is not None:
            proc.terminate()
            proc.wait(timeout=5)
        if tmpdir is not None:
            tmpdir.cleanup()


if __name__ == "__main__":
    main()
