"""かわいい記号を描く。中心線を角丸の線で太らせる方式で、白抜き(☆♡○)と塗り(★♥●)の外形を揃える。

draw_symbol(cp, cx, cy, r, t) -> pathops.Path
  cx, cy: 記号の中心、r: 外形の半径の目安、t: 線の太さ
"""

import contextlib
import math

import pathops

STROKE = dict(cap=pathops.LineCap.ROUND_CAP, join=pathops.LineJoin.ROUND_JOIN, miter_limit=4)


# ---- 基本の道具 ----

def polyline(points, closed=False):
    path = pathops.Path()
    path.moveTo(*points[0])
    for p in points[1:]:
        path.lineTo(*p)
    if closed:
        path.close()
    return path


def stroked(path, t):
    out = pathops.Path(path)
    out.stroke(t, **STROKE)
    out.convertConicsToQuads()  # 角丸の線端は conic で出てくるので TrueType の二次曲線にする
    # 太い線で折り返すと輪郭が自己交差するので整理する。整理できない形でも、後の op で合成できることが多い
    with contextlib.suppress(pathops.PathOpsError):
        out.simplify()
    return out


def union(*paths):
    out = pathops.Path()
    for p in paths:
        out = pathops.op(out, p, pathops.PathOp.UNION)
    return out


def outline(centerline, t):
    """白抜き: 中心線だけを太らせる"""
    return stroked(centerline, t)


def filled(centerline, t):
    """塗り: 中を塗り、さらに中心線を太らせて角を丸める"""
    return union(pathops.op(centerline, centerline, pathops.PathOp.UNION), stroked(centerline, t))


def circle_pts(cx, cy, r, n=72, a0=0.0, a1=2 * math.pi):
    return [(cx + r * math.cos(a0 + (a1 - a0) * i / n), cy + r * math.sin(a0 + (a1 - a0) * i / n))
            for i in range(n + 1)]


def circle(cx, cy, r):
    return polyline(circle_pts(cx, cy, r)[:-1], closed=True)


def heart_pts(cx, cy, r, flip=False, n=96):
    # 古典的なハートの媒介変数表示を、幅 2r に正規化
    pts = []
    for i in range(n):
        a = 2 * math.pi * i / n
        x = 16 * math.sin(a) ** 3
        y = 13 * math.cos(a) - 5 * math.cos(2 * a) - 2 * math.cos(3 * a) - math.cos(4 * a)
        pts.append((x, y))
    # 実寸の範囲 x:-16..16, y:-17..12 を中心に寄せる
    s = r / 16
    return [(cx + x * s, cy + ((-y if flip else y) + (2.5 if not flip else -2.5)) * s) for x, y in pts]


def star_pts(cx, cy, r, inner=0.45, points=5):
    pts = []
    for i in range(points * 2):
        a = math.pi / 2 + i * math.pi / points
        rr = r if i % 2 == 0 else r * inner
        pts.append((cx + rr * math.cos(a), cy + rr * math.sin(a)))
    return pts


def arrow_head(tip, direction, size):
    """開いた矢じり(2本の線)"""
    dx, dy = direction
    n = math.hypot(dx, dy)
    dx, dy = dx / n, dy / n
    pts = []
    for side in (1, -1):
        a = math.atan2(dy, dx) + math.pi + side * math.radians(42)
        pts.append((tip[0] + size * math.cos(a), tip[1] + size * math.sin(a)))
    return polyline([pts[0], tip, pts[1]])


# ---- 記号の定義 ----

def _suit(kind, cx, cy, r, t, fill):
    k = r - t / 2
    if kind == "heart":
        shape = polyline(heart_pts(cx, cy, k), closed=True)
    elif kind == "diamond":
        shape = polyline([(cx, cy + k), (cx + k * 0.72, cy), (cx, cy - k), (cx - k * 0.72, cy)], closed=True)
    else:
        if kind == "spade":
            top = polyline(heart_pts(cx, cy + k * 0.12, k * 0.86, flip=True), closed=True)
        else:  # club
            cr = k * 0.38
            centers = [(cx, cy + k * 0.52), (cx - k * 0.5, cy - k * 0.06), (cx + k * 0.5, cy - k * 0.06)]
            # 3つの円の間を三角形で埋めて、穴の無い1つの塊にする(白抜き版で内側に線が出ないように)
            top = union(*(circle(x, y, cr) for x, y in centers), polyline(centers, closed=True))
        stem = polyline([(cx - k * 0.08, cy), (cx + k * 0.08, cy), (cx + k * 0.32, cy - k),
                         (cx - k * 0.32, cy - k)], closed=True)
        shape = union(top, stem)
    return filled(shape, t) if fill else outline(shape, t)


def _note(kind, cx, cy, r, t):
    k = r - t / 2
    hr = k * 0.3  # 符頭の大きさ

    def head(x, y):
        pts = []
        for i in range(48):
            a = 2 * math.pi * i / 48
            px, py = hr * 1.25 * math.cos(a), hr * 0.85 * math.sin(a)
            rot = math.radians(22)
            pts.append((x + px * math.cos(rot) - py * math.sin(rot), y + px * math.sin(rot) + py * math.cos(rot)))
        return filled(polyline(pts, closed=True), t * 0.5)

    def stem(x, y0, y1):
        return stroked(polyline([(x, y0), (x, y1)]), t)

    if kind in ("quarter", "eighth"):
        x, y = cx - k * 0.2, cy - k * 0.68
        sx = x + hr * 1.05
        parts = [head(x, y), stem(sx, y + hr * 0.2, cy + k)]
        if kind == "eighth":
            flag = polyline([(sx, cy + k), (sx + k * 0.35, cy + k * 0.55), (sx + k * 0.42, cy + k * 0.15)])
            parts.append(stroked(flag, t))
        return union(*parts)
    # 連桁の2音符(♫ は1本、♬ は2本)
    xl, xr = cx - k * 0.62, cx + k * 0.38
    yl, yr = cy - k * 0.62, cy - k * 0.78
    sl, sr = xl + hr * 1.05, xr + hr * 1.05
    tl, tr = cy + k * 0.95, cy + k * 0.8
    parts = [head(xl, yl), head(xr, yr), stem(sl, yl + hr * 0.2, tl), stem(sr, yr + hr * 0.2, tr),
             stroked(polyline([(sl, tl), (sr, tr)]), t * 1.6)]
    if kind == "sixteenth":
        parts.append(stroked(polyline([(sl, tl - t * 2.2), (sr, tr - t * 2.2)]), t * 1.4))
    return union(*parts)


def draw_symbol(cp, cx, cy, r, t):
    k = r - t / 2  # 中心線の半径
    c = chr(cp)
    if c in "○●◯":
        rr = k * (1.12 if c == "◯" else 1.0)
        shape = circle(cx, cy, rr)
        return filled(shape, t) if c == "●" else outline(shape, t)
    if c == "◎":
        return union(outline(circle(cx, cy, k), t), outline(circle(cx, cy, k * 0.5), t))
    if c in "□■⬜⬛":
        s = k * (0.86 if c in "□■" else 0.98)
        shape = polyline([(cx - s, cy - s), (cx - s, cy + s), (cx + s, cy + s), (cx + s, cy - s)], closed=True)
        return filled(shape, t) if c in "■⬛" else outline(shape, t)
    if c in "◇◆":
        shape = polyline([(cx, cy + k * 1.05), (cx + k * 0.88, cy), (cx, cy - k * 1.05), (cx - k * 0.88, cy)], closed=True)
        return filled(shape, t) if c == "◆" else outline(shape, t)
    if c in "△▲▽▼▷▶◁◀":
        # 重心が中心に来るよう、頂点側を長めに取る
        base = [(0, 1.0), (0.98, -0.72), (-0.98, -0.72)]
        rot = {"△": 0, "▲": 0, "▽": 180, "▼": 180, "▷": -90, "▶": -90, "◁": 90, "◀": 90}[c]
        a = math.radians(rot)
        pts = [(cx + k * (x * math.cos(a) - y * math.sin(a)), cy + k * (x * math.sin(a) + y * math.cos(a)) + (0 if rot % 180 else -k * 0.06))
               for x, y in base]
        shape = polyline(pts, closed=True)
        return filled(shape, t) if c in "▲▼▶◀" else outline(shape, t)
    if c in "☆★":
        shape = polyline(star_pts(cx, cy - k * 0.04, k * 1.06), closed=True)
        return filled(shape, t) if c == "★" else outline(shape, t)
    if c in "♡♥":
        return _suit("heart", cx, cy, r, t, c == "♥")
    if c in "♤♠":
        return _suit("spade", cx, cy, r, t, c == "♠")
    if c in "♧♣":
        return _suit("club", cx, cy, r, t, c == "♣")
    if c in "♢♦":
        return _suit("diamond", cx, cy, r, t, c == "♦")
    if c in "♩♪♫♬":
        return _note({"♩": "quarter", "♪": "eighth", "♫": "beamed", "♬": "sixteenth"}[c], cx, cy, r, t)
    if c == "※":
        d = k * 0.42
        lines = union(stroked(polyline([(cx - d, cy - d), (cx + d, cy + d)]), t),
                      stroked(polyline([(cx - d, cy + d), (cx + d, cy - d)]), t))
        dot = t * 0.85
        dots = union(*(filled(circle(cx + x, cy + y, dot), 1) for x, y in ((0, k * 0.9), (0, -k * 0.9), (k * 0.9, 0), (-k * 0.9, 0))))
        return union(lines, dots)
    if c in "♂♀":
        cr = k * 0.52
        if c == "♂":
            ox, oy = cx - k * 0.28, cy - k * 0.28
            tip = (cx + k * 0.82, cy + k * 0.82)
            d = (tip[0] - ox, tip[1] - oy)
            n = math.hypot(*d)
            start = (ox + d[0] / n * cr, oy + d[1] / n * cr)
            return union(outline(circle(ox, oy, cr), t), stroked(polyline([start, tip]), t),
                         stroked(arrow_head(tip, d, k * 0.42), t))
        oy = cy + k * 0.35
        return union(outline(circle(cx, oy, cr), t), stroked(polyline([(cx, oy - cr), (cx, cy - k)]), t),
                     stroked(polyline([(cx - k * 0.32, cy - k * 0.62), (cx + k * 0.32, cy - k * 0.62)]), t))
    if c == "⌘":
        s, b = k * 0.3, k * 0.38
        parts = [stroked(polyline([(x * s, -(s + b)), (x * s, s + b)]), t) for x in (-1, 1)]
        parts += [stroked(polyline([(-(s + b), y * s), (s + b, y * s)]), t) for y in (-1, 1)]
        for sx in (-1, 1):
            for sy in (-1, 1):
                ccx, ccy = sx * (s + b), sy * (s + b)
                # 内側の 1/4 を除いた 3/4 周
                inner = math.atan2(-sy, -sx)
                pts = circle_pts(ccx, ccy, b, 54, inner + math.pi / 4 + 0.0001, inner + 2 * math.pi - math.pi / 4)
                parts.append(stroked(polyline(pts), t))
        shape = union(*parts)
        return _translate(shape, cx, cy)
    if c in "⏎↵":
        x1, y1, y0, x0 = cx + k * 0.7, cy + k * 0.75, cy - k * 0.25, cx - k * 0.75
        return union(stroked(polyline([(x1, y1), (x1, y0), (x0, y0)]), t),
                     stroked(arrow_head((x0, y0), (-1, 0), k * 0.45), t))
    if c == "⎋":
        gap0, gap1 = math.radians(100), math.radians(170)
        arc = circle_pts(cx, cy, k, 60, gap1, gap0 + 2 * math.pi)
        tip = (cx - k * 0.72, cy + k * 0.72)
        return union(stroked(polyline(arc), t), stroked(polyline([(cx + k * 0.1, cy - k * 0.1), tip]), t),
                     stroked(arrow_head(tip, (-1, 1), k * 0.42), t))
    if c == "⏻":
        arc = circle_pts(cx, cy - k * 0.08, k * 0.9, 60, math.radians(125) - 2 * math.pi, math.radians(55))
        return union(stroked(polyline(arc), t), stroked(polyline([(cx, cy + k), (cx, cy + k * 0.05)]), t))
    if c in TERMINAL:
        return _terminal(c, cx, cy, k, t)
    raise KeyError(c)


def teardrop(cx, cy, angle, length, w):
    """中心で細く、先が丸いしずく形の放射線"""
    tx, ty = cx + (length - w) * math.cos(angle), cy + (length - w) * math.sin(angle)
    px, py = -math.sin(angle) * w, math.cos(angle) * w
    return union(circle(tx, ty, w), polyline([(cx, cy), (tx + px, ty + py), (tx - px, ty - py)], closed=True))


def _terminal(c, cx, cy, k, t):
    if c in "◐◑◓◒":
        start = {"◐": math.pi / 2, "◑": -math.pi / 2, "◓": 0, "◒": math.pi}[c]
        half = polyline(circle_pts(cx, cy, k, 36, start, start + math.pi), closed=True)
        return union(outline(circle(cx, cy, k), t), filled(half, t))
    if c in "◴◷◶◵":
        # 白丸に、指定の 1/4 を区切る2本の半径を引く
        qx, qy = {"◴": (-1, 1), "◷": (1, 1), "◶": (1, -1), "◵": (-1, -1)}[c]
        # 端が円の線とちょうど重なると合成が乱れるので、2本に分けて円の内側で止める
        e = k - t * 0.25
        radii = union(stroked(polyline([(cx + qx * e, cy), (cx, cy)]), t), stroked(polyline([(cx, cy), (cx, cy + qy * e)]), t))
        return union(outline(circle(cx, cy, k), t), radii)
    if c in "✢✻✽":
        n, w = {"✢": (4, 0.3), "✻": (6, 0.24), "✽": (8, 0.24)}[c]
        spokes = union(*(teardrop(cx, cy, math.pi / 2 + 2 * math.pi * i / n, k * 1.02, k * w) for i in range(n)))
        return filled(spokes, t * 0.4)
    if c == "✳":
        return union(*(stroked(polyline([(cx, cy), (cx + k * 0.95 * math.cos(a), cy + k * 0.95 * math.sin(a))]), t)
                       for a in (math.pi / 2 + i * math.pi / 4 for i in range(8))))
    if c == "✶":
        return filled(polyline(star_pts(cx, cy, k * 1.02, inner=0.5, points=6), closed=True), t)
    if c == "⏺":
        return filled(circle(cx, cy, k * 0.78), t)
    if c == "ℹ":
        dot = filled(circle(cx, cy + k * 0.76, t * 0.62), 1)
        stem = stroked(polyline([(cx - k * 0.22, cy + k * 0.12), (cx, cy + k * 0.12), (cx, cy - k * 0.82)]), t * 0.95)
        return union(dot, stem)
    raise KeyError(c)


def _translate(path, dx, dy):
    out = pathops.Path()
    pen = out.getPen()

    class Shift:
        def __getattr__(self, name):
            fn = getattr(pen, name)
            return lambda *pts: fn(*[(p[0] + dx, p[1] + dy) if p else p for p in pts])

    path.draw(Shift())
    return out


# ターミナルのスピナー・状態表示用。コマごとに幅が揃わないとちらつくので、どの版でも半角
TERMINAL = "◐◓◑◒◴◷◶◵✢✳✶✻✽⏺ℹ"
SYMBOLS = "○●◯◎□■⬜⬛◇◆△▲▽▼▷▶◁◀☆★♡♥♤♠♧♣♢♦♩♪♫♬※♂♀⌘⏎↵⎋⏻" + TERMINAL
ALWAYS_FULL = "⬜⬛"  # East Asian Width が W のもの
TECH = "⌘⏎↵⎋⏻" + TERMINAL  # キー表記・ターミナル用。JPDOC 版でも半角
