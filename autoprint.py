#!/usr/bin/env python3
"""
AutoPrint - Automatic weekly color print.
Helps prevent printhead nozzle clogging, especially yellow.

Canon GX2050 MegaTank: tank ink does not dry out,
but fine printhead nozzles can clog when idle for long periods.

Image source:
- Drop PNG/JPG/PDF files into /data/images/ (or upload via the web UI).
- AutoPrint round-robins through them (oldest-printed first).
- An empty folder falls back to a synthetic color test page.
- A slim maintenance strip is always appended so every ink channel
  is exercised, regardless of what the user image looks like.
"""
import json
import logging
import os
import shutil
import subprocess
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

import schedule
from flask import Flask, jsonify, render_template, request, send_file
from PIL import Image, ImageDraw, ImageFont
from werkzeug.utils import secure_filename

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration from environment variables
# ---------------------------------------------------------------------------
PRINTER_NAME  = os.environ.get("PRINTER_NAME", "AutoPrinter")
PRINTER_URI   = os.environ.get("PRINTER_URI", "")
PRINT_WEEKDAY = os.environ.get("PRINT_WEEKDAY", "monday").lower()
PRINT_TIME    = os.environ.get("PRINT_TIME", "10:00")
DATA_DIR      = Path(os.environ.get("DATA_DIR", "/data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)
IMAGE_DIR     = DATA_DIR / "images"
IMAGE_DIR.mkdir(parents=True, exist_ok=True)
STATE_FILE    = DATA_DIR / "state.json"
ALLOWED_EXT   = {".png", ".jpg", ".jpeg", ".pdf"}

# Always-on by default. Set MAINTENANCE_STRIP=off to disable the
# color strip on user images (the synthetic fallback page is unaffected).
MAINTENANCE_STRIP_ENABLED = os.environ.get("MAINTENANCE_STRIP", "on").strip().lower() in (
    "on", "1", "true", "yes",
)

# A4 @ 150 DPI
PAGE_W, PAGE_H = 1240, 1754
STRIP_H = int(PAGE_H * 0.12)  # ~210 px


# ---------------------------------------------------------------------------
# Runtime state (last print, history, per-image last-printed)
# ---------------------------------------------------------------------------

def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except Exception:
            pass
    return {"last_print": None, "print_count": 0, "history": [], "image_history": {}}


def save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, indent=2, ensure_ascii=False))


# ---------------------------------------------------------------------------
# Image generation
# ---------------------------------------------------------------------------

def _get_font(size: int) -> ImageFont.ImageFont:
    for path in [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf",
    ]:
        try:
            return ImageFont.truetype(path, size)
        except Exception:
            pass
    return ImageFont.load_default()


def _paint_maintenance_strip(draw: ImageDraw.ImageDraw, x0: int, y0: int, w: int, h: int) -> None:
    """Paint a slim maintenance color strip into the given rectangle.

    Exercises every ink channel with extra yellow emphasis so the
    GX2050 nozzles stay clear regardless of the surrounding content.
    """
    # 1. Rainbow band (top quarter)
    rainbow = [
        (220,   0,   0), (255, 110,   0), (255, 240,   0), (  0, 200,   0),
        (  0, 210, 210), (  0,   0, 220), (160,   0, 220), (220,   0, 160),
    ]
    band_h = max(h // 4, 1)
    seg_w  = w / len(rainbow)
    for i, c in enumerate(rainbow):
        sx = int(x0 + i * seg_w)
        ex = int(x0 + (i + 1) * seg_w)
        draw.rectangle([sx, y0, ex, y0 + band_h], fill=c)

    # 2. CMYK + extra Yellow blocks (middle half)
    blocks_y = y0 + band_h
    blocks_h = h // 2
    blocks = [
        ((  0, 210, 210), "C"),
        ((210,   0, 210), "M"),
        ((255, 240,   0), "Y"),
        ((255, 240,   0), "Y"),   # Extra yellow coverage
        ((  0,   0,   0), "K"),
    ]
    bw = w / len(blocks)
    label_size = max(min(blocks_h - 10, 60), 20)
    font = _get_font(label_size)
    for i, (color, lbl) in enumerate(blocks):
        sx = int(x0 + i * bw)
        ex = int(x0 + (i + 1) * bw) - 2
        draw.rectangle([sx, blocks_y, ex, blocks_y + blocks_h], fill=color)
        tc = (255, 255, 255) if color == (0, 0, 0) else (40, 40, 40)
        try:
            tw = draw.textlength(lbl, font=font)
        except Exception:
            tw = len(lbl) * label_size * 0.6
        draw.text(
            (sx + (ex - sx) // 2 - int(tw) // 2, blocks_y + blocks_h // 6),
            lbl, fill=tc, font=font,
        )

    # 3. Footer band (bottom quarter): date + printer name
    footer_y = blocks_y + blocks_h
    footer_h = h - (footer_y - y0)
    draw.rectangle([x0, footer_y, x0 + w, y0 + h], fill=(248, 248, 248))
    info_font = _get_font(max(min(footer_h // 2, 26), 14))
    date_str = datetime.now().strftime("%d.%m.%Y %H:%M")
    draw.text(
        (x0 + 14, footer_y + max(footer_h // 6, 4)),
        f"AutoPrint maintenance • {PRINTER_NAME} • {date_str}",
        fill=(80, 80, 80), font=info_font,
    )


def build_color_test_image() -> Image.Image:
    """Synthetic A4 color test page used when no user images are available."""
    W, H = PAGE_W, PAGE_H
    img  = Image.new("RGB", (W, H), "white")
    draw = ImageDraw.Draw(img)

    # Top half: rainbow stripes + large CMYK+W blocks
    rainbow = [
        (220,   0,   0), (255, 110,   0), (255, 240,   0), (  0, 200,   0),
        (  0, 210, 210), (  0,   0, 220), (160,   0, 220), (220,   0, 160),
    ]
    band_h = (H // 4) // len(rainbow)
    for i, color in enumerate(rainbow):
        y0 = i * band_h
        draw.rectangle([0, y0, W, y0 + band_h - 1], fill=color)

    row2_y = H // 4
    row2_h = H // 5
    cmyk_blocks = [
        ((  0, 210, 210), "C"),
        ((210,   0, 210), "M"),
        ((255, 240,   0), "Y"),
        ((  0,   0,   0), "K"),
        ((255, 255, 255), "W"),
    ]
    bw = W // len(cmyk_blocks)
    font_big = _get_font(80)
    for i, (color, label) in enumerate(cmyk_blocks):
        x0 = i * bw
        draw.rectangle([x0, row2_y, x0 + bw - 2, row2_y + row2_h], fill=color)
        text_color = (255, 255, 255) if color == (0, 0, 0) else (30, 30, 30)
        draw.text((x0 + bw // 4, row2_y + row2_h // 3), label, fill=text_color, font=font_big)

    # Gradient Yellow->Red and Cyan->Blue
    row3_y = row2_y + row2_h
    row3_h = H // 7
    for x in range(W):
        if x < W // 2:
            t = x / (W // 2)
            r, g, b = 255, int(240 * (1 - t)), 0
        else:
            t = (x - W // 2) / (W // 2)
            r, g, b = 0, int(200 * (1 - t)), 220
        draw.line([x, row3_y, x, row3_y + row3_h - 1], fill=(r, g, b))

    # Mixed color fields
    row4_y = row3_y + row3_h
    row4_h = H // 7
    mixed = [
        (255, 128,   0), (200, 255,   0), (  0, 255, 130), (  0, 130, 255),
        (130,   0, 255), (255,   0, 130), (255, 210,   0), (  0, 255, 255),
    ]
    pw = W // len(mixed)
    for i, c in enumerate(mixed):
        x0 = i * pw
        draw.rectangle([x0, row4_y, x0 + pw - 2, row4_y + row4_h], fill=c)

    # Footer info (no separate maintenance strip needed - whole page is a test page)
    footer_y = row4_y + row4_h
    draw.rectangle([0, footer_y, W, H], fill=(248, 248, 248))
    font_title = _get_font(46)
    font_sub   = _get_font(36)
    date_str   = datetime.now().strftime("%d.%m.%Y %H:%M")
    draw.text((40, footer_y +  28), "AutoPrint - Printhead Maintenance", fill=(40,  40,  40),  font=font_title)
    draw.text((40, footer_y +  90), f"Printed at:  {date_str}",          fill=(100, 100, 100), font=font_sub)
    draw.text((40, footer_y + 135), f"Printer:     {PRINTER_NAME}",      fill=(100, 100, 100), font=font_sub)
    return img


def build_strip_only_page() -> Image.Image:
    """A4 page used as the appended maintenance page for user PDFs."""
    img  = Image.new("RGB", (PAGE_W, PAGE_H), "white")
    draw = ImageDraw.Draw(img)
    title = _get_font(48)
    sub   = _get_font(28)
    draw.text((40, 70),  "Maintenance color test",                    fill=(60, 60, 60),    font=title)
    draw.text((40, 140), "All ink nozzles exercised below - keeps",   fill=(120, 120, 120), font=sub)
    draw.text((40, 180), "the Canon GX2050 printhead clear.",         fill=(120, 120, 120), font=sub)
    _paint_maintenance_strip(draw, 0, PAGE_H - STRIP_H, PAGE_W, STRIP_H)
    return img


def fit_raster_to_a4(src: Path, with_strip: bool) -> Image.Image:
    """Load a user raster image, fit it to A4 portrait, optionally compositing
    the maintenance strip into the bottom of the same page."""
    strip_h  = STRIP_H if with_strip else 0
    target_h = PAGE_H - strip_h

    canvas = Image.new("RGB", (PAGE_W, PAGE_H), "white")
    with Image.open(src) as user:
        # Flatten transparency on white
        if user.mode in ("RGBA", "LA") or (user.mode == "P" and "transparency" in user.info):
            user = user.convert("RGBA")
            bg = Image.new("RGB", user.size, "white")
            bg.paste(user, mask=user.split()[-1])
            user = bg
        else:
            user = user.convert("RGB")
        user.thumbnail((PAGE_W, target_h), Image.LANCZOS)
        ox = (PAGE_W - user.width) // 2
        oy = (target_h - user.height) // 2
        canvas.paste(user, (ox, oy))

    if with_strip:
        draw = ImageDraw.Draw(canvas)
        _paint_maintenance_strip(draw, 0, target_h, PAGE_W, strip_h)
    return canvas


# ---------------------------------------------------------------------------
# Image selection (round-robin) + print preparation
# ---------------------------------------------------------------------------

def pick_next_image() -> Optional[Path]:
    """Return the next user image to print (oldest-printed first), or None if
    the images folder is empty - in which case the synthetic page is used."""
    files = [
        p for p in IMAGE_DIR.iterdir()
        if p.is_file() and p.suffix.lower() in ALLOWED_EXT
    ]
    if not files:
        return None
    image_history = load_state().get("image_history", {})
    return min(files, key=lambda p: (image_history.get(p.name, ""), p.name.lower()))


def prepare_print_file(src: Optional[Path]) -> Path:
    """Build a print-ready PDF in DATA_DIR and return its path.

    - src is None:       synthetic full-page color test (no extra strip needed).
    - src is a PDF:      original pages, then an appended maintenance-strip page
                         (skipped if MAINTENANCE_STRIP=off, in which case the
                         user PDF is passed through unchanged).
    - src is a raster:   image fit to A4, with the maintenance strip composited
                         into the bottom of the same page (when enabled).
    """
    ts  = datetime.now().strftime("%Y%m%d_%H%M%S")
    out = DATA_DIR / f"print_{ts}.pdf"

    if src is None:
        build_color_test_image().save(str(out), "PDF", resolution=150.0)
        return out

    if src.suffix.lower() == ".pdf":
        if not MAINTENANCE_STRIP_ENABLED:
            shutil.copy(src, out)
            return out
        from pypdf import PdfReader, PdfWriter
        strip_tmp = DATA_DIR / f"_strip_{ts}.pdf"
        build_strip_only_page().save(str(strip_tmp), "PDF", resolution=150.0)
        writer = PdfWriter()
        for page in PdfReader(str(src)).pages:
            writer.add_page(page)
        for page in PdfReader(str(strip_tmp)).pages:
            writer.add_page(page)
        with open(out, "wb") as f:
            writer.write(f)
        strip_tmp.unlink(missing_ok=True)
        return out

    fit_raster_to_a4(src, MAINTENANCE_STRIP_ENABLED).save(str(out), "PDF", resolution=150.0)
    return out


# ---------------------------------------------------------------------------
# Print execution
# ---------------------------------------------------------------------------

def _check_printer_reachable() -> bool:
    """Direct IPP connectivity test using ipptool (no CUPS queue required)."""
    if not PRINTER_URI:
        return False
    try:
        result = subprocess.run(
            ["ipptool", "-T", "10", "-q", PRINTER_URI, "get-printer-attributes.test"],
            capture_output=True, timeout=15,
        )
        return result.returncode == 0
    except Exception as exc:
        log.warning(f"ipptool check failed: {exc}")
        return False


def do_print(manual: bool = False, image: Optional[Path] = None) -> bool:
    success = False
    msg     = ""

    if not _check_printer_reachable():
        msg = (
            f"Printer not reachable: {PRINTER_URI}\n"
            "Check DNS/hostname resolution and power/network state."
        )
        log.error(msg)
        _record(success=False, manual=manual, msg=msg, image=image)
        return False

    if image is None:
        image = pick_next_image()

    try:
        out_path = prepare_print_file(image)
    except Exception as exc:
        log.error(f"Print preparation failed: {exc}")
        _record(success=False, manual=manual, msg=str(exc), image=image)
        return False

    try:
        result = subprocess.run(
            [
                "lp", "-d", PRINTER_NAME,
                "-o", "media=A4",
                "-o", "print-color-mode=color",
                "-o", "print-quality=5",
                str(out_path),
            ],
            capture_output=True, text=True, timeout=30,
        )
        success = result.returncode == 0
        msg = result.stdout.strip() or result.stderr.strip()
        label = image.name if image else "synthetic"
        if success:
            log.info(f"Print job submitted ({label}): {msg}")
        else:
            log.error(f"Print failed (rc={result.returncode}, {label}): {msg}")
    except subprocess.TimeoutExpired:
        msg = "Print command timed out"
        log.error(msg)
    except FileNotFoundError:
        msg = "'lp' not found - CUPS unavailable?"
        log.error(msg)
    except Exception as exc:
        msg = str(exc)
        log.error(f"Print error: {exc}")

    _record(success=success, manual=manual, msg=msg if not success else "OK", image=image)

    # Cleanup old generated outputs, keep only latest 5
    for old in sorted(DATA_DIR.glob("print_*.pdf"))[:-5]:
        try:
            old.unlink()
        except Exception:
            pass

    return success


def _record(success: bool, manual: bool, msg: str, image: Optional[Path] = None) -> None:
    state   = load_state()
    now_iso = datetime.now().isoformat()
    if success:
        state["last_print"]  = now_iso
        state["print_count"] = state.get("print_count", 0) + 1
        if image is not None:
            state.setdefault("image_history", {})[image.name] = now_iso
    history = state.get("history", [])
    history.insert(0, {
        "time":    datetime.now().strftime("%d.%m.%Y %H:%M"),
        "success": success,
        "manual":  manual,
        "msg":     msg,
        "image":   image.name if image else None,
    })
    state["history"] = history[:20]
    save_state(state)


# ---------------------------------------------------------------------------
# Weekly schedule
# ---------------------------------------------------------------------------
WEEKDAY_DE = {
    "monday":    "Monday",
    "tuesday":   "Tuesday",
    "wednesday": "Wednesday",
    "thursday":  "Thursday",
    "friday":    "Friday",
    "saturday":  "Saturday",
    "sunday":    "Sunday",
}


def run_scheduler() -> None:
    try:
        getattr(schedule.every(), PRINT_WEEKDAY).at(PRINT_TIME).do(do_print)
        log.info(f"Scheduled: every {PRINT_WEEKDAY} at {PRINT_TIME}")
    except AttributeError:
        log.error(f"Invalid PRINT_WEEKDAY value: '{PRINT_WEEKDAY}'. Falling back to 'monday'.")
        schedule.every().monday.at(PRINT_TIME).do(do_print)

    while True:
        schedule.run_pending()
        time.sleep(30)


# ---------------------------------------------------------------------------
# Flask web UI
# ---------------------------------------------------------------------------
app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 10 * 1024 * 1024  # 10 MB upload cap
_scheduler_started = False
_scheduler_lock    = threading.Lock()


def _format_last_printed(iso: Optional[str]) -> str:
    if not iso:
        return "never"
    try:
        return datetime.fromisoformat(iso).strftime("%Y-%m-%d %H:%M")
    except Exception:
        return iso


def _list_images() -> list:
    state         = load_state()
    image_history = state.get("image_history", {})
    out = []
    for p in sorted(IMAGE_DIR.iterdir(), key=lambda x: x.name.lower()) if IMAGE_DIR.exists() else []:
        if not p.is_file() or p.suffix.lower() not in ALLOWED_EXT:
            continue
        out.append({
            "name":         p.name,
            "last_printed": _format_last_printed(image_history.get(p.name)),
        })
    return out


def _render(msg=None, msg_class=None):
    state     = load_state()
    reachable = _check_printer_reachable()
    return render_template(
        "dashboard.html",
        printer_name      = PRINTER_NAME,
        printer_uri       = PRINTER_URI,
        printer_ok        = reachable,
        weekday           = WEEKDAY_DE.get(PRINT_WEEKDAY, PRINT_WEEKDAY),
        print_time        = PRINT_TIME,
        last_print        = _format_last_printed(state.get("last_print")) if state.get("last_print") else "Never",
        print_count       = state.get("print_count", 0),
        history           = state.get("history", []),
        images            = _list_images(),
        maintenance_strip = MAINTENANCE_STRIP_ENABLED,
        msg               = msg,
        msg_class         = msg_class,
    )


@app.route("/")
def index():
    return _render()


@app.route("/print_now")
def print_now():
    threading.Thread(target=do_print, kwargs={"manual": True}, daemon=True).start()
    return _render(msg="Print job started.", msg_class="ok")


@app.route("/preview")
def preview():
    """Render an on-screen PNG of what the next print will look like."""
    image = pick_next_image()
    p = DATA_DIR / "preview.png"
    if image is None:
        img = build_color_test_image()
    elif image.suffix.lower() == ".pdf":
        img = build_strip_only_page()
    else:
        img = fit_raster_to_a4(image, MAINTENANCE_STRIP_ENABLED)
    img.save(str(p), "PNG")
    return send_file(str(p), mimetype="image/png")


@app.route("/upload", methods=["POST"])
def upload():
    f = request.files.get("file")
    if not f or not f.filename:
        return _render(msg="No file selected.", msg_class="err")
    name = secure_filename(f.filename)
    if not name:
        return _render(msg="Invalid filename.", msg_class="err")
    ext = Path(name).suffix.lower()
    if ext not in ALLOWED_EXT:
        return _render(msg=f"Unsupported file type: {ext}", msg_class="err")
    dest = IMAGE_DIR / name
    try:
        f.save(str(dest))
    except Exception as exc:
        log.error(f"Upload failed: {exc}")
        return _render(msg=f"Upload failed: {exc}", msg_class="err")
    log.info(f"Uploaded image: {name}")
    return _render(msg=f"Uploaded: {name}", msg_class="ok")


@app.errorhandler(413)
def _too_large(_e):
    return _render(msg="Upload too large (max 10 MB).", msg_class="err"), 413


@app.route("/images/<name>/print", methods=["POST"])
def print_image(name):
    safe = secure_filename(name)
    src  = IMAGE_DIR / safe
    if not src.exists():
        return _render(msg=f"Not found: {safe}", msg_class="err")
    threading.Thread(target=do_print, kwargs={"manual": True, "image": src}, daemon=True).start()
    return _render(msg=f"Printing: {safe}", msg_class="ok")


@app.route("/images/<name>/delete", methods=["POST"])
def delete_image(name):
    safe = secure_filename(name)
    src  = IMAGE_DIR / safe
    if not src.exists():
        return _render(msg=f"Not found: {safe}", msg_class="err")
    try:
        src.unlink()
    except Exception as exc:
        return _render(msg=f"Delete failed: {exc}", msg_class="err")
    state = load_state()
    if state.get("image_history", {}).pop(safe, None) is not None:
        save_state(state)
    return _render(msg=f"Deleted: {safe}", msg_class="ok")


@app.route("/status")
def status():
    state = load_state()
    state["printer_reachable"] = _check_printer_reachable()
    state["printer_uri"]       = PRINTER_URI
    state["printer_name"]      = PRINTER_NAME
    state["maintenance_strip"] = MAINTENANCE_STRIP_ENABLED
    state["images"]            = [img["name"] for img in _list_images()]
    return jsonify(state)


def start_background_services() -> None:
    """Start scheduler exactly once (needed for gunicorn import mode)."""
    global _scheduler_started
    with _scheduler_lock:
        if _scheduler_started:
            return
        threading.Thread(target=run_scheduler, daemon=True).start()
        _scheduler_started = True
        log.info(
            f"AutoPrint ready | Printer: {PRINTER_NAME} "
            f"| Schedule: {PRINT_WEEKDAY} @ {PRINT_TIME} "
            f"| MaintenanceStrip: {'on' if MAINTENANCE_STRIP_ENABLED else 'off'}"
        )


start_background_services()


# ---------------------------------------------------------------------------
# Start
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080)
