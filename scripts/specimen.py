"""ビルド済みの等幅フォントを見本テキストでサブセット化して、見本帳の単一 HTML を書き出す。"""

import base64
import io
import json

from fontTools import subset
from fontTools.ttLib import TTFont

from fontutil import CFG, ROOT

TEMPLATE = ROOT / "scripts" / "specimen.template.html"
OUT = ROOT / "build" / "kiwi-specimen.html"
WEIGHT_VALUES = {"Thin": 100, "ExtraLight": 200, "Light": 300, "Regular": 400,
                 "Medium": 500, "SemiBold": 600, "Bold": 700, "ExtraBold": 800}


def woff2_b64(path, text):
    font = TTFont(path)
    opts = subset.Options()
    opts.flavor = "woff2"
    # aalt(代替字形の一覧)と hwid は大量のグリフを引き込むので見本帳には入れない
    tags = {fr.FeatureTag for fr in font["GSUB"].table.FeatureList.FeatureRecord}
    opts.layout_features = sorted(tags - {"aalt", "hwid"})
    opts.name_IDs = ["*"]
    opts.notdef_outline = True
    opts.hinting = False  # ブラウザ表示用なのでヒンティングは不要
    s = subset.Subsetter(opts)
    s.populate(text=text)
    s.subset(font)
    buf = io.BytesIO()
    font.save(buf)
    return base64.b64encode(buf.getvalue()).decode()


def maple_features(path):
    """Maple 由来の cv/ss とその説明(和文用の cv9x は除く)"""
    font = TTFont(path)
    name = font["name"]
    feats = {}
    for fr in font["GSUB"].table.FeatureList.FeatureRecord:
        tag = fr.FeatureTag
        if not (tag.startswith("cv") or tag.startswith("ss")) or tag.startswith("cv9") or tag == "ss20":
            continue
        p = fr.Feature.FeatureParams
        nid = getattr(p, "FeatUILabelNameID", getattr(p, "UINameID", None))
        label = name.getDebugName(nid) if nid else ""
        feats[tag] = (label or "").split(": ", 1)[-1].replace("$$$", " ")
    return sorted(feats.items())


def main():
    family = CFG["family"]
    html = TEMPLATE.read_text().replace("__FAMILY__", family).replace("__PROP_FAMILY__", CFG["prop_family"])
    ps = family.replace(" ", "")
    prop_ps = CFG["prop_family"].replace(" ", "")
    mono = ROOT / "build" / "fonts" / "mono"
    html = html.replace("/*__MAPLE_FEATS__*/[]", json.dumps(maple_features(mono / f"{ps}-Regular.ttf"), ensure_ascii=False))
    extra = "".join(chr(c) for c in range(0x20, 0x7F))
    extra += "".join(chr(c) for c in range(0x3041, 0x3097)) + "".join(chr(c) for c in range(0x30A1, 0x30FB))
    text = html + extra
    faces = []
    for weight, value in WEIGHT_VALUES.items():
        for italic in (False, True):
            style = ("Italic" if weight == "Regular" else f"{weight}Italic") if italic else weight
            data = woff2_b64(mono / f"{ps}-{style}.ttf", text)
            faces.append(f"@font-face{{font-family:'PK';font-weight:{value};font-style:{'italic' if italic else 'normal'};"
                         f"src:url(data:font/woff2;base64,{data}) format('woff2');font-display:block}}")
    prop = ROOT / "build" / "fonts" / "prop"
    # プロポーショナル版はその欄の文章にだけ使うので、埋め込む文字を絞る
    start = html.index('class="sample wrap-text prop-sample"')
    prop_text = html[start:html.index("</section>", start)] + extra
    for weight, value in WEIGHT_VALUES.items():
        for italic in (False, True):
            style = ("Italic" if weight == "Regular" else f"{weight}Italic") if italic else weight
            data = woff2_b64(prop / f"{prop_ps}-{style}.ttf", prop_text)
            faces.append(f"@font-face{{font-family:'PKP';font-weight:{value};font-style:{'italic' if italic else 'normal'};"
                         f"src:url(data:font/woff2;base64,{data}) format('woff2');font-display:block}}")
    OUT.write_text(html.replace("/*__FONTFACES__*/", "\n".join(faces)))
    print(OUT, f"{OUT.stat().st_size / 1e6:.2f} MB")


if __name__ == "__main__":
    main()
