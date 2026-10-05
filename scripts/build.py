"""Maple Mono と Kiwi Maru を合成して、等幅の日英フォント一式を作る。

usage: uv run python scripts/build.py [--only Regular,Italic] [--variants plain,nf,jpdoc] [-j 6]

出力:
  build/fonts/mono/<Family>-<Style>.ttf          英字ヒンティング済み(ttfautohint)
  build/fonts/mono-nf/<Family>NF-<Style>.ttf     Nerd Fonts 入り
  build/fonts/mono-jpdoc/<Family>JPDOC-<Style>.ttf  矢印・図形などを全角にした文書向け
"""

import argparse
import math
import pathlib
import re
import tomllib
import unicodedata
from concurrent.futures import ProcessPoolExecutor

from fontTools.feaLib.builder import addOpenTypeFeaturesFromString
from fontTools.pens.areaPen import AreaPen
from fontTools.pens.boundsPen import BoundsPen
from fontTools.pens.recordingPen import DecomposingRecordingPen, RecordingPen
from fontTools.pens.reverseContourPen import ReverseContourPen
from fontTools.pens.transformPen import TransformPen
from fontTools.pens.ttGlyphPen import TTGlyphPen
from fontTools.ttLib import TTFont, newTable
from fontTools.ttLib.tables import otTables as ot

import pathops
import ttfautohint

import symbols
from fontutil import CFG, ROOT, SRC, FlattenPen, stroke_thickness
from symbols import ALWAYS_FULL, SYMBOLS, TECH, draw_symbol

OUT = ROOT / "build" / "fonts"

KIWI_CENTER_Y = 380  # Kiwi の仮想ボディ -120..880 の中心
VERT_FEATURES = ("vert", "vrt2", "vkna")
# Kiwi Maru から引き継ぐ機能。英字向け(liga, frac 等)と等幅を崩すもの(pwid, kern)は除く
KIWI_FEATURES = VERT_FEATURES + ("jp78", "jp90", "trad", "nlck", "hojo", "expt", "hwid", "dlig", "cjct")
# 同じタグの機能が Maple にもあるとき、別の機能レコードにせず Maple 側に lookup を足すもの
MERGE_INTO_EXISTING = ("calt",)

# 全角形 -> Maple から字形を借りる半角の文字
FULLWIDTH = {0xFF01 + i: 0x21 + i for i in range(94)}
FULLWIDTH.update({0xFF5F: 0x2985, 0xFF60: 0x2986, 0xFFE0: 0xA2, 0xFFE1: 0xA3, 0xFFE2: 0xAC,
                  0xFFE3: 0xAF, 0xFFE4: 0xA6, 0xFFE5: 0xA5, 0xFFE6: 0x20A9})

# 全角の括弧と句読点の寄せ方(開き括弧は右、閉じ括弧と句読点は左)
FW_ALIGN = {0xFF08: "right", 0xFF3B: "right", 0xFF5B: "right", 0xFF5F: "right",
            0xFF09: "left", 0xFF3D: "left", 0xFF5D: "left", 0xFF60: "left",
            0xFF0C: "left", 0xFF0E: "left"}

LONG_VOWEL = 0x30FC
WAVE_DASH = 0x301C
HBAR = 0x2015  # 2倍ダッシュに使う水平線
JOIN_OVERLAP = 8  # つなぎ目に隙間が見えないよう隣のセルへ少しはみ出す
HE = {0x30D8, 0x30D9, 0x30DA}  # ヘベペ
IDSP = 0x3000
VARIANTS = {
    # name: (Maple のディレクトリ, ファイル名の接頭辞, ファミリー名の接尾辞, JPDOC か)
    # Maple v7.9 のヒンティング済み配布物(TTF-AutoHint, NF)は無限矢印リガチャが calt から外れているため、
    # ヒンティング無しの配布物を土台にし、合成後に ttfautohint をかける
    "plain": ("maple", "MapleMono-", "", False),
    "nf": ("maple-nf-unhinted", "MapleMono-NF-", " NF", False),
    "jpdoc": ("maple", "MapleMono-", " JPDOC", True),
}


def styles():
    """(Maple のスタイル名, Kiwi のウェイト, ウェイト名, イタリックか)"""
    for weight, kiwi in CFG["weights"].items():
        yield weight, kiwi, weight, False
        yield ("Italic" if weight == "Regular" else f"{weight}Italic"), kiwi, weight, True


LATIN1_WIDE_OK = {0xD7, 0xF7, 0xB1, 0xA7, 0xB6}  # × ÷ ± § ¶ は JPDOC で全角にしてよい


def ambiguous_symbol(cp):
    """幅をそろえる対象: East Asian Width が A の記号・数字(罫線、引用符、文字類は除く)"""
    c = chr(cp)
    if unicodedata.east_asian_width(c) != "A":
        return False
    if 0x2500 <= cp <= 0x259F or 0x2018 <= cp <= 0x201F:
        return False
    if cp < 0x100 and cp not in LATIN1_WIDE_OK:
        return False
    return unicodedata.category(c) in ("So", "Sm", "No", "Nl", "Po", "Pd")


def category(cp):
    """字種別の拡大率(mono.scale_*)を選ぶための分類"""
    if 0x3040 <= cp <= 0x30FF or 0x31F0 <= cp <= 0x31FF or 0xFF65 <= cp <= 0xFF9F:
        return "kana"
    if (0x3400 <= cp <= 0x4DBF or 0x4E00 <= cp <= 0x9FFF or 0xF900 <= cp <= 0xFAFF
            or cp >= 0x20000 or 0x3005 <= cp <= 0x3007):
        return "kanji"
    return "other"


def gsub_rules(font, tags):
    """GSUB の置換を {tag: [("single", in, out) | ("alt", in, [outs]) | ("liga", [ins], out)]} で返す"""
    out = {}
    if "GSUB" not in font:
        return out
    gsub = font["GSUB"].table
    for fr in gsub.FeatureList.FeatureRecord:
        if fr.FeatureTag not in tags:
            continue
        rules = out.setdefault(fr.FeatureTag, [])
        for li in fr.Feature.LookupListIndex:
            for st in gsub.LookupList.Lookup[li].SubTable:
                st = getattr(st, "ExtSubTable", st)
                if hasattr(st, "mapping"):
                    rules += [("single", a, b) for a, b in st.mapping.items()]
                elif hasattr(st, "alternates"):
                    rules += [("alt", a, list(bs)) for a, bs in st.alternates.items()]
                elif hasattr(st, "ligatures"):
                    for first, ligs in st.ligatures.items():
                        rules += [("liga", [first] + list(lg.Component), lg.LigGlyph) for lg in ligs]
    return out


def scale_about(sx, sy, cx, cy):
    """点 (cx, cy) を中心に拡大縮小するアフィン変換"""
    return (sx, 0, 0, sy, cx - sx * cx, cy - sy * cy)


def split_contours(rec):
    """RecordingPen の記録を輪郭ごとに分け、各輪郭の bbox を付けて返す"""
    contours, cur = [], []
    for op, args in rec.value:
        cur.append((op, args))
        if op in ("closePath", "endPath"):
            pts = [p for _, a in cur for p in a if p is not None]
            xs, ys = [p[0] for p in pts], [p[1] for p in pts]
            contours.append((cur, (min(xs), min(ys), max(xs), max(ys))))
            cur = []
    return contours


def union_bounds(boxes):
    return (min(b[0] for b in boxes), min(b[1] for b in boxes), max(b[2] for b in boxes), max(b[3] for b in boxes))


class Builder:
    """Maple Mono の 1 スタイルに、Kiwi Maru の和文・描き直した記号・機能を足して 1 ファイルを作る"""

    def __init__(self, maple_path, kiwi_path, kiwi_weight, italic, jpdoc=False):
        self.font = TTFont(maple_path)
        self.kiwi = TTFont(kiwi_path)
        self.kiwi_gs = self.kiwi.getGlyphSet()
        self.kiwi_weight = kiwi_weight
        self.italic = italic
        self.jpdoc = jpdoc
        m = CFG["mono"]
        self.half, self.full, self.cy = m["half"], m["full"], m["center_y"]
        self.scales = {"kanji": m["scale_kanji"], "kana": m["scale_kana"], "other": m["scale_other"]}
        self.skew = math.tan(math.radians(m["italic_angle"])) if italic else 0.0
        self.order = list(self.font.getGlyphOrder())
        self.glyf = self.font["glyf"]
        self.hmtx = self.font["hmtx"]
        self.base_cmap = self.font.getBestCmap()
        self.kiwi_cmap = self.kiwi.getBestCmap()
        self.new_cmap = {}
        # 和文側の GSUB は feature ファイルの文字列として組み立て、最後に feaLib でまとめて作る。
        # lookup は書いた順に適用されるので、置き場所を 3 つに分けている
        self.fea_head = []  # 先頭: Texture Healing(Maple の calt の直後に効かせる)
        self.fea = {}  # 中: タグごとの置換規則 {tag: [rule]}(Kiwi の機能、cv91〜94)
        self.fea_tail = []  # 末尾: 縦書きの置換より後に効かせたいもの(和文のつなぎ、ss20)
        self.voiced = self._voiced_map()
        gs = self.font.getGlyphSet()
        hy = BoundsPen(gs)
        gs[self.base_cmap[ord("-")]].draw(hy)
        self.stroke = hy.bounds[3] - hy.bounds[1]  # 記号の線幅は Maple のハイフンの太さに合わせる
        self.jp_offset = self._weight_offset() if CFG["weight_match"]["enabled"] else 0.0
        self.offset_failures = []

    # ---- グリフの追加 ----

    def add(self, name, recording, width, *transforms):
        pen = TTGlyphPen(None)
        target = pen
        # transforms は適用順。TransformPen は外側から順に変換を受け取るので逆順に包む
        for t in reversed(transforms):
            target = TransformPen(target, t)
        recording.replay(target)
        return self._store(name, pen.glyph(), width)

    def _store(self, name, glyph, width):
        glyph.recalcBounds(self.glyf)
        self.glyf[name] = glyph
        self.hmtx[name] = (width, getattr(glyph, "xMin", 0))
        self.order.append(name)
        return name

    def add_component(self, name, base, dx, width):
        """平行移動しただけの字形は、元のグリフを参照する複合グリフにする(ヒンティングを引き継ぐ)"""
        pen = TTGlyphPen(self.font.getGlyphSet())
        pen.addComponent(base, (1, 0, 0, 1, dx, 0))
        return self._store(name, pen.glyph(), width)

    def italic_transform(self):
        return (1, 0, self.skew, 1, -self.skew * self.cy, 0)

    def _weight_offset(self):
        """和文の線の太さを Maple のウェイトに合わせるための、輪郭の片側あたりのずらし量。
        Regular 同士の太さの比を全ウェイトで保つ"""
        s = self.scales["kanji"]
        ratio = (stroke_thickness(SRC / "KiwiMaru-Regular.ttf", "ー") * s
                 / stroke_thickness(SRC / "maple" / "MapleMono-Regular.ttf", "-"))
        current = stroke_thickness(SRC / f"KiwiMaru-{self.kiwi_weight}.ttf", "ー") * s
        return (self.stroke * ratio - current) / 2

    def offset_outline(self, name, d):
        """輪郭を外側(d>0)/内側(d<0)へ d だけ平行にずらす。角は丸く保つ。

        太いウェイトでは、全体の太さより字の内側の白い部分を残すことを優先する。字面に占めるインクの割合が
        dense_from から dense_to にかけて、ずらし量を dense_max_offset まで下げる(負なら土台より細くする)。
        画数の多い字が小さいサイズで潰れないようにするため
        """
        path = pathops.Path()
        self.glyf[name].draw(path.getPen(), self.glyf)
        if path.area == 0:
            return
        wm = CFG["weight_match"]
        cap = wm["dense_max_offset"]
        if d > cap:
            density = abs(path.area) / (1000 * self.scales["kanji"]) ** 2
            t = min(max((density - wm["dense_from"]) / (wm["dense_to"] - wm["dense_from"]), 0), 1)
            d -= t * (d - cap)
        if abs(d) < 0.5:
            return
        result = self._offset_path(path, d)
        if result is None:
            self.offset_failures.append(name)  # 合成できない形は元の太さのまま残す
            return
        self.replace_path(name, result)

    @staticmethod
    def _offset_path(path, d):
        """skia で輪郭を d だけずらした Path。合成は数値的に不安定なことがあるので、量をわずかに変えて数回試す"""
        for jitter in (0, 0.3, -0.3, 0.7):
            try:
                edge = symbols.stroked(path, (abs(d) + jitter) * 2)
                return pathops.op(path, edge, pathops.PathOp.UNION if d > 0 else pathops.PathOp.DIFFERENCE)
            except pathops.PathOpsError:
                continue
        return None

    def _voiced_map(self):
        """濁音・半濁音のかな -> 清音のかな"""
        out = {}
        for cp in self.kiwi_cmap:
            if not (0x3040 <= cp <= 0x30FF):
                continue
            nfd = unicodedata.normalize("NFD", chr(cp))
            if len(nfd) == 2 and nfd[1] in "゙゚" and ord(nfd[0]) in self.kiwi_cmap:
                out[cp] = ord(nfd[0])
        return out

    def enlarge_dakuten(self, rec, cp):
        """濁点・半濁点の輪郭(清音の字に無い輪郭)を、左下を基点に拡大する"""
        base = DecomposingRecordingPen(self.kiwi_gs)
        self.kiwi_gs[self.kiwi_cmap[self.voiced[cp]]].draw(base)
        base_boxes = [b for _, b in split_contours(base)]
        contours = split_contours(rec)
        marks = [i for i, (_, b) in enumerate(contours)
                 if not any(all(abs(b[k] - bb[k]) <= 30 for k in range(4)) for bb in base_boxes)]
        if not marks or len(marks) > 3:
            return rec
        mb = union_bounds([contours[i][1] for i in marks])
        src_w = self.kiwi["hmtx"][self.kiwi_cmap[cp]][0]
        if (mb[0] + mb[2]) / 2 < src_w * 0.55 or (mb[1] + mb[3]) / 2 < 450:
            return rec  # 右上以外に見つかったら濁点とみなさない
        s = CFG["dakuten"]["scale"]
        out = RecordingPen()
        for i, (ops, _) in enumerate(contours):
            target = TransformPen(out, scale_about(s, s, mb[0], mb[1])) if i in marks else out
            for op, args in ops:
                getattr(target, op)(*args)
        return out

    def kiwi_glyph(self, gname, cp, vertical=False, extra=None, suffix="", dakuten=True, fit_half=False):
        """Kiwi のグリフを字種別に拡大して全角/半角セルに置く。fit_half は全角の記号を半角に縮めて収める"""
        rec = DecomposingRecordingPen(self.kiwi_gs)
        self.kiwi_gs[gname].draw(rec)
        if dakuten and cp in self.voiced and self.kiwi_cmap.get(cp) == gname:
            rec = self.enlarge_dakuten(rec, cp)
        src_w = self.kiwi["hmtx"][gname][0]
        width = self.half if src_w <= 600 else self.full
        s = self.scales[category(cp)]
        base = (s, 0, 0, s, width / 2 - src_w / 2 * s, self.cy - KIWI_CENTER_Y * s)
        if fit_half:
            b = BoundsPen(self.kiwi_gs)
            self.kiwi_gs[gname].draw(b)
            if b.bounds:
                x0, _, x1, _ = b.bounds
                width = self.half
                s = min(s, CFG["symbols"]["half_fit"] / max(x1 - x0, 1))
                base = (s, 0, 0, s, width / 2 - (x0 + x1) / 2 * s, self.cy - KIWI_CENTER_Y * s)
        ts = [base]
        d = CFG["distinguish"]
        if cp == LONG_VOWEL:
            # 縦書き用の「ー」は縦棒なので縦方向に縮める
            k = d["long_vowel_x"]
            ts.append(scale_about(1, k, width / 2, self.cy) if vertical else scale_about(k, 1, width / 2, self.cy))
        if cp in HE:
            ts.append(scale_about(d["he_x"], d["he_y"], width / 2, self.cy))
        if extra:
            ts.append(extra)
        if self.skew:
            ts.append(self.italic_transform())
        name = self.add(f"jp.{gname}{suffix}", rec, width, *ts)
        # 太らせるウェイトでは、量が小さくても混んだ字を細くする調整が要るので必ず通す
        if abs(self.jp_offset) >= 1.5 or self.jp_offset > 0:
            self.offset_outline(name, self.jp_offset)
        if cp == HBAR:
            self.extend_bar(name, vertical)
        return name

    # ---- 和文のつなぎ(無限リガチャ) ----

    def outline_union(self, name, polygons):
        """グリフに多角形を足し合わせる(輪郭の向きは TrueType の時計回りに揃える)"""
        path = pathops.Path()
        self.glyf[name].draw(path.getPen(), self.glyf)
        for poly in polygons:
            pen = path.getPen()
            pen.moveTo(poly[0])
            for pt in poly[1:]:
                pen.lineTo(pt)
            pen.closePath()
        path.simplify()
        self.replace_path(name, path)

    def replace_path(self, name, path):
        """skia の Path でグリフの輪郭を置き換える。skia が出す輪郭は外側が反時計回りなので向きを反転する
        (skia の area は常に正なので、replace_outline には「反転する」ことだけを伝えている)"""
        self.replace_outline(name, path.draw, path.area)

    def replace_outline(self, name, draw, area=None):
        """draw(pen) が描く輪郭でグリフを置き換える。TrueType の外側の輪郭は時計回りなので、
        符号付き面積が正(反時計回り)なら向きを反転する。area を渡せば面積の計算を省く"""
        if area is None:
            pen = AreaPen()
            draw(pen)
            area = pen.value
        pen = TTGlyphPen(None)
        draw(ReverseContourPen(pen) if area > 0 else pen)
        glyph = pen.glyph()
        glyph.recalcBounds(self.glyf)
        self.glyf[name] = glyph
        self.hmtx[name] = (self.hmtx[name][0], getattr(glyph, "xMin", 0))

    def italic_points(self, pts):
        return [(x + self.skew * (y - self.cy), y) for x, y in pts]

    def extend_bar(self, name, vertical):
        """2倍ダッシュ用の水平線をセルいっぱいに伸ばし、2つ並べると1本になるようにする"""
        g = self.glyf[name]
        ov, full = JOIN_OVERLAP, self.hmtx[name][0]
        if vertical:
            full = self.full
            top, bottom = self.cy + full / 2, self.cy - full / 2
            rect = [(g.xMin, bottom - ov), (g.xMin, top + ov), (g.xMax, top + ov), (g.xMax, bottom - ov)]
        else:
            rect = [(-ov, g.yMin), (-ov, g.yMax), (full + ov, g.yMax), (full + ov, g.yMin)]
            rect = self.italic_points(rect)
        self.outline_union(name, [rect])

    def bar_join_variants(self, base):
        """長音のつなぎ用: 右へ伸ばす(sta)・両側(mid)・左へ伸ばす(end)"""
        g = self.glyf[base]
        ov, full = JOIN_OVERLAP, self.full
        mid_x = full / 2
        names = {}
        for part, (x0, x1) in {"sta": (mid_x, full + ov), "mid": (-ov, full + ov), "end": (-ov, mid_x)}.items():
            name = f"{base}.join.{part}"
            self.glyf[name] = g
            self.hmtx[name] = self.hmtx[base]
            self.order.append(name)
            rect = self.italic_points([(x0, g.yMin), (x0, g.yMax), (x1, g.yMax), (x1, g.yMin)])
            self.outline_union(name, [rect])
            names[part] = name
        return names

    def wave_join_variants(self, base, long_vowel):
        """波ダッシュのつなぎ用。周期をセル幅に合わせた正弦波で、境目の高さと傾きを一致させる"""
        g = self.glyf[base]
        flat = FlattenPen(self.skew)
        self.glyf[base].draw(flat, self.glyf)
        b = flat.bounds()
        x_start, x_end = b[0] + self.skew * self.cy, b[2] + self.skew * self.cy
        lv = self.glyf[long_vowel]
        t = lv.yMax - lv.yMin  # 線の太さは長音に合わせる
        amp = (g.yMax - g.yMin - t) / 2
        cy = (g.yMax + g.yMin) / 2
        full, ov = self.full, JOIN_OVERLAP
        names = {}
        for part, (x0, x1, cap0, cap1) in {"sta": (x_start + t / 2, full + ov, True, False),
                                           "mid": (-ov, full + ov, False, False),
                                           "end": (-ov, x_end - t / 2, False, True)}.items():
            poly = self.thick_wave(x0, x1, cy, amp, t, cap0, cap1)
            name = f"{base}.join.{part}"
            rec = RecordingPen()
            rec.moveTo(poly[0])
            for pt in poly[1:]:
                rec.lineTo(pt)
            rec.closePath()
            ts = [self.italic_transform()] if self.skew else []
            self.add(name, rec, full, *ts)
            self.replace_outline(name, lambda pen, n=name: self.glyf[n].draw(pen, self.glyf))
            names[part] = name
        return names

    def thick_wave(self, x0, x1, cy, amp, t, cap0, cap1, step=12):
        k = 2 * math.pi / self.full

        def center(x):
            return x, cy + amp * math.sin(k * x)

        def normal(x):
            dy = amp * k * math.cos(k * x)
            n = math.hypot(1, dy)
            return -dy / n, 1 / n

        n = max(2, int((x1 - x0) / step))
        upper, lower = [], []
        for i in range(n + 1):
            x = x0 + (x1 - x0) * i / n
            (px, py), (nx, ny) = center(x), normal(x)
            upper.append((px + nx * t / 2, py + ny * t / 2))
            lower.append((px - nx * t / 2, py - ny * t / 2))

        def cap(x, sign):
            # sign=+1 で右端、-1 で左端の半円
            cx, cyy = center(x)
            nx, ny = normal(x)
            a0 = math.atan2(ny, nx)
            pts = []
            for i in range(1, 12):
                a = a0 - sign * math.pi * i / 12
                pts.append((cx + math.cos(a) * t / 2, cyy + math.sin(a) * t / 2))
            return pts

        poly = upper[:]
        poly += cap(x1, 1) if cap1 else []
        poly += lower[::-1]
        poly += cap(x0, -1) if cap0 else []
        return poly

    def add_joins(self):
        """長音と波ダッシュが 2 つ以上続いたとき、1 本の線・波につながる字形に置き換える(calt)"""
        final = {**self.base_cmap, **self.new_cmap}
        rules = []

        def run(base, parts, tag):
            # 2つ以上続いたときだけ置き換える。先頭は sta、途中は mid、最後は end
            joined = f"[{parts['sta']} {parts['mid']}]"
            for part in ("sta", "mid", "end"):
                rules.append(f"lookup {tag}_{part} {{ sub {base} by {parts[part]}; }} {tag}_{part};")
            rules.append(f"""lookup {tag} {{
  sub {joined} {base}' lookup {tag}_mid {base};
  sub {joined} {base}' lookup {tag}_end;
  sub {base}' lookup {tag}_sta {base};
}} {tag};""")

        long_vowel = final[LONG_VOWEL]
        run(long_vowel, self.bar_join_variants(long_vowel), "join_lv")
        if WAVE_DASH in final and final[WAVE_DASH].startswith("jp."):
            run(final[WAVE_DASH], self.wave_join_variants(final[WAVE_DASH], long_vowel), "join_wave")
        names = [r.split()[1] for r in rules if r.startswith("lookup join_") and "'" in r]
        self.fea_tail += rules
        self.fea_tail.append("feature calt {\n" + "".join(f"  lookup {n};\n" for n in names) + "} calt;")

    def maple_fullwidth(self, cp, src_cp):
        """Maple の字形を全角セルに置く。括弧と句読点は和文の慣習どおり片側へ寄せる"""
        dx = (self.full - self.half) // 2
        side = FW_ALIGN.get(cp)
        if side:
            b = BoundsPen(self.font.getGlyphSet())
            self.font.getGlyphSet()[self.base_cmap[src_cp]].draw(b)
            margin = CFG["mono"]["fw_punct_margin"]
            if side == "right":
                dx = round(self.full - margin - b.bounds[2])
            else:
                dx = round(margin - b.bounds[0])
        return self.add_component(f"fw.uni{cp:04X}", self.base_cmap[src_cp], dx, self.full)

    def ideographic_space(self):
        c = CFG["ideographic_space"]
        side = 1000 * self.scales["kanji"] * c["size"]
        r = side * c["radius"]
        dot = c["dot"][self.kiwi_weight]
        half_side = side / 2 - dot / 2  # 点の中心が通る角丸四角の半辺
        straight = 2 * (half_side - r)
        perimeter = 4 * straight + 2 * math.pi * r
        rec = RecordingPen()
        n = c["dots"]
        for i in range(n):
            px, py = self._rounded_rect_point(i * perimeter / n, half_side, r, straight)
            self._circle(rec, self.full / 2 + px, self.cy + py, dot / 2)
        ts = [self.italic_transform()] if self.skew else []
        return self.add("jp.idsp.dots", rec, self.full, *ts)

    @staticmethod
    def _rounded_rect_point(t, h, r, straight):
        """上辺の中央から時計回りに弧長 t 進んだ点(中心原点)"""
        segs = []
        corners = [(h - r, h - r, 90), (h - r, -(h - r), 0), (-(h - r), -(h - r), -90), (-(h - r), h - r, 180)]
        lines = [((0, h), (1, 0), straight / 2), ((h, h - r), (0, -1), straight),
                 ((h - r, -h), (-1, 0), straight), ((-h, -(h - r)), (0, 1), straight)]
        for k in range(4):
            segs.append(("line",) + lines[k])
            segs.append(("arc",) + corners[k])
        segs.append(("line", (-(h - r), h), (1, 0), straight / 2))
        arc_len = math.pi * r / 2
        for seg in segs:
            if seg[0] == "line":
                (x0, y0), (dx, dy), length = seg[1], seg[2], seg[3]
                if t <= length:
                    return x0 + dx * t, y0 + dy * t
                t -= length
            else:
                ox, oy, start = seg[1], seg[2], seg[3]
                if t <= arc_len:
                    a = math.radians(start - math.degrees(t / r))
                    return ox + r * math.cos(a), oy + r * math.sin(a)
                t -= arc_len
        return 0, h

    @staticmethod
    def _circle(pen, x, y, radius):
        # 8 個のオフカーブ点だけで閉じた二次曲線の円(時計回り)
        rr = radius / math.cos(math.pi / 8)
        pts = [(x + rr * math.cos(-k * math.pi / 4), y + rr * math.sin(-k * math.pi / 4)) for k in range(8)]
        pen.qCurveTo(*pts, None)
        pen.closePath()

    def empty(self, name, width):
        return self._store(name, TTGlyphPen(None).glyph(), width)

    # ---- 記号 ----

    def store_path(self, name, path, width):
        """skia の Path から新しいグリフを作る"""
        self.empty(name, width)
        self.replace_path(name, path)
        return name

    def symbol_glyph(self, cp, full):
        c = CFG["symbols"]
        if full:
            path = draw_symbol(cp, self.full / 2, self.cy, c["full_radius"], self.stroke)
            return self.store_path(f"sym.uni{cp:04X}.f", path, self.full)
        path = draw_symbol(cp, self.half / 2, c["half_center_y"], c["half_radius"], self.stroke * c["half_stroke"])
        return self.store_path(f"sym.uni{cp:04X}.h", path, self.half)

    def circled_number(self, cp):
        """等幅版の ①〜⑳・❶〜❿: 円を描き、Maple の数字を太らせて中に置く(縮めた Kiwi の字形は線が細すぎるため)"""
        c = CFG["symbols"]
        negative = 0x2776 <= cp <= 0x277F
        n = cp - 0x2776 + 1 if negative else cp - 0x2460 + 1
        t = self.stroke * c["half_stroke"] * 0.8
        cx, cy, r = self.half / 2, c["half_center_y"], c["half_radius"]
        ring = symbols.circle(cx, cy, r - t / 2)
        base = symbols.filled(ring, t) if negative else symbols.outline(ring, t)
        digits = str(n)
        gs = self.font.getGlyphSet()
        k = (r * 0.92) / self.font["OS/2"].sCapHeight  # 数字の高さを円の直径の半分弱に
        kx = k * (0.6 if len(digits) == 2 else 1.0)
        adv = self.half * kx * 0.78
        total = adv * len(digits)
        digit_path = pathops.Path()
        for i, d in enumerate(digits):
            pen = digit_path.getPen()
            x0 = cx - total / 2 + adv * i - self.half * kx * 0.09
            y0 = cy - self.font["OS/2"].sCapHeight * k / 2
            gs[self.base_cmap[ord(d)]].draw(TransformPen(pen, (kx, 0, 0, k, x0, y0)))
        digit_path.simplify()
        digit_path = symbols.union(digit_path, symbols.stroked(digit_path, t * 0.25))
        op = pathops.PathOp.DIFFERENCE if negative else pathops.PathOp.UNION
        return self.store_path(f"sym.uni{cp:04X}.h", pathops.op(base, digit_path, op), self.half)

    def add_symbols(self):
        """描き直した記号を入れ、版ごとに半角/全角を選ぶ"""
        for ch in SYMBOLS:
            cp = ord(ch)
            if ch in ALWAYS_FULL:
                full = True
            elif ch in TECH:
                full = False
            else:
                full = self.jpdoc
            self.new_cmap[cp] = self.symbol_glyph(cp, full)
        if not self.jpdoc:
            for cp in list(range(0x2460, 0x2474)) + list(range(0x2776, 0x2780)):
                self.new_cmap[cp] = self.circled_number(cp)
        # ss20: <3 をハートに。< を空白に、3 を前のセルまで広がるハートにする(2セル幅を保つ)
        lt3 = draw_symbol(ord("♡"), 0, self.cy, CFG["symbols"]["full_radius"] * 0.9, self.stroke)
        heart_name = self.store_path("sym.lt3", lt3, self.half)
        less, three = self.base_cmap[ord("<")], self.base_cmap[ord("3")]
        spc = "SPC" if "SPC" in self.glyf else self.empty("sym.lt3.spc", self.half)
        self.fea_tail += [f"lookup lt3_less {{ sub {less} by {spc}; }} lt3_less;",
                          f"lookup lt3_three {{ sub {three} by {heart_name}; }} lt3_three;",
                          "feature ss20 {",
                          '  featureNames { name 3 1 0x409 "<3 をハートに / <3 as heart"; };',
                          f"  sub {less}' lookup lt3_less {three}' lookup lt3_three;",
                          "} ss20;"]

    # ---- 組み立て ----

    def build(self):
        added = {}  # kiwi glyph -> new name
        cp_of = {}  # kiwi glyph -> codepoint(字種判定用)
        d = CFG["distinguish"]
        strong = {ord(ch) for ch in d["strong_chars"]}

        drawn = {ord(c) for c in SYMBOLS}
        half_added = {}

        def from_maple(cp):
            # JPDOC 版では曖昧幅の記号を Kiwi の全角で出す
            return cp in self.base_cmap and not (self.jpdoc and ambiguous_symbol(cp) and cp in self.kiwi_cmap)

        # 全角英数記号は Maple の字形から作る
        fw_sources = {cp: src for cp, src in FULLWIDTH.items() if src in self.base_cmap}
        for cp, src in fw_sources.items():
            self.new_cmap[cp] = self.maple_fullwidth(cp, src)

        for cp, gname in sorted(self.kiwi_cmap.items()):
            if from_maple(cp) or cp in fw_sources or cp == IDSP or cp in drawn:
                continue
            if not self.jpdoc and ambiguous_symbol(cp):
                # 等幅版: Kiwi にしか無い曖昧幅の記号は半角に縮める(同じグリフを共有する文字があるので使い回す)
                if gname not in half_added:
                    half_added[gname] = self.kiwi_glyph(gname, cp, fit_half=True, suffix=".amb")
                self.new_cmap[cp] = half_added[gname]
                continue
            if gname not in added:
                added[gname] = self.kiwi_glyph(gname, cp)
                cp_of[gname] = cp
            self.new_cmap[cp] = added[gname]

        if self.jpdoc:
            # Kiwi にも無い曖昧幅の記号は、Maple の字形を全角セルの中央に置く
            for cp in sorted(self.base_cmap):
                if ambiguous_symbol(cp) and cp not in self.kiwi_cmap and cp not in drawn:
                    self.new_cmap[cp] = self.maple_fullwidth(cp, cp)

        self.import_kiwi_features(added, cp_of)

        # 全角スペース: 既定は点線の枠、cv91 で空白
        self.new_cmap[IDSP] = self.ideographic_space()
        self.fea["cv91"] = [f"sub {self.new_cmap[IDSP]} by {self.empty('jp.idsp.blank', self.full)};"]

        # cv92: 漢字と紛らわしいカタカナをさらに小さく
        s = d["strong_scale"]
        self.fea["cv92"] = []
        for cp in sorted(strong):
            gname = self.kiwi_cmap.get(cp)
            if gname in added:
                alt = self.kiwi_glyph(gname, cp, extra=scale_about(s, s, self.full / 2, self.cy), suffix=".cv92")
                self.fea["cv92"].append(f"sub {added[gname]} by {alt};")

        # cv93: 全角英数記号を Kiwi の字形に戻す
        self.fea["cv93"] = []
        for cp in sorted(fw_sources):
            gname = self.kiwi_cmap.get(cp)
            if gname:
                alt = self.kiwi_glyph(gname, cp, suffix=".cv93")
                self.fea["cv93"].append(f"sub {self.new_cmap[cp]} by {alt};")

        # cv94: 濁点・半濁点を元の大きさに戻す
        self.fea["cv94"] = []
        for cp in sorted(self.voiced):
            gname = self.kiwi_cmap[cp]
            if gname in added:
                alt = self.kiwi_glyph(gname, cp, suffix=".cv94", dakuten=False)
                self.fea["cv94"].append(f"sub {added[gname]} by {alt};")

        self.add_symbols()
        self.add_joins()
        self.add_healing()
        self.split_arrow_bars()

        self.font.setGlyphOrder(self.order)
        self.glyf.glyphOrder = self.order
        for table in self.font["cmap"].tables:
            if table.isUnicode():
                table.cmap.update({k: v for k, v in self.new_cmap.items() if table.format != 4 or k <= 0xFFFF})
        self.add_vertical_metrics()
        self.add_features()
        self.fix_tables()
        return self.font

    def import_kiwi_features(self, added, cp_of):
        """Kiwi Maru の和文向け機能を、合成後のグリフ名に置き換えて引き継ぐ"""
        kiwi_rev = {}
        for cp, g in self.kiwi_cmap.items():
            kiwi_rev.setdefault(g, cp)
        final_cmap = {**self.base_cmap, **self.new_cmap}

        def resolve(kname, hint_cp=None, vertical=False):
            if kname in added:
                return added[kname]
            cp = kiwi_rev.get(kname)
            if cp is not None and cp in final_cmap:
                return final_cmap[cp]
            if hint_cp is None or kname not in self.kiwi_gs:
                return None
            added[kname] = self.kiwi_glyph(kname, hint_cp, vertical=vertical)
            cp_of[kname] = hint_cp
            return added[kname]

        def jp_side(names):
            return any(n and (n.startswith("jp.") or n.startswith("fw.")) for n in names)

        for tag, rules in gsub_rules(self.kiwi, KIWI_FEATURES).items():
            vertical = tag in VERT_FEATURES
            lines, seen = [], set()
            for kind, a, b in rules:
                ins = a if kind == "liga" else [a]
                mapped = [resolve(n) for n in ins]
                if None in mapped or not jp_side(mapped):
                    continue
                hint = cp_of.get(ins[0], kiwi_rev.get(ins[0]))
                if kind == "alt":
                    outs = [resolve(o, hint, vertical) for o in b]
                    outs = [o for o in outs if o]
                    if outs and mapped[0] not in seen:
                        lines.append(f"sub {mapped[0]} from [{' '.join(outs)}];")
                        seen.add(mapped[0])
                    continue
                out = resolve(b, hint, vertical)
                key = tuple(mapped)
                if out and key not in seen and out != mapped[0]:
                    lines.append(f"sub {' '.join(mapped)} by {out};")
                    seen.add(key)
            if lines:
                self.fea[tag] = lines

    # ---- 無限矢印の横線 ----

    def split_arrow_bars(self):
        """無限矢印の部品のうち矢じりや縦棒を含むものを、横線の帯だけのグリフと残りのグリフに分ける。
        ヒンティングはグリフごとに丸めるので、横線と矢じりが 1 つの輪郭だと、矢じりの端に引かれて
        横線だけが隣の = や - の部品と 1px ずれる(Maple Mono issue #508)。横線だけのグリフは
        = や - の部品と同じ高さに丸まる。残りは送り幅 0 の .head にして、calt の最後で後ろに足す"""
        gs = self.font.getGlyphSet()

        def path_of(name):
            rec = DecomposingRecordingPen(gs)
            gs[name].draw(rec)
            path = pathops.Path()
            rec.replay(path.getPen())
            return path

        def rect(x0, y0, x1, y1):
            path = pathops.Path()
            pen = path.getPen()
            pen.moveTo((x0, y0)); pen.lineTo((x1, y0)); pen.lineTo((x1, y1)); pen.lineTo((x0, y1)); pen.closePath()
            return path

        def union(paths):
            out = pathops.Path()
            for p in paths:
                out = pathops.op(out, p, pathops.PathOp.UNION)
            return out

        # 横線の帯は、ウェイトごとに = と - の中間の部品の輪郭から取る
        bands, reach = {}, {}
        for kind in ("equal", "hyphen"):
            ys = sorted((round(c.bounds[1]), round(c.bounds[3])) for c in path_of(f"{kind}.mid.seq").contours)
            bands[kind] = union(rect(-2000, y0, 2000, y1) for y0, y1 in ys)
            reach[kind] = min(y1 - y0 for y0, y1 in ys) / 2
        pure = {f"{k}.{p}.seq" for k in bands for p in ("sta", "mid", "end")}
        rules = []
        for name in list(self.order):
            is_seq = name.endswith(".seq") or ".seq." in name  # .seq.cv01 などの切り替え後の字形も含む
            if not is_seq or name in pure:
                continue
            kind = "equal" if "equal" in name else "hyphen" if "hyphen" in name else None
            if not kind:
                continue
            whole = path_of(name)
            bars = pathops.op(whole, bands[kind], pathops.PathOp.INTERSECTION)
            if name.startswith("bar_"):
                # 縦棒は切らずに残りの側へまとめる(横線のグリフからは縦棒の幅の分を抜く)
                rest = pathops.op(whole, bands[kind], pathops.PathOp.DIFFERENCE)
                x0, _, x1, _ = rest.bounds
                bars = pathops.op(bars, rect(x0, -2000, x1, 2000), pathops.PathOp.DIFFERENCE)
            rest = pathops.op(whole, bars, pathops.PathOp.DIFFERENCE)
            if not list(rest.contours) or not list(bars.contours):
                continue
            # 矢じりの切れ端は丸めで 1px ずれることがあるので、横線の太さの半分まで線の中へ伸ばして、
            # つなぎ目が線に隠れるようにする(元の輪郭の内側だけを足すので、字形は変わらない)
            d = reach[kind]
            grown = [rest]
            for dy in (-d, -d / 2, d / 2, d):
                moved = pathops.Path()
                rest.draw(TransformPen(moved.getPen(), (1, 0, 0, 1, 0, dy)))
                grown.append(moved)
            rest = pathops.op(union(grown), whole, pathops.PathOp.INTERSECTION)
            width = self.hmtx[name][0]
            shifted = pathops.Path()
            rest.draw(TransformPen(shifted.getPen(), (1, 0, 0, 1, -width, 0)))
            self.replace_path(name, bars)
            self.store_path(f"{name}.head", shifted, 0)
            rules.append(f"sub {name} by {name} {name}.head;")
        gdef = self.font["GDEF"].table if "GDEF" in self.font else None
        if gdef and gdef.GlyphClassDef:
            for r in rules:
                gdef.GlyphClassDef.classDefs[r.split()[-1].rstrip(";")] = 1
        # calt の他の規則で部品が決まったあとに分けるため、最後の lookup にする
        self.fea_tail += ["lookup arrow_split {", *[f"  {r}" for r in rules], "} arrow_split;",
                          "feature calt { lookup arrow_split; } calt;"]

    # ---- Texture Healing / Smart Kerning ----

    def add_healing(self):
        """細い字と広い字の組で、位置をずらした字形と置き換え規則(calt)を作る"""
        h = CFG["healing"]
        alt_tags = [fr.FeatureTag for fr in self.font["GSUB"].table.FeatureList.FeatureRecord
                    if re.fullmatch(r"(cv|ss)\d\d|zero", fr.FeatureTag)]
        alternates = {}
        for rules in gsub_rules(self.font, alt_tags).values():
            for kind, a, b in rules:
                if kind == "single":
                    alternates.setdefault(a, set()).add(b)

        def members(chars):
            names = {self.base_cmap[ord(c)] for c in chars if ord(c) in self.base_cmap}
            for n in list(names):
                names |= alternates.get(n, set())
            return sorted(n for n in names if self.hmtx[n][0] == self.half)

        letters = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
        narrow, wide = members(h["narrow"]), members(h["wide"])
        other = [n for n in members(letters) if n not in narrow and n not in wide]
        cls = {}
        ns, os_ = h["narrow_shift"], h["other_shift"]
        for key, names, dx in (("N.l", narrow, -ns), ("N.r", narrow, ns), ("O.l", other, -os_), ("O.r", other, os_)):
            cls[key] = [self.add_component(f"{n}.heal{key[-1]}", n, dx, self.half) for n in names]
        for side in ("l", "r", "b"):
            cls[f"W.{side}"] = [self.stretched(n, side, h["wide_expand"]) for n in wide]
        cls["N"], cls["W"], cls["O"] = narrow, wide, other

        def c(*keys):
            return "[" + " ".join(n for k in keys for n in cls[k]) + "]"

        self.fea_head.append(f"""
@hN = {c('N')}; @hNl = {c('N.l')}; @hNr = {c('N.r')};
@hW = {c('W')}; @hWl = {c('W.l')}; @hWr = {c('W.r')}; @hWb = {c('W.b')};
@hO = {c('O')}; @hOl = {c('O.l')}; @hOr = {c('O.r')};
@hWx = {c('W', 'W.l', 'W.r', 'W.b')}; @hNx = {c('N', 'N.l', 'N.r')};
lookup heal_wb {{ sub @hW by @hWb; }} heal_wb;
lookup heal_wl {{ sub @hW by @hWl; }} heal_wl;
lookup heal_wr {{ sub @hW by @hWr; }} heal_wr;
lookup heal_nl {{ sub @hN by @hNl; }} heal_nl;
lookup heal_nr {{ sub @hN by @hNr; }} heal_nr;
lookup heal_ol {{ sub @hO by @hOl; }} heal_ol;
lookup heal_or {{ sub @hO by @hOr; }} heal_or;
feature calt {{
  lookup heal_wide {{
    sub @hN @hW' lookup heal_wb @hN;
    sub @hN @hW' lookup heal_wl;
    sub @hW' lookup heal_wr @hN;
  }} heal_wide;
  lookup heal_narrow {{
    ignore sub @hWx @hN' @hWx;
    sub @hWx @hN' lookup heal_nr;
    sub @hN' lookup heal_nl @hWx;
  }} heal_narrow;
  lookup heal_other {{
    sub @hWx @hO' lookup heal_or @hNx;
    sub @hNx @hO' lookup heal_ol @hWx;
  }} heal_other;
}} calt;
""")

    def stretched(self, name, side, d):
        """縦画の太さを保ったまま、広い字を左/右/両側へ d だけ広げる"""
        rec = DecomposingRecordingPen(self.font.getGlyphSet())
        self.font.getGlyphSet()[name].draw(rec)
        flat = FlattenPen(self.skew)
        rec.replay(flat)
        b = flat.bounds()
        os2 = self.font["OS/2"]
        y = (os2.sxHeight if b[3] < os2.sxHeight + 80 else os2.sCapHeight) / 2
        hits = sorted(flat.hits(y))
        stems = [(hits[i], hits[i + 1]) for i in range(0, len(hits) - 1, 2)]
        if len(stems) < 2:
            stems = [(b[0], b[0]), (b[2], b[2])]
        n = len(stems)
        # 縦画ごとの移動量。画の内側は同じだけ動かし、画の間(カウンター)で補間する
        if side == "l":
            moves = [-d * (n - 1 - k) / (n - 1) for k in range(n)]
        elif side == "r":
            moves = [d * k / (n - 1) for k in range(n)]
        else:
            moves = [-d + 2 * d * k / (n - 1) for k in range(n)]
        knots = []
        for (a, z), mv in zip(stems, moves, strict=True):
            knots += [(a, mv), (z, mv)]

        def disp(x):
            if x <= knots[0][0]:
                return knots[0][1]
            for (xa, ma), (xb, mb) in zip(knots, knots[1:], strict=False):
                if x <= xb:
                    return ma if xb == xa else ma + (mb - ma) * (x - xa) / (xb - xa)
            return knots[-1][1]

        out = RecordingPen()
        for op, args in rec.value:
            out.value.append((op, tuple(None if p is None else (p[0] + disp(p[0] - self.skew * p[1]), p[1])
                                        for p in args)))
        suffix = {"l": "xl", "r": "xr", "b": "xb"}[side]
        return self.add(f"{name}.heal{suffix}", out, self.half)

    def add_vertical_metrics(self):
        top = self.cy + self.full / 2
        vmtx = newTable("vmtx")
        vmtx.metrics = {}
        for name in self.order:
            g = self.glyf[name]
            tsb = round(top - g.yMax) if getattr(g, "numberOfContours", 0) else 0
            vmtx.metrics[name] = (self.full, tsb)
        self.font["vmtx"] = vmtx
        vhea = newTable("vhea")
        vhea.tableVersion = 0x00011000
        vhea.ascent, vhea.descent, vhea.lineGap = self.full // 2, -self.full // 2, 0
        vhea.advanceHeightMax = self.full
        vhea.minTopSideBearing = vhea.minBottomSideBearing = vhea.yMaxExtent = 0
        vhea.caretSlopeRise, vhea.caretSlopeRun, vhea.caretOffset = 0, 1, 0
        vhea.reserved1 = vhea.reserved2 = vhea.reserved3 = vhea.reserved4 = 0
        vhea.metricDataFormat = 0
        vhea.numberOfVMetrics = len(self.order)
        self.font["vhea"] = vhea

    def add_features(self):
        labels = {"cv91": "全角スペースを空白で表示 / Plain ideographic space",
                  "cv92": "紛らわしいカタカナを強調 / Emphasize katakana vs kanji",
                  "cv93": "全角英数記号を和文の字形に / Kiwi Maru fullwidth forms",
                  "cv94": "濁点・半濁点を元の大きさに / Original dakuten size"}
        lines = ["languagesystem DFLT dflt;", "languagesystem latn dflt;",
                 "languagesystem kana dflt;", "languagesystem hani dflt;"]
        lines += self.fea_head
        for tag, rules in self.fea.items():
            if not rules:
                continue
            lines.append(f"feature {tag} {{")
            if tag in labels:
                lines.append(f'  cvParameters {{ FeatUILabelNameID {{ name 3 1 0x409 "{labels[tag]}"; }}; }};')
            lines += [f"  {r}" for r in rules]
            lines.append(f"}} {tag};")
        lines += self.fea_tail
        maple_gsub = self.font["GSUB"]
        del self.font["GSUB"]
        addOpenTypeFeaturesFromString(self.font, "\n".join(lines), tables=["GSUB", "name"])
        jp_gsub = self.font["GSUB"]
        self.font["GSUB"] = maple_gsub
        merge_gsub(maple_gsub.table, jp_gsub.table)

    def fix_tables(self):
        for tag in ("hdmx", "LTSH", "VDMX"):
            if tag in self.font:
                del self.font[tag]
        os2 = self.font["OS/2"]
        os2.ulCodePageRange1 |= 1 << 17  # JIS/Japan
        os2.recalcUnicodeRanges(self.font)
        os2.xAvgCharWidth = self.half
        self.font["post"].formatType = 2.0
        # 版は Maple の値が残るので、名前テーブル(Version x.y.z)と同じ版に直す
        major, minor = CFG["version"].split(".")[:2]
        self.font["head"].fontRevision = float(f"{major}.{minor}")


def shift_nested_lookups(lookups, offset):
    """文脈置換の中から参照している lookup 番号を offset だけずらす"""
    seen = set()

    def walk(obj):
        if id(obj) in seen:
            return
        seen.add(id(obj))
        if isinstance(obj, list):
            for x in obj:
                walk(x)
            return
        if not hasattr(obj, "__dict__"):
            return
        for key, value in vars(obj).items():
            if key == "SubstLookupRecord":
                for rec in value:
                    rec.LookupListIndex += offset
            elif isinstance(value, list) or hasattr(value, "__dict__"):
                walk(value)

    for lookup in lookups:
        walk(lookup.SubTable)


def merge_gsub(dst, src):
    """src の lookup と feature を dst に足す。両者は同じ glyph order 上で作られている前提"""
    offset = len(dst.LookupList.Lookup)
    shift_nested_lookups(src.LookupList.Lookup, offset)
    dst.LookupList.Lookup.extend(src.LookupList.Lookup)
    dst.LookupList.LookupCount = len(dst.LookupList.Lookup)

    feats = [(fr.FeatureTag, fr.Feature) for fr in dst.FeatureList.FeatureRecord]
    first_of = {}
    for i, (tag, _) in enumerate(feats):
        first_of.setdefault(tag, i)
    src_index = {}
    for i, fr in enumerate(src.FeatureList.FeatureRecord):
        lookups = [li + offset for li in fr.Feature.LookupListIndex]
        if fr.FeatureTag in MERGE_INTO_EXISTING and fr.FeatureTag in first_of:
            # Maple の calt の後ろに足す(lookup は番号順に適用されるので Maple のリガチャが先)
            j = first_of[fr.FeatureTag]
            feat = feats[j][1]
            feat.LookupListIndex = list(feat.LookupListIndex) + lookups
            feat.LookupCount = len(feat.LookupListIndex)
            src_index[i] = j
        else:
            fr.Feature.LookupListIndex = lookups
            fr.Feature.LookupCount = len(lookups)
            src_index[i] = len(feats)
            feats.append((fr.FeatureTag, fr.Feature))

    def langsys_map(table, index_of):
        m = {}
        for sr in table.ScriptList.ScriptRecord:
            if sr.Script.DefaultLangSys:
                m[(sr.ScriptTag, None)] = [index_of(i) for i in sr.Script.DefaultLangSys.FeatureIndex]
            for lr in sr.Script.LangSysRecord:
                m[(sr.ScriptTag, lr.LangSysTag)] = [index_of(i) for i in lr.LangSys.FeatureIndex]
        return m

    dm, sm = langsys_map(dst, lambda i: i), langsys_map(src, lambda i: src_index[i])
    scripts = sorted({s for s, _ in dm} | {s for s, _ in sm})

    # feature を tag 順に並べ直す
    order = sorted(range(len(feats)), key=lambda i: (feats[i][0], i))
    remap = {old: new for new, old in enumerate(order)}
    dst.FeatureList.FeatureRecord = []
    for i in order:
        fr = ot.FeatureRecord()
        fr.FeatureTag, fr.Feature = feats[i]
        dst.FeatureList.FeatureRecord.append(fr)
    dst.FeatureList.FeatureCount = len(feats)

    def langsys(indices):
        ls = ot.LangSys()
        ls.LookupOrder = None
        ls.ReqFeatureIndex = 0xFFFF
        ls.FeatureIndex = sorted({remap[i] for i in indices})
        ls.FeatureCount = len(ls.FeatureIndex)
        return ls

    records = []
    for script in scripts:
        d_def = dm.get((script, None), dm.get(("DFLT", None), []))
        s_def = sm.get((script, None), sm.get(("DFLT", None), []))
        sr = ot.ScriptRecord()
        sr.ScriptTag = script
        sr.Script = ot.Script()
        sr.Script.DefaultLangSys = langsys(d_def + s_def)
        sr.Script.LangSysRecord = []
        for (sc, lang), idx in sorted(dm.items(), key=lambda kv: str(kv[0])):
            if sc == script and lang is not None:
                lr = ot.LangSysRecord()
                lr.LangSysTag = lang
                lr.LangSys = langsys(idx + s_def)
                sr.Script.LangSysRecord.append(lr)
        sr.Script.LangSysCount = len(sr.Script.LangSysRecord)
        records.append(sr)
    dst.ScriptList.ScriptRecord = records
    dst.ScriptList.ScriptCount = len(records)


def rename(font, family, weight, italic):
    name = font["name"]
    style = weight if not italic else ("Italic" if weight == "Regular" else f"{weight} Italic")
    ribbi = weight in ("Regular", "Bold")
    ps = f"{family.replace(' ', '')}-{style.replace(' ', '')}"
    kiwi_copyright = "Copyright 2020 The Kiwi Maru Project Authors (https://github.com/Kiwi-KawagotoKajiru/Kiwi-Maru)"
    maple_copyright = name.getDebugName(0) or "Copyright 2022 The Maple Mono Project Authors (https://github.com/subframe7536/maple-font)"
    values = {
        0: f"Copyright 2026 The Pancake Mono Project Authors\n{maple_copyright}\n{kiwi_copyright}",
        1: family if ribbi else f"{family} {weight}",
        2: ("Bold " if weight == "Bold" else "") + ("Italic" if italic else ("Regular" if weight != "Bold" else "")),
        3: f"{CFG['version']};{ps}",
        4: f"{family} {style}" if style != "Regular" else family,
        5: f"Version {CFG['version']}",
        6: ps,
        8: "The Pancake Mono Project Authors",
        9: "The Pancake Mono Project Authors (based on Maple Mono by subframe7536 and Kiwi Maru by Kiwi-KawagotoKajiru)",
        10: CFG["description"],
        11: CFG["url"],
        12: CFG["url"],
        13: "This Font Software is licensed under the SIL Open Font License, Version 1.1.",
        14: "https://openfontlicense.org",
        16: family,
        17: style,
    }
    values[2] = values[2].strip() or "Regular"
    for rec in list(name.names):
        if rec.nameID in values or rec.nameID in (18, 21, 22, 25):
            name.removeNames(nameID=rec.nameID)
    for nid, value in values.items():
        name.setName(value, nid, 3, 1, 0x409)


def build_one(job):
    style, kiwi_weight, weight, italic, variant, hint, out_root = job
    maple_dir, prefix, fam_suffix, jpdoc = VARIANTS[variant]
    family = CFG["family"] + fam_suffix
    builder = Builder(SRC / maple_dir / f"{prefix}{style}.ttf", SRC / f"KiwiMaru-{kiwi_weight}.ttf",
                      kiwi_weight, italic, jpdoc)
    font = builder.build()
    rename(font, family, weight, italic)
    if builder.offset_failures:
        print(f"{style}: 太さを合わせられなかったグリフ {len(builder.offset_failures)} 個: {builder.offset_failures[:8]}")
    out_dir = out_root / ("mono" if variant == "plain" else f"mono-{variant}")
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"{family.replace(' ', '')}-{style}.ttf"
    font.save(out)
    if hint:
        # 英字だけヒンティングする(和文は fallback_script=none で対象外)
        tmp = out.with_suffix(".unhinted.ttf")
        out.rename(tmp)
        ttfautohint.ttfautohint(in_file=str(tmp), out_file=str(out), default_script="latn",
                                fallback_script="none", no_info=True)
        tmp.unlink()
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", help="カンマ区切りの Maple スタイル名 (例: Regular,Italic)")
    ap.add_argument("--variants", default="plain,nf,jpdoc")
    ap.add_argument("--no-hint", action="store_true", help="ttfautohint をかけない(試作の高速化用)")
    ap.add_argument("--set", action="append", default=[], metavar="KEY=VALUE",
                    help="config.toml の値を一時的に上書き (例: mono.scale_kana=1.2)。-j 1 のときだけ使える")
    ap.add_argument("--outdir", help="出力先 (既定: build/fonts)")
    ap.add_argument("-j", type=int, default=6)
    a = ap.parse_args()
    out_root = pathlib.Path(a.outdir).resolve() if a.outdir else OUT
    for item in a.set:
        key, value = item.split("=", 1)
        *path, leaf = key.split(".")
        node = CFG
        for k in path:
            node = node[k]
        node[leaf] = tomllib.loads(f"v = {value}")["v"]
    if a.set and a.j != 1:
        ap.error("--set は -j 1 と一緒に使う(並列の子プロセスには上書きが渡らない)")
    only = set(a.only.split(",")) if a.only else None
    variants = a.variants.split(",")
    # 出力先はジョブごとに渡す(並列の子プロセスはモジュールを読み直すので、グローバル変数の書き換えは届かない)
    jobs = [(s, k, w, i, v, not a.no_hint, out_root)
            for s, k, w, i in styles() if not only or s in only for v in variants]
    if a.j == 1:
        for job in jobs:
            print(build_one(job))
        return
    with ProcessPoolExecutor(a.j) as ex:
        for out in ex.map(build_one, jobs):
            print(out.relative_to(ROOT) if out.is_relative_to(ROOT) else out)


if __name__ == "__main__":
    main()
