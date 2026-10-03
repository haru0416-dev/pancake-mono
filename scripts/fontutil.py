"""各スクリプトで共通に使う設定と、輪郭を測る道具。"""

import functools
import pathlib
import tomllib

from fontTools.pens.basePen import BasePen
from fontTools.pens.boundsPen import BoundsPen
from fontTools.ttLib import TTFont

ROOT = pathlib.Path(__file__).resolve().parent.parent
CFG = tomllib.loads((ROOT / "config.toml").read_text())
# バージョンは pyproject.toml だけに書く(Makefile と CI も同じ場所を読む)
CFG["version"] = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["version"]
SRC = ROOT / "sources"


class FlattenPen(BasePen):
    """輪郭を折れ線の辺(edges)に分解して、高さごとのインクの位置を測る。

    skew を渡すと、イタリックの傾きを戻した座標で測る。曲線の分割数は呼び出し側の精度に合わせて変える。
    """

    def __init__(self, skew=0.0, q_steps=8, c_steps=12, glyphset=None):
        super().__init__(glyphset)
        self.edges = []
        self.skew = skew
        self.q_steps, self.c_steps = q_steps, c_steps
        self._start = self._cur = None

    def _p(self, pt):
        return (pt[0] - self.skew * pt[1], pt[1])

    def _moveTo(self, pt):
        self._start = self._cur = self._p(pt)

    def _lineTo(self, pt):
        p = self._p(pt)
        self.edges.append((self._cur, p))
        self._cur = p

    def _qCurveToOne(self, p1, p2):
        a, b, c = self._cur, self._p(p1), self._p(p2)
        n = self.q_steps
        for i in range(1, n + 1):
            t = i / n
            p = ((1 - t) ** 2 * a[0] + 2 * (1 - t) * t * b[0] + t * t * c[0],
                 (1 - t) ** 2 * a[1] + 2 * (1 - t) * t * b[1] + t * t * c[1])
            self.edges.append((self._cur, p))
            self._cur = p

    def _curveToOne(self, p1, p2, p3):
        a, b, c, d = self._cur, self._p(p1), self._p(p2), self._p(p3)
        n = self.c_steps
        for i in range(1, n + 1):
            t = i / n
            mt = 1 - t
            p = (mt ** 3 * a[0] + 3 * mt * mt * t * b[0] + 3 * mt * t * t * c[0] + t ** 3 * d[0],
                 mt ** 3 * a[1] + 3 * mt * mt * t * b[1] + 3 * mt * t * t * c[1] + t ** 3 * d[1])
            self.edges.append((self._cur, p))
            self._cur = p

    def _closePath(self):
        if self._cur != self._start:
            self.edges.append((self._cur, self._start))

    def bounds(self):
        xs = [p[0] for e in self.edges for p in e]
        ys = [p[1] for e in self.edges for p in e]
        return (min(xs), min(ys), max(xs), max(ys)) if xs else None

    def hits(self, y):
        """高さ y の水平線と輪郭が交わる x(並べ替えていない)"""
        return [x0 + (y - y0) * (x1 - x0) / (y1 - y0)
                for (x0, y0), (x1, y1) in self.edges if (y0 <= y < y1) or (y1 <= y < y0)]

    def profile(self, y):
        """高さ y での最も左と右のインクの x。インクが無ければ None"""
        hits = self.hits(y)
        return (min(hits), max(hits)) if hits else None


@functools.cache
def stroke_thickness(path, ch):
    """フォント path の文字 ch(ハイフンや長音のような横棒)の上下の厚み"""
    font = TTFont(path)
    gs = font.getGlyphSet()
    b = BoundsPen(gs)
    gs[font.getBestCmap()[ord(ch)]].draw(b)
    return b.bounds[3] - b.bounds[1]
