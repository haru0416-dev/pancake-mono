"""等幅版(build/fonts/mono)から、文章・デザイン用のプロポーショナル版を作る。

- 英字(Maple)は輪郭の左右の凹みを測って、見かけの余白がそろうよう字間を決め直す。数字は同じ幅にする
- かな・約物は字面に合わせて詰め、漢字と全角英数記号は全角(prop.full)の中央に置く
- 等幅前提のリガチャと Texture Healing は外し、和文のつなぎ(ーーー・〜〜〜)だけ残す
- カーニングと和欧間のアキを足す(kerning.py)

usage: uv run python scripts/build_prop.py [--only Regular,Italic] [-j 6]
出力: build/fonts/prop/<prop_family>-<Style>.ttf
"""

import argparse
import math
import re
import unicodedata
from concurrent.futures import ProcessPoolExecutor

from fontTools.feaLib.builder import addOpenTypeFeaturesFromString
from fontTools.pens.recordingPen import DecomposingRecordingPen
from fontTools.pens.ttGlyphPen import TTGlyphPen
from fontTools.ttLib import TTFont

from fontutil import CFG, ROOT, FlattenPen
from kerning import kerning_fea

MONO = ROOT / "build" / "fonts" / "mono"
OUT = ROOT / "build" / "fonts" / "prop"

LIGATURE_FEATURES = ("calt", "liga", "dlig")
DIGITS = "0123456789"


def is_punct(cp):
    return (0x3000 <= cp <= 0x303F or cp == 0x30FB or 0xFF61 <= cp <= 0xFF65
            or 0x2010 <= cp <= 0x2027 or 0x2030 <= cp <= 0x205E)


OPEN_FW = {0xFF08, 0xFF3B, 0xFF5B, 0xFF5F}
CLOSE_FW = {0xFF09, 0xFF3D, 0xFF5D, 0xFF60}


def is_fw_alnum(cp):
    return 0xFF10 <= cp <= 0xFF19 or 0xFF21 <= cp <= 0xFF3A or 0xFF41 <= cp <= 0xFF5A


def cp_from_name(name):
    m = re.search(r"uni([0-9A-F]{4,6})", name)
    return int(m.group(1), 16) if m else None


def is_kana(cp):
    if cp == 0x30FB:  # 中黒は約物として扱う
        return False
    return 0x3040 <= cp <= 0x30FF or 0x31F0 <= cp <= 0x31FF or 0xFF66 <= cp <= 0xFF9F


class PropBuilder:
    def __init__(self, path, italic):
        self.font = TTFont(path)
        self.glyf = self.font["glyf"]
        self.hmtx = self.font["hmtx"]
        m, p = CFG["mono"], CFG["prop"]
        self.cy = m["center_y"]
        self.p = p
        self.skew = math.tan(math.radians(m["italic_angle"])) if italic else 0.0
        os2 = self.font["OS/2"]
        self.xh, self.cap = os2.sxHeight, os2.sCapHeight

    def decompose_all(self):
        gs = self.font.getGlyphSet()
        for name in self.font.getGlyphOrder():
            g = self.glyf[name]
            if g.isComposite():
                rec = DecomposingRecordingPen(gs)
                gs[name].draw(rec)
                pen = TTGlyphPen(None)
                rec.replay(pen)
                new = pen.glyph()
                new.recalcBounds(self.glyf)
                self.glyf[name] = new
        if "fpgm" in self.font:
            for tag in ("fpgm", "prep", "cvt "):
                if tag in self.font:
                    del self.font[tag]
            for name in self.font.getGlyphOrder():
                g = self.glyf[name]
                if hasattr(g, "program"):
                    g.program.fromBytecode(b"")

    def flat(self, name):
        pen = FlattenPen(self.skew)
        self.font.getGlyphSet()[name].draw(pen)
        return pen

    def shift(self, name, dx, width):
        g = self.glyf[name]
        if g.numberOfContours > 0 and dx:
            g.coordinates.translate((round(dx), 0))
            g.recalcBounds(self.glyf)
        self.hmtx[name] = (round(width), getattr(g, "xMin", 0))

    def space_latin(self, name):
        """輪郭の左右の凹みを測り、見かけの余白が latin_sb になるよう幅を決める"""
        pen = self.flat(name)
        b = pen.bounds()
        if not b:
            return
        x0, y0, x1, y1 = b
        top = self.xh if y1 <= self.xh + 60 else self.cap
        lo, hi = 0, top
        if y1 < lo + 40 or y0 > hi - 40:  # 測定域の外にある記号(_ ^ ` など)は字面で測る
            lo, hi = y0, y1
        height = max(hi - lo, 1)
        limit = height * self.p["depth_limit"]
        n = 24
        left = right = 0.0
        for i in range(n):
            y = lo + (i + 0.5) * height / n
            prof = pen.profile(y)
            if prof:
                left += min(prof[0] - x0, limit)
                right += min(x1 - prof[1], limit)
            else:
                left += limit
                right += limit
        target, min_sb = self.p["latin_sb"], self.p["min_sb"]
        lsb = max(target - left / n, min_sb)
        rsb = max(target - right / n, min_sb)
        self.shift(name, lsb - x0, (x1 - x0) + lsb + rsb)

    def scale_cell(self, name, width):
        k = self.p["full"] / CFG["mono"]["full"]
        g = self.glyf[name]
        if g.numberOfContours > 0:
            g.coordinates.scale((k, 1))
            g.recalcBounds(self.glyf)
        self.hmtx[name] = (round(width * k), getattr(g, "xMin", 0))

    def center(self, name, width):
        pen = self.flat(name)
        b = pen.bounds()
        if not b:
            self.hmtx[name] = (round(width), 0)
            return
        self.shift(name, (width - (b[2] - b[0])) / 2 - b[0], width)

    def voiced_base(self, cp, cmap):
        """濁音・半濁音のかなに対応する清音のグリフ(字幅を清音に合わせるため)"""
        nfd = unicodedata.normalize("NFD", chr(cp))
        if len(nfd) == 2 and nfd[1] in "\u3099\u309a" and ord(nfd[0]) in cmap:
            return cmap[ord(nfd[0])]
        return None

    def fit(self, name, sb, voiced_base=None):
        pen = self.flat(name)
        b = pen.bounds()
        if not b:
            return
        x_max = b[2]
        if voiced_base and voiced_base in self.mono_bounds:
            # 濁点の張り出しは一部だけ字幅に含め、残りは隣の字の余白へはみ出させる
            base_max = self.mono_bounds[voiced_base][2]
            if base_max < x_max:
                x_max = base_max + (x_max - base_max) * self.p["dakuten_width"]
        self.shift(name, sb - b[0], (x_max - b[0]) + 2 * sb)

    def half_punct(self, name, mono_width):
        """全角の約物: 字面が片側に寄っているものは、その側の半角に収める(palt 相当)"""
        pen = self.flat(name)
        b = pen.bounds()
        full = self.p["full"]
        if not b:
            self.hmtx[name] = (full // 2, 0)
            return
        cell_mid = mono_width / 2
        margin = (mono_width - full) / 2  # 等幅の全角セルと和文ボディの差
        if b[2] - b[0] > full / 2:
            self.fit(name, self.p["punct_sb"])
        elif b[2] <= cell_mid + 20:
            self.shift(name, -margin, full / 2)
        elif b[0] >= cell_mid - 20:
            self.shift(name, -margin - full / 2, full / 2)
        else:
            self.center(name, full / 2)

    def align_half(self, name, side):
        pen = self.flat(name)
        b = pen.bounds()
        half, sb = self.p["full"] / 2, self.p["punct_sb"]
        width = max(half, (b[2] - b[0]) + 2 * sb)
        if side == "right":
            self.shift(name, width - sb - b[2], width)
        else:
            self.shift(name, sb - b[0], width)

    def build(self):
        self.decompose_all()
        cmap = self.font.getBestCmap()
        cp_of = {}
        for cp, name in cmap.items():
            cp_of.setdefault(name, cp)
        full = self.p["full"]
        mono_half = CFG["mono"]["half"]
        digits = {cmap[ord(d)] for d in DIGITS}
        # 字幅を決め直す前の字面(濁点付きのかなの幅を清音に合わせるのに使う)
        self.mono_bounds = {}
        for cp in cmap:
            if is_kana(cp):
                b = self.flat(cmap[cp]).bounds()
                if b:
                    self.mono_bounds[cmap[cp]] = b

        for name in self.font.getGlyphOrder():
            width = self.hmtx[name][0]
            cp = cp_of.get(name)
            base_cp = cp if cp is not None else cp_from_name(name)
            if name.startswith("sym.lt3"):
                pass  # <3 のハートは前のセルに広がる前提なので幅を変えない
            elif name.startswith("sym.") and width == CFG["mono"]["full"]:
                self.center(name, full)
            elif ".join" in name or cp == 0x2015:
                # 和文のつなぎ用の字形はセルいっぱいに描かれているので、セルごと全角幅へ縮める
                self.scale_cell(name, width)
            elif name.startswith("jp.idsp") or (base_cp is not None and is_fw_alnum(base_cp)):
                self.center(name, full)
            elif base_cp in OPEN_FW:
                # 全角の開き括弧は右の半角へ、閉じ括弧は左の半角へ
                self.align_half(name, "right")
            elif base_cp in CLOSE_FW:
                self.align_half(name, "left")
            elif name.startswith("fw."):
                self.fit(name, self.p["fw_punct_sb"])
            elif name.startswith("jp."):
                if base_cp is not None and is_kana(base_cp):
                    self.fit(name, self.p["kana_sb"], self.voiced_base(base_cp, cmap))
                elif base_cp is not None and is_punct(base_cp) and width > mono_half:
                    self.half_punct(name, width)
                elif width == mono_half:
                    self.fit(name, self.p["punct_sb"])
                else:
                    # 漢字・記号・縦書き用の字形は全角の中央へ
                    self.center(name, full)
            elif cp is not None and 0x2500 <= cp <= 0x259F:
                pass  # 罫線・ブロック要素はつながるよう等幅のまま
            elif width == mono_half and name not in digits:
                self.space_latin(name)

        # 数字は桁が揃うよう同じ幅(最も広い数字に合わせる)
        if self.p["tabular_digits"]:
            for name in digits:
                self.space_latin(name)
            w = max(self.hmtx[n][0] for n in digits)
            for name in digits:
                self.center(name, w)
        for cp in (0x20, 0xA0):
            if cp in cmap:
                self.hmtx[cmap[cp]] = (self.p["space"], 0)

        self.drop_ligatures()
        self.fix_metrics()
        self.add_kerning(cmap)
        return self.font

    def add_kerning(self, cmap):
        def is_kanji(cp):
            return 0x3400 <= cp <= 0x4DBF or 0x4E00 <= cp <= 0x9FFF or 0xF900 <= cp <= 0xFAFF or 0x3005 <= cp <= 0x3007

        kana_cps = [cp for cp in cmap if is_kana(cp) and cp < 0xFF00]
        wa = {cmap[cp] for cp in cmap if (is_kana(cp) and cp < 0xFF00) or is_kanji(cp)}
        ou = {cmap[ord(c)] for c in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"}
        fea, n_latin, n_kana = kerning_fea(self.font, kana_cps, wa, ou)
        addOpenTypeFeaturesFromString(self.font, fea, tables=["GPOS"])
        self.kern_stats = (n_latin, n_kana)

    def drop_ligatures(self):
        """等幅前提のリガチャと Texture Healing を外す。和文のつなぎ(.join を出力する lookup)は残す"""
        lookups = self.font["GSUB"].table.LookupList.Lookup

        def outputs(li):
            names = set()
            for st in lookups[li].SubTable:
                st = getattr(st, "ExtSubTable", st)
                names |= set(getattr(st, "mapping", {}).values())
            return names

        def nested(li):
            found = []
            for st in lookups[li].SubTable:
                st = getattr(st, "ExtSubTable", st)
                for rs in getattr(st, "ChainSubRuleSet", None) or []:
                    for r in rs.ChainSubRule:
                        found += [x.LookupListIndex for x in r.SubstLookupRecord]
                found += [x.LookupListIndex for x in getattr(st, "SubstLookupRecord", None) or []]
                for cs in getattr(st, "ChainSubClassSet", None) or []:
                    for r in (cs.ChainSubClassRule if cs else []):
                        found += [x.LookupListIndex for x in r.SubstLookupRecord]
            return found

        def is_join(li):
            return any(any(".join" in n for n in outputs(n_li)) for n_li in nested(li))

        for fr in self.font["GSUB"].table.FeatureList.FeatureRecord:
            if fr.FeatureTag in LIGATURE_FEATURES:
                fr.Feature.LookupListIndex = [li for li in fr.Feature.LookupListIndex if is_join(li)]
                fr.Feature.LookupCount = len(fr.Feature.LookupListIndex)

    def fix_metrics(self):
        full = self.p["full"]
        top = self.cy + full / 2
        vmtx = self.font["vmtx"]
        for name in self.font.getGlyphOrder():
            g = self.glyf[name]
            tsb = round(top - g.yMax) if g.numberOfContours > 0 else 0
            vmtx[name] = (full, tsb)
        vhea = self.font["vhea"]
        vhea.ascent, vhea.descent, vhea.advanceHeightMax = full // 2, -full // 2, full
        widths = [w for w, _ in self.hmtx.metrics.values() if w]
        os2 = self.font["OS/2"]
        os2.xAvgCharWidth = round(sum(widths) / len(widths))
        os2.panose.bProportion = 3
        self.font["post"].isFixedPitch = 0


def rename_prop(font, family):
    name = font["name"]
    base = CFG["family"]
    for rec in name.names:
        if rec.nameID in (1, 3, 4, 6, 16):
            s = rec.toUnicode()
            if rec.nameID in (3, 6):
                s = s.replace(f"{base.replace(' ', '')}-", f"{family.replace(' ', '')}-")
            else:
                s = s.replace(base, family, 1)
            rec.string = s
        elif rec.nameID == 10:
            rec.string = CFG["prop_description"]


def build_one(job):
    style, italic = job
    family = CFG["family"].replace(" ", "")
    builder = PropBuilder(MONO / f"{family}-{style}.ttf", italic)
    font = builder.build()
    print(f"{style}: カーニング 英字 {builder.kern_stats[0]} 組、かな {builder.kern_stats[1]} 組")
    prop_family = CFG["prop_family"]
    rename_prop(font, prop_family)
    OUT.mkdir(parents=True, exist_ok=True)
    out = OUT / f"{prop_family.replace(' ', '')}-{style}.ttf"
    font.save(out)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--only")
    ap.add_argument("-j", type=int, default=6)
    a = ap.parse_args()
    only = set(a.only.split(",")) if a.only else None
    jobs = []
    for weight in CFG["weights"]:
        jobs.append((weight, False))
        jobs.append(("Italic" if weight == "Regular" else f"{weight}Italic", True))
    jobs = [j for j in jobs if not only or j[0] in only]
    with ProcessPoolExecutor(a.j) as ex:
        for out in ex.map(build_one, jobs):
            print(out.relative_to(ROOT))


if __name__ == "__main__":
    main()
