#!/usr/bin/env python3
"""pcb-viewer: local, searchable Gerber + pick-and-place viewer with remote control.

Each Gerber/drill layer is rendered to SVG by gerbv over one common window, so
all layers line up in millimetres. A browser viewer is served on 127.0.0.1,
and the CLI (or anything that can POST JSON) can steer it: focus a part, go to
a coordinate, switch side, toggle layers. The viewer reports what it is showing
back to the server, so `status` tells the other side what the user sees.

Viewing and search need only the standard library and `gerbv` on PATH. Copper
tracing also needs numpy, scipy and Pillow; Altium PDF netlists need poppler's
`pdftohtml`.
"""

import argparse
import csv
import hashlib
import io
import json
import os
import queue
import re
import shutil
import struct
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
CONFIG_DIR = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "pcb-viewer"
BOARDS_FILE = CONFIG_DIR / "boards.json"
CACHE_DIR = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "pcb-viewer"
DEFAULT_PORT = int(os.environ.get("PCB_VIEWER_PORT", "8765"))
MARGIN_MM = 1.0
# gerbv truncates export sizes to whole device units, and the browser then
# stretches each layer to the board window. At its default 72 dpi that is up to
# 1 pt (0.35 mm) of stretch across the board, so every export uses 1016 dpi
# (0.025 mm units) and a window padded by half a unit so nothing is truncated.
RENDER_DPI = 1016.0


def gerbv_window_args(win, dpi=RENDER_DPI):
    pad = 0.5 / dpi  # inches: half a device unit
    return ["-D", f"{dpi:.1f}",
            "-O", f'{win["x0"] / 25.4:.6f}x{win["y0"] / 25.4:.6f}',
            "-W", f'{win["w"] / 25.4 + pad:.6f}x{win["h"] / 25.4 + pad:.6f}']


# ---------------------------------------------------------------------------
# Layer identification
# ---------------------------------------------------------------------------

COLORS = {
    ("copper", "top"): "#c8823cff",
    ("copper", "bottom"): "#3c7fc8ff",
    ("copper", "inner"): "#8c8c8cff",
    ("silk", "top"): "#f2f2f2ff",
    ("silk", "bottom"): "#f2e6a0ff",
    ("mask", "top"): "#2fae5a80",
    ("mask", "bottom"): "#2f8fae80",
    ("paste", "top"): "#b0b0b0c0",
    ("paste", "bottom"): "#b0b0b0c0",
    ("outline", None): "#ffe44dff",
    ("drill", None): "#000000ff",
    ("mech", None): "#c080ffc0",
}

# Protel/Altium extensions, also used by KiCad ("Protel extensions") and JLC.
EXT_MAP = {
    "gtl": ("copper", "top"), "gbl": ("copper", "bottom"),
    "gto": ("silk", "top"), "gbo": ("silk", "bottom"),
    "gts": ("mask", "top"), "gbs": ("mask", "bottom"),
    "gtp": ("paste", "top"), "gbp": ("paste", "bottom"),
    "gko": ("outline", None), "gm1": ("outline", None), "gml": ("outline", None),
    # Eagle CAM
    "cmp": ("copper", "top"), "sol": ("copper", "bottom"),
    "plc": ("silk", "top"), "pls": ("silk", "bottom"),
    "stc": ("mask", "top"), "sts": ("mask", "bottom"),
    "crc": ("paste", "top"), "crs": ("paste", "bottom"),
    "dim": ("outline", None),
}

KICAD_PATTERNS = [
    (r"[-_.]F[._]Cu$", ("copper", "top")), (r"[-_.]B[._]Cu$", ("copper", "bottom")),
    (r"[-_.]In(\d+)[._]Cu$", ("copper", "inner")),
    (r"[-_.]F[._]Silk(screen|S)$", ("silk", "top")), (r"[-_.]B[._]Silk(screen|S)$", ("silk", "bottom")),
    (r"[-_.]F[._]Mask$", ("mask", "top")), (r"[-_.]B[._]Mask$", ("mask", "bottom")),
    (r"[-_.]F[._]Paste$", ("paste", "top")), (r"[-_.]B[._]Paste$", ("paste", "bottom")),
    (r"[-_.]Edge[._]Cuts$", ("outline", None)),
]


# Reports, rule files and aperture libraries that sit next to Gerbers.
NON_LAYER_EXT = {".apr", ".apr_lib", ".rep", ".extrep", ".rul", ".drr", ".ldp", ".pdf", ".csv", ".txt~",
                 ".zip", ".xlsx", ".html", ".log"}


def sniff(path):
    """Return 'gerber', 'drill' or None from file content."""
    if path.suffix.lower() in NON_LAYER_EXT:
        return None
    try:
        head = path.read_bytes()[:65536].decode("latin-1")
    except OSError:
        return None
    if re.search(r"%FS|%MO|%AD|^G04", head, re.M):
        return "gerber"
    if re.search(r"^M48", head, re.M) or re.search(r"^T\d+C\d*\.?\d", head, re.M):
        return "drill"
    return None


def altium_extrep(files):
    """Map extension -> layer description from Altium .EXTREP reports."""
    desc = {}
    for f in files:
        if f.suffix.lower() != ".extrep":
            continue
        for line in f.read_text(errors="ignore").splitlines():
            m = re.match(r"^\.(\w+)\s{2,}(.+?)\s*$", line)
            if m:
                desc[m.group(1).lower()] = m.group(2)
    return desc


def x2_function(path):
    head = path.read_bytes()[:8192].decode("latin-1")
    m = re.search(r"TF\.FileFunction,([^*]+)\*", head)
    return m.group(1).split(",") if m else None


def classify(path, extrep):
    """Return (kind, side, inner_index, label) or None to skip the file."""
    ext = path.suffix.lower().lstrip(".")
    stem = path.stem
    desc = extrep.get(ext, "")
    dl = desc.lower()
    if "drill drawing" in dl or "drill guide" in dl or re.fullmatch(r"g[dg]\d+", ext):
        return None
    label = desc or path.name
    edge = "outline" in dl or dl in ("board", "board shape")  # Altium mechanical layer names

    ff = x2_function(path)
    if ff:
        fn = ff[0].lower()
        side = "top" if "top" in [x.lower() for x in ff] else "bottom" if "bot" in [x.lower() for x in ff] else None
        if fn == "copper":
            m = re.match(r"L(\d+)", ff[1]) if len(ff) > 1 else None
            if side is None:
                return ("copper", "inner", int(m.group(1)) if m else 0, label)
            return ("copper", side, 0, label)
        if fn == "legend":
            return ("silk", side or "top", 0, label)
        if fn == "soldermask":
            return ("mask", side or "top", 0, label)
        if fn == "paste":
            return ("paste", side or "top", 0, label)
        if fn == "profile":
            return ("outline", None, 0, label)
        if fn in ("plated", "nonplated"):
            return ("drill", None, 0, label)

    for pat, (kind, side) in KICAD_PATTERNS:
        m = re.search(pat, stem)
        if m:
            idx = int(m.group(1)) if kind == "copper" and side == "inner" else 0
            return (kind, side, idx, label)

    if ext in EXT_MAP:
        kind, side = EXT_MAP[ext]
        if kind == "outline" and desc and not edge and ext == "gm1":
            return ("mech", None, 0, label)
        return (kind, side, 0, label)
    m = re.fullmatch(r"g(\d+)|gp(\d+)|g(\d+)l", ext)
    if m:
        return ("copper", "inner", int(next(g for g in m.groups() if g)), label)
    if re.fullmatch(r"gm\d*", ext):
        return ("outline" if edge else "mech", None, 0, label)
    if edge or "profile" in dl:
        return ("outline", None, 0, label)
    return ("mech", None, 0, label)


def discover_layers(gerber_dir, drill_dirs):
    files = sorted(p for p in Path(gerber_dir).iterdir() if p.is_file())
    extra = []
    for d in drill_dirs:
        extra += sorted(p for p in Path(d).iterdir() if p.is_file())
    extrep = altium_extrep(files + extra)
    layers = []
    for p in files + extra:
        kind = sniff(p)
        if kind == "drill":
            name = p.name
            micro = "micro" in name.lower() or re.search(r"\.(tx|dr)\d+$", name, re.I)
            layers.append({"kind": "drill", "side": None, "inner": 0, "label": name,
                           "path": str(p), "default_visible": not micro})
        elif kind == "gerber":
            c = classify(p, extrep)
            if c is None:
                continue
            k, s, idx, label = c
            layers.append({"kind": k, "side": s, "inner": idx, "label": label, "path": str(p),
                           "default_visible": k in ("copper", "silk", "outline") and s != "inner"})
    # drill labels: drop the prefix shared by every file name; disambiguate repeats
    names = [Path(l["path"]).name for l in layers]
    prefix = os.path.commonprefix(names) if len(names) > 1 else ""
    for l in layers:
        if l["kind"] == "drill":
            l["label"] = Path(l["path"]).name[len(prefix):].strip(" -_.") or l["label"]
    seen = {}
    for l in layers:
        seen.setdefault(l["label"], []).append(l)
    for label, ls in seen.items():
        if len(ls) > 1:
            for l in ls:
                l["label"] = f'{label} ({Path(l["path"]).name[len(prefix):].strip(" -_.")})'
    outlines = [l for l in layers if l["kind"] == "outline"]
    # keep one outline; demote the rest
    for l in outlines[1:]:
        l["kind"] = "mech"
        l["default_visible"] = False
    for l in layers:
        l["color"] = COLORS[(l["kind"], l["side"] if l["kind"] in ("copper", "silk", "mask", "paste") else None)]
        l["id"] = hashlib.sha1(l["path"].encode()).hexdigest()[:10]
    return layers


# ---------------------------------------------------------------------------
# Gerber extents (for a common render window)
# ---------------------------------------------------------------------------

def gerber_bbox(path):
    """Approximate bbox in mm from X/Y coordinates (arcs by endpoints)."""
    text = Path(path).read_text(errors="ignore")
    fmt = re.search(r"%FS([LTD])?A?X(\d)(\d)Y(\d)(\d)", text)
    zero, xi, xd = (fmt.group(1) or "L", int(fmt.group(2)), int(fmt.group(3))) if fmt else ("L", 2, 4)
    inch = bool(re.search(r"%MOIN|G70", text)) and not re.search(r"%MOMM", text)
    scale = 25.4 if inch else 1.0
    body = re.sub(r"%[^%]*%", "", text)

    def val(s):
        neg = s.startswith("-")
        s = s.lstrip("+-")
        if zero == "T":
            s = s.ljust(xi + xd, "0")
        v = int(s) / 10 ** xd
        return (-v if neg else v) * scale

    xs, ys = [], []
    x = y = None
    for m in re.finditer(r"(?:X([+-]?\d+))?(?:Y([+-]?\d+))?(?:I[+-]?\d+)?(?:J[+-]?\d+)?D0?[123]\*", body):
        if m.group(1):
            x = val(m.group(1))
        if m.group(2):
            y = val(m.group(2))
        if x is not None and y is not None:
            xs.append(x)
            ys.append(y)
    if not xs:
        return None
    return min(xs), min(ys), max(xs), max(ys)


def board_window(layers):
    src = [l for l in layers if l["kind"] == "outline"] or [l for l in layers if l["kind"] == "copper"]
    boxes = [b for b in (gerber_bbox(l["path"]) for l in src) if b]
    if not boxes:
        raise SystemExit("could not determine board extents from outline/copper layers")
    x0 = min(b[0] for b in boxes) - MARGIN_MM
    y0 = min(b[1] for b in boxes) - MARGIN_MM
    x1 = max(b[2] for b in boxes) + MARGIN_MM
    y1 = max(b[3] for b in boxes) + MARGIN_MM
    return {"x0": x0, "y0": y0, "w": x1 - x0, "h": y1 - y0}


def render_layer(layer, win, outdir, force=False):
    st = Path(layer["path"]).stat()
    key = f'{layer["path"]}|{st.st_mtime_ns}|{st.st_size}|{layer["color"]}|{win}|{RENDER_DPI}'
    out = outdir / (hashlib.sha1(key.encode()).hexdigest()[:16] + ".svg")
    if out.exists() and not force:
        return out.name, None
    cmd = ["gerbv", "-x", "svg", "-B", "0", *gerbv_window_args(win),
           "-f", layer["color"], "-o", str(out), layer["path"]]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    if r.returncode != 0 or not out.exists() or out.stat().st_size < 200:
        out.unlink(missing_ok=True)  # or the next run would take it as a cached render
        return None, (r.stderr or r.stdout).strip()[-300:]
    return out.name, None


# ---------------------------------------------------------------------------
# Pick-and-place parsing
# ---------------------------------------------------------------------------

COLS = {
    "ref": ["designator", "refdes", "reference", "ref", "part", "name", "partname"],
    "x": ["centerx", "midx", "posx", "x", "locationx", "centroidx", "refx", "padx"],
    "y": ["centery", "midy", "posy", "y", "locationy", "centroidy", "refy", "pady"],
    "rot": ["rotation", "rot", "angle", "orientation"],
    "side": ["layer", "side", "tb", "placement"],
    "val": ["comment", "val", "value"],
    "fp": ["footprint", "package", "pattern", "packagename", "packagereference"],
}


def _norm(h):
    unit = re.search(r"\((mm|mil|mils|in|inch)\)", h, re.I)
    return re.sub(r"[^a-z0-9]", "", re.sub(r"\(.*?\)", "", h.lower())), (unit.group(1).lower() if unit else None)


def _num(s, unit):
    m = re.match(r"\s*([-+]?\d*\.?\d+(?:[eE][-+]?\d+)?)\s*(mm|mils?|in|inch)?", s or "")
    if not m:
        return None
    v = float(m.group(1))
    u = (m.group(2) or unit or "mm").lower()
    return v * (0.0254 if u.startswith("mil") else 25.4 if u.startswith("in") else 1.0)


def _side(s, default):
    s = (s or "").strip().lower()
    if s in ("b", "bot", "bottom", "bottomlayer", "back", "bottom layer") or s.startswith("bot"):
        return "bottom"
    if s in ("t", "top", "toplayer", "front", "top layer") or s.startswith("top"):
        return "top"
    return default


def _cfb_streams(path, want):
    """Named streams of an OLE2 compound file (an Altium .PcbDoc): {'Storage/Stream': bytes}."""
    b = Path(path).read_bytes()
    if b[:8] != bytes.fromhex("d0cf11e0a1b11ae1"):
        raise SystemExit(f"{path}: not an OLE compound file")
    ss, mss = 1 << struct.unpack_from("<H", b, 30)[0], 1 << struct.unpack_from("<H", b, 32)[0]
    n_fat, dir_start = struct.unpack_from("<II", b, 44)
    cutoff, minifat_start, n_minifat, difat_next, n_difat = struct.unpack_from("<5I", b, 56)
    sector = lambda i: b[(i + 1) * ss:(i + 2) * ss]
    difat = list(struct.unpack_from("<109I", b, 76))
    for _ in range(n_difat):
        d = sector(difat_next)
        difat += struct.unpack_from(f"<{ss // 4 - 1}I", d)
        difat_next = struct.unpack_from("<I", d, ss - 4)[0]
    fat = [e for i in difat[:n_fat] for e in struct.unpack(f"<{ss // 4}I", sector(i))]

    def chain(start, table):
        for _ in range(len(table)):  # bounded, in case of a corrupt loop
            if start >= 0xFFFFFFFA:
                return
            yield start
            start = table[start]

    read = lambda start: b"".join(sector(i) for i in chain(start, fat))
    d = read(dir_start)
    ents = []
    for o in range(0, len(d) - 127, 128):
        nl, typ = struct.unpack_from("<HB", d, o + 64)
        left, right, child = struct.unpack_from("<iii", d, o + 68)
        start, size = struct.unpack_from("<IQ", d, o + 116)
        ents.append((d[o:o + max(nl - 2, 0)].decode("utf-16-le"), typ, left, right, child, start,
                     size & 0xFFFFFFFF if ss == 512 else size))
    mini = read(ents[0][5])
    mb = read(minifat_start) if n_minifat else b""
    minifat = struct.unpack(f"<{len(mb) // 4}I", mb)
    out = {}

    def walk(i, prefix):
        if not 0 <= i < len(ents):
            return
        name, typ, left, right, child, start, size = ents[i]
        walk(left, prefix)
        walk(right, prefix)
        if typ == 1:
            walk(child, prefix + name + "/")
        elif typ == 2 and prefix + name in want:
            out[prefix + name] = (b"".join(mini[j * mss:(j + 1) * mss] for j in chain(start, minifat))
                                  if size < cutoff else read(start))[:size]

    walk(ents[0][4], "")
    return out


def _altium_records(blob):
    """Altium's length-prefixed |KEY=VALUE|... text records."""
    out, o = [], 0
    while o + 4 <= len(blob):
        n = struct.unpack_from("<I", blob, o)[0] & 0xFFFFFF
        text = blob[o + 4:o + 4 + n].rstrip(b"\0").decode("latin-1")
        out.append(dict(kv.split("=", 1) for kv in text.split("|") if "=" in kv))
        o += 4 + n
    return out


def parse_altium_pcbdoc(path, offset=(0.0, 0.0)):
    """Placements from an Altium .PcbDoc, relative to the board origin as Altium's Gerber
    and pick-and-place outputs are by default. A part's x/y is its footprint reference
    point, which is usually but not always the centre."""
    s = _cfb_streams(path, {"Board6/Data", "Components6/Data"})
    if "Components6/Data" not in s:
        raise SystemExit(f"{path}: no Components6 stream (not an Altium PcbDoc?)")
    board = (_altium_records(s.get("Board6/Data", b"")) or [{}])[0]
    ox, oy = _num(board.get("ORIGINX", "0"), "mil"), _num(board.get("ORIGINY", "0"), "mil")
    parts = []
    for c in _altium_records(s["Components6/Data"]):
        if not c.get("SOURCEDESIGNATOR") or "X" not in c or "Y" not in c:
            continue
        parts.append({"ref": c["SOURCEDESIGNATOR"], "val": c.get("SOURCEDESCRIPTION", ""),
                      "fp": c.get("PATTERN", ""), "side": _side(c.get("LAYER"), "top"),
                      "x": round(_num(c["X"], "mil") - ox + offset[0], 4),
                      "y": round(_num(c["Y"], "mil") - oy + offset[1], 4),
                      "rot": float(c.get("ROTATION") or 0)})
    return parts


def parse_pnp(path, offset=(0.0, 0.0), flip_y=False):
    if Path(path).suffix.lower() == ".pcbdoc":
        return parse_altium_pcbdoc(path, offset)
    text = Path(path).read_bytes().decode("latin-1")
    default_side = "bottom" if Path(path).suffix.lower() in (".mnb",) else "top"
    um = re.search(r"units?\s*(?:used)?\s*[:=]\s*(mm|mil|mils|inch|in)\b", text, re.I)
    file_unit = um.group(1).lower() if um else None
    lines = text.splitlines()

    rows, header = [], None
    csv_like = sum(l.count(",") for l in lines[:50]) > 10 or sum(l.count(";") for l in lines[:50]) > 10
    if csv_like:
        delim = "," if sum(l.count(",") for l in lines[:50]) >= sum(l.count(";") for l in lines[:50]) else ";"
        for i, l in enumerate(lines):
            low = l.lower()
            if any(k in re.sub(r"[^a-z]", "", low) for k in ("designator", "refdes", "reference")) or \
               re.match(r'^\W*(ref|part|name)\W', low):
                reader = list(csv.reader(io.StringIO("\n".join(lines[i:])), delimiter=delim))
                header, rows = reader[0], reader[1:]
                break
    else:
        for i, l in enumerate(lines):
            if l.lstrip().startswith("#") and re.search(r"\bref\b", l, re.I) and re.search(r"pos\s*x", l, re.I):
                header = l.lstrip("# ").split()
                rows = [x.split() for x in lines[i + 1:] if x.strip() and not x.lstrip().startswith("#")]
                break
        if header is None:  # Eagle .mnt/.mnb: name x y rot value package
            header = ["name", "x", "y", "rot", "value", "package"]
            rows = [x.split() for x in lines if x.strip() and not x.lstrip().startswith(("#", ";"))]

    if not header:
        raise SystemExit(f"{path}: no pick-and-place header found")
    normed = [_norm(h) for h in header]
    idx, units = {}, {}
    for key, names in COLS.items():
        for n in names:
            hit = next((i for i, (h, _) in enumerate(normed) if h == n), None)
            if hit is not None:
                idx[key], units[key] = hit, normed[hit][1]
                break
    if not all(k in idx for k in ("ref", "x", "y")):
        raise SystemExit(f"{path}: need designator/x/y columns, header was {header}")

    parts = []
    for r in rows:
        if len(r) <= max(idx["ref"], idx["x"], idx["y"]) or not r[idx["ref"]].strip():
            continue
        x = _num(r[idx["x"]], units.get("x") or file_unit)
        y = _num(r[idx["y"]], units.get("y") or file_unit)
        if x is None or y is None:
            continue
        if flip_y:
            y = -y
        get = lambda k: r[idx[k]].strip() if k in idx and idx[k] < len(r) else ""
        rot = _num(get("rot"), None) if get("rot") else 0.0
        parts.append({"ref": get("ref"), "val": get("val"), "fp": get("fp"),
                      "side": _side(get("side"), default_side),
                      "x": round(x + offset[0], 4), "y": round(y + offset[1], 4), "rot": rot or 0.0})
    return parts


def search(parts, q, limit=200):
    q = q.strip()
    if not q:
        return []
    terms = [t for t in re.split(r"[,\s]+", q) if t]
    if len(terms) > 1 and all(any(p["ref"].lower() == t.lower() for p in parts) for t in terms):
        want = {t.lower() for t in terms}
        return [p for p in parts if p["ref"].lower() in want]
    ql = q.lower()
    scored = []
    for p in parts:
        r, v, f = p["ref"].lower(), p["val"].lower(), p["fp"].lower()
        s = 100 if r == ql else 60 if r.startswith(ql) else 40 if ql in r else \
            30 if v == ql else 20 if ql in v else 10 if ql in f else 0
        if s:
            scored.append((s, p["ref"], p))
    scored.sort(key=lambda t: (-t[0], len(t[1]), t[1]))
    return [p for _, _, p in scored[:limit]]


# ---------------------------------------------------------------------------
# Netlists
# ---------------------------------------------------------------------------

def _split_pin(s):
    ref, _, pin = s.rpartition("-")
    return (ref, pin) if ref else (s, "")


def _pdf_outline(path):
    """Bookmark tree of a PDF via poppler's pdftohtml: [{n, p, ch}]."""
    import html
    r = subprocess.run(["pdftohtml", "-xml", "-i", "-q", "-f", "1", "-l", "1", "-stdout", str(path)],
                       capture_output=True, text=True, errors="replace", timeout=120)
    xml = r.stdout
    i = xml.find("<outline>")
    if i < 0:
        raise SystemExit(f"{path}: PDF has no bookmarks (not an Altium Smart PDF?)")
    root, stack, last, first = [], None, None, True
    stack = [root]
    for m in re.finditer(r"<outline>|</outline>|<item(?: page=\"(\d+)\")?[^>]*>(.*?)</item>", xml[i:], re.S):
        t = m.group(0)
        if t == "<outline>":
            if first:
                first = False
                continue
            stack.append(last["ch"])
        elif t == "</outline>":
            if len(stack) > 1:
                stack.pop()
        else:
            last = {"n": html.unescape(m.group(2)).strip(), "p": int(m.group(1) or 0), "ch": []}
            stack[-1].append(last)
    return root


def parse_altium_pdf_netlist(path):
    """Netlist from an Altium Smart PDF schematic's bookmarks.

    Each sheet lists its nets (Pins / NetLabels / Ports). Sheet-local nets are
    joined across the hierarchy through port names, and nets without net
    labels (power ports) are joined by name, which is how Altium connects them.
    """
    parent = {}

    def find(a):
        while parent.setdefault(a, a) != a:
            parent[a] = parent[parent[a]]
            a = parent[a]
        return a

    nodes = {}

    def walk(items, sheet):
        for x in items:
            if x["n"] == "Nets":
                for net in x["ch"]:
                    cats = {c["n"]: [y["n"] for y in c["ch"]] for c in net["ch"]}
                    node = f'{sheet}|{net["n"]}'
                    nodes[node] = (net["n"], set(cats.get("Pins", [])), net["p"])
                    find(node)
                    for port in cats.get("Ports", []):
                        parent[find(node)] = find("port:" + port)
                    if cats.get("Ports") or not cats.get("NetLabels"):
                        parent[find(node)] = find("name:" + net["n"])
            elif x["n"] != "Components":
                walk(x["ch"], re.sub(r"\.SchDoc.*", "", x["n"]))

    walk(_pdf_outline(path), "")
    # a pin is on one net only, so entries sharing a pin are the same net (this also
    # folds in the project-wide net list some Smart PDFs carry beside the sheets)
    owner = {}
    for node, (_, pins, _) in nodes.items():
        for pin in pins:
            if pin in owner:
                parent[find(node)] = find(owner[pin])
            else:
                owner[pin] = node
    groups = {}
    for node in nodes:
        groups.setdefault(find(node), []).append(node)
    nets = {}
    for members in groups.values():
        pins = set().union(*(nodes[m][1] for m in members))
        if not pins:
            continue
        names = sorted({nodes[m][0] for m in members}, key=lambda n: (n.startswith("Net"), len(n), n))
        name = names[0]
        while name in nets:  # unrelated sheet-local nets that share a label
            name += "'"
        pages = sorted({nodes[m][2] for m in members if nodes[m][2]})
        nets[name] = {"aliases": names[1:], "pins": sorted(pins), "pages": pages}
    return nets


def parse_ipc356(path):
    """IPC-D-356(A) netlist: 317/327/367 records -> {net: {pins}}."""
    nets, full = {}, {}
    for line in Path(path).read_text(errors="ignore").splitlines():
        if line.startswith("P  NNAME"):  # long net name alias: P  NNAME1  <full name>
            parts = line.split(None, 2)
            if len(parts) == 3:
                full[parts[1]] = parts[2].strip()
            continue
        if line[:3] not in ("317", "327", "367") or len(line) < 31:
            continue
        net = line[3:17].strip()
        ref = line[20:26].strip()
        pin = line[27:31].strip()
        if not net or not ref or ref == "VIA" or net == "N/C":
            continue
        net = full.get(net, net)
        nets.setdefault(net, {"aliases": [], "pins": set(), "pages": []})["pins"].add(f"{ref}-{pin}")
    for v in nets.values():
        v["pins"] = sorted(v["pins"])
    return nets


def parse_netlist(path):
    if Path(path).suffix.lower() == ".pdf":
        return parse_altium_pdf_netlist(path)
    return parse_ipc356(path)


def search_nets(nets, q, limit=50):
    q = q.strip()
    if q.lower().startswith("net:"):
        q = q[4:].strip()
    if not q:
        return []
    ql = q.lower()
    scored = []
    for name, n in nets.items():
        names = [name] + n["aliases"]
        low = [x.lower() for x in names]
        s = 100 if ql in low else 60 if any(x.startswith(ql) for x in low) else 30 if any(ql in x for x in low) else 0
        if s:
            scored.append((s, name))
    scored.sort(key=lambda t: (-t[0], len(t[1]), t[1]))
    return [{"name": nm, **nets[nm]} for _, nm in scored[:limit]]


# ---------------------------------------------------------------------------
# Copper connectivity (trace highlighting)
# ---------------------------------------------------------------------------
# Copper layers are rasterised by gerbv over the common window and labelled
# into connected copper islands (scipy.ndimage). Plated holes join the islands
# they pierce on every layer they connect, and a graph pass groups islands into
# electrical nets once per board, so a trace is a lookup. Which layers a drill
# file connects comes from Altium's layer-pair (.LDP) file; without one, plated
# drill files are treated as through-holes. numpy, scipy and Pillow are needed
# here; the rest of the tool is standard library.

TRACE_PPMM = 40        # connectivity raster, px per mm (0.025 mm per px)
TRACE_SHOW_DIV = 2     # overlays are sent at half that resolution
TRACE_SNAP_MM = 0.4    # a seed off copper snaps to copper this close
TRACE_KEEP = 12        # trace results kept on disk per board


def parse_excellon(path):
    """Hole centres in mm; routed slots (M15..M16, G85) are sampled every 0.1 mm."""
    text = Path(path).read_text(errors="ignore")
    inch = bool(re.search(r"^\s*INCH|^M72", text, re.M))
    fm = re.search(r"FILE_FORMAT=(\d+):(\d+)", text)
    ints, decs = (int(fm.group(1)), int(fm.group(2))) if fm else ((2, 4) if inch else (3, 3))
    trailing_kept = bool(re.search(r"^\s*(METRIC|INCH)\s*,\s*TZ", text, re.M))
    scale = 25.4 if inch else 1.0

    def val(v):
        if "." in v:
            return float(v) * scale
        neg = v.startswith("-")
        v = v.lstrip("+-")
        n = int(v) / 10 ** decs if trailing_kept else int(v.ljust(ints + decs, "0")) / 10 ** decs
        return (-n if neg else n) * scale

    def seg(a, b):
        n = max(1, int(((b[0] - a[0]) ** 2 + (b[1] - a[1]) ** 2) ** 0.5 / 0.1))
        return [(a[0] + (b[0] - a[0]) * i / n, a[1] + (b[1] - a[1]) * i / n) for i in range(n + 1)]

    holes, x, y, route, down = [], None, None, False, False
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith(";"):
            continue
        if line.startswith(("G00", "G01")):
            route = True
        if line.startswith(("G05", "G81")):
            route = False
        if line.startswith("M15"):
            down = True
            if x is not None:
                holes.append((x, y))
            continue
        if line.startswith(("M16", "M17")):
            down = False
            continue
        if "G85" in line:  # canned slot: X..Y..G85X..Y..
            a, b = line.split("G85", 1)
            p0 = [x, y]
            for ax, v in re.findall(r"([XY])([+-]?[\d.]+)", a):
                p0["XY".index(ax)] = val(v)
            p1 = list(p0)
            for ax, v in re.findall(r"([XY])([+-]?[\d.]+)", b):
                p1["XY".index(ax)] = val(v)
            holes += seg(tuple(p0), tuple(p1))
            x, y = p1
            continue
        coords = re.findall(r"([XY])([+-]?[\d.]+)", line)
        if not coords or re.match(r"^T\d", line):
            continue
        nx, ny = x, y
        for ax, v in coords:
            if ax == "X":
                nx = val(v)
            else:
                ny = val(v)
        if nx is None or ny is None:
            x, y = nx, ny
            continue
        if route:
            if down and line.startswith("G01") and x is not None:
                holes += seg((x, y), (nx, ny))
        else:
            holes.append((nx, ny))
        x, y = nx, ny
    return holes


def drill_layer_sets(cfg, layers):
    """{drill path: [copper layer ids it connects]} for plated drill files."""
    copper = [l for l in layers if l["kind"] == "copper"]
    by_ext = {Path(l["path"]).suffix.lower().lstrip("."): l["id"] for l in copper}
    ldp = {}
    for d in [cfg["gerber_dir"]] + list(cfg.get("drill_dirs", [])):
        for f in Path(d).iterdir():
            if f.suffix.lower() == ".ldp":
                for line in f.read_text(errors="ignore").splitlines():
                    m = re.search(r"DrillFile=([^|]+)\|DrillLayers=(.+)$", line)
                    if m:
                        ldp[m.group(1).strip().lower()] = [e.strip().lower() for e in m.group(2).split(",")]
    sets = {}
    for l in layers:
        if l["kind"] != "drill":
            continue
        name = Path(l["path"]).name.lower()
        if re.search(r"non.?plated|npth", name):
            continue
        if ldp:
            if name in ldp:
                sets[l["path"]] = [by_ext[e] for e in ldp[name] if e in by_ext]
        else:
            sets[l["path"]] = [c["id"] for c in copper]
    return sets


class TraceEngine:
    def __init__(self, name, cfg):
        self.name, self.cfg = name, cfg
        self.lock = threading.Lock()
        self.ready = False
        self.outdir = CACHE_DIR / name / "trace"

    def _load(self):
        import numpy as np
        from PIL import Image
        Image.MAX_IMAGE_PIXELS = None
        layers = discover_layers(self.cfg["gerber_dir"], self.cfg.get("drill_dirs", []))
        self.win = win = board_window(layers)
        rank = {"top": 0, "inner": 1, "bottom": 2}
        self.copper = sorted((l for l in layers if l["kind"] == "copper"),
                             key=lambda l: (rank[l["side"]], l["inner"]))
        self.outdir.mkdir(parents=True, exist_ok=True)
        dpi = TRACE_PPMM * 25.4

        def raster(l):
            st = Path(l["path"]).stat()
            key = f'{l["path"]}|{st.st_mtime_ns}|{st.st_size}|{win}|{dpi}|padded'
            out = self.outdir / ("r_" + hashlib.sha1(key.encode()).hexdigest()[:16] + ".png")
            if not out.exists():
                subprocess.run(["gerbv", "-x", "png", "-B", "0", *gerbv_window_args(win, dpi),
                                "-b", "#000000", "-f", "#ffffffff", "-o", str(out), l["path"]],
                               capture_output=True, timeout=600)
            return l["id"], np.asarray(Image.open(out).convert("L")) > 127

        with ThreadPoolExecutor(max_workers=min(4, os.cpu_count() or 2)) as ex:
            self.masks = dict(ex.map(raster, self.copper))
        from scipy import ndimage
        from scipy.sparse import coo_matrix
        from scipy.sparse.csgraph import connected_components
        self.h, self.w = next(iter(self.masks.values())).shape
        self.sx, self.sy = self.w / win["w"], self.h / win["h"]

        def label(lid):
            lab, n = ndimage.label(self.masks[lid])  # 4-connected copper islands
            return lid, (lab.astype(np.uint16) if n < 65535 else lab), n

        with ThreadPoolExecutor(max_workers=min(4, os.cpu_count() or 2)) as ex:
            res = list(ex.map(label, [l["id"] for l in self.copper]))
        self.labels, self.offset, total = {}, {}, 0
        for lid, lab, n in res:
            self.labels[lid], self.offset[lid] = lab, total
            total += n + 1  # component 0 (bare board) gets a node too, never linked
        del self.masks
        rows, cols = [], []
        for path, lids in drill_layer_sets(self.cfg, layers).items():
            pts = parse_excellon(path)
            if not pts or len(lids) < 2:
                continue
            xs = np.clip(np.rint((np.array([p[0] for p in pts]) - win["x0"]) * self.sx).astype(int), 0, self.w - 1)
            ys = np.clip(np.rint((win["y0"] + win["h"] - np.array([p[1] for p in pts])) * self.sy).astype(int), 0, self.h - 1)
            comps = [self.labels[lid][ys, xs].astype(np.int64) for lid in lids]
            for a in range(len(lids)):
                for b in range(a + 1, len(lids)):
                    ok = (comps[a] > 0) & (comps[b] > 0)
                    rows.append(comps[a][ok] + self.offset[lids[a]])
                    cols.append(comps[b][ok] + self.offset[lids[b]])
        r = np.concatenate(rows) if rows else np.zeros(0, np.int64)
        c = np.concatenate(cols) if cols else np.zeros(0, np.int64)
        graph = coo_matrix((np.ones(len(r), np.int8), (r, c)), shape=(total, total))
        _, self.group = connected_components(graph, directed=False)
        self.ready = True

    def warm(self):
        with self.lock:
            if not self.ready:
                try:
                    self._load()
                except Exception as e:  # tracing stays unavailable; viewing still works
                    print(f"trace engine for {self.name} failed: {e}", flush=True)

    def layer_for(self, side=None, label=None):
        if label:
            for l in self.copper:
                if label.lower() in l["label"].lower():
                    return l
            raise ValueError(f"no copper layer matching {label!r}")
        want = side or "top"
        return next(l for l in self.copper if l["side"] == want)

    def trace(self, x, y, side=None, label=None, parts=(), nets=None):
        import numpy as np
        from PIL import Image
        with self.lock:
            if not self.ready:
                self._load()
            start = self.layer_for(side, label)
            px = int(round((x - self.win["x0"]) * self.sx))
            py = int(round((self.win["y0"] + self.win["h"] - y) * self.sy))
            if not (0 <= px < self.w and 0 <= py < self.h):
                raise ValueError("point is outside the board")
            lab = self.labels[start["id"]]
            if not lab[py, px]:  # snap to the nearest copper pixel
                r = int(TRACE_SNAP_MM * TRACE_PPMM)
                y0, y1, x0, x1 = max(py - r, 0), min(py + r + 1, self.h), max(px - r, 0), min(px + r + 1, self.w)
                cy, cx = np.nonzero(lab[y0:y1, x0:x1])
                if not len(cx):
                    raise ValueError(f'no copper on {start["label"]} within {TRACE_SNAP_MM} mm of ({x}, {y})')
                i = np.argmin((cx + x0 - px) ** 2 + (cy + y0 - py) ** 2)
                px, py = int(cx[i] + x0), int(cy[i] + y0)
            g = self.group[self.offset[start["id"]] + int(lab[py, px])]

            tid = hashlib.sha1(f"{x},{y},{start['id']},{time.time()}".encode()).hexdigest()[:10]
            out, masks = [], {}
            d = TRACE_SHOW_DIV
            for l in self.copper:
                lab = self.labels[l["id"]]
                o = self.offset[l["id"]]
                lut = self.group[o:o + int(lab.max()) + 1] == g
                lut[0] = False
                if not lut.any():
                    continue
                reached = lut[lab]
                n = int(reached.sum())
                if not n:
                    continue
                masks[l["id"]] = reached
                h2, w2 = self.h // d, self.w // d
                small = reached[:h2 * d, :w2 * d].reshape(h2, d, w2, d).any(axis=(1, 3))
                rgba = np.zeros((h2, w2, 4), np.uint8)
                rgba[..., 0], rgba[..., 1], rgba[..., 2] = 255, 48, 224
                rgba[..., 3] = small * 235
                fname = f"t_{tid}_{l['id']}.png"
                Image.fromarray(rgba, "RGBA").save(self.outdir / fname, compress_level=3)
                out.append({"id": l["id"], "label": l["label"], "side": l["side"], "file": fname,
                            "area_mm2": round(n / (self.sx * self.sy), 2)})

            # parts whose centroid sits on reached copper of their own outer layer
            touched = []
            r = int(0.35 * TRACE_PPMM)
            outer = {l["side"]: l["id"] for l in self.copper if l["side"] in ("top", "bottom")}
            for p in parts:
                mk = masks.get(outer.get(p["side"]))
                if mk is None:
                    continue
                qx = int(round((p["x"] - self.win["x0"]) * self.sx))
                qy = int(round((self.win["y0"] + self.win["h"] - p["y"]) * self.sy))
                if 0 <= qx < self.w and 0 <= qy < self.h and \
                        mk[max(qy - r, 0):qy + r + 1, max(qx - r, 0):qx + r + 1].any():
                    touched.append(p["ref"])
            likely = None
            if nets and touched:
                count = {}
                for p in parts:
                    if p["ref"] in touched:
                        for net in set((p.get("nets") or {}).values()):
                            count[net] = count.get(net, 0) + 1
                if count:
                    likely = max(count.items(), key=lambda kv: (kv[1], -len(nets.get(kv[0], {}).get("pins", []))))[0]
            old = sorted(self.outdir.glob("t_*.png"), key=lambda f: f.stat().st_mtime)
            for f in old[:-TRACE_KEEP * len(self.copper)]:
                f.unlink(missing_ok=True)
            return {"id": tid, "seed": {"x": x, "y": y, "layer": start["label"],
                                        "snapped": [round(self.win["x0"] + px / self.sx, 3),
                                                    round(self.win["y0"] + self.win["h"] - py / self.sy, 3)]},
                    "layers": out, "parts": touched, "likely_net": likely}


# ---------------------------------------------------------------------------
# Board registry and preparation
# ---------------------------------------------------------------------------

def load_registry():
    if BOARDS_FILE.exists():
        return json.loads(BOARDS_FILE.read_text())
    return {}


def save_registry(reg):
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    BOARDS_FILE.write_text(json.dumps(reg, indent=2))


def prepare(name, cfg, force=False, log=print):
    outdir = CACHE_DIR / name
    outdir.mkdir(parents=True, exist_ok=True)
    layers = discover_layers(cfg["gerber_dir"], cfg.get("drill_dirs", []))
    win = board_window(layers)
    with ThreadPoolExecutor(max_workers=os.cpu_count() or 4) as ex:
        results = list(ex.map(lambda l: render_layer(l, win, outdir, force), layers))
    kept = []
    for l, (fname, err) in zip(layers, results):
        if err:
            log(f"  skip {Path(l['path']).name}: gerbv failed: {err}")
            continue
        l["file"] = fname
        kept.append(l)
    parts = []
    if cfg.get("pnp"):
        parts = parse_pnp(cfg["pnp"], tuple(cfg.get("offset", (0, 0))), cfg.get("flip_y", False))
    # drop pick-and-place entries far outside the board (e.g. Altium's PCB logo part)
    win_ok = lambda p: (win["x0"] - 5 <= p["x"] <= win["x0"] + win["w"] + 5 and
                        win["y0"] - 5 <= p["y"] <= win["y0"] + win["h"] + 5)
    parts = [p for p in parts if win_ok(p)]
    nets = parse_netlist(cfg["netlist"]) if cfg.get("netlist") else {}
    by_ref = {p["ref"]: p for p in parts}
    for net, n in nets.items():
        for pin in n["pins"]:
            ref, num = _split_pin(pin)
            if ref in by_ref:
                by_ref[ref].setdefault("nets", {})[num] = net
    meta = {"name": name, "title": cfg.get("title", name), "bbox": win,
            "layers": [{k: l[k] for k in ("id", "label", "kind", "side", "inner", "color", "file",
                                           "default_visible")} for l in kept],
            "parts": parts, "nets": nets}
    (outdir / "board.json").write_text(json.dumps(meta))
    return meta


# ---------------------------------------------------------------------------
# HTTP server
# ---------------------------------------------------------------------------

class Hub:
    def __init__(self):
        self.lock = threading.Lock()
        self.clients = []
        self.view = {}
        self.boards = {}
        self.engines = {}

    def broadcast(self, msg):
        data = json.dumps(msg)
        with self.lock:
            for q in list(self.clients):
                q.put(data)
        return len(self.clients)


HUB = Hub()


def resolve_board(name):
    if name and name in HUB.boards:
        return name
    if HUB.view.get("board") in HUB.boards:
        return HUB.view["board"]
    return next(iter(HUB.boards), None)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _json(self, obj, code=200):
        b = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def _file(self, path, ctype, cache=True):
        b = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(b)))
        # layer/trace files are content-addressed; the page itself must never be stale
        self.send_header("Cache-Control", "max-age=3600" if cache else "no-cache")
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        q = dict(urllib.parse.parse_qsl(u.query))
        if u.path in ("/", "/index.html"):
            return self._file(HERE / "viewer.html", "text/html; charset=utf-8", cache=False)
        if u.path == "/api/boards":
            return self._json([{"name": n, "title": b["title"]} for n, b in HUB.boards.items()])
        if u.path == "/api/board":
            b = HUB.boards.get(q.get("name", ""))
            return self._json(b) if b else self._json({"error": "no such board"}, 404)
        if u.path == "/api/find":
            name = resolve_board(q.get("board"))
            if not name:
                return self._json({"error": "no boards"}, 404)
            b = HUB.boards[name]
            query = q.get("q", "")
            parts = [] if query.lower().startswith("net:") else search(b["parts"], query)
            return self._json({"board": name, "matches": parts, "nets": search_nets(b.get("nets", {}), query)})
        if u.path == "/api/status":
            return self._json({"view": HUB.view, "viewers": len(HUB.clients)})
        if u.path == "/api/events":
            return self._events()
        m = re.fullmatch(r"/trace/([\w.-]+)/(t_[0-9a-f]{10}_[0-9a-f]{10}\.png)", u.path)
        if m and m.group(1) in HUB.boards:
            p = CACHE_DIR / m.group(1) / "trace" / m.group(2)
            if p.exists():
                return self._file(p, "image/png")
        m = re.fullmatch(r"/layers/([\w.-]+)/([0-9a-f]{16}\.svg)", u.path)
        if m and m.group(1) in HUB.boards:
            p = CACHE_DIR / m.group(1) / m.group(2)
            if p.exists():
                return self._file(p, "image/svg+xml")
        self._json({"error": "not found"}, 404)

    def do_POST(self):
        n = int(self.headers.get("Content-Length", "0"))
        try:
            body = json.loads(self.rfile.read(n) or b"{}")
        except json.JSONDecodeError:
            return self._json({"error": "bad json"}, 400)
        if self.path == "/api/view":
            HUB.view = body
            return self._json({"ok": True})
        if self.path != "/api/cmd":
            return self._json({"error": "not found"}, 404)
        action = body.get("action")
        name = resolve_board(body.get("board"))
        msg = {"action": action, "board": name}
        reply = {"ok": True, "board": name}
        if action == "focus":
            b, query = HUB.boards[name], body.get("query", "")
            matches, net = [], None
            if query.lower().startswith("net:"):
                hits = search_nets(b.get("nets", {}), query, limit=1)
                if hits:
                    net = hits[0]
                    want = {_split_pin(p)[0] for p in net["pins"]}
                    matches = [p for p in b["parts"] if p["ref"] in want]
            else:
                matches = search(b["parts"], query, limit=int(body.get("limit", 50)))
            if not matches:
                return self._json({"ok": False, "error": f"no match for {body.get('query')!r} on {name}"}, 404)
            msg["refs"] = [p["ref"] for p in matches]
            if net:
                msg["net"] = net["name"]
                reply["net"] = net
            reply["matches"] = matches
        elif action == "trace":
            b = HUB.boards[name]
            x, y, side = body.get("x"), body.get("y"), body.get("side")
            if body.get("ref"):
                p = next((p for p in b["parts"] if p["ref"].lower() == body["ref"].lower()), None)
                if not p:
                    return self._json({"ok": False, "error": f"no part {body['ref']!r}"}, 404)
                x, y, side = p["x"], p["y"], side or p["side"]
            try:
                res = HUB.engines[name].trace(float(x), float(y), side, body.get("layer"),
                                              b["parts"], b.get("nets"))
            except (ValueError, TypeError) as e:
                return self._json({"ok": False, "error": str(e)}, 400)
            for l in res["layers"]:
                l["url"] = f"/trace/{name}/{l['file']}"
            msg["trace"] = res
            reply["trace"] = res
        elif action == "goto":
            msg.update({k: body[k] for k in ("x", "y", "window", "side") if k in body})
        elif action == "mark":
            b, out = HUB.boards[name], []
            palette = ["#39c5ff", "#ff5a5a", "#7ee07e", "#ffd23f", "#c38bff", "#ff9f1a"]
            for i, mk in enumerate(body.get("marks", [])):
                color = mk.get("color") or palette[i % len(palette)]
                if mk.get("ref"):
                    p = next((q for q in b["parts"] if q["ref"].lower() == mk["ref"].lower()), None)
                    if not p:
                        return self._json({"ok": False, "error": f"no part {mk['ref']!r}"}, 404)
                    out.append({"x": p["x"], "y": p["y"], "side": p["side"], "color": color,
                                "text": mk.get("text") or p["ref"]})
                else:
                    out.append({"x": float(mk["x"]), "y": float(mk["y"]), "side": mk.get("side"),
                                "color": color, "text": mk.get("text", "")})
            msg.update({"marks": out, "append": bool(body.get("append")), "fit": body.get("fit", True)})
            reply["marks"] = out
        elif action in ("side", "layer", "clear", "fit", "board", "trace_clear", "reload", "unmark"):
            msg.update({k: v for k, v in body.items() if k not in ("action", "board")})
        else:
            return self._json({"error": f"unknown action {action!r}"}, 400)
        reply["viewers"] = HUB.broadcast(msg)
        self._json(reply)

    def _events(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        q = queue.Queue()
        with HUB.lock:
            HUB.clients.append(q)
        try:
            while True:
                try:
                    data = q.get(timeout=15)
                    self.wfile.write(f"data: {data}\n\n".encode())
                except queue.Empty:
                    self.wfile.write(b": ping\n\n")
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            with HUB.lock:
                HUB.clients.remove(q)


def serve(port, force):
    reg = load_registry()
    if not reg:
        raise SystemExit("no boards registered; use: pcb-viewer add NAME GERBER_DIR --pnp FILE")
    for name, cfg in reg.items():
        print(f"preparing {name} ...", flush=True)
        HUB.boards[name] = prepare(name, cfg, force)
        HUB.engines[name] = TraceEngine(name, cfg)
        threading.Thread(target=HUB.engines[name].warm, daemon=True).start()
        b = HUB.boards[name]
        print(f"  {len(b['layers'])} layers, {len(b['parts'])} parts", flush=True)
    srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    srv.daemon_threads = True
    print(f"pcb-viewer on http://127.0.0.1:{port}/", flush=True)
    srv.serve_forever()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def api(port, path, body=None):
    url = f"http://127.0.0.1:{port}{path}"
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        return json.loads(e.read() or b"{}")
    except urllib.error.URLError:
        raise SystemExit(f"pcb-viewer server not running on port {port} (start: pcb-viewer serve)")


def fmt_part(p):
    return f'{p["ref"]:<10} {p["side"]:<6} x={p["x"]:8.3f} y={p["y"]:8.3f} rot={p["rot"]:6.1f}  {p["val"][:24]:<24} {p["fp"][:28]}'


def main():
    ap = argparse.ArgumentParser(prog="pcb-viewer", description=__doc__.splitlines()[0])
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    sub = ap.add_subparsers(dest="cmd", required=True)

    a = sub.add_parser("add", help="register a board")
    a.add_argument("name")
    a.add_argument("gerber_dir")
    a.add_argument("--pnp", help="pick-and-place file (Altium CSV, KiCad .pos, Eagle .mnt/.mnb, JLC CSV), "
                                 "or an Altium .PcbDoc")
    a.add_argument("--drill-dir", action="append", default=[], help="extra folder with drill files")
    a.add_argument("--netlist", help="IPC-D-356 netlist, or an Altium Smart PDF schematic (nets from its bookmarks)")
    a.add_argument("--title")
    a.add_argument("--offset", default="0,0", help="dx,dy mm added to pick-and-place coordinates")
    a.add_argument("--flip-y", action="store_true", help="negate pick-and-place Y (KiCad)")

    sub.add_parser("list", help="list registered boards")
    r = sub.add_parser("remove", help="unregister a board")
    r.add_argument("name")
    r = sub.add_parser("render", help="(re)render a board's layers")
    r.add_argument("name")
    r.add_argument("--force", action="store_true")
    s = sub.add_parser("serve", help="run the viewer server (foreground)")
    s.add_argument("--force", action="store_true", help="re-render all layers")
    o = sub.add_parser("open", help="open the viewer in the browser")
    o.add_argument("name", nargs="?")

    f = sub.add_parser("find", help="search parts (designator, value, footprint)")
    f.add_argument("query")
    f.add_argument("--board")
    n = sub.add_parser("net", help="list a net's pins (and the parts they belong to)")
    n.add_argument("name")
    n.add_argument("--board")
    f = sub.add_parser("focus", help="zoom the open viewer to matching parts, or net:NAME")
    f.add_argument("query")
    f.add_argument("--board")
    f.add_argument("--limit", type=int, default=50)
    t = sub.add_parser("trace", help="highlight copper connected to x,y (mm) or to a part's copper")
    t.add_argument("x", nargs="?", type=float)
    t.add_argument("y", nargs="?", type=float)
    t.add_argument("--ref", help="start from this part's centroid (exact for test points and single pads)")
    t.add_argument("--side", choices=["top", "bottom"], help="start on this outer layer (default top, or the part's side)")
    t.add_argument("--layer", help="start on the copper layer whose label contains this text, e.g. 'Inner 3'")
    t.add_argument("--board")
    sub.add_parser("trace-clear", help="remove trace highlights")
    mk = sub.add_parser("mark", help='label parts/points: mark TP1="UART TX" TP2="UART RX" 12.5,40="note"')
    mk.add_argument("items", nargs="+", help='REF=TEXT or X,Y=TEXT (mm); TEXT may be omitted for REF')
    mk.add_argument("--append", action="store_true", help="keep existing marks")
    mk.add_argument("--no-fit", action="store_true", help="do not move the view")
    mk.add_argument("--board")
    sub.add_parser("unmark", help="remove all marks")
    g = sub.add_parser("goto", help="centre the open viewer on x,y (mm)")
    g.add_argument("x", type=float)
    g.add_argument("y", type=float)
    g.add_argument("--window", type=float, default=10.0, help="visible width in mm")
    g.add_argument("--side", choices=["top", "bottom"])
    g.add_argument("--board")
    sd = sub.add_parser("side", help="view top or bottom")
    sd.add_argument("side", choices=["top", "bottom"])
    sd.add_argument("--board")
    ly = sub.add_parser("layer", help="show/hide layers whose label contains TEXT")
    ly.add_argument("match")
    ly.add_argument("state", choices=["on", "off"])
    ly.add_argument("--board")
    sub.add_parser("fit", help="fit the board in the viewer")
    sub.add_parser("reload", help="reload the viewer page (after an update)")
    sub.add_parser("clear", help="clear highlights")
    sub.add_parser("status", help="what the viewer is currently showing")

    args = ap.parse_args()
    reg = load_registry()

    if args.cmd == "add":
        if not shutil.which("gerbv"):
            raise SystemExit("gerbv not found on PATH")
        gd = str(Path(args.gerber_dir).resolve())
        drills = [str(Path(d).resolve()) for d in args.drill_dir]
        if not drills:  # Altium/others often put drills in a sibling folder
            for sib in Path(gd).parent.iterdir():
                if sib.is_dir() and sib.resolve() != Path(gd) and re.search(r"drill", sib.name, re.I):
                    drills.append(str(sib.resolve()))
        cfg = {"gerber_dir": gd, "drill_dirs": drills, "title": args.title or args.name,
               "pnp": str(Path(args.pnp).resolve()) if args.pnp else None,
               "netlist": str(Path(args.netlist).resolve()) if args.netlist else None,
               "offset": [float(v) for v in args.offset.split(",")], "flip_y": args.flip_y}
        meta = prepare(args.name, cfg)
        reg[args.name] = cfg
        save_registry(reg)
        print(f"added {args.name}: {len(meta['layers'])} layers, {len(meta['parts'])} parts, {len(meta['nets'])} nets, "
              f"window {meta['bbox']['w']:.1f} x {meta['bbox']['h']:.1f} mm")
        for l in meta["layers"]:
            print(f"  {l['kind']:<8} {str(l['side'] or ''):<7} {'on ' if l['default_visible'] else 'off'} {l['label']}")
        print("restart `pcb-viewer serve` if it is running")
    elif args.cmd == "list":
        for n, c in reg.items():
            print(f"{n}: {c['gerber_dir']}  pnp={c.get('pnp')}")
    elif args.cmd == "remove":
        reg.pop(args.name, None)
        save_registry(reg)
        shutil.rmtree(CACHE_DIR / args.name, ignore_errors=True)
    elif args.cmd == "render":
        prepare(args.name, reg[args.name], args.force)
    elif args.cmd == "serve":
        serve(args.port, args.force)
    elif args.cmd == "open":
        url = f"http://127.0.0.1:{args.port}/" + (f"#{args.name}" if args.name else "")
        webbrowser.open(url)
        print(url)
    elif args.cmd == "find":
        res = api(args.port, "/api/find?" + urllib.parse.urlencode({"q": args.query, "board": args.board or ""}))
        for p in res.get("matches", []):
            print(fmt_part(p))
        for nt in res.get("nets", [])[:20]:
            print(f'NET {nt["name"]:<24} {len(nt["pins"]):4d} pins' + (f'  aka {", ".join(nt["aliases"])}' if nt["aliases"] else ""))
        if not res.get("matches") and not res.get("nets"):
            print(f"no match on {res.get('board')}")
    elif args.cmd == "net":
        res = api(args.port, "/api/find?" + urllib.parse.urlencode({"q": "net:" + args.name, "board": args.board or ""}))
        if not res.get("nets"):
            raise SystemExit(f"no net matching {args.name!r} on {res.get('board')}")
        nt = res["nets"][0]
        print(f'{nt["name"]}: {len(nt["pins"])} pins' + (f'  (aka {", ".join(nt["aliases"])})' if nt["aliases"] else "")
              + (f'  schematic page(s) {", ".join(map(str, nt["pages"]))}' if nt.get("pages") else ""))
        print("  " + "  ".join(nt["pins"]))
    elif args.cmd == "focus":
        res = api(args.port, "/api/cmd", {"action": "focus", "query": args.query, "board": args.board,
                                          "limit": args.limit})
        if not res.get("ok"):
            raise SystemExit(res.get("error"))
        for p in res["matches"]:
            print(fmt_part(p))
        if res.get("net"):
            print(f'net {res["net"]["name"]}: {"  ".join(res["net"]["pins"])}')
        print(f"focused {len(res['matches'])} part(s) on {res['board']} ({res['viewers']} viewer(s))")
    elif args.cmd == "trace":
        if args.ref is None and (args.x is None or args.y is None):
            raise SystemExit("give x y, or --ref PART")
        body = {"action": "trace", "board": args.board, "ref": args.ref, "x": args.x, "y": args.y,
                "side": args.side, "layer": args.layer}
        res = api(args.port, "/api/cmd", body)
        if not res.get("ok"):
            raise SystemExit(res.get("error"))
        tr = res["trace"]
        sd = tr["seed"]
        print(f'trace from ({sd["snapped"][0]}, {sd["snapped"][1]}) on {sd["layer"]}:')
        for l in tr["layers"]:
            print(f'  {l["label"]:<12} {l["area_mm2"]:9.2f} mm2')
        print(f'  parts touched ({len(tr["parts"])}): {" ".join(tr["parts"][:40])}{" ..." if len(tr["parts"]) > 40 else ""}')
        if tr.get("likely_net"):
            print(f'  likely net: {tr["likely_net"]}')
    elif args.cmd == "mark":
        items = []
        for it in args.items:
            key, _, text = it.partition("=")
            mm = re.fullmatch(r"\s*(-?[\d.]+)\s*,\s*(-?[\d.]+)\s*", key)
            items.append({"x": float(mm.group(1)), "y": float(mm.group(2)), "text": text} if mm
                         else {"ref": key, "text": text})
        res = api(args.port, "/api/cmd", {"action": "mark", "marks": items, "append": args.append,
                                          "fit": not args.no_fit, "board": args.board})
        if not res.get("ok"):
            raise SystemExit(res.get("error"))
        for m_ in res["marks"]:
            print(f'  {m_["text"]:<28} {m_.get("side") or "":<6} ({m_["x"]:.3f}, {m_["y"]:.3f})')
    elif args.cmd == "unmark":
        print(api(args.port, "/api/cmd", {"action": "unmark"}))
    elif args.cmd == "trace-clear":
        print(api(args.port, "/api/cmd", {"action": "trace_clear"}))
    elif args.cmd == "goto":
        body = {"action": "goto", "x": args.x, "y": args.y, "window": args.window, "board": args.board}
        if args.side:
            body["side"] = args.side
        print(api(args.port, "/api/cmd", body))
    elif args.cmd == "side":
        print(api(args.port, "/api/cmd", {"action": "side", "side": args.side, "board": args.board}))
    elif args.cmd == "layer":
        print(api(args.port, "/api/cmd", {"action": "layer", "match": args.match, "on": args.state == "on",
                                          "board": args.board}))
    elif args.cmd in ("fit", "clear", "reload"):
        print(api(args.port, "/api/cmd", {"action": args.cmd}))
    elif args.cmd == "status":
        print(json.dumps(api(args.port, "/api/status"), indent=2))


if __name__ == "__main__":
    main()
