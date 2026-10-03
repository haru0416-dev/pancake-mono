"""デザインの調整候補をビルドして、見比べるための比較ページを書き出す。

usage: uv run python scripts/tune.py [-j 4]
出力: build/tune/<項目>-<案>/mono/*.ttf, build/tune-board.html
"""

import argparse
import base64
import html
import io
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor

from fontTools import subset
from fontTools.ttLib import TTFont

from fontutil import CFG, ROOT

TUNE = ROOT / "build" / "tune"
OUT = ROOT / "build" / "tune-board.html"

m, sym = CFG["mono"], CFG["symbols"]
k0, kana0 = m["scale_kanji"], m["scale_kana"]
ratio0 = kana0 / k0

# (項目キー, 見出し, 説明, 見本の文章, [(案の名前, [上書き])...]) 真ん中が現在の値
PARAMS = [
    ("size", "和文の大きさ", "英字に対する和文全体の大きさ。かなと漢字の比率は保つ",
     "// 日本語とEnglish\nconst 名前 = \"キウイ\";\nあいう 漢字 カナ Ham",
     [("小さめ", [f"mono.scale_kanji={k0 - 0.05:.3f}", f"mono.scale_kana={(k0 - 0.05) * ratio0:.3f}"]),
      ("現在", []),
      ("大きめ", [f"mono.scale_kanji={k0 + 0.05:.3f}", f"mono.scale_kana={(k0 + 0.05) * ratio0:.3f}"])]),
    ("kana", "かなと漢字の比率", "漢字に対するかなの大きさ。大きいほど字面がそろい、小さいほど教科書体らしい抑揚が残る",
     "吾輩は猫である。名前はまだ無い。\nどこで生れたかとんと見当がつかぬ。\nカタカナとひらがなと漢字",
     [("控えめ", [f"mono.scale_kana={k0 * 1.0:.3f}"]), ("現在", []), ("大きめ", [f"mono.scale_kana={k0 * 1.1:.3f}"])]),
    ("center", "和文の上下位置", "英字のベースラインに対する和文の高さ。下げるほど英字の小文字に寄る",
     "Maple丸ゴでcodeを書く\nxyz あいう ABC 漢字 gjpq\n(かっこ) [配列] {波}",
     [("下げる", ["mono.center_y=350"]), ("現在", []), ("上げる", ["mono.center_y=390"])]),
    ("weight", "和文の太さの割り当て", "Maple の Regular に合わせる Kiwi のウェイト。Medium にすると和文が濃くなる",
     "const 名前 = \"キウイ\";\n日本語の文章と English text を\n同じ濃さで読めるか",
     [("Light", ["weights.Regular=\"Light\""]), ("現在", []), ("Medium", ["weights.Regular=\"Medium\""])]),
    ("dakuten", "濁点・半濁点の大きさ", "濁点と半濁点の拡大率",
     "ばびぶべぼ ぱぴぷぺぽ\nガギグゲゴ パピプペポ\nバーベキュー ポップコーン",
     [("1.18倍", ["dakuten.scale=1.18"]), ("現在", []), ("1.38倍", ["dakuten.scale=1.38"])]),
    ("healing", "Texture Healing の強さ", "細い字と広い字の間合いの調整量",
     "minimum will film limit\nWilliam imitation mint\nfunction illuminate()",
     [("なし", ["healing.narrow_shift=0", "healing.other_shift=0", "healing.wide_expand=0"]),
      ("現在", []),
      ("強め", ["healing.narrow_shift=72", "healing.other_shift=48", "healing.wide_expand=68"])]),
    ("longvowel", "長音「ー」の短さ", "漢数字「一」と区別するための縮め具合",
     "ラーメン コーヒー\n一ー 一二三 ー一ー\nスーパーマーケット",
     [("短め", ["distinguish.long_vowel_x=0.74"]), ("現在", []), ("長め", ["distinguish.long_vowel_x=0.9"])]),
    ("symbols", "半角記号の大きさ", "★♡○ などの半角記号の大きさ",
     "★☆♡♥♠♣○●□■◇◆△▲♪♫※\nI ★ fonts  ○ OK  ※ note\n✻ Thinking… ⏺ Read ◐◓◑◒",
     [("小さめ", [f"symbols.half_radius={sym['half_radius'] - 30}"]), ("現在", []),
      ("大きめ", [f"symbols.half_radius={sym['half_radius'] + 30}"])]),
    ("idsp", "全角スペースの印", "全角スペースを示す点の枠",
     "全角[　]スペース\n名前　＝　値\n　　インデント",
     [("点が多い", ["ideographic_space.dots=24", "ideographic_space.dot={ Light = 46, Regular = 56, Medium = 68 }"]),
      ("現在", []),
      ("点が少ない", ["ideographic_space.dots=12", "ideographic_space.dot={ Light = 80, Regular = 96, Medium = 116 }"])]),
]


def build_variant(key, label, sets):
    out = TUNE / f"{key}-{label}"
    font = out / "mono" / f"{CFG['family'].replace(' ', '')}-Regular.ttf"
    if not sets:
        out = TUNE / "current"
        font = out / "mono" / f"{CFG['family'].replace(' ', '')}-Regular.ttf"
    cmd = [sys.executable, str(ROOT / "scripts" / "build.py"), "--only", "Regular", "--variants", "plain",
           "--no-hint", "-j", "1", "--outdir", str(out)]
    for s in sets:
        cmd += ["--set", s]
    subprocess.run(cmd, check=True, capture_output=True)
    return font


def woff2_b64(path, text):
    font = TTFont(path)
    opts = subset.Options()
    opts.flavor = "woff2"
    opts.layout_features = ["calt", "ccmp", "locl", "vert"]
    opts.hinting = False
    opts.notdef_outline = True
    s = subset.Subsetter(opts)
    s.populate(text=text)
    s.subset(font)
    buf = io.BytesIO()
    font.save(buf)
    return base64.b64encode(buf.getvalue()).decode()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("-j", type=int, default=4)
    a = ap.parse_args()
    jobs = {}
    for key, _, _, _, options in PARAMS:
        for label, sets in options:
            jobs[(key, label)] = sets
    unique = {}
    with ThreadPoolExecutor(a.j) as ex:
        futures = {}
        for (key, label), sets in jobs.items():
            sig = tuple(sets)
            if sig not in futures:
                futures[sig] = ex.submit(build_variant, key, label, sets)
        for sig, fut in futures.items():
            unique[sig] = fut.result()

    faces, sections = [], []
    n = 0
    for key, title, desc, text, options in PARAMS:
        cards = []
        for i, (label, sets) in enumerate(options):
            fam = f"t{n}"
            n += 1
            data = woff2_b64(unique[tuple(sets)], text + "現在案ABC")
            faces.append(f"@font-face{{font-family:'{fam}';src:url(data:font/woff2;base64,{data}) format('woff2');font-display:block}}")
            letter = "ABC"[i]
            badge = '<span class="now">現在</span>' if not sets else ""
            cards.append(f'<figure class="opt{" is-now" if not sets else ""}"><figcaption><b>{key}-{letter}</b> {html.escape(label)} {badge}</figcaption>'
                         f'<pre style="font-family:\'{fam}\',monospace">{html.escape(text)}</pre></figure>')
        sections.append(f'<section><h2>{html.escape(title)} <code>{key}</code></h2><p class="desc">{html.escape(desc)}</p>'
                        f'<div class="opts">{"".join(cards)}</div></section>')
    page = TEMPLATE.replace("/*FACES*/", "\n".join(faces)).replace("<!--SECTIONS-->", "\n".join(sections))
    OUT.write_text(page)
    print(OUT, f"{OUT.stat().st_size / 1e6:.2f} MB")


TEMPLATE = """<title>Pancake Mono 調整ボード</title>
<style>
/*FACES*/
/* Layout: 調整項目ごとに 1 段。各段に A/B/C の 3 案を横に並べ、狭い画面では縦に積む */
:root { --bg: #f4f6ef; --panel: #ffffff; --fg: #22301f; --muted: #66735f; --line: #dde3d3; --accent: #5b8a2e; --accent-soft: #e6f0d8;
  --ui: system-ui, -apple-system, "Hiragino Sans", "Noto Sans JP", sans-serif; }
@media (prefers-color-scheme: dark) { :root:not([data-theme="light"]) { --bg: #151a13; --panel: #1d241a; --fg: #e8eee2; --muted: #9aa891; --line: #2f3a2a; --accent: #a5d16f; --accent-soft: #2a3721; color-scheme: dark; } }
:root[data-theme="dark"] { --bg: #151a13; --panel: #1d241a; --fg: #e8eee2; --muted: #9aa891; --line: #2f3a2a; --accent: #a5d16f; --accent-soft: #2a3721; color-scheme: dark; }
body { background: var(--bg); color: var(--fg); font-family: var(--ui); line-height: 1.6; }
.wrap { max-width: 1200px; margin: 0 auto; padding-inline: 20px; padding-block: 28px 56px; display: grid; gap: 28px; }
h1 { margin: 0; font-size: 1.6rem; text-wrap: balance; }
.lede { margin: 0; color: var(--muted); max-width: 72ch; }
section { display: grid; gap: 8px; }
h2 { margin: 0; font-size: 1.05rem; display: flex; gap: 10px; align-items: baseline; }
h2 code { font-size: .75rem; color: var(--muted); font-weight: 500; }
.desc { margin: 0; color: var(--muted); font-size: .85rem; }
.opts { display: grid; grid-template-columns: repeat(auto-fit, minmax(min(100%, 320px), 1fr)); gap: 12px; }
.opt { margin: 0; background: var(--panel); border: 1px solid var(--line); border-radius: 12px; padding: 12px 14px; display: grid; gap: 6px; min-width: 0; }
.opt.is-now { border-color: var(--accent); }
figcaption { font-size: .8rem; color: var(--muted); display: flex; gap: 6px; align-items: center; }
figcaption b { color: var(--fg); font-variant-numeric: tabular-nums; }
.now { font-size: .7rem; padding: 0 8px; border-radius: 999px; background: var(--accent-soft); color: var(--accent); font-weight: 600; }
pre { margin: 0; font-size: 22px; line-height: normal; white-space: pre; overflow-x: auto; }
</style>
<div class="wrap">
  <header><h1>Pancake Mono 調整ボード</h1>
  <p class="lede">項目ごとに 3 案を並べています。枠が緑のものが現在の設定です。気に入った案を「size-A, kana-C」のように教えてください。すべて Regular の等幅版です。</p></header>
  <!--SECTIONS-->
</div>
"""

if __name__ == "__main__":
    main()
