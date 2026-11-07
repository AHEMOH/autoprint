#!/usr/bin/env python3
"""
AutoPrint - Automatic weekly color print.
Helps prevent printhead nozzle clogging, especially yellow.

Canon GX2050 MegaTank: tank ink does not dry out,
but fine printhead nozzles can clog when idle for long periods.

Image source:
- Drop PNG/JPG/PDF files into /data/images/, upload them via the web UI
  (multiple files, drag & drop) or import them from a URL.
- AutoPrint round-robins through them (oldest-printed first) or picks
  one at random, depending on the "image_order" setting.
- An empty folder falls back to a synthetic color test page.
- A slim maintenance strip is always added so every ink channel is
  exercised, regardless of what the user image looks like. By default
  the strip sits at the TOP of the page so the nozzles are flushed
  before the actual picture is printed.

Settings (quality, paper type, strip position, image order) live in
/data/settings.json and can be changed from the dashboard. Environment
variables only provide the initial defaults.
"""
import json
import logging
import os
import random
import re
import secrets
import subprocess
import tempfile
import threading
import time
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Optional

import schedule
from flask import (
    Flask, abort, flash, jsonify, redirect, render_template, request,
    send_file, url_for,
)
from PIL import Image, ImageDraw, ImageFont
from werkzeug.utils import secure_filename

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

# Bump this on every release - it shows up in the startup log, in /status,
# and in the dashboard header so you can verify which build is running.
VERSION = "2.2.0"

# ---------------------------------------------------------------------------
# Configuration from environment variables
# ---------------------------------------------------------------------------
PRINTER_NAME  = os.environ.get("PRINTER_NAME", "AutoPrinter")
PRINTER_URI   = os.environ.get("PRINTER_URI", "")
PRINT_WEEKDAY = os.environ.get("PRINT_WEEKDAY", "monday").lower()
PRINT_TIME    = os.environ.get("PRINT_TIME", "10:00")
DATA_DIR      = Path(os.environ.get("DATA_DIR", "/data"))
IMAGE_DIR     = DATA_DIR / "images"
THUMB_DIR     = DATA_DIR / "thumbs"
STATE_FILE    = DATA_DIR / "state.json"
SETTINGS_FILE = DATA_DIR / "settings.json"
CAPS_FILE     = DATA_DIR / "printer_caps.json"
for _d in (DATA_DIR, IMAGE_DIR, THUMB_DIR):
    _d.mkdir(parents=True, exist_ok=True)

ALLOWED_EXT       = {".png", ".jpg", ".jpeg", ".pdf"}
MAX_FILE_BYTES    = 10 * 1024 * 1024   # per file (upload + URL import)
MAX_REQUEST_BYTES = 50 * 1024 * 1024   # per upload request (several files)


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in ("on", "1", "true", "yes")


def _env_choice(name: str, choices, default):
    raw = os.environ.get(name, "").strip().lower()
    return raw if raw in choices else default


# Initial values for /data/settings.json. Once the file exists its values win;
# a key that is missing from the file falls back to these defaults.
IMAGE_ORDERS = ("sequence", "roundrobin", "random")
ORDER_LABELS = {"sequence": "Feste Reihenfolge", "roundrobin": "Round-Robin", "random": "Zufällig"}
PAGE_SIZE = 20          # pool tiles per dashboard page
API_TOKEN = os.environ.get("API_TOKEN", "").strip()   # optional, protects /api/*
MAX_SPLIT_PAGES = 200   # multi-page PDFs are split into single pages up to this size

SETTINGS_DEFAULTS = {
    "quality":          5 if os.environ.get("PRINT_QUALITY", "5").strip() == "5" else 4,
    "media_type":       os.environ.get("PRINT_MEDIA_TYPE", "stationery").strip() or "stationery",
    "content_optimize": _env_choice("PRINT_CONTENT_OPTIMIZE", ("auto", "photo"), "photo"),
    "strip_enabled":    _env_bool("MAINTENANCE_STRIP", True),
    "strip_position":   _env_choice("STRIP_POSITION", ("top", "bottom"), "top"),
    "image_order":      _env_choice("IMAGE_ORDER", IMAGE_ORDERS, "sequence"),
}

QUALITY_LABELS = {4: "Normal", 5: "Hoch"}

# Human-readable German labels for the IPP media-type keywords a printer
# typically advertises. Unknown keywords are shown as-is.
MEDIA_LABELS = {
    "auto":                       "Automatisch",
    "stationery":                 "Normalpapier",
    "stationery-inkjet":          "Normalpapier (Inkjet)",
    "stationery-letterhead":      "Briefpapier",
    "stationery-lightweight":     "Dünnes Papier",
    "stationery-heavyweight":     "Schweres Papier",
    "photographic":               "Fotopapier (mehr Tinte)",
    "photographic-glossy":        "Fotopapier glänzend (mehr Tinte)",
    "photographic-high-gloss":    "Fotopapier hochglänzend (mehr Tinte)",
    "photographic-semi-gloss":    "Fotopapier seidenglanz (mehr Tinte)",
    "photographic-satin":         "Fotopapier satin (mehr Tinte)",
    "photographic-matte":         "Fotopapier matt (mehr Tinte)",
    "envelope":                   "Umschlag",
    "cardstock":                  "Karton",
    "labels":                     "Etiketten",
    "transparency":               "Folie",
}
# Canon vendor media keywords (com.canon-*) as reported by GX/MegaTank printers.
# Photo papers make the printer lay down more ink; Hagaki are Japanese postcards.
CANON_MEDIA_LABELS = {
    "mtglossy":           ("photo", "Canon Fotopapier glänzend (mehr Tinte)"),
    "mtsemisuper":        ("photo", "Canon Fotopapier Plus seidenglanz (mehr Tinte)"),
    "mtluster":           ("photo", "Canon Fotopapier Pro Luster (mehr Tinte)"),
    "mtmat":              ("photo", "Canon Fotopapier matt (mehr Tinte)"),
    "mthires":            ("plain", "Canon High-Resolution-Papier"),
    "mtgreeting":         ("other", "Canon Grußkarte"),
    "mtinkjet":           ("other", "Inkjet-Postkarte (Hagaki, Japan)"),
    "mtinkjeta":          ("other", "Inkjet-Postkarte A (Hagaki, Japan)"),
    "mthagaki":           ("other", "Postkarte (Hagaki, Japan)"),
    "mthagakia":          ("other", "Postkarte A (Hagaki, Japan)"),
    "mthagakiglossy":     ("other", "Postkarte glänzend (Hagaki, Japan)"),
    "mthagakiglossya":    ("other", "Postkarte glänzend A (Hagaki, Japan)"),
}


def media_label(keyword: str) -> str:
    if keyword in MEDIA_LABELS:
        return MEDIA_LABELS[keyword]
    if keyword.startswith("com.canon-"):
        code = keyword[len("com.canon-"):]
        if code in CANON_MEDIA_LABELS:
            return CANON_MEDIA_LABELS[code][1]
        return f"Canon-Sondermedium ({code})"
    return keyword


def _media_group(keyword: str) -> str:
    if keyword.startswith("stationery") or keyword == "auto":
        return "plain"
    if keyword.startswith("photographic"):
        return "photo"
    if keyword.startswith("com.canon-"):
        entry = CANON_MEDIA_LABELS.get(keyword[len("com.canon-"):])
        return entry[0] if entry else "unknown"
    return "other"


FALLBACK_MEDIA_TYPES = ["stationery", "photographic-glossy", "photographic"]

WEEKDAY_DE = {
    "monday":    "Montag",
    "tuesday":   "Dienstag",
    "wednesday": "Mittwoch",
    "thursday":  "Donnerstag",
    "friday":    "Freitag",
    "saturday":  "Samstag",
    "sunday":    "Sonntag",
}

# A4 @ 150 DPI
PAGE_W, PAGE_H = 1240, 1754
STRIP_H = int(PAGE_H * 0.12)  # ~210 px

# Pure process colors. (255,240,0) looked yellow on screen but the missing
# red forces the printer to add a little magenta, so the yellow nozzles do
# less work than they should.
C_CYAN    = (0, 255, 255)
C_MAGENTA = (255, 0, 255)
C_YELLOW  = (255, 255, 0)
C_BLACK   = (0, 0, 0)
RAINBOW   = [
    (255, 0, 0), (255, 128, 0), C_YELLOW, (0, 200, 0),
    C_CYAN, (0, 0, 255), (160, 0, 255), C_MAGENTA,
]

_io_lock = threading.Lock()
_state_lock = threading.RLock()   # serialises load-modify-save of state.json


# ---------------------------------------------------------------------------
# Persistent JSON helpers: state, settings, printer capabilities
# ---------------------------------------------------------------------------

def _read_json(path: Path, default):
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            log.warning(f"Could not read {path.name}: {exc}")
    return default


def _write_json(path: Path, data) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


def load_state() -> dict:
    state = _read_json(STATE_FILE, {})
    state.setdefault("last_print", None)
    state.setdefault("print_count", 0)
    state.setdefault("history", [])
    state.setdefault("image_history", {})
    state.setdefault("order", [])
    state.setdefault("sequence_last", None)
    return state


def save_state(state: dict) -> None:
    _write_json(STATE_FILE, state)


def sanitize_settings(raw: dict, base: Optional[dict] = None) -> dict:
    """Validate a settings dict coming from the file or from the web form.
    Invalid or missing keys fall back to `base` (default: the env defaults)."""
    s = dict(base or SETTINGS_DEFAULTS)
    q = str(raw.get("quality", s["quality"]))
    if q in ("4", "5"):
        s["quality"] = int(q)
    mt = str(raw.get("media_type", s["media_type"])).strip().lower()
    if re.fullmatch(r"[a-z0-9][a-z0-9._-]{0,63}", mt):
        s["media_type"] = mt
    co = str(raw.get("content_optimize", s["content_optimize"])).strip().lower()
    if co in ("auto", "photo"):
        s["content_optimize"] = co
    se = raw.get("strip_enabled", s["strip_enabled"])
    s["strip_enabled"] = se if isinstance(se, bool) else str(se).strip().lower() in ("on", "1", "true", "yes")
    sp = str(raw.get("strip_position", s["strip_position"])).strip().lower()
    if sp in ("top", "bottom"):
        s["strip_position"] = sp
    io_ = str(raw.get("image_order", s["image_order"])).strip().lower()
    if io_ in IMAGE_ORDERS:
        s["image_order"] = io_
    return s


def load_settings() -> dict:
    return sanitize_settings(_read_json(SETTINGS_FILE, {}))


def save_settings(settings: dict) -> dict:
    clean = sanitize_settings(settings)
    _write_json(SETTINGS_FILE, clean)
    return clean


# ---------------------------------------------------------------------------
# Image generation
# ---------------------------------------------------------------------------

def _get_font(size: int) -> ImageFont.ImageFont:
    for path in [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf",
        "C:/Windows/Fonts/arialbd.ttf",
        "C:/Windows/Fonts/arial.ttf",
    ]:
        try:
            return ImageFont.truetype(path, size)
        except Exception:
            pass
    return ImageFont.load_default()


def _centered_text(draw: ImageDraw.ImageDraw, box, text: str, font, fill) -> None:
    x0, y0, x1, y1 = box
    try:
        l, t, r, b = draw.textbbox((0, 0), text, font=font)
        tw, th = r - l, b - t
    except Exception:
        tw, th = len(text) * 10, 12
        l = t = 0
    draw.text((x0 + (x1 - x0 - tw) // 2 - l, y0 + (y1 - y0 - th) // 2 - t), text, fill=fill, font=font)


def _yellow_ramp(density: float) -> tuple:
    """Yellow at a given ink density (1.0 = solid, 0.25 = light dither)."""
    return (255, 255, int(round(255 * (1 - density))))


def _paint_maintenance_strip(draw: ImageDraw.ImageDraw, x0: int, y0: int, w: int, h: int) -> None:
    """Paint the maintenance color strip into the given rectangle.

    Layout (top to bottom):
      1. rainbow band                          ~22 %
      2. C | M | Y | Y | Y | K solid blocks    ~45 %
      3. yellow density ramp 100/75/50/25 %    ~20 %
      4. one-line footer (date, printer)       rest
    Yellow gets three solid blocks plus the ramp: it is the channel that
    clogs first on the GX2050, and the ramp also fires the fine dither
    patterns that a solid block alone does not exercise.
    """
    band_h  = max(int(h * 0.22), 1)
    block_h = max(int(h * 0.45), 1)
    ramp_h  = max(int(h * 0.20), 1)

    # 1. Rainbow band
    seg_w = w / len(RAINBOW)
    for i, c in enumerate(RAINBOW):
        draw.rectangle([int(x0 + i * seg_w), y0, int(x0 + (i + 1) * seg_w), y0 + band_h], fill=c)

    # 2. Solid process-color blocks, yellow three times
    blocks = [(C_CYAN, "C"), (C_MAGENTA, "M"), (C_YELLOW, "Y"), (C_YELLOW, "Y"), (C_YELLOW, "Y"), (C_BLACK, "K")]
    by  = y0 + band_h
    bw  = w / len(blocks)
    font = _get_font(max(min(block_h - 16, 56), 18))
    for i, (color, lbl) in enumerate(blocks):
        sx, ex = int(x0 + i * bw), int(x0 + (i + 1) * bw) - 2
        draw.rectangle([sx, by, ex, by + block_h], fill=color)
        tc = (255, 255, 255) if color == C_BLACK else (40, 40, 40)
        _centered_text(draw, (sx, by, ex, by + block_h), lbl, font, tc)

    # 3. Yellow density ramp
    ry = by + block_h
    densities = [1.0, 0.75, 0.5, 0.25]
    rw = w / len(densities)
    small = _get_font(max(min(ramp_h - 10, 22), 12))
    for i, d in enumerate(densities):
        sx, ex = int(x0 + i * rw), int(x0 + (i + 1) * rw) - 2
        draw.rectangle([sx, ry, ex, ry + ramp_h], fill=_yellow_ramp(d))
        _centered_text(draw, (sx, ry, ex, ry + ramp_h), f"Y {int(d * 100)}%", small, (90, 90, 90))

    # 4. Footer
    fy = ry + ramp_h
    fh = max(y0 + h - fy, 1)
    draw.rectangle([x0, fy, x0 + w, y0 + h], fill=(246, 246, 246))
    info_font = _get_font(max(min(fh - 8, 20), 11))
    date_str = datetime.now().strftime("%d.%m.%Y %H:%M")
    draw.text((x0 + 12, fy + max((fh - info_font.size) // 2, 1)),
              f"AutoPrint Düsenpflege · {PRINTER_NAME} · {date_str}",
              fill=(90, 90, 90), font=info_font)


def build_color_test_image() -> Image.Image:
    """Synthetic A4 color test page used when no user images are available."""
    W, H = PAGE_W, PAGE_H
    img  = Image.new("RGB", (W, H), "white")
    draw = ImageDraw.Draw(img)

    # Rainbow stripes (top quarter)
    band_h = (H // 4) // len(RAINBOW)
    for i, color in enumerate(RAINBOW):
        draw.rectangle([0, i * band_h, W, (i + 1) * band_h - 1], fill=color)

    # Big CMYK + white blocks
    row2_y, row2_h = H // 4, H // 5
    cmyk_blocks = [(C_CYAN, "C"), (C_MAGENTA, "M"), (C_YELLOW, "Y"), (C_BLACK, "K"), ((255, 255, 255), "W")]
    bw = W // len(cmyk_blocks)
    font_big = _get_font(90)
    for i, (color, label) in enumerate(cmyk_blocks):
        x0 = i * bw
        draw.rectangle([x0, row2_y, x0 + bw - 2, row2_y + row2_h], fill=color)
        tc = (255, 255, 255) if color == C_BLACK else (30, 30, 30)
        _centered_text(draw, (x0, row2_y, x0 + bw, row2_y + row2_h), label, font_big, tc)

    # Gradients: yellow -> red and cyan -> blue
    row3_y, row3_h = row2_y + row2_h, H // 7
    for x in range(W):
        if x < W // 2:
            t = x / (W // 2)
            r, g, b = 255, int(255 * (1 - t)), 0
        else:
            t = (x - W // 2) / (W // 2)
            r, g, b = 0, int(255 * (1 - t)), 255
        draw.line([x, row3_y, x, row3_y + row3_h - 1], fill=(r, g, b))

    # Mixed color fields
    row4_y, row4_h = row3_y + row3_h, H // 7
    mixed = [
        (255, 128, 0), (200, 255, 0), (0, 255, 130), (0, 130, 255),
        (130, 0, 255), (255, 0, 130), (255, 210, 0), (0, 255, 255),
    ]
    pw = W // len(mixed)
    for i, c in enumerate(mixed):
        draw.rectangle([i * pw, row4_y, (i + 1) * pw - 2, row4_y + row4_h], fill=c)

    # Yellow density ramp
    row5_y, row5_h = row4_y + row4_h, H // 12
    densities = [1.0, 0.9, 0.75, 0.6, 0.5, 0.35, 0.25, 0.1]
    rw = W // len(densities)
    small = _get_font(30)
    for i, d in enumerate(densities):
        draw.rectangle([i * rw, row5_y, (i + 1) * rw - 2, row5_y + row5_h], fill=_yellow_ramp(d))
        _centered_text(draw, (i * rw, row5_y, (i + 1) * rw, row5_y + row5_h), f"{int(d * 100)}%", small, (90, 90, 90))

    # Footer
    footer_y = row5_y + row5_h
    draw.rectangle([0, footer_y, W, H], fill=(246, 246, 246))
    font_title, font_sub = _get_font(46), _get_font(34)
    date_str = datetime.now().strftime("%d.%m.%Y %H:%M")
    draw.text((40, footer_y + 30), "AutoPrint – Druckkopf-Pflege", fill=(40, 40, 40), font=font_title)
    draw.text((40, footer_y + 95), f"Gedruckt am:  {date_str}", fill=(100, 100, 100), font=font_sub)
    draw.text((40, footer_y + 140), f"Drucker:      {PRINTER_NAME}", fill=(100, 100, 100), font=font_sub)
    return img


def _flatten(img: Image.Image) -> Image.Image:
    """Convert any PIL image to RGB, flattening transparency on white."""
    if img.mode in ("RGBA", "LA") or (img.mode == "P" and "transparency" in img.info):
        img = img.convert("RGBA")
        bg = Image.new("RGB", img.size, "white")
        bg.paste(img, mask=img.split()[-1])
        return bg
    return img.convert("RGB")


def _render_pdf_first_page(pdf: Path, width: int) -> Optional[Image.Image]:
    """Rasterize page 1 of a PDF via poppler's pdftoppm so that its longer
    side is `width` pixels. None if poppler is unavailable."""
    try:
        with tempfile.TemporaryDirectory() as td:
            prefix = Path(td) / "page"
            subprocess.run(
                ["pdftoppm", "-png", "-r", "72", "-f", "1", "-l", "1", "-scale-to", str(width), str(pdf), str(prefix)],
                capture_output=True, timeout=60, check=True,
            )
            files = sorted(Path(td).glob("page*.png"))
            if not files:
                return None
            with Image.open(files[0]) as im:
                return _flatten(im).copy()
    except Exception as exc:
        log.warning(f"PDF rendering failed for {pdf.name}: {exc}")
        return None


def _pdf_placeholder(width: int) -> Image.Image:
    img = Image.new("RGB", (width, int(width * 1.414)), (235, 235, 235))
    draw = ImageDraw.Draw(img)
    _centered_text(draw, (0, 0, img.width, img.height), "PDF", _get_font(max(width // 4, 20)), (150, 150, 150))
    return img


def load_source_image(src: Path, width: int = PAGE_W) -> Image.Image:
    """Return an RGB image for a pool file: the raster itself, or page 1 of a PDF."""
    if src.suffix.lower() == ".pdf":
        return _render_pdf_first_page(src, width) or _pdf_placeholder(width)
    with Image.open(src) as im:
        return _flatten(im).copy()


def compose_page(user: Image.Image, with_strip: bool, position: str) -> Image.Image:
    """Fit a user image onto A4 portrait, with the maintenance strip at the
    top (default) or bottom of the same page."""
    strip_h  = STRIP_H if with_strip else 0
    target_h = PAGE_H - strip_h
    img_top  = strip_h if (with_strip and position == "top") else 0

    canvas = Image.new("RGB", (PAGE_W, PAGE_H), "white")
    # Scale up or down so the picture fills the available area (small kids'
    # pictures would otherwise print as a tiny stamp).
    scale = min(PAGE_W / user.width, target_h / user.height)
    size  = (max(int(user.width * scale), 1), max(int(user.height * scale), 1))
    user  = user.resize(size, Image.LANCZOS)
    canvas.paste(user, ((PAGE_W - user.width) // 2, img_top + (target_h - user.height) // 2))

    if with_strip:
        draw = ImageDraw.Draw(canvas)
        _paint_maintenance_strip(draw, 0, 0 if position == "top" else target_h, PAGE_W, strip_h)
    return canvas


def fit_raster_to_a4(src: Path, with_strip: bool, position: str = "top") -> Image.Image:
    """Exactly one A4 page: the image (or page 1 of a PDF) plus the strip."""
    return compose_page(load_source_image(src, PAGE_H), with_strip, position)


def build_preview_image(src: Optional[Path], settings: dict) -> Image.Image:
    """What the next print will look like (always one A4 page)."""
    if src is None:
        return build_color_test_image()
    return fit_raster_to_a4(src, settings["strip_enabled"], settings["strip_position"])


# ---------------------------------------------------------------------------
# Image pool: listing, selection, thumbnails, upload, URL import
# ---------------------------------------------------------------------------

def _pool_files() -> list:
    """Pool files in the user-defined order (state["order"]); files that are
    not in that list yet (e.g. copied into the volume by hand) follow by name."""
    if not IMAGE_DIR.exists():
        return []
    files = [p for p in IMAGE_DIR.iterdir() if p.is_file() and p.suffix.lower() in ALLOWED_EXT]
    pos = {n: i for i, n in enumerate(load_state().get("order", []))}
    return sorted(files, key=lambda p: (pos.get(p.name, len(pos)), p.name.lower()))


def set_pool_order(names: list) -> list:
    """Persist a new pool order. Unknown names are ignored, missing pool files
    are appended in their current order. Returns the effective order."""
    with _state_lock:
        current = [p.name for p in _pool_files()]
        known = set(current)
        wanted = [n for n in dict.fromkeys(names) if n in known]
        taken = set(wanted)
        order = wanted + [n for n in current if n not in taken]
        state = load_state()
        state["order"] = order
        save_state(state)
        return order


def move_pool_image(name: str, direction: str) -> bool:
    """Move one image: up/down by one position, or to first/last."""
    with _state_lock:
        order = [p.name for p in _pool_files()]
        if name not in order:
            return False
        i = order.index(name)
        order.pop(i)
        j = {"up": i - 1, "down": i + 1, "first": 0, "last": len(order)}.get(direction, i)
        order.insert(max(0, min(j, len(order))), name)
        set_pool_order(order)
        return True


def reorder_subset(names: list) -> list:
    """Reorder only the given images among the positions they already occupy
    (used for drag & drop on one dashboard page)."""
    with _state_lock:
        current = [p.name for p in _pool_files()]
        known = set(current)
        given = [n for n in dict.fromkeys(names) if n in known]
        slots = sorted(current.index(n) for n in given)
        for i, n in zip(slots, given):
            current[i] = n
        return set_pool_order(current)


def move_selection(names: list, where: str) -> list:
    """Move the selected images (keeping their relative order) to the start or end."""
    with _state_lock:
        current = [p.name for p in _pool_files()]
        chosen = set(names)
        picked = [n for n in current if n in chosen]
        rest = [n for n in current if n not in chosen]
        return set_pool_order(picked + rest if where == "first" else rest + picked)


def sort_pool(by: str) -> list:
    """Sort the whole pool: name, newest / oldest added (file mtime) or random."""
    files = _pool_files()
    if by == "name":
        files.sort(key=lambda p: p.name.lower())
    elif by == "newest":
        files.sort(key=lambda p: (-p.stat().st_mtime, p.name.lower()))
    elif by == "oldest":
        files.sort(key=lambda p: (p.stat().st_mtime, p.name.lower()))
    elif by == "random":
        random.shuffle(files)
    else:
        raise ValueError(by)
    return set_pool_order([p.name for p in files])


def clear_pool() -> int:
    """Delete every pool image, its thumbnail and the per-image state."""
    with _state_lock:
        files = _pool_files()
        for p in files:
            p.unlink(missing_ok=True)
            _thumb_path(p.name).unlink(missing_ok=True)
        state = load_state()
        state["order"] = []
        state["sequence_last"] = None
        state["image_history"] = {}
        save_state(state)
    log.info(f"Pool cleared ({len(files)} files)")
    return len(files)


def _order_replace(old: str, new_names: list) -> None:
    """Put new_names where `old` sits in the order (or at the end)."""
    with _state_lock:
        current = [n for n in (p.name for p in _pool_files()) if n == old or n not in new_names]
        if old in current:
            i = current.index(old)
            current[i:i + 1] = new_names
        else:
            current += [n for n in new_names if n not in current]
        state = load_state()
        state["order"] = current
        if state.get("sequence_last") == old and old not in new_names:
            state["sequence_last"] = new_names[-1] if new_names else None
        save_state(state)


def split_pdf_pages(src: Path) -> list:
    """Split a multi-page PDF in the pool into single-page PDFs
    (<stem>_Seite01.pdf, ...) at the same position in the order.
    Returns the resulting pool names (just [src.name] if nothing to split)."""
    from pypdf import PdfReader, PdfWriter
    try:
        reader = PdfReader(str(src))
        if reader.is_encrypted:
            reader.decrypt("")
        count = len(reader.pages)
    except Exception as exc:
        log.warning(f"Could not read PDF {src.name} for splitting: {exc}")
        return [src.name]
    if count <= 1:
        return [src.name]
    if count > MAX_SPLIT_PAGES:
        log.warning(f"{src.name} has {count} pages (> {MAX_SPLIT_PAGES}), not splitting")
        return [src.name]

    digits = max(2, len(str(count)))
    names = []
    try:
        for i, page in enumerate(reader.pages, 1):
            writer = PdfWriter()
            writer.add_page(page)
            dest = _unique_dest(f"{src.stem}_Seite{i:0{digits}d}.pdf")
            with open(dest, "wb") as f:
                writer.write(f)
            names.append(dest.name)
    except Exception as exc:
        log.warning(f"Splitting {src.name} failed, keeping original: {exc}")
        for n in names:
            (IMAGE_DIR / n).unlink(missing_ok=True)
        return [src.name]

    with _state_lock:
        _order_replace(src.name, names)
        src.unlink()
        _thumb_path(src.name).unlink(missing_ok=True)
        state = load_state()
        if state["image_history"].pop(src.name, None) is not None:
            save_state(state)
    log.info(f"Split {src.name} into {count} single pages")
    return names


def add_to_pool(name: str) -> list:
    """Register a freshly stored pool file: append it to the order and split
    multi-page PDFs. Returns the resulting pool names."""
    with _state_lock:
        order = [n for n in (p.name for p in _pool_files()) if n != name] + [name]
        state = load_state()
        state["order"] = order
        save_state(state)
    src = IMAGE_DIR / name
    if src.suffix.lower() == ".pdf":
        return split_pdf_pages(src)
    return [name]


def split_existing_pdfs() -> None:
    """Startup migration: split multi-page PDFs already in the pool."""
    for p in _pool_files():
        if p.suffix.lower() == ".pdf":
            try:
                split_pdf_pages(p)
            except Exception as exc:
                log.warning(f"PDF split of {p.name} failed: {exc}")


def pick_next_image(settings: Optional[dict] = None) -> Optional[Path]:
    """Next pool image to print, or None if the pool is empty (synthetic page).

    sequence:   the image after the last automatically printed one, in the
                order set in the dashboard (wraps around).
    roundrobin: the image printed longest ago (never-printed first).
    random:     any image except the one printed most recently.
    """
    files = _pool_files()
    if not files:
        return None
    settings = settings or load_settings()
    state = load_state()
    image_history = state.get("image_history", {})
    if settings["image_order"] == "sequence":
        names = [p.name for p in files]
        last = state.get("sequence_last")
        if last in names:
            return files[(names.index(last) + 1) % len(files)]
        return files[0]
    if settings["image_order"] == "random" and len(files) > 1:
        last = max(files, key=lambda p: image_history.get(p.name, ""))
        if image_history.get(last.name):
            files = [p for p in files if p != last]
        return random.choice(files)
    return min(files, key=lambda p: (image_history.get(p.name, ""), p.name.lower()))


def _sniff_kind(head: bytes) -> Optional[str]:
    if head.startswith(b"%PDF"):
        return ".pdf"
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png"
    if head.startswith(b"\xff\xd8\xff"):
        return ".jpg"
    return None


def _unique_dest(name: str) -> Path:
    dest = IMAGE_DIR / name
    stem, ext = Path(name).stem, Path(name).suffix
    n = 2
    while dest.exists():
        dest = IMAGE_DIR / f"{stem}_{n}{ext}"
        n += 1
    return dest


def _thumb_path(name: str) -> Path:
    return THUMB_DIR / (name + ".jpg")


def ensure_thumbnail(src: Path) -> Path:
    thumb = _thumb_path(src.name)
    if thumb.exists() and thumb.stat().st_mtime >= src.stat().st_mtime:
        return thumb
    with _io_lock:
        img = load_source_image(src, width=320)
        img.thumbnail((320, 453), Image.LANCZOS)
        img.save(str(thumb), "JPEG", quality=82)
    return thumb


def save_upload(storage) -> str:
    """Validate a werkzeug FileStorage and store it in the pool. Returns the
    stored filename; raises ValueError with a German message otherwise."""
    name = secure_filename(storage.filename or "")
    if not name:
        raise ValueError("Ungültiger Dateiname")
    ext = Path(name).suffix.lower()
    if ext not in ALLOWED_EXT:
        raise ValueError(f"{name}: nur PNG, JPG oder PDF")
    head = storage.stream.read(16)
    storage.stream.seek(0, os.SEEK_END)
    size = storage.stream.tell()
    storage.stream.seek(0)
    if size > MAX_FILE_BYTES:
        raise ValueError(f"{name}: größer als 10 MB")
    if _sniff_kind(head) is None:
        raise ValueError(f"{name}: Inhalt ist kein PNG, JPG oder PDF")
    dest = _unique_dest(name)
    storage.save(str(dest))
    log.info(f"Uploaded image: {dest.name} ({size} bytes)")
    return dest.name


def fetch_url_to_pool(url: str) -> str:
    """Download a PNG/JPG/PDF from an http(s) URL into the pool."""
    url = (url or "").strip()
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise ValueError("Bitte einen http(s)-Link angeben")
    req = urllib.request.Request(url, headers={"User-Agent": f"AutoPrint/{VERSION}"})
    data = bytearray()
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            while True:
                chunk = resp.read(64 * 1024)
                if not chunk:
                    break
                data += chunk
                if len(data) > MAX_FILE_BYTES:
                    raise ValueError("Datei ist größer als 10 MB")
            disposition = resp.headers.get("Content-Disposition", "")
    except ValueError:
        raise
    except Exception as exc:
        raise ValueError(f"Download fehlgeschlagen: {exc}") from exc

    ext = _sniff_kind(bytes(data[:16]))
    if ext is None:
        raise ValueError("Der Link liefert kein PNG, JPG oder PDF (vielleicht eine Webseite statt der Datei?)")

    name = ""
    m = re.search(r'filename\*?=(?:UTF-8\'\')?"?([^";]+)', disposition, re.IGNORECASE)
    if m:
        name = urllib.parse.unquote(m.group(1))
    if not name:
        name = Path(urllib.parse.unquote(parsed.path)).name
    name = secure_filename(name) or f"download_{datetime.now():%Y%m%d_%H%M%S}"
    if Path(name).suffix.lower() not in ALLOWED_EXT:
        name = Path(name).stem + ext
    dest = _unique_dest(name)
    dest.write_bytes(bytes(data))
    log.info(f"Imported image from URL: {dest.name} ({len(data)} bytes) <- {url}")
    return dest.name


def delete_pool_image(name: str) -> None:
    with _state_lock:
        order = [p.name for p in _pool_files()]
        src = IMAGE_DIR / name
        src.unlink()
        _thumb_path(name).unlink(missing_ok=True)
        state = load_state()
        state["image_history"].pop(name, None)
        state["order"] = [n for n in order if n != name]
        if state.get("sequence_last") == name:
            # keep the sequence going: the image after the deleted one is next
            i = order.index(name)
            state["sequence_last"] = order[i - 1] if i > 0 else None
        save_state(state)


# ---------------------------------------------------------------------------
# Print preparation + execution
# ---------------------------------------------------------------------------

def prepare_print_file(src: Optional[Path], settings: dict) -> Path:
    """Build a print-ready, single-page A4 PDF in DATA_DIR and return its path.
    Every print is exactly one sheet of paper.

    - src is None:     synthetic full-page color test.
    - src is a PDF:    page 1 is rendered (pdftoppm) and composed like a raster.
                       Without poppler, page 1 is passed through without strip.
    - src is a raster: image fit to A4 with the strip composited into the
                       top or bottom of the same page (when enabled).
    """
    ts  = datetime.now().strftime("%Y%m%d_%H%M%S")
    out = DATA_DIR / f"print_{ts}.pdf"
    strip, pos = settings["strip_enabled"], settings["strip_position"]

    if src is None:
        build_color_test_image().save(str(out), "PDF", resolution=150.0)
        return out

    if src.suffix.lower() == ".pdf":
        page = _render_pdf_first_page(src, PAGE_H)
        if page is not None:
            compose_page(page, strip, pos).save(str(out), "PDF", resolution=150.0)
            return out
        # No poppler: never print more than one sheet, pass page 1 through as is.
        from pypdf import PdfReader, PdfWriter
        log.warning(f"pdftoppm unavailable, printing page 1 of {src.name} without strip")
        writer = PdfWriter()
        writer.add_page(PdfReader(str(src)).pages[0])
        with open(out, "wb") as f:
            writer.write(f)
        return out

    fit_raster_to_a4(src, strip, pos).save(str(out), "PDF", resolution=150.0)
    return out


_reach_cache = {"ts": 0.0, "ok": False}
_REACH_TTL = 30


def _check_printer_reachable(force: bool = False) -> bool:
    """Direct IPP connectivity test using ipptool (no CUPS queue required).
    Cached for 30 s so the dashboard stays snappy while the printer sleeps."""
    if not PRINTER_URI:
        return False
    now = time.time()
    if not force and now - _reach_cache["ts"] < _REACH_TTL:
        return _reach_cache["ok"]
    try:
        result = subprocess.run(
            ["ipptool", "-T", "10", "-q", PRINTER_URI, "get-printer-attributes.test"],
            capture_output=True, timeout=15,
        )
        ok = result.returncode == 0
    except Exception as exc:
        log.warning(f"ipptool check failed: {exc}")
        ok = False
    _reach_cache.update(ts=now, ok=ok)
    return ok


_caps_cache = {"ts": 0.0, "caps": None}
_CAPS_TTL = 3600
_CAPS_ATTRS = {
    "media-type-supported":             "media_types",
    "print-quality-supported":          "qualities",
    "print-content-optimize-supported": "content_optimize",
}


def _parse_ipptool_attrs(text: str) -> dict:
    """Pick the attributes we care about out of `ipptool -tv` output.
    Lines look like:  media-type-supported (1setOf keyword) = stationery,photographic-glossy"""
    caps = {}
    for line in text.splitlines():
        m = re.match(r"\s*([a-z][a-z0-9-]*)\s+\([^)]*\)\s*=\s*(.*)$", line)
        if not m or m.group(1) not in _CAPS_ATTRS:
            continue
        values = [v.strip().strip('"') for v in m.group(2).split(",") if v.strip()]
        caps[_CAPS_ATTRS[m.group(1)]] = values
    return caps


def _printer_capabilities(force: bool = False) -> dict:
    """media types / qualities / content-optimize values the printer advertises.
    Cached in memory (1 h) and on disk so a sleeping printer still yields a
    sensible dropdown. Falls back to a generic list."""
    now = time.time()
    if not force and _caps_cache["caps"] is not None and now - _caps_cache["ts"] < _CAPS_TTL:
        return _caps_cache["caps"]

    caps = {}
    if PRINTER_URI:
        try:
            result = subprocess.run(
                ["ipptool", "-T", "10", "-tv", PRINTER_URI, "get-printer-attributes.test"],
                capture_output=True, text=True, timeout=20,
            )
            caps = _parse_ipptool_attrs(result.stdout) if result.returncode == 0 else {}
        except Exception as exc:
            log.warning(f"ipptool capability query failed: {exc}")
    if caps.get("media_types"):
        caps["source"] = "printer"
        caps["queried"] = datetime.now().isoformat(timespec="seconds")
        _write_json(CAPS_FILE, caps)
    else:
        caps = _read_json(CAPS_FILE, {}) or {}
        if not caps.get("media_types"):
            caps = {"media_types": list(FALLBACK_MEDIA_TYPES), "qualities": [3, 4, 5],
                    "content_optimize": [], "source": "fallback"}
        else:
            caps["source"] = "cache"
    _caps_cache.update(ts=now, caps=caps)
    return caps


def media_type_options(settings: dict, caps: dict) -> list:
    """Dropdown entries: plain paper first, photo papers next, rest after.
    The currently selected value is always present."""
    types = list(dict.fromkeys(caps.get("media_types", []) + [settings["media_type"]]))

    order = {"plain": 0, "photo": 1, "other": 2, "unknown": 3}
    first = ("stationery", "photographic", "photographic-glossy")

    def rank(t: str):
        prio = first.index(t) if t in first else len(first)
        return (order[_media_group(t)], prio, t.startswith("com.canon-"), media_label(t))

    return [{"value": t, "label": media_label(t)} for t in sorted(types, key=rank)]


def _lp_options(settings: dict, caps: dict) -> list:
    opts = [
        "media=A4",
        "print-color-mode=color",
        f"print-quality={settings['quality']}",
        f"media-type={settings['media_type']}",
    ]
    if settings["content_optimize"] in caps.get("content_optimize", []):
        opts.append(f"print-content-optimize={settings['content_optimize']}")
    return opts


def _opts_summary(settings: dict) -> dict:
    return {
        "quality":    QUALITY_LABELS.get(settings["quality"], str(settings["quality"])),
        "media_type": media_label(settings["media_type"]).replace(" (mehr Tinte)", ""),
        "strip":      ("oben" if settings["strip_position"] == "top" else "unten") if settings["strip_enabled"] else "aus",
    }


def do_print(manual: bool = False, image: Optional[Path] = None) -> bool:
    settings = load_settings()
    opts_summary = _opts_summary(settings)
    success = False
    msg     = ""

    if not _check_printer_reachable(force=True):
        msg = f"Drucker nicht erreichbar: {PRINTER_URI}"
        log.error(msg + " (check DNS/hostname resolution and power/network state)")
        _record(success=False, manual=manual, msg=msg, image=image, opts=opts_summary)
        return False

    picked = image is None   # chosen by the selection logic, not a per-image button
    if image is None:
        image = pick_next_image(settings)

    try:
        out_path = prepare_print_file(image, settings)
    except Exception as exc:
        log.error(f"Print preparation failed: {exc}")
        _record(success=False, manual=manual, msg=f"Vorbereitung fehlgeschlagen: {exc}", image=image, opts=opts_summary)
        return False

    lp_opts = _lp_options(settings, _printer_capabilities())
    cmd = ["lp", "-d", PRINTER_NAME]
    for o in lp_opts:
        cmd += ["-o", o]
    cmd.append(str(out_path))
    label = image.name if image else "synthetic"
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        success = result.returncode == 0
        msg = result.stdout.strip() or result.stderr.strip()
        if success:
            log.info(f"Print job submitted ({label}) [{' '.join(lp_opts)}]: {msg}")
        else:
            log.error(f"Print failed (rc={result.returncode}, {label}): {msg}")
    except subprocess.TimeoutExpired:
        msg = "lp-Befehl hat zu lange gedauert"
        log.error(msg)
    except FileNotFoundError:
        msg = "'lp' nicht gefunden – läuft CUPS?"
        log.error(msg)
    except Exception as exc:
        msg = str(exc)
        log.error(f"Print error: {exc}")

    _record(success=success, manual=manual, msg=msg if not success else "OK", image=image, opts=opts_summary,
            picked=picked)

    # Cleanup old generated outputs, keep only latest 5
    for old in sorted(DATA_DIR.glob("print_*.pdf"))[:-5]:
        try:
            old.unlink()
        except Exception:
            pass
    return success


def _record(success: bool, manual: bool, msg: str, image: Optional[Path] = None,
            opts: Optional[dict] = None, picked: bool = False) -> None:
    with _state_lock:
        _record_locked(success, manual, msg, image, opts, picked)


def _record_locked(success, manual, msg, image, opts, picked) -> None:
    state = load_state()
    now   = datetime.now()
    if success:
        state["last_print"]  = now.isoformat()
        state["print_count"] = state.get("print_count", 0) + 1
        if image is not None:
            state["image_history"][image.name] = now.isoformat()
            if picked:
                state["sequence_last"] = image.name
    history = state.get("history", [])
    history.insert(0, {
        "time":    now.strftime("%d.%m.%Y %H:%M"),
        "iso":     now.isoformat(timespec="seconds"),
        "success": success,
        "manual":  manual,
        "msg":     msg,
        "image":   image.name if image else None,
        "opts":    opts or {},
    })
    state["history"] = history[:20]
    save_state(state)


# ---------------------------------------------------------------------------
# Weekly schedule
# ---------------------------------------------------------------------------

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


def _fmt_dt(iso: Optional[str], fallback: str = "noch nie") -> str:
    if not iso:
        return fallback
    try:
        return datetime.fromisoformat(iso).strftime("%d.%m.%Y %H:%M")
    except Exception:
        return iso


def describe_next_run() -> dict:
    nr = schedule.next_run()
    if nr is None:
        return {"iso": None, "label": "–", "relative": ""}
    now = datetime.now()
    days = (nr.date() - now.date()).days
    minutes = int((nr - now).total_seconds() // 60)
    if minutes < 60:
        rel = f"in {max(minutes, 1)} Minuten"
    elif days == 0:
        rel = f"heute um {nr:%H:%M}"
    elif days == 1:
        rel = f"morgen um {nr:%H:%M}"
    else:
        rel = f"in {days} Tagen"
    weekday = WEEKDAY_DE.get(nr.strftime("%A").lower(), nr.strftime("%A"))
    return {"iso": nr.isoformat(timespec="minutes"), "label": f"{weekday}, {nr:%d.%m.%Y %H:%M}", "relative": rel}


# ---------------------------------------------------------------------------
# Flask web UI
# ---------------------------------------------------------------------------
app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = MAX_REQUEST_BYTES
# Only needed for flash() cookies; a per-start random key is fine for that.
app.secret_key = os.environ.get("SECRET_KEY") or secrets.token_hex(32)
_scheduler_started = False
_scheduler_lock    = threading.Lock()


def _list_images(next_image: Optional[Path]) -> list:
    image_history = load_state().get("image_history", {})
    out = []
    for pos, p in enumerate(_pool_files(), 1):
        st = p.stat()
        out.append({
            "pos":           pos,
            "name":          p.name,
            "kind":          "pdf" if p.suffix.lower() == ".pdf" else "bild",
            "size_kb":       max(st.st_size // 1024, 1),
            "mtime":         int(st.st_mtime),
            "last_printed":  _fmt_dt(image_history.get(p.name)),
            "added":         datetime.fromtimestamp(st.st_mtime).strftime("%d.%m.%Y %H:%M"),
            "printed_iso":   image_history.get(p.name),
            "is_next":       next_image is not None and p.name == next_image.name,
        })
    return out


def _page_arg() -> int:
    try:
        return max(1, int(request.values.get("page", 1)))
    except (TypeError, ValueError):
        return 1


def _back_to_pool(page: Optional[int] = None):
    page = page or _page_arg()
    target = url_for("index", page=page) if page > 1 else url_for("index")
    return redirect(target + "#pool-card")


def _render():
    state    = load_state()
    settings = load_settings()
    caps     = _printer_capabilities()
    next_img = pick_next_image(settings)
    all_imgs = _list_images(next_img)
    pages    = max(1, -(-len(all_imgs) // PAGE_SIZE))
    page     = min(_page_arg(), pages)
    return render_template(
        "dashboard.html",
        version        = VERSION,
        printer_name   = PRINTER_NAME,
        printer_uri    = PRINTER_URI,
        printer_ok     = _check_printer_reachable(),
        weekday        = WEEKDAY_DE.get(PRINT_WEEKDAY, PRINT_WEEKDAY),
        print_time     = PRINT_TIME,
        next_run       = describe_next_run(),
        last_print     = _fmt_dt(state.get("last_print")),
        print_count    = state.get("print_count", 0),
        history        = state.get("history", []),
        images         = all_imgs[(page - 1) * PAGE_SIZE: page * PAGE_SIZE],
        total_images   = len(all_imgs),
        page           = page,
        pages          = pages,
        next_image     = next_img.name if next_img else None,
        settings       = settings,
        media_options  = media_type_options(settings, caps),
        caps_source    = caps.get("source", "fallback"),
        order_labels   = ORDER_LABELS,
        opts_summary   = _opts_summary(settings),
    )


@app.route("/")
def index():
    return _render()


@app.route("/print_now", methods=["POST"])
def print_now():
    threading.Thread(target=do_print, kwargs={"manual": True}, daemon=True).start()
    flash("Druckauftrag gestartet.", "ok")
    return redirect(url_for("index"))


@app.route("/preview")
def preview():
    """On-screen PNG of what the next print (or ?image=<name>) will look like."""
    settings = load_settings()
    name = request.args.get("image")
    if name:
        image = IMAGE_DIR / secure_filename(name)
        if not image.exists():
            abort(404)
    else:
        image = pick_next_image(settings)
    p = DATA_DIR / "preview.png"
    with _io_lock:
        build_preview_image(image, settings).save(str(p), "PNG")
    return send_file(str(p), mimetype="image/png", max_age=0)


@app.route("/settings", methods=["POST"])
def update_settings():
    current = load_settings()
    submitted = {key: request.form.get(key) for key in SETTINGS_DEFAULTS if key in request.form}
    saved = save_settings(sanitize_settings(submitted, base=current))
    log.info(f"Settings updated: {saved}")
    flash("Einstellungen gespeichert.", "ok")
    return redirect(url_for("index"))


@app.route("/upload", methods=["POST"])
def upload():
    files = [f for f in request.files.getlist("files") if f and f.filename]
    if not files:
        flash("Keine Datei ausgewählt.", "err")
        return redirect(url_for("index"))
    saved, errors = [], []
    for f in files:
        try:
            name = save_upload(f)
            pages = add_to_pool(name)
            saved.append(name)
            if len(pages) > 1:
                flash(f"„{name}“ hat {len(pages)} Seiten und wurde in Einzelseiten aufgeteilt.", "ok")
        except ValueError as exc:
            errors.append(str(exc))
        except Exception as exc:
            log.error(f"Upload failed: {exc}")
            errors.append(f"{f.filename}: {exc}")
    if saved:
        flash(f"{len(saved)} Datei(en) hochgeladen: {', '.join(saved)}", "ok")
    for e in errors:
        flash(e, "err")
    return redirect(url_for("index"))


@app.route("/fetch", methods=["POST"])
def fetch():
    try:
        name = fetch_url_to_pool(request.form.get("url", ""))
        pages = add_to_pool(name)
        if len(pages) > 1:
            flash(f"Importiert: {name} ({len(pages)} Seiten, in Einzelseiten aufgeteilt)", "ok")
        else:
            flash(f"Importiert: {name}", "ok")
    except ValueError as exc:
        flash(str(exc), "err")
    except Exception as exc:
        log.error(f"URL import failed: {exc}")
        flash(f"Import fehlgeschlagen: {exc}", "err")
    return redirect(url_for("index"))


@app.errorhandler(413)
def _too_large(_e):
    flash("Upload zu groß (max. 10 MB pro Datei, 50 MB pro Vorgang).", "err")
    return redirect(url_for("index"))


@app.route("/images/<name>/thumb")
def image_thumb(name):
    src = IMAGE_DIR / secure_filename(name)
    if not src.exists():
        abort(404)
    try:
        thumb = ensure_thumbnail(src)
    except Exception as exc:
        log.warning(f"Thumbnail failed for {src.name}: {exc}")
        abort(500)
    return send_file(str(thumb), mimetype="image/jpeg", max_age=3600)


@app.route("/images/<name>/print", methods=["POST"])
def print_image(name):
    safe = secure_filename(name)
    src  = IMAGE_DIR / safe
    if not src.exists():
        flash(f"Nicht gefunden: {safe}", "err")
        return redirect(url_for("index"))
    threading.Thread(target=do_print, kwargs={"manual": True, "image": src}, daemon=True).start()
    flash(f"Druckauftrag gestartet: {safe}", "ok")
    return _back_to_pool()


def _switch_to_sequence() -> bool:
    """Reordering only matters in 'sequence' mode; switch to it automatically."""
    settings = load_settings()
    if settings["image_order"] == "sequence":
        return False
    settings["image_order"] = "sequence"
    save_settings(settings)
    log.info("Image order switched to 'sequence' after manual reordering")
    return True


@app.route("/images/<name>/move", methods=["POST"])
def move_image(name):
    safe = secure_filename(name)
    direction = request.form.get("dir", "")
    if direction not in ("up", "down", "first", "last") or not move_pool_image(safe, direction):
        flash(f"Verschieben nicht möglich: {safe}", "err")
        return _back_to_pool()
    if _switch_to_sequence():
        flash("Bildauswahl auf „Feste Reihenfolge“ umgestellt.", "ok")
    return _back_to_pool()


@app.route("/images/sort", methods=["POST"])
def sort_images():
    by = request.form.get("by", "")
    labels = {"name": "nach Name", "newest": "neueste zuerst", "oldest": "älteste zuerst", "random": "zufällig gemischt"}
    if by not in labels:
        flash("Unbekannte Sortierung.", "err")
        return _back_to_pool(1)
    sort_pool(by)
    _switch_to_sequence()
    flash(f"Reihenfolge {labels[by]}. Gedruckt wird ab jetzt in dieser Reihenfolge.", "ok")
    return _back_to_pool(1)


@app.route("/images/bulk", methods=["POST"])
def bulk_images():
    action = request.form.get("action", "")
    if request.form.get("all") == "1":
        names = [p.name for p in _pool_files()]
        everything = True
    else:
        names = [secure_filename(n) for n in request.form.getlist("names")]
        names = [n for n in names if (IMAGE_DIR / n).exists()]
        everything = False
    if not names:
        flash("Keine Bilder ausgewählt.", "err")
        return _back_to_pool()
    if action == "delete":
        if everything:
            count = clear_pool()
            flash(f"Bilderpool geleert ({count} Bilder gelöscht).", "ok")
            return _back_to_pool(1)
        for n in names:
            try:
                delete_pool_image(n)
            except Exception as exc:
                flash(f"Löschen fehlgeschlagen: {n}: {exc}", "err")
        flash(f"{len(names)} Bild(er) gelöscht.", "ok")
        return _back_to_pool()
    if action in ("first", "last"):
        move_selection(names, action)
        _switch_to_sequence()
        flash(f"{len(names)} Bild(er) {'an den Anfang' if action == 'first' else 'ans Ende'} verschoben.", "ok")
        return _back_to_pool(1 if action == "first" else 10 ** 6)
    flash("Unbekannte Aktion.", "err")
    return _back_to_pool()


@app.route("/images/order", methods=["POST"])
def reorder_images():
    data = request.get_json(silent=True) or {}
    names = data.get("order")
    if not isinstance(names, list) or not all(isinstance(n, str) for n in names):
        return jsonify({"ok": False, "error": "order must be a list of names"}), 400
    order = reorder_subset([secure_filename(n) for n in names])
    switched = _switch_to_sequence()
    if switched:
        flash("Bildauswahl auf „Feste Reihenfolge“ umgestellt.", "ok")
    return jsonify({"ok": True, "order": order, "switched": switched})


@app.route("/images/<name>/delete", methods=["POST"])
def delete_image(name):
    safe = secure_filename(name)
    if not (IMAGE_DIR / safe).exists():
        flash(f"Nicht gefunden: {safe}", "err")
        return redirect(url_for("index"))
    try:
        delete_pool_image(safe)
        flash(f"Gelöscht: {safe}", "ok")
    except Exception as exc:
        flash(f"Löschen fehlgeschlagen: {exc}", "err")
    return _back_to_pool()


# ---------------------------------------------------------------------------
# JSON API (e.g. for Node-RED / Home Assistant)
# ---------------------------------------------------------------------------

_api_print_lock = threading.Lock()


def _api_authorized() -> bool:
    """With API_TOKEN set, require it as Bearer token, X-API-Key header or ?token=."""
    if not API_TOKEN:
        return True
    auth = request.headers.get("Authorization", "")
    given = auth[7:].strip() if auth.lower().startswith("bearer ") else ""
    given = given or request.headers.get("X-API-Key", "") or request.args.get("token", "")
    return secrets.compare_digest(given.encode(), API_TOKEN.encode())


@app.before_request
def _api_guard():
    if request.path.startswith("/api/") and not _api_authorized():
        return jsonify({"ok": False, "error": "unauthorized"}), 401


@app.route("/api/next", methods=["GET"])
def api_next():
    """Which image the next print will use, plus schedule and printer state."""
    settings = load_settings()
    nxt = pick_next_image(settings)
    return jsonify({
        "ok":                True,
        "next_image":        nxt.name if nxt else None,
        "pool_size":         len(_pool_files()),
        "image_order":       settings["image_order"],
        "next_run":          describe_next_run(),
        "printer_reachable": _check_printer_reachable(),
        "version":           VERSION,
    })


@app.route("/api/print", methods=["POST"])
def api_print():
    """Print now and wait for the result.

    Without a body: the next image according to the selection (advances the
    fixed order exactly like the weekly job). Optional JSON / form field
    "image": print that pool file instead (does not advance the order).
    """
    data = request.get_json(silent=True) or request.form or {}
    name = (data.get("image") or request.args.get("image") or "").strip()
    image = None
    if name:
        image = IMAGE_DIR / secure_filename(name)
        if not image.exists():
            return jsonify({"ok": False, "error": f"image not found: {name}"}), 404

    if not _api_print_lock.acquire(blocking=False):
        return jsonify({"ok": False, "error": "a print job is already running"}), 409
    try:
        success = do_print(manual=True, image=image)
    finally:
        _api_print_lock.release()

    entry = (load_state().get("history") or [{}])[0]
    nxt = pick_next_image()
    return jsonify({
        "ok":         success,
        "image":      entry.get("image"),
        "message":    entry.get("msg"),
        "options":    entry.get("opts", {}),
        "time":       entry.get("iso"),
        "next_image": nxt.name if nxt else None,
    }), (200 if success else 502)


@app.route("/status")
def status():
    state = load_state()
    settings = load_settings()
    next_img = pick_next_image(settings)
    state["version"]           = VERSION
    state["printer_reachable"] = _check_printer_reachable()
    state["printer_uri"]       = PRINTER_URI
    state["printer_name"]      = PRINTER_NAME
    state["schedule"]          = {"weekday": PRINT_WEEKDAY, "time": PRINT_TIME}
    state["next_run"]          = describe_next_run()
    state["settings"]          = settings
    state["printer_caps"]      = _printer_capabilities()
    state["images"]            = [p.name for p in _pool_files()]
    state["next_image"]        = next_img.name if next_img else None
    return jsonify(state)


def start_background_services() -> None:
    """Start scheduler exactly once (needed for gunicorn import mode)."""
    global _scheduler_started
    with _scheduler_lock:
        if _scheduler_started:
            return
        threading.Thread(target=run_scheduler, daemon=True).start()
        _scheduler_started = True
        split_existing_pdfs()
        settings = load_settings()
        log.info(f"=== AutoPrint v{VERSION} starting ===")
        log.info(
            f"AutoPrint ready | Version: {VERSION} | Printer: {PRINTER_NAME} "
            f"| Schedule: {PRINT_WEEKDAY} @ {PRINT_TIME} "
            f"| Quality: {settings['quality']} | MediaType: {settings['media_type']} "
            f"| Strip: {settings['strip_position'] if settings['strip_enabled'] else 'off'} "
            f"| Order: {settings['image_order']} | ImagesDir: {IMAGE_DIR}"
        )


start_background_services()


# ---------------------------------------------------------------------------
# Start
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080)
