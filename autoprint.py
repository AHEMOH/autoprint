#!/usr/bin/env python3
"""
AutoPrint - Automatic weekly color print.
Helps prevent printhead nozzle clogging, especially yellow.

Canon GX2050 MegaTank: tank ink does not dry out,
but fine printhead nozzles can clog when idle for long periods.
"""
import json
import logging
import os
import subprocess
import threading
import time
from datetime import datetime
from pathlib import Path

import schedule
from flask import Flask, jsonify, render_template_string, send_file
from PIL import Image, ImageDraw, ImageFont

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration from environment variables
# ---------------------------------------------------------------------------
PRINTER_NAME = os.environ.get("PRINTER_NAME", "AutoPrinter")
PRINTER_URI  = os.environ.get("PRINTER_URI", "")
PRINT_WEEKDAY = os.environ.get("PRINT_WEEKDAY", "monday").lower()
PRINT_TIME    = os.environ.get("PRINT_TIME", "10:00")
DATA_DIR      = Path(os.environ.get("DATA_DIR", "/data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)
STATE_FILE = DATA_DIR / "state.json"

# ---------------------------------------------------------------------------
# Runtime state (last print, history)
# ---------------------------------------------------------------------------

def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except Exception:
            pass
    return {"last_print": None, "print_count": 0, "history": []}


def save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, indent=2, ensure_ascii=False))


# ---------------------------------------------------------------------------
# Generate colorful test image
# Uses all color channels, with extra yellow coverage.
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


def generate_colorful_image(path: Path) -> None:
    """
    Create a colorful A4 test page (150 DPI = 1240x1754 px).
    Covers all CMYK channels with extra emphasis on yellow.
    Output: PDF (for reliable CUPS page geometry handling).
    """
    W, H = 1240, 1754  # A4 @ 150 DPI

    img  = Image.new("RGB", (W, H), "white")
    draw = ImageDraw.Draw(img)

    # ------------------------------------------------------------------
    # 1. Rainbow stripes (top quarter)
    # ------------------------------------------------------------------
    rainbow = [
        (220,   0,   0),   # Red
        (255, 110,   0),   # Orange
        (255, 240,   0),   # Yellow
        (  0, 200,   0),   # Green
        (  0, 210, 210),   # Cyan
        (  0,   0, 220),   # Blue
        (160,   0, 220),   # Violet
        (220,   0, 160),   # Magenta/Pink
    ]
    band_h = (H // 4) // len(rainbow)
    for i, color in enumerate(rainbow):
        y0 = i * band_h
        draw.rectangle([0, y0, W, y0 + band_h - 1], fill=color)

    # ------------------------------------------------------------------
    # 2. Large CMYK blocks (second quarter)
    # ------------------------------------------------------------------
    row2_y = H // 4
    row2_h = H // 5
    cmyk_blocks = [
        ((  0, 210, 210), "C"),   # Cyan
        ((210,   0, 210), "M"),   # Magenta
        ((255, 240,   0), "Y"),   # Yellow (large area)
        ((  0,   0,   0), "K"),   # Black (Key)
        ((255, 255, 255), "W"),   # White (paper)
    ]
    bw = W // len(cmyk_blocks)
    font_big = _get_font(80)
    for i, (color, label) in enumerate(cmyk_blocks):
        x0 = i * bw
        draw.rectangle([x0, row2_y, x0 + bw - 2, row2_y + row2_h], fill=color)
        text_color = (255, 255, 255) if color == (0, 0, 0) else (30, 30, 30)
        draw.text((x0 + bw // 4, row2_y + row2_h // 3), label, fill=text_color, font=font_big)

    # ------------------------------------------------------------------
    # 3. Gradient Yellow->Red and Cyan->Blue (third quarter, first half)
    # ------------------------------------------------------------------
    row3_y = row2_y + row2_h
    row3_h = H // 7
    for x in range(W):
        if x < W // 2:
            t = x / (W // 2)             # 0..1
            r, g, b = 255, int(240 * (1 - t)), 0          # Yellow -> Red
        else:
            t = (x - W // 2) / (W // 2)  # 0..1
            r, g, b = 0, int(200 * (1 - t)), 220           # Cyan -> Blue
        draw.line([x, row3_y, x, row3_y + row3_h - 1], fill=(r, g, b))

    # ------------------------------------------------------------------
    # 4. Mixed color fields (third quarter, second half)
    # ------------------------------------------------------------------
    row4_y = row3_y + row3_h
    row4_h = H // 7
    mixed = [
        (255, 128,   0),   # Orange
        (200, 255,   0),   # Yellow-green
        (  0, 255, 130),   # Mint green
        (  0, 130, 255),   # Sky blue
        (130,   0, 255),   # Lila
        (255,   0, 130),   # Pink
        (255, 210,   0),   # Golden yellow
        (  0, 255, 255),   # Light cyan
    ]
    pw = W // len(mixed)
    for i, c in enumerate(mixed):
        x0 = i * pw
        draw.rectangle([x0, row4_y, x0 + pw - 2, row4_y + row4_h], fill=c)

    # ------------------------------------------------------------------
    # 5. Footer with date and metadata
    # ------------------------------------------------------------------
    footer_y = row4_y + row4_h
    draw.rectangle([0, footer_y, W, H], fill=(248, 248, 248))

    font_title = _get_font(46)
    font_sub   = _get_font(36)

    date_str = datetime.now().strftime("%d.%m.%Y %H:%M")
    draw.text((40, footer_y + 28),  "AutoPrint - Printhead Maintenance",   fill=(40,  40,  40),  font=font_title)
    draw.text((40, footer_y + 90),  f"Printed at:  {date_str}",             fill=(100, 100, 100), font=font_sub)
    draw.text((40, footer_y + 135), f"Printer:     {PRINTER_NAME}",         fill=(100, 100, 100), font=font_sub)

    # Save as PDF for reliable CUPS page handling
    img.save(str(path), "PDF", resolution=150.0)
    log.info(f"Image generated: {path}")


# ---------------------------------------------------------------------------
# Execute print job
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


def do_print(manual: bool = False) -> bool:
    ts       = datetime.now().strftime("%Y%m%d_%H%M%S")
    img_path = DATA_DIR / f"print_{ts}.pdf"
    success  = False

    # Connectivity check via IPP before submitting CUPS job
    if not _check_printer_reachable():
        msg = (
            f"Printer not reachable: {PRINTER_URI}\n"
            "Check DNS/hostname resolution and power/network state."
        )
        log.error(msg)
        _record(success=False, manual=manual, msg=msg)
        return False

    # Bild erzeugen
    try:
        generate_colorful_image(img_path)
    except Exception as exc:
        log.error(f"Image generation failed: {exc}")
        _record(success=False, manual=manual, msg=str(exc))
        return False

    # Send to printer
    # Canon GX2050 defaults: A4, color mode, quality 5 (best)
    try:
        result = subprocess.run(
            [
                "lp", "-d", PRINTER_NAME,
                "-o", "media=A4",
                "-o", "print-color-mode=color",
                "-o", "print-quality=5",
                str(img_path),
            ],
            capture_output=True, text=True, timeout=30,
        )
        success = result.returncode == 0
        msg = result.stdout.strip() or result.stderr.strip()
        if success:
            log.info(f"Print job submitted: {msg}")
        else:
            log.error(f"Print failed (rc={result.returncode}): {msg}")
    except subprocess.TimeoutExpired:
        msg = "Print command timed out"
        log.error(msg)
    except FileNotFoundError:
        msg = "'lp' not found - CUPS unavailable?"
        log.error(msg)
    except Exception as exc:
        msg = str(exc)
        log.error(f"Print error: {exc}")

    _record(success=success, manual=manual, msg=msg if not success else "OK")

    # Cleanup old files, keep only latest 5
    for old in sorted(DATA_DIR.glob("print_*.pdf"))[:-5]:
        try:
            old.unlink()
        except Exception:
            pass

    return success


def _record(success: bool, manual: bool, msg: str) -> None:
    state = load_state()
    if success:
        state["last_print"]  = datetime.now().isoformat()
        state["print_count"] = state.get("print_count", 0) + 1
    history = state.get("history", [])
    history.insert(0, {
        "time":    datetime.now().strftime("%d.%m.%Y %H:%M"),
        "success": success,
        "manual":  manual,
        "msg":     msg,
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
_scheduler_started = False
_scheduler_lock = threading.Lock()

_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>AutoPrint</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:'Segoe UI',Arial,sans-serif;background:#f4f4f4;color:#333}
.wrap{max-width:620px;margin:40px auto;padding:0 16px}
h1{font-size:26px;margin-bottom:20px}
.rainbow{height:7px;border-radius:4px;margin-bottom:22px;
  background:linear-gradient(to right,red,orange,gold,green,cyan,blue,violet,deeppink)}
.card{background:#fff;border-radius:12px;padding:20px;margin-bottom:16px;
  box-shadow:0 2px 10px rgba(0,0,0,.07)}
.card h2{font-size:11px;text-transform:uppercase;letter-spacing:1px;color:#999;margin-bottom:14px}
.row{display:flex;justify-content:space-between;padding:7px 0;border-bottom:1px solid #f0f0f0}
.row:last-child{border-bottom:none}
.btn{display:inline-block;padding:11px 26px;border-radius:8px;font-size:14px;
  text-decoration:none;cursor:pointer;margin-top:6px;margin-right:8px}
.btn-blue{background:#1a73e8;color:#fff}.btn-blue:hover{background:#1558b0}
.btn-gray{background:#6c757d;color:#fff}.btn-gray:hover{background:#545b62}
.alert{margin-top:14px;padding:11px 14px;border-radius:7px;font-size:14px}
.ok{background:#d4edda;color:#155724}.err{background:#f8d7da;color:#721c24}
.hist{font-size:13px;padding:7px 0;border-bottom:1px solid #f5f5f5}
.hist:last-child{border-bottom:none}
.dot{display:inline-block;width:8px;height:8px;border-radius:50%;margin-right:6px}
.g{background:#28a745}.r{background:#dc3545}
</style>
</head>
<body>
<div class="wrap">
  <div class="rainbow"></div>
    <h1>AutoPrint</h1>

  <div class="card">
        <h2>Status</h2>
        <div class="row"><span>Printer</span>
      <strong>{{ printer_name }}
        {% if printer_ok %}
                    <span style="color:#28a745;font-size:11px"> &#x2714; reachable</span>
        {% else %}
                    <span style="color:#dc3545;font-size:11px"> &#x26a0; unreachable</span>
        {% endif %}
      </strong>
    </div>
    <div class="row"><span>URI</span><code style="font-size:12px">{{ printer_uri }}</code></div>
        <div class="row"><span>Schedule</span><strong>{{ weekday }}, {{ print_time }}</strong></div>
        <div class="row"><span>Last Print</span><strong>{{ last_print }}</strong></div>
        <div class="row"><span>Total Prints</span><strong>{{ print_count }}&times;</strong></div>
    {% if not printer_ok %}
    <div class="alert err" style="margin-top:10px;font-size:13px">
            &#x26a0; Printer unreachable. PRINTER_URI may be missing/incorrect,
            or hostname resolution may fail inside the container.<br>
            Fix: set <code>dns:</code> in <code>docker-compose.yml</code>,
            or use <code>extra_hosts</code> with the printer IP.
    </div>
    {% endif %}
  </div>

  <div class="card">
        <h2>Actions</h2>
        <a href="/print_now" class="btn btn-blue">Print Now</a>
        <a href="/preview"   class="btn btn-gray">Preview (PNG)</a>
    {% if msg %}
    <div class="alert {{ msg_class }}">{{ msg }}</div>
    {% endif %}
  </div>

  {% if history %}
  <div class="card">
    <h2>History</h2>
    {% for e in history %}
    <div class="hist">
      <span class="dot {{ 'g' if e.success else 'r' }}"></span>
      {{ e.time }}
    {% if e.manual %}<span style="color:#aaa">(manual)</span>{% endif %}
      {% if not e.success %}<span style="color:#c00"> – {{ e.msg }}</span>{% endif %}
    </div>
    {% endfor %}
  </div>
  {% endif %}
</div>
</body>
</html>"""


def _render(msg=None, msg_class=None):
    state     = load_state()
    last      = state.get("last_print")
    last_fmt  = datetime.fromisoformat(last).strftime("%Y-%m-%d %H:%M") if last else "Never"
    reachable = _check_printer_reachable()
    return render_template_string(
        _HTML,
        printer_name = PRINTER_NAME,
        printer_uri  = PRINTER_URI,
        printer_ok   = reachable,
        weekday      = WEEKDAY_DE.get(PRINT_WEEKDAY, PRINT_WEEKDAY),
        print_time   = PRINT_TIME,
        last_print   = last_fmt,
        print_count  = state.get("print_count", 0),
        history      = state.get("history", []),
        msg          = msg,
        msg_class    = msg_class,
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
    """Generate preview image and show it in browser (no print)."""
    p = DATA_DIR / "preview.png"
    # Save preview as PNG (not PDF) for direct browser rendering
    W, H = 1240, 1754
    img  = Image.new("RGB", (W, H), "white")
    draw = ImageDraw.Draw(img)

    rainbow = [
        (220,0,0),(255,110,0),(255,240,0),(0,200,0),
        (0,210,210),(0,0,220),(160,0,220),(220,0,160),
    ]
    band_h = (H // 4) // len(rainbow)
    for i, c in enumerate(rainbow):
        y0 = i * band_h
        draw.rectangle([0, y0, W, y0 + band_h - 1], fill=c)

    row2_y, row2_h = H // 4, H // 5
    cmyk = [((0,210,210),"C"),((210,0,210),"M"),((255,240,0),"Y"),((0,0,0),"K"),((255,255,255),"W")]
    bw = W // len(cmyk)
    font_b = _get_font(80)
    for i, (col, lbl) in enumerate(cmyk):
        x0 = i * bw
        draw.rectangle([x0, row2_y, x0+bw-2, row2_y+row2_h], fill=col)
        tc = (255,255,255) if col == (0,0,0) else (30,30,30)
        draw.text((x0+bw//4, row2_y+row2_h//3), lbl, fill=tc, font=font_b)

    row3_y, row3_h = row2_y + row2_h, H // 7
    for x in range(W):
        if x < W // 2:
            t = x / (W // 2); r,g,b = 255, int(240*(1-t)), 0
        else:
            t = (x - W//2) / (W//2); r,g,b = 0, int(200*(1-t)), 220
        draw.line([x, row3_y, x, row3_y+row3_h-1], fill=(r,g,b))

    row4_y, row4_h = row3_y + row3_h, H // 7
    mixed = [(255,128,0),(200,255,0),(0,255,130),(0,130,255),(130,0,255),(255,0,130),(255,210,0),(0,255,255)]
    pw = W // len(mixed)
    for i, c in enumerate(mixed):
        draw.rectangle([i*pw, row4_y, i*pw+pw-2, row4_y+row4_h], fill=c)

    footer_y = row4_y + row4_h
    draw.rectangle([0, footer_y, W, H], fill=(248,248,248))
    font_t = _get_font(46); font_s = _get_font(36)
    date_str = datetime.now().strftime("%d.%m.%Y %H:%M")
    draw.text((40, footer_y+28),  "AutoPrint - Printhead Maintenance", fill=(40,40,40),   font=font_t)
    draw.text((40, footer_y+90),  f"Preview: {date_str}",              fill=(100,100,100),font=font_s)
    draw.text((40, footer_y+135), f"Printer: {PRINTER_NAME}",          fill=(100,100,100),font=font_s)

    img.save(str(p), "PNG")
    return send_file(str(p), mimetype="image/png")


@app.route("/status")
def status():
    state = load_state()
    state["printer_reachable"] = _check_printer_reachable()
    state["printer_uri"]  = PRINTER_URI
    state["printer_name"] = PRINTER_NAME
    return jsonify(state)


def start_background_services() -> None:
    """Start scheduler exactly once (needed for gunicorn import mode)."""
    global _scheduler_started
    with _scheduler_lock:
        if _scheduler_started:
            return
        threading.Thread(target=run_scheduler, daemon=True).start()
        _scheduler_started = True
        log.info(f"AutoPrint ready | Printer: {PRINTER_NAME} | Schedule: {PRINT_WEEKDAY} @ {PRINT_TIME}")


start_background_services()


# ---------------------------------------------------------------------------
# Start
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080)
