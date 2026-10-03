"""プロポーショナル版の自動カーニングと和欧間のアキ。

字の右側と次の字の左側の輪郭を高さごとに測り、凹みを depth_cap で打ち切った「見た目の隙間」を出す。
基準は、英字は小文字どうし、かなはかなどうしの全組の隙間の中央値で、そこから threshold 以上外れた組だけを詰める。
対象は片側が大きく開いた字(open_glyphs / kana_open)と、濁点付きのかなを含む組に限る。
空ける方向の調整は、どの高さでも min_gap(濁点付きのかなが左に来る組は kana_min_gap)より近づけないための分だけ入れる。
和文(かな・漢字)と英数字が接するところには wakan を足す。
"""

import unicodedata

from fontutil import CFG, FlattenPen

K = CFG["kerning"]

BANDS = list(range(-240, 960, 20))  # 輪郭を測る高さ(20 ユニットおき)
LATIN = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789.,:;'\"-()/!?"
DIGITS = set("0123456789")


def profile(font, name):
    """(送り幅, 高さごとの左端, 右端, 字面の下端, 上端)。インクの無い高さは None"""
    gs = font.getGlyphSet()
    pen = FlattenPen(q_steps=6, c_steps=8, glyphset=gs)
    gs[name].draw(pen)
    adv = font["hmtx"][name][0]
    ys = [p[1] for e in pen.edges for p in e]
    if not ys:
        return None
    left, right = [], []
    for y in BANDS:
        prof = pen.profile(y)
        left.append(prof[0] if prof else None)
        right.append(prof[1] if prof else None)
    return adv, left, right, min(ys), max(ys)


def visual_gap(pl, pr):
    """2字を並べたときの見た目の隙間(平均)と、実際に最も近い距離"""
    adv, _, right, ymin_l, ymax_l = pl
    _, left, _, ymin_r, ymax_r = pr
    lo, hi = max(ymin_l, ymin_r), min(ymax_l, ymax_r)
    if hi - lo < 60:
        return None, None
    cap = K["depth_cap"]
    total, n, nearest = 0.0, 0, None
    for i, y in enumerate(BANDS):
        if not lo <= y <= hi:
            continue
        sl = adv - right[i] if right[i] is not None else cap
        sr = left[i] if left[i] is not None else cap
        total += min(sl, cap) + min(sr, cap)
        n += 1
        if right[i] is not None and left[i] is not None:
            d = adv - right[i] + left[i]
            nearest = d if nearest is None else min(nearest, d)
    return (total / n if n else None), nearest


def pair_kerns(font, names, ref_names, threshold, skip=lambda a, b: False, max_positive=0, wide_left=frozenset()):
    """基準は ref_names 同士の全組の隙間の中央値。そこから threshold 以上外れた組だけを直す"""
    profs = {n: profile(font, n) for n in names}
    profs = {n: p for n, p in profs.items() if p}
    gaps = {}
    for a, pa in profs.items():
        for b, pb in profs.items():
            if not skip(a, b):
                gaps[(a, b)] = visual_gap(pa, pb)
    ref = sorted(g for (a, b), (g, _) in gaps.items() if g is not None and a in ref_names and b in ref_names)
    ideal = ref[len(ref) // 2]
    kerns = {}
    for (a, b), (gap, nearest) in gaps.items():
        if gap is None:
            continue
        k = max(min(ideal - gap, max_positive), -K["max_kern"])
        # 左が wide_left(濁点を隣へはみ出させた字)の組は、広めの最小間隔を守る
        floor = K["kana_min_gap"] if a in wide_left else K["min_gap"]
        pushed = nearest is not None and nearest + k < floor
        if pushed:
            k = floor - nearest
        k = round(k)
        if abs(k) >= threshold or (pushed and k > 0):  # 重なりを避けるための調整は小さくても入れる
            kerns[(a, b)] = k
    return kerns


def kerning_fea(font, kana_cps, wa_names, ou_names):
    cmap = font.getBestCmap()
    latin = [cmap[ord(c)] for c in LATIN if ord(c) in cmap]
    digit_names = {cmap[ord(c)] for c in DIGITS}
    lower = {cmap[ord(c)] for c in "abcdefghijklmnopqrstuvwxyz"}
    exclude = {(cmap[ord(p[0])], cmap[ord(p[1])]) for p in K["exclude"]}
    # 桁揃えの数字同士と、詰めると別の字に見える組(rn → m など)は調整しない
    open_names = {cmap[ord(c)] for c in K["open_glyphs"] if ord(c) in cmap}
    # 片側が大きく開いた字(T V W Y A L r f 引用符 ピリオドなど)を含む組だけを詰める
    lk = pair_kerns(font, latin, lower, K["threshold"],
                    skip=lambda a, b: (a not in open_names and b not in open_names)
                    or (a in digit_names and b in digit_names) or (a, b) in exclude)
    kana = sorted({cmap[c] for c in kana_cps if c in cmap})
    kana_open = {cmap[ord(c)] for c in K["kana_open"] if ord(c) in cmap}
    # 濁点付きのかなは濁点を少し隣へはみ出させているので、重ならないよう min_gap の確保の対象にする
    voiced = {cmap[c] for c in kana_cps if c in cmap and len(unicodedata.normalize("NFD", chr(c))) == 2}
    kana_open |= voiced
    # かなも、片側が大きく開いた字(ト く へ し など)を含む組だけを詰める
    kk = pair_kerns(font, kana, set(kana), K["kana_threshold"],
                    skip=lambda a, b: a not in kana_open and b not in kana_open, wide_left=frozenset(voiced))
    lines = ["languagesystem DFLT dflt;", "languagesystem latn dflt;", "languagesystem kana dflt;", "languagesystem hani dflt;",
             f"@WA = [{' '.join(sorted(wa_names))}];", f"@OU = [{' '.join(sorted(ou_names))}];",
             "lookup kern_pairs {"]
    lines += [f"  pos {a} {b} {v};" for (a, b), v in sorted({**lk, **kk}.items())]
    lines += ["} kern_pairs;",
              "lookup kern_wakan {", f"  pos @WA @OU {K['wakan']};", f"  pos @OU @WA {K['wakan']};", "} kern_wakan;",
              "feature kern {", "  lookup kern_pairs;", "  lookup kern_wakan;", "} kern;"]
    return "\n".join(lines), len(lk), len(kk)
