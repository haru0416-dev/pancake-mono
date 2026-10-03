"""ビルドしたフォントを Web 用の woff2 にする。

usage: uv run python scripts/woff2.py [-j 6]
出力: build/woff2/<系統>/<ファイル名>.woff2(字の間引きはしないので、1 本あたり数 MB になる)
"""

import argparse
import pathlib
from concurrent.futures import ProcessPoolExecutor

from fontTools.ttLib import TTFont

ROOT = pathlib.Path(__file__).resolve().parent.parent
FONTS = ROOT / "build" / "fonts"
OUT = ROOT / "build" / "woff2"


def convert(src):
    dst = OUT / src.parent.name / src.with_suffix(".woff2").name
    dst.parent.mkdir(parents=True, exist_ok=True)
    f = TTFont(src)
    f.flavor = "woff2"
    f.save(dst)
    return dst


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("-j", type=int, default=6)
    args = ap.parse_args()
    srcs = sorted(FONTS.glob("*/*.ttf"))
    if not srcs:
        raise SystemExit("build/fonts/ にフォントがありません。先に make を実行してください")
    with ProcessPoolExecutor(args.j) as ex:
        for dst in ex.map(convert, srcs):
            print(dst.relative_to(ROOT))


if __name__ == "__main__":
    main()
