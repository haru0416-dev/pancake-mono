"""ビルドしたフォントの検証。失敗があれば終了コード 1。

置かれたフォルダで種類を見分ける(prop/ はプロポーショナル版、mono-jpdoc/ は JPDOC 版、それ以外は等幅版)。
usage: uv run python scripts/check.py build/fonts/*/*.ttf
"""

import collections
import pathlib
import subprocess
import sys

import ots
import uharfbuzz as hb
from fontTools.pens.boundsPen import BoundsPen
from fontTools.ttLib import TTFont


def jis_chars():
    out = []
    for hi in range(0x88, 0xEB):
        for lo in list(range(0x40, 0x7F)) + list(range(0x80, 0xFD)):
            try:
                c = bytes([hi, lo]).decode("shift_jis")
            except UnicodeDecodeError:
                continue
            if len(c) == 1 and ord(c) >= 0x4E00:
                out.append(c)
    return out


JIS = jis_chars()


def shape(blob_face, text, features=None, direction="ltr"):
    font = hb.Font(blob_face)
    buf = hb.Buffer()
    buf.add_str(text)
    buf.guess_segment_properties()
    buf.direction = direction
    hb.shape(font, buf, features or {})
    names = [font.glyph_to_string(i.codepoint) for i in buf.glyph_infos]
    return names, [p.x_advance for p in buf.glyph_positions]


def check(path, prop=False):
    errors = []
    try:
        ots.sanitize(path, "/dev/null", check=True, capture_output=True)
    except subprocess.CalledProcessError as e:
        errors.append(f"OTS: {e.stderr.decode().strip()[:300]}")

    f = TTFont(path)
    cmap = f.getBestCmap()
    hmtx = f["hmtx"]
    missing = [c for c in JIS if ord(c) not in cmap]
    if missing:
        errors.append(f"JIS 漢字が {len(missing)} 字欠落: {''.join(missing[:20])}")
    for cp in list(range(0x3041, 0x3097)) + list(range(0x30A1, 0x30FB)) + [0x3000, 0xFF21, 0xFF5E]:
        if cp not in cmap:
            errors.append(f"U+{cp:04X} が無い")

    if prop:
        def w(ch):
            return hmtx[cmap[ord(ch)]][0]
        if not (w("i") < w("n") < w("m")):
            errors.append(f"英字がプロポーショナルになっていない i={w('i')} n={w('n')} m={w('m')}")
        if len({w(d) for d in "0123456789"}) != 1:
            errors.append("数字の幅が揃っていない")
        if not (w("い") < w("漢") and w("。") < w("漢")):
            errors.append("かな・約物が詰まっていない")
    else:
        widths = collections.Counter(hmtx[g][0] for g in set(cmap.values()))
        odd = {w: n for w, n in widths.items() if w not in (0, 600, 1200)}
        if odd:
            errors.append(f"600/1200 以外の幅: {odd}")
        for ch, want in [("A", 600), ("あ", 1200), ("漢", 1200), ("ｱ", 600), ("Ａ", 1200), ("　", 1200)]:
            if hmtx[cmap[ord(ch)]][0] != want:
                errors.append(f"{ch!r} の幅 {hmtx[cmap[ord(ch)]][0]} != {want}")

    face = hb.Face(hb.Blob(pathlib.Path(path).read_bytes()))

    def differs(text, feats, direction="ltr"):
        return shape(face, text, {}, direction)[0] != shape(face, text, feats, direction)[0]

    lig_on = shape(face, "=>", {"calt": False})[0] != shape(face, "=>")[0]
    if not prop and not lig_on:
        errors.append("Maple のリガチャ(calt '=>')が効いていない")
    if prop and lig_on:
        errors.append("プロポーショナル版でリガチャが残っている")
    vert_default = shape(face, "「ー", {}, "ttb")[0]
    if vert_default == shape(face, "「ー", {"vert": False}, "ttb")[0]:
        errors.append("縦書き(vert)が効いていない")
    for feat, text in [("cv91", "　"), ("cv92", "カ"), ("cv93", "Ａ"), ("cv94", "ばパ"), ("cv01", "@a&$"),
                       ("hwid", "アイ"), ("jp78", "唖"), ("trad", "国"), ("nlck", "嘘")]:
        if not differs(text, {feat: True}):
            errors.append(f"{feat} が {text!r} に効いていない")
    tags = {fr.FeatureTag for fr in f["GSUB"].table.FeatureList.FeatureRecord}
    for tag in ("jp78", "jp90", "trad", "nlck", "expt", "hwid", "vert", "vrt2"):
        if tag not in tags:
            errors.append(f"Kiwi Maru の {tag} が引き継がれていない")
    for text, part in [("ラーーーメン", ".join.mid"), ("ああ〜〜ん", ".join.sta")]:
        g = shape(face, text)[0]
        if not any(part in n for n in g):
            errors.append(f"和文のつなぎが {text!r} で効いていない: {g}")
    if any(".join" in n for n in shape(face, "ーー", {}, "ttb")[0]):
        errors.append("縦書きで長音のつなぎがかかっている")
    if any(".join" in n for n in shape(face, "ラーメン")[0]):
        errors.append("長音1つでもつなぎがかかっている")
    if prop:
        def kern(text):
            """カーニングで変わった送り幅の合計"""
            return sum(shape(face, text)[1]) - sum(shape(face, text, {"kern": False})[1])

        if not (kern("AV") < 0 and kern("To") < 0):
            errors.append("英字のカーニング(AV, To)が効いていない")
        if kern("11") != 0 or kern("rn") != 0 or kern("nn") != 0:
            errors.append("詰めない組(数字同士、rn、nn)にカーニングが入っている")
        if not (kern("日A") > 0 and kern("A日") > 0):
            errors.append("和欧間のアキが入っていない")
    if not prop:
        healed = shape(face, "mint if im")[0]
        if not any(".heal" in n for n in healed):
            errors.append(f"Texture Healing が効いていない: {healed}")
        all_widths = {hmtx[g][0] for g in f.getGlyphOrder()}
        if not all_widths <= {0, 600, 1200}:
            errors.append(f"cmap に無いグリフに 600/1200 以外の幅: {sorted(all_widths - {0, 600, 1200})[:10]}")
        jpdoc = "/mono-jpdoc/" in path
        for ch, plain_w, doc_w in [("★", 600, 1200), ("♡", 600, 1200), ("♥", 600, 1200), ("○", 600, 1200),
                                   ("①", 600, 1200), ("※", 600, 1200), ("⬛", 1200, 1200), ("⌘", 600, 600),
                                   ("◐", 600, 600), ("◓", 600, 600), ("✻", 600, 600), ("✳", 600, 600),
                                   ("✢", 600, 600), ("✽", 600, 600), ("⏺", 600, 600), ("ℹ", 600, 600)]:
            w = hmtx[cmap[ord(ch)]][0]
            if w != (doc_w if jpdoc else plain_w):
                errors.append(f"{ch} の幅が {w}")
        if not cmap[ord("♡")].startswith("sym.") or not cmap[ord("♥")].startswith("sym."):
            errors.append("♡♥ が描き直した記号になっていない")
        if "sym.lt3" not in shape(face, "<3", {"ss20": True})[0]:
            errors.append("ss20 (<3 → ♡) が効いていない")
        if shape(face, "<3")[0] == shape(face, "<3", {"ss20": True})[0]:
            errors.append("ss20 が既定で効いている/効いていない")
        if "cv95" in tags:
            errors.append("cv95 が残っている")
        arrow = hmtx[cmap[ord("→")]][0]
        if arrow != (1200 if "/mono-jpdoc/" in path else 600):
            errors.append(f"→ の幅が {arrow}")
        arrow_seq = shape(face, "<----->")[0]
        if not any(n.endswith(".seq") for n in arrow_seq):
            errors.append(f"無限矢印リガチャが効いていない: {arrow_seq}")
        gs = f.getGlyphSet()
        # 矢じりを横線と別のグリフに分けてあるか(Maple Mono issue #508 の横線のずれ)
        for text in ("==>", "<--", "|=>", "=<="):
            names = shape(face, text)[0]
            heads = [n for n in names if n.endswith(".head")]
            if not heads:
                errors.append(f"{text} の矢じりが横線と分かれていない: {names}")
            elif any(hmtx[n][0] != 0 for n in heads):
                errors.append(f"{text} の矢じりのグリフに幅がある: {[(n, hmtx[n][0]) for n in heads]}")
        if "Italic" in path:
            # イタリックの部品は、横線の右端をセルの境界の先まで伸ばしてある(継ぎ目の薄い縦線を防ぐ)
            # 横線ごとに見る(上の線は斜めに右へはみ出すので、グリフ全体の右端では下の線の不足を見逃す)
            glyf = f["glyf"]
            for name in ("equal.mid.seq", "hyphen.mid.seq", "equal.sta.seq"):
                g = glyf[name]
                coords, start = g.getCoordinates(glyf)[0], 0
                for end in g.endPtsOfContours:
                    xs = [x for x, _ in coords[start:end + 1]]
                    start = end + 1
                    if max(xs) < hmtx[name][0]:
                        errors.append(f"{name} の横線の右端がセルの境界に届いていない: {max(xs)}")
        bars = BoundsPen(gs)
        gs["equal.mid.seq"].draw(bars)
        for name in ("greater_equal.end.seq", "less_equal.sta.seq"):
            b = BoundsPen(gs)
            gs[name].draw(b)
            if (round(b.bounds[1]), round(b.bounds[3])) != (round(bars.bounds[1]), round(bars.bounds[3])):
                errors.append(f"{name} の横線の帯が = の部品と違う: {b.bounds} / {bars.bounds}")

        def thick(ch):
            b = BoundsPen(gs)
            gs[cmap[ord(ch)]].draw(b)
            return b.bounds[3] - b.bounds[1]
        ratio = thick("一") / thick("-")
        if not 0.85 <= ratio <= 1.0:
            errors.append(f"和文と英字の線の太さの比が {ratio:.2f}(0.85〜1.0 を期待)")
        if "fpgm" not in f:
            errors.append("英字のヒンティング(fpgm)が無い")
    version = f["name"].getDebugName(5) or ""
    if not version.startswith(f"Version {f['head'].fontRevision:.1f}"):
        errors.append(f"head の版 {f['head'].fontRevision} と名前の版 {version!r} が食い違っている")
    name = f["name"]
    for fr in f["GSUB"].table.FeatureList.FeatureRecord:
        params = fr.Feature.FeatureParams
        label = getattr(params, "FeatUILabelNameID", getattr(params, "UINameID", None))
        if label is not None and not name.getDebugName(label):
            errors.append(f"{fr.FeatureTag} の機能名(name ID {label})が無い")
    return errors, len(f.getGlyphOrder())


def main():
    failed = False
    for path in sys.argv[1:]:
        prop = "/prop/" in path
        errors, n = check(path, prop)
        fc = subprocess.run(["fc-scan", "--format", "%{spacing}", path], capture_output=True, text=True).stdout.strip()
        if prop and fc not in ("", "0"):
            errors.append(f"fontconfig spacing={fc!r}(プロポーショナルを期待)")
        if not prop and fc != "90":
            errors.append(f"fontconfig spacing={fc!r}(90=dual を期待)")
        status = "OK " if not errors else "NG "
        print(f"{status}{path} ({n} glyphs)")
        for e in errors:
            print("    -", e)
        failed |= bool(errors)
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
