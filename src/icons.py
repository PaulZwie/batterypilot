#!/usr/bin/env python3
"""Menu bar icons, drawn without an imaging library. Black on transparent, so macOS tints them for light and dark bars.
States are battery (on battery), bolt (charging), plug (plugged in, battery resting) and alert. Edit the shapes below to restyle."""
import os, struct, zlib

W, H, S = 68, 36, 3  # pixels at 2x density, supersampling factor
CACHE = os.path.expanduser("~/.batterypilot/icons")


def rrect(x0, y0, x1, y1, r):
    r = min(r, (x1 - x0) / 2, (y1 - y0) / 2)

    def f(x, y):
        if not (x0 <= x <= x1 and y0 <= y <= y1):
            return False
        cx, cy = min(max(x, x0 + r), x1 - r), min(max(y, y0 + r), y1 - r)
        return (x - cx) ** 2 + (y - cy) ** 2 <= r * r
    return f


def poly(pts):
    def f(x, y):
        inside, j = False, len(pts) - 1
        for i, (xi, yi) in enumerate(pts):
            xj, yj = pts[j]
            if (yi > y) != (yj > y) and x < (xj - xi) * (y - yi) / (yj - yi) + xi:
                inside = not inside
            j = i
        return inside
    return f


def grid(*fns):
    return [[any(f((i + .5) / S, (j + .5) / S) for f in fns) for i in range(W * S)] for j in range(H * S)]


def dilate(g, r):
    def run(rows):
        out = []
        for row in rows:
            ps, n = [0], len(row)
            for v in row:
                ps.append(ps[-1] + v)
            out.append([ps[min(n, i + r + 1)] - ps[max(0, i - r)] > 0 for i in range(n)])
        return out
    t = [list(c) for c in zip(*run(g))]
    return [list(c) for c in zip(*run(t))]


GLYPHS = {
    "bolt": [poly([(36, 8), (26, 19), (32, 19), (29, 28), (42, 16), (36, 16)])],
    "plug": [rrect(25, 14, 39, 23, 3), rrect(27, 8, 30, 14, 1), rrect(34, 8, 37, 14, 1), rrect(31, 23, 33, 28, 1)],
    "alert": [rrect(31, 8, 35, 21, 1.5), rrect(30.5, 23, 35.5, 28, 2.5)],
}


def render(state, pct):
    ring, inner = grid(rrect(3, 4, 61, 32, 7)), grid(rrect(6, 7, 58, 29, 4.5))
    nub = grid(rrect(62, 12, 66, 24, 2))
    fill = grid(rrect(8, 9, 8 + 48 * pct / 100, 27, 2)) if pct > 0 else None
    g = grid(*GLYPHS[state]) if state in GLYPHS else None
    halo = dilate(g, 2 * S) if g else None  # a clear gap around the glyph keeps it readable on the fill
    rows = []
    for py in range(H):
        row = []
        for px in range(W):
            n = 0
            for j in range(py * S, py * S + S):
                for i in range(px * S, px * S + S):
                    b = (ring[j][i] and not inner[j][i]) or nub[j][i] or (fill is not None and fill[j][i])
                    if g:
                        b = (b and not halo[j][i]) or g[j][i]
                    n += b
            row.append(round(255 * n / (S * S)))
        rows.append(row)
    return rows


def write_png(path, rows):
    raw = b"".join(b"\x00" + b"".join(bytes((0, a)) for a in row) for row in rows)

    def chunk(t, d):
        return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xFFFFFFFF)
    with open(path, "wb") as f:
        f.write(b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", W, H, 8, 4, 0, 0, 0))
                + chunk(b"pHYs", struct.pack(">IIB", 5669, 5669, 1))  # 144 dpi, so the icon is 34 by 18 points
                + chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b""))


def icon(state, pct, cache=CACHE):
    """Returns the path of the icon, drawing and caching it on first use. Levels snap to steps of 5%."""
    pct = max(0, min(100, int(round(pct / 5.0)) * 5))
    path = f"{cache}/{state}-{pct}.png"
    if not os.path.exists(path):
        os.makedirs(cache, exist_ok=True)
        write_png(path, render(state, pct))
    return path
