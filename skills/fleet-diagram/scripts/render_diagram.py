#!/usr/bin/env python3
"""fleet-diagram renderer (ccc-node #2109 B).

Turns a small JSON spec into a diagram file the bridge can deliver as an
attachment (a real file path named in the answer is sent to Telegram and
Matrix — core/deliverables.py). No external renderer is required anywhere in
the fleet: SVG is always produced with the standard library; PNG is produced
too when Pillow is importable (it is not on every node — the script says so
instead of failing).

Spec kinds (one JSON object; UTF-8):

  {"kind": "matrix", "title": "...", "rows": ["node-a", ...],
   "cols": ["bridge", "worker", ...],
   "cells": [["ok", "warn"], ...],           # one row per rows[], values are
   "legend": {"ok": "green", "warn": "amber", # status keys or free text
              "fail": "red", "n/a": "grey"}}  # (optional; default palette)

  {"kind": "timeline", "title": "...",
   "events": [{"t": "2026-10-02T04:28Z", "label": "dispatch", "lane": "broker",
               "status": "ok"}, ...]}          # t: ISO-8601 or any sortable
                                              # string; lane optional

  {"kind": "dag", "title": "...",
   "nodes": [{"id": "pr1", "label": "PR #1", "status": "ok"}, ...],
   "edges": [["pr1", "pr2"], ...]}            # from -> to (must be acyclic)

Usage:
  render_diagram.py --spec spec.json --out <dir-or-file> [--format svg|png|both]
                    [--name fleet-matrix] [--max-nodes 200]
Prints one JSON line: {"ok": true, "files": [...], "png": bool, "reason": ...}.
Labels are rendered verbatim — never put secrets in a spec.
"""
from __future__ import annotations

import argparse
import html
import json
import os
import sys
from pathlib import Path

PALETTE = {
    "ok": "#2e9e5b",
    "warn": "#d9a400",
    "fail": "#c93b3b",
    "n/a": "#9a9a9a",
    "info": "#3b74c9",
    "pending": "#b48bd9",
}
COLOR_NAMES = {
    "green": PALETTE["ok"],
    "amber": PALETTE["warn"],
    "yellow": PALETTE["warn"],
    "red": PALETTE["fail"],
    "grey": PALETTE["n/a"],
    "gray": PALETTE["n/a"],
    "blue": PALETTE["info"],
    "purple": PALETTE["pending"],
}
DEFAULT_COLOR = "#5b6b7a"
BG = "#ffffff"
FG = "#1f2933"
GRID = "#d5dbe1"
FONT_FAMILY = "Noto Sans CJK KR, NanumGothic, DejaVu Sans, sans-serif"
MAX_LABEL = 48


class SpecError(ValueError):
    pass


# --------------------------------------------------------------------------- spec

def _text(value, fallback=""):
    if value is None:
        return fallback
    s = str(value).strip()
    return s if s else fallback


def _label(value):
    s = _text(value)
    return s if len(s) <= MAX_LABEL else s[: MAX_LABEL - 1] + "…"


def _color_for(status, legend):
    key = _text(status).lower()
    if legend and key in legend:
        c = _text(legend[key]).lower()
        if c.startswith("#"):
            return c
        return COLOR_NAMES.get(c) or PALETTE.get(c) or PALETTE.get(key, DEFAULT_COLOR)
    return PALETTE.get(key) or COLOR_NAMES.get(key, DEFAULT_COLOR)


def load_spec(path: Path, max_nodes: int) -> dict:
    try:
        spec = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise SpecError(f"spec unreadable or not JSON: {exc.__class__.__name__}") from exc
    if not isinstance(spec, dict):
        raise SpecError("spec must be a JSON object")
    kind = _text(spec.get("kind")).lower()
    if kind not in ("matrix", "timeline", "dag"):
        raise SpecError("kind must be one of matrix, timeline, dag")
    spec["kind"] = kind
    spec["title"] = _label(spec.get("title"))
    legend = spec.get("legend")
    spec["legend"] = {str(k).lower(): str(v) for k, v in legend.items()} if isinstance(legend, dict) else {}
    if kind == "matrix":
        rows = spec.get("rows")
        cols = spec.get("cols")
        cells = spec.get("cells")
        if not isinstance(rows, list) or not rows or not isinstance(cols, list) or not cols:
            raise SpecError("matrix needs non-empty rows and cols")
        if len(rows) * len(cols) > max_nodes * 4:
            raise SpecError("matrix too large")
        if not isinstance(cells, list) or len(cells) != len(rows) or any(
            not isinstance(r, list) or len(r) != len(cols) for r in cells
        ):
            raise SpecError("cells must be rows x cols")
        spec["rows"] = [_label(r) for r in rows]
        spec["cols"] = [_label(c) for c in cols]
        spec["cells"] = [[_label(c) for c in r] for r in cells]
    elif kind == "timeline":
        events = spec.get("events")
        if not isinstance(events, list) or not events:
            raise SpecError("timeline needs non-empty events")
        if len(events) > max_nodes:
            raise SpecError("too many events")
        norm = []
        for ev in events:
            if not isinstance(ev, dict) or not _text(ev.get("t")):
                raise SpecError("each event needs t")
            norm.append({
                "t": _label(ev.get("t")),
                "label": _label(ev.get("label")),
                "lane": _label(ev.get("lane")) or "events",
                "status": _text(ev.get("status")).lower(),
            })
        spec["events"] = sorted(norm, key=lambda e: e["t"])
    else:
        nodes = spec.get("nodes")
        edges = spec.get("edges") or []
        if not isinstance(nodes, list) or not nodes:
            raise SpecError("dag needs non-empty nodes")
        if len(nodes) > max_nodes:
            raise SpecError("too many nodes")
        ids = []
        norm_nodes = {}
        for n in nodes:
            if not isinstance(n, dict) or not _text(n.get("id")):
                raise SpecError("each node needs id")
            nid = _text(n.get("id"))
            if nid in norm_nodes:
                raise SpecError(f"duplicate node id {nid}")
            norm_nodes[nid] = {"id": nid, "label": _label(n.get("label")) or _label(nid), "status": _text(n.get("status")).lower()}
            ids.append(nid)
        norm_edges = []
        for e in edges:
            if not isinstance(e, (list, tuple)) or len(e) != 2:
                raise SpecError("each edge is [from, to]")
            a, b = _text(e[0]), _text(e[1])
            if a not in norm_nodes or b not in norm_nodes:
                raise SpecError(f"edge references unknown node: {a}->{b}")
            if a == b:
                raise SpecError("self edges are not allowed")
            norm_edges.append((a, b))
        spec["nodes"] = [norm_nodes[i] for i in ids]
        spec["edges"] = norm_edges
        spec["layers"] = _layers(ids, norm_edges)
    return spec


def _layers(ids, edges):
    """Longest-path layering; raises SpecError on a cycle."""
    preds = {i: set() for i in ids}
    succs = {i: set() for i in ids}
    for a, b in edges:
        preds[b].add(a)
        succs[a].add(b)
    layer = {}
    ready = [i for i in ids if not preds[i]]
    remaining = {i: len(preds[i]) for i in ids}
    order = []
    while ready:
        n = ready.pop(0)
        order.append(n)
        layer[n] = max((layer[p] for p in preds[n]), default=-1) + 1
        for s in sorted(succs[n]):
            remaining[s] -= 1
            if remaining[s] == 0:
                ready.append(s)
    if len(order) != len(ids):
        raise SpecError("dag has a cycle")
    out = {}
    for n in ids:
        out.setdefault(layer[n], []).append(n)
    return [out[k] for k in sorted(out)]


# --------------------------------------------------------------------------- scene

def build_scene(spec: dict) -> dict:
    """Backend-neutral drawing list: rects, lines, texts; all coordinates in px."""
    items = []
    legend = spec["legend"]
    title_h = 36 if spec["title"] else 12
    pad = 16

    def rect(x, y, w, h, fill, stroke=GRID, rx=4):
        items.append({"op": "rect", "x": x, "y": y, "w": w, "h": h, "fill": fill, "stroke": stroke, "rx": rx})

    def text(x, y, s, size=13, anchor="start", color=FG, bold=False):
        items.append({"op": "text", "x": x, "y": y, "s": s, "size": size, "anchor": anchor, "color": color, "bold": bold})

    def line(x1, y1, x2, y2, color=GRID, width=1.5, arrow=False):
        items.append({"op": "line", "x1": x1, "y1": y1, "x2": x2, "y2": y2, "color": color, "width": width, "arrow": arrow})

    if spec["kind"] == "matrix":
        rows, cols, cells = spec["rows"], spec["cols"], spec["cells"]
        label_w = min(260, 16 + 8 * max(len(r) for r in rows))
        cw = max(72, min(150, 20 + 7 * max(len(c) for c in cols)))
        rh = 30
        w = pad + label_w + cw * len(cols) + pad
        h = title_h + 32 + rh * len(rows) + pad
        for j, c in enumerate(cols):
            text(pad + label_w + j * cw + cw / 2, title_h + 20, c, 12, "middle", bold=True)
        for i, r in enumerate(rows):
            y = title_h + 32 + i * rh
            text(pad, y + rh / 2 + 4, r, 12, "start", bold=True)
            for j, v in enumerate(cells[i]):
                x = pad + label_w + j * cw
                rect(x + 2, y + 2, cw - 4, rh - 4, _color_for(v, legend))
                text(x + cw / 2, y + rh / 2 + 4, v, 11, "middle", "#ffffff")
    elif spec["kind"] == "timeline":
        events = spec["events"]
        lanes = []
        for e in events:
            if e["lane"] not in lanes:
                lanes.append(e["lane"])
        lane_w = min(200, 16 + 8 * max(len(l) for l in lanes))
        step = 28
        w = pad + lane_w + 20 + max(360, 9 * max(len(e["t"]) + len(e["label"]) + 2 for e in events)) + pad
        h = title_h + 16 + step * len(events) + pad
        axis_x = pad + lane_w + 10
        for k, e in enumerate(events):
            y = title_h + 16 + k * step
            rect(axis_x - 6, y + 6, 12, 12, _color_for(e["status"], legend), rx=6)
            text(pad, y + 16, e["lane"], 11, "start", "#5b6b7a")
            text(axis_x + 14, y + 16, f"{e['t']}  {e['label']}", 12, "start")
            if k + 1 < len(events):
                line(axis_x, y + 18, axis_x, y + step + 6)
    else:
        layers = spec["layers"]
        nodes = {n["id"]: n for n in spec["nodes"]}
        bw, bh, hgap, vgap = 150, 40, 36, 60
        maxw = max(len(l) for l in layers)
        w = pad + maxw * (bw + hgap) - hgap + pad
        h = title_h + len(layers) * (bh + vgap) - vgap + pad * 2
        pos = {}
        for li, layer in enumerate(layers):
            row_w = len(layer) * (bw + hgap) - hgap
            x0 = (w - row_w) / 2
            for ni, nid in enumerate(layer):
                x = x0 + ni * (bw + hgap)
                y = title_h + pad + li * (bh + vgap)
                pos[nid] = (x, y)
        for a, b in spec["edges"]:
            ax, ay = pos[a]
            bx, by = pos[b]
            line(ax + bw / 2, ay + bh, bx + bw / 2, by, "#7a8794", 1.5, arrow=True)
        for nid, (x, y) in pos.items():
            n = nodes[nid]
            rect(x, y, bw, bh, "#f4f6f8", _color_for(n["status"], legend) if n["status"] else "#7a8794", rx=8)
            text(x + bw / 2, y + bh / 2 + 4, n["label"], 12, "middle")
    if spec["title"]:
        items.insert(0, {"op": "text", "x": pad, "y": 24, "s": spec["title"], "size": 16, "anchor": "start", "color": FG, "bold": True})
    legend_keys = [k for k in legend] if legend else []
    if legend_keys:
        lx = pad
        ly = h - pad + 4
        h += 24
        for k in legend_keys:
            rect(lx, ly, 12, 12, _color_for(k, legend), rx=3)
            text(lx + 16, ly + 11, k, 11)
            lx += 28 + 7 * len(k)
    return {"w": int(w), "h": int(h), "items": items}


# --------------------------------------------------------------------------- backends

def to_svg(scene: dict) -> str:
    out = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{scene["w"]}" height="{scene["h"]}" viewBox="0 0 {scene["w"]} {scene["h"]}">',
           '<defs><marker id="arrow" markerWidth="8" markerHeight="8" refX="7" refY="4" orient="auto">'
           '<path d="M0,0 L8,4 L0,8 z" fill="#7a8794"/></marker></defs>',
           f'<rect width="100%" height="100%" fill="{BG}"/>']
    for it in scene["items"]:
        if it["op"] == "rect":
            out.append(f'<rect x="{it["x"]:.1f}" y="{it["y"]:.1f}" width="{it["w"]:.1f}" height="{it["h"]:.1f}" rx="{it["rx"]}" fill="{it["fill"]}" stroke="{it["stroke"]}" stroke-width="1.2"/>')
        elif it["op"] == "line":
            marker = ' marker-end="url(#arrow)"' if it["arrow"] else ""
            out.append(f'<line x1="{it["x1"]:.1f}" y1="{it["y1"]:.1f}" x2="{it["x2"]:.1f}" y2="{it["y2"]:.1f}" stroke="{it["color"]}" stroke-width="{it["width"]}"{marker}/>')
        else:
            weight = ' font-weight="bold"' if it["bold"] else ""
            out.append(f'<text x="{it["x"]:.1f}" y="{it["y"]:.1f}" font-family="{FONT_FAMILY}" font-size="{it["size"]}" text-anchor="{it["anchor"]}" fill="{it["color"]}"{weight}>{html.escape(it["s"])}</text>')
    out.append("</svg>")
    return "\n".join(out) + "\n"


_FONT_CANDIDATES = [
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJKkr-Regular.otf",
    "/usr/share/fonts/opentype/noto/NotoSerifCJK-Regular.ttc",
    "/usr/share/fonts/opentype/noto/NotoSerifCJK-Bold.ttc",
    "/usr/share/fonts/truetype/nanum/NanumGothic.ttf",
    "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/opentype/unifont/unifont_jp.otf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
]


def _font(size: int, bold: bool):
    from PIL import ImageFont  # noqa: WPS433 (optional dependency)

    env = os.environ.get("FLEET_DIAGRAM_FONT")
    for cand in ([env] if env else []) + _FONT_CANDIDATES:
        if cand and os.path.isfile(cand):
            try:
                return ImageFont.truetype(cand, size=size, index=0)
            except OSError:
                continue
    return ImageFont.load_default()


def to_png(scene: dict, path: Path) -> bool:
    try:
        from PIL import Image, ImageDraw
    except ImportError:
        return False
    scale = 2
    img = Image.new("RGB", (scene["w"] * scale, scene["h"] * scale), BG)
    d = ImageDraw.Draw(img)
    fonts = {}
    for it in scene["items"]:
        if it["op"] == "rect":
            d.rounded_rectangle(
                [it["x"] * scale, it["y"] * scale, (it["x"] + it["w"]) * scale, (it["y"] + it["h"]) * scale],
                radius=it["rx"] * scale, fill=it["fill"], outline=it["stroke"], width=max(1, int(1.2 * scale)),
            )
        elif it["op"] == "line":
            d.line([it["x1"] * scale, it["y1"] * scale, it["x2"] * scale, it["y2"] * scale], fill=it["color"], width=max(1, int(it["width"] * scale)))
            if it["arrow"]:
                x2, y2 = it["x2"] * scale, it["y2"] * scale
                d.polygon([(x2, y2), (x2 - 5 * scale, y2 - 8 * scale), (x2 + 5 * scale, y2 - 8 * scale)], fill=it["color"])
        else:
            key = (it["size"], it["bold"])
            if key not in fonts:
                fonts[key] = _font(int(it["size"] * scale), it["bold"])
            f = fonts[key]
            tw = d.textlength(it["s"], font=f)
            x = it["x"] * scale
            if it["anchor"] == "middle":
                x -= tw / 2
            elif it["anchor"] == "end":
                x -= tw
            d.text((x, it["y"] * scale - it["size"] * scale), it["s"], font=f, fill=it["color"])
    img = img.resize((scene["w"], scene["h"]), Image.LANCZOS)
    img.save(path, format="PNG", optimize=True)
    return True


# --------------------------------------------------------------------------- main

def _resolve_out(out: str, name: str) -> tuple[Path, str]:
    p = Path(out).expanduser()
    if p.suffix.lower() in (".png", ".svg"):
        return p.parent.resolve(), p.stem
    return p.resolve(), name


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--spec", required=True)
    ap.add_argument("--out", required=True, help="output directory, or a file path ending in .png/.svg")
    ap.add_argument("--format", choices=("svg", "png", "both"), default="both")
    ap.add_argument("--name", default="fleet-diagram")
    ap.add_argument("--max-nodes", type=int, default=200)
    args = ap.parse_args(argv)

    try:
        spec = load_spec(Path(args.spec), max(1, args.max_nodes))
    except SpecError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        return 2
    scene = build_scene(spec)
    out_dir, name = _resolve_out(args.out, args.name)
    safe = "".join(ch if ch.isalnum() or ch in "-_." else "-" for ch in name).strip("-.") or "fleet-diagram"
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        print(json.dumps({"ok": False, "error": f"cannot create output dir: {exc.__class__.__name__}"}))
        return 3
    files = []
    png_ok = False
    reason = ""
    if args.format in ("svg", "both"):
        svg_path = out_dir / f"{safe}.svg"
        svg_path.write_text(to_svg(scene), encoding="utf-8")
        files.append(str(svg_path))
    if args.format in ("png", "both"):
        png_path = out_dir / f"{safe}.png"
        png_ok = to_png(scene, png_path)
        if png_ok:
            files.insert(0, str(png_path))
        else:
            reason = "Pillow not importable on this node; SVG only"
            if args.format == "png":
                print(json.dumps({"ok": False, "error": reason, "files": files}, ensure_ascii=False))
                return 4
    print(json.dumps({"ok": True, "kind": spec["kind"], "files": files, "png": png_ok, "size": [scene["w"], scene["h"]], "reason": reason}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
