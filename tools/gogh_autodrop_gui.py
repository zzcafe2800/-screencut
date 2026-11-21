"""
gogh 自動ドロップくん (Windows 用 / 画面つき)

便利くんGで分割した画像 (01.png 〜) を、gogh の画面上のプロジェクター(カード)の g のマスへ
「左上 → 右 → 下の段」の順に、マウス操作でドラッグ&ドロップして自動で貼ります。

■ 位置の決め方
  - あなたが教えるのは「プロジェクター(カード全体)の4つの角」だけ。
  - 縦横のマス数はフォルダ名 (…_5x8) = ファイル名の並びが基準。4つの角を 5×8 に等分して位置を出します。
  - 明るく g が見えるときは、画像で g を探して位置をさらに補正します。
    夜で真っ暗・色が変わって g が見えないときは、4つの角と枚数だけで計算します (斜め・あおりにも対応)。

■ 安全性
  - 外部ライブラリなし (Python 標準のみ)。通信は「最新版に更新」の確認・取得のみ (このリポジトリだけ)。
  - ゲームのメモリやファイルには触りません。人間と同じようにマウスを動かすだけです。
  - g の確認と読み込み待ちのため、指定した範囲だけ画面を撮影します。画像は送信しません。
  - エクスプローラーのファイルの場所は、Windows 標準の UI Automation で「名前と位置」を聞いて探します。
  - 管理者権限は不要。Esc キーでいつでも中止できます。
"""
import base64
import ctypes
import json
import math
import os
import queue
import re
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
import zlib
from array import array
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

VERSION = "v0.15 (2026-11-21)"   # 更新するたびにここを上げる
IMG_EXT = (".png", ".jpg", ".jpeg", ".gif", ".webp")
BASE_DIR = os.path.dirname(sys.executable if getattr(sys, "frozen", False) else os.path.abspath(__file__))
SETTINGS = os.path.join(BASE_DIR, "gogh_autodrop_settings.json")
UPDATE_URL = "https://raw.githubusercontent.com/zzcafe2800/-screencut/claude/gallant-heisenberg-4vjhkx/tools/gogh_autodrop_gui.py"
SELF = os.path.abspath(__file__)


# ───────────── アップデート ─────────────
def ver_tuple(text):
    m = re.search(r"v(\d+(?:\.\d+)*)", text or "")
    return tuple(int(x) for x in m.group(1).split(".")) if m else (0,)


def fetch_latest():
    """GitHub から最新版のソースを取得 (配信キャッシュを避けるため毎回違う URL)"""
    req = urllib.request.Request(f"{UPDATE_URL}?nocache={int(time.time())}",
                                 headers={"User-Agent": "gogh-autodrop", "Cache-Control": "no-cache"})
    with urllib.request.urlopen(req, timeout=10) as r:
        return r.read().decode("utf-8")


def remote_version(src):
    m = re.search(r'^VERSION = "([^"]+)"', src, re.M)
    return m.group(1) if m else ""


# ───────────── 計算 (OS に依存しない部分) ─────────────
def natural_key(s):
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", s)]


def list_images(folder):
    return sorted([f for f in os.listdir(folder) if f.lower().endswith(IMG_EXT)], key=natural_key)


def parse_grid(folder):
    """フォルダ名の末尾が _5x8 なら (列, 行) = (5, 8) (便利くんGの分割フォルダ名)"""
    m = re.search(r"_(\d+)x(\d+)$", os.path.basename(os.path.normpath(folder)))
    return (int(m.group(1)), int(m.group(2))) if m else None


def solve_linear(A, b):
    n = len(b)
    M = [row[:] + [b[i]] for i, row in enumerate(A)]
    for i in range(n):
        p = max(range(i, n), key=lambda r: abs(M[r][i]))
        M[i], M[p] = M[p], M[i]
        if abs(M[i][i]) < 1e-12:
            raise ValueError("4つの角の位置が正しくありません。指定し直してください。")
        for r in range(i + 1, n):
            f = M[r][i] / M[i][i]
            for c in range(i, n + 1):
                M[r][c] -= f * M[i][c]
    x = [0.0] * n
    for i in range(n - 1, -1, -1):
        x[i] = (M[i][n] - sum(M[i][j] * x[j] for j in range(i + 1, n))) / M[i][i]
    return x


def fit_h(src, dst):
    """src→dst の射影変換 (斜め・あおり対応)。4点ちょうどなら厳密、それ以上は最小二乗"""
    A, b = [], []
    for (x, y), (u, v) in zip(src, dst):
        A.append([x, y, 1, 0, 0, 0, -u * x, -u * y]); b.append(u)
        A.append([0, 0, 0, x, y, 1, -v * x, -v * y]); b.append(v)
    if len(A) == 8:
        return solve_linear(A, b) + [1.0]
    AtA = [[sum(A[k][i] * A[k][j] for k in range(len(A))) for j in range(8)] for i in range(8)]
    Atb = [sum(A[k][i] * b[k] for k in range(len(A))) for i in range(8)]
    return solve_linear(AtA, Atb) + [1.0]


def apply_h(h, x, y):
    w = h[6] * x + h[7] * y + h[8]
    return (h[0] * x + h[1] * y + h[2]) / w, (h[3] * x + h[4] * y + h[5]) / w


def inv3(h):
    a, b, c, d, e, f, g, i_, j = h
    A_, B_, C_ = e * j - f * i_, -(d * j - f * g), d * i_ - e * g
    det = a * A_ + b * B_ + c * C_
    if abs(det) < 1e-12:
        raise ValueError("4つの角の位置が正しくありません。")
    return [A_ / det, -(b * j - c * i_) / det, (b * f - c * e) / det,
            B_ / det, (a * j - c * g) / det, -(a * f - c * d) / det,
            C_ / det, -(a * i_ - b * g) / det, (a * e - b * d) / det]


UNIT = [(0, 0), (1, 0), (0, 1), (1, 1)]   # 左上, 右上, 左下, 右下


def quad_area(c):
    tl, tr, bl, br = c
    pts = [tl, tr, br, bl]
    return abs(sum(pts[i][0] * pts[(i + 1) % 4][1] - pts[(i + 1) % 4][0] * pts[i][1] for i in range(4))) / 2


def geo_targets(board, cols, rows):
    """カードの4つの角を cols×rows に等分した各マスの中心 (画面座標)"""
    H = fit_h(UNIT, board)
    return [tuple(int(round(z)) for z in apply_h(H, (k % cols + 0.5) / cols, (k // cols + 0.5) / rows))
            for k in range(cols * rows)]


def capture_rect(board):
    xs = [p[0] for p in board]; ys = [p[1] for p in board]
    x0, y0 = max(int(min(xs)) - 4, 0), max(int(min(ys)) - 4, 0)
    return x0, y0, int(max(xs)) + 4 - x0, int(max(ys)) + 4 - y0


def _dist(c1, c2):
    return abs(c1[0] - c2[0]) + abs(c1[1] - c2[1]) + abs(c1[2] - c2[2])


def _period(vals, max_n=60):
    """0〜1 に並んだ g の位置から、マスがいくつ並んでいるかを数える (マス中心が (i+0.5)/n)"""
    if len(vals) < 40:
        return None, 0.0
    vals = vals[:: max(1, len(vals) // 4000)]
    best_n, best = None, 0.0
    for n in range(1, max_n + 1):
        s = -sum(math.cos(2 * math.pi * n * v) for v in vals) / len(vals)
        if s > best:
            best_n, best = n, s
    return (best_n, best) if best >= 0.3 else (None, best)


def _norm(v):
    m = sum(v) / len(v)
    d = [x - m for x in v]
    n = math.sqrt(sum(x * x for x in d)) or 1.0
    return [x / n for x in d]


def _dot(a, b):
    return sum(x * y for x, y in zip(a, b))


def _skew_y(P, L):
    """形の縦方向の偏り。「9」(尻尾が下) なら正、「6」なら負"""
    tot = sum(P) or 1.0
    ys = [j - (L - 1) / 2 for j in range(L) for _ in range(L)]
    my = sum(w * y for w, y in zip(P, ys)) / tot
    v = sum(w * (y - my) ** 2 for w, y in zip(P, ys)) / tot or 1.0
    return sum(w * (y - my) ** 3 for w, y in zip(P, ys)) / tot / v ** 1.5


def _orientations(patches, L):
    """patches: {マス: L×L の g らしさ (重心を中心・マスの傾きに合わせて切り出したもの)} → {マス: 'up'/'down'}
    小さな g 1つでは形がはっきりしないので、全部のマスを使う:
    1. 「同じ向き」「逆向き」の2グループに分ける (揃えて平均したお手本と、そのお手本を180度回したものの
       どちらに近いかで振り分けて、お手本を作り直す…をくり返す)
    2. 揃えたお手本が「9」の形 (下に尻尾) になる側を、正しい向きとする"""
    if len(patches) < 2:
        return {}
    keys = list(patches)
    G = {k: _norm(patches[k]) for k in keys}
    side = {k: _skew_y(patches[k], L) >= 0 for k in keys}      # 最初は1マスごとの偏りで振り分け
    for _ in range(10):
        T = [0.0] * (L * L)
        for k in keys:
            v = G[k] if side[k] else G[k][::-1]
            for i in range(L * L):
                T[i] += v[i]
        Tr = T[::-1]
        new = {k: _dot(G[k], T) >= _dot(G[k], Tr) for k in keys}
        if new == side:
            break
        side = new
    T = [0.0] * (L * L)
    for k in keys:
        v = patches[k] if side[k] else patches[k][::-1]
        for i in range(L * L):
            T[i] += v[i]
    if _skew_y(T, L) < 0:                          # お手本が「6」なら意味を入れ替える
        side = {k: not v for k, v in side.items()}
        T = T[::-1]
    Tn = _norm(T)
    Tr = Tn[::-1]
    mg = {k: _dot(G[k], Tn) - _dot(G[k], Tr) for k in keys}
    med = sorted(abs(v) for v in mg.values())[len(mg) // 2]
    th = max(0.002, 0.3 * med)                     # 差が小さすぎるマスは「どちらとも言えない」→ 回転しない
    return {k: ("up" if v > 0 else "down") for k, v in mg.items() if abs(v) >= th}


def png_size(path):
    """PNG の幅と高さだけを読む (速い)。読めなければ None"""
    try:
        with open(path, "rb") as f:
            head = f.read(24)
        if head[:8] == b"\x89PNG\r\n\x1a\n" and head[12:16] == b"IHDR":
            return int.from_bytes(head[16:20], "big"), int.from_bytes(head[20:24], "big")
    except OSError:
        pass
    return None


def board_aspect(board):
    tl, tr, bl, br = board
    return (math.dist(tl, tr) + math.dist(bl, br)) / max(math.dist(tl, bl) + math.dist(tr, br), 1e-6)


def guess_grid(n, piece, aspect):
    """ファイルの枚数 n を 横×縦 に分ける。並べた全体の縦横比 (1枚の大きさ piece から計算) が
    aspect (カードの縦横比) に一番近い組み合わせを選ぶ"""
    if n < 1:
        return None
    pw, ph = piece if piece else (1, 1)
    best = None
    for c in range(1, n + 1):
        if n % c:
            continue
        r = n // c
        err = abs(math.log((c * pw) / (r * ph) / aspect))
        if best is None or err < best[0]:
            best = (err, c, r)
    return best[1], best[2]


def median3(a, b, c):
    """3枚の撮影画像のピクセルごとの中央値。動く落ち葉などは2枚以上で同じ所にないので消える"""
    return bytes(max(min(x, y), min(max(x, y), z)) for x, y, z in zip(a, b, c))


def median_n(bufs):
    if len(bufs) == 3:
        return median3(*bufs)
    m = len(bufs) // 2
    return bytes(sorted(v)[m] for v in zip(*bufs))


def composite_clean(bufs, bg, glyph):
    """ピクセルごとに、複数枚の中から「タイルの地の色か g の色に一番近い」ものを選ぶ。
    落ち葉などの別の色が乗っている枚は選ばれないので、1枚でも見えていれば復元できる"""
    n = len(bufs[0])
    out = bytearray(n)
    b0, b1, b2 = bg[2], bg[1], bg[0]               # BGRA の並びに合わせる
    g0, g1, g2 = glyph[2], glyph[1], glyph[0]
    for i in range(0, n, 4):
        best = None
        for b in bufs:
            x, y, z = b[i], b[i + 1], b[i + 2]
            d = min(abs(x - b0) + abs(y - b1) + abs(z - b2), abs(x - g0) + abs(y - g1) + abs(z - g2))
            if best is None or d < best[0]:
                best = (d, b)
        out[i:i + 4] = best[1][i:i + 4]
    return bytes(out)


def analyze_multi(bufs, bw, bh, ox, oy, board, cols, rows, autogrid=True):
    """1秒おきに撮った複数枚を使って解析する。
    - 中央値の画像 (落ち葉を消したもの) で位置・マス数・向きを解析
    - さらに1枚ずつでも向きを判定し、多数決で g / 6 を決める (二重チェック)"""
    clean = median_n(bufs) if len(bufs) >= 3 else bufs[0]
    c_, r_ = cols, rows
    targets, info = analyze_board(clean, bw, bh, ox, oy, board, c_, r_)
    if autogrid and info.get("wall") and info["wall"] != (c_, r_):
        c_, r_ = info["wall"]
        targets, info = analyze_board(clean, bw, bh, ox, oy, board, c_, r_)
        info["auto_grid"] = (c_, r_)
    if len(bufs) >= 2 and info.get("glyph") and info.get("bg"):
        # 中央値でも消えない葉 (2枚以上に写った葉) は、色で選んだ合成画像で消す
        comp = composite_clean(bufs, info["bg"], info["glyph"])
        targets2, info2 = analyze_board(comp, bw, bh, ox, oy, board, c_, r_)
        if info2["mode"] == "refined" or info["mode"] != "refined":
            clean, targets, info = comp, targets2, info2
            if c_ != cols or r_ != rows:
                info["auto_grid"] = (c_, r_)
    info["frames"] = len(bufs)
    info["disagree"] = 0
    if info["mode"] == "refined" and len(bufs) >= 2:
        results = [info["orient"]]
        for b in bufs:
            _, inf = analyze_board(b, bw, bh, ox, oy, board, c_, r_)
            if inf["mode"] == "refined":
                results.append(inf["orient"])
        final = {}
        for k in range(c_ * r_):
            votes = [res[k] for res in results if k in res]
            if not votes:
                continue
            up, dn = votes.count("up"), votes.count("down")
            if up != dn:
                final[k] = "up" if up > dn else "down"
            elif k in info["orient"]:
                final[k] = info["orient"][k]          # 同数なら落ち葉を消した画像の判定
            if up and dn:
                info["disagree"] += 1
        info["orient"] = final
        info["flip"] = sorted(k for k, d in final.items() if d == "down")
        info["orient_known"] = len(final)
        info["votes"] = len(results)
    return clean, targets, info, c_, r_


def analyze_board(buf, bw, bh, ox, oy, board, cols, rows):
    """撮影画像 (BGRA, 左上が画面の ox,oy) から g を探し、位置を補正する。

    基準はあくまで「カードの4つの角」と「ファイル名の cols×rows」。
    - g が十分見つかれば、見つかった g 全部で射影変換を合わせ直す (クリックの誤差を補正)
    - 暗い・色が違う・g が少ない等で信用できなければ、4つの角の等分 (geometry) を使う
    戻り値: (targets[(x, y, gが見つかったか)], info)
    """
    geo = geo_targets(board, cols, rows)
    info = {"mode": "geometry", "reason": "", "detected": 0, "wall": None, "mismatch": False,
            "flip": [], "orient": {}, "orient_known": 0}
    Hb = fit_h(UNIT, board)
    Hbi = inv3(Hb)
    tile = (quad_area(board) / (cols * rows)) ** 0.5
    info["tile"] = tile
    step = max(1, int((bw * bh / 150000) ** 0.5))
    gw, gh = (bw + step - 1) // step, (bh + step - 1) // step
    px = [None] * (gw * gh)
    uv = [None] * (gw * gh)
    hist = {}
    for gy in range(gh):
        y = gy * step
        for gx in range(gw):
            x = gx * step
            u, v = apply_h(Hbi, ox + x, oy + y)
            if not (0.003 < u < 0.997 and 0.003 < v < 0.997):
                continue
            i = (y * bw + x) * 4
            c = (buf[i + 2], buf[i + 1], buf[i])
            k = gy * gw + gx
            px[k] = c; uv[k] = (u, v)
            q = (c[0] >> 4, c[1] >> 4, c[2] >> 4)
            hist[q] = hist.get(q, 0) + 1
    out = [(x, y, False) for x, y in geo]
    total = sum(hist.values())
    if not total:
        info["reason"] = "撮影範囲が空です"
        return out, info
    top = max(hist, key=hist.get)
    cs = [c for c in px if c and (c[0] >> 4, c[1] >> 4, c[2] >> 4) == top]
    bg = tuple(sum(c[i] for c in cs) / len(cs) for i in range(3))
    info["bg"] = bg
    isbg = [c is not None and _dist(c, bg) < 40 for c in px]
    d = max(1, int(0.22 * tile / step))

    def bg_around(k):
        x0, y0 = k % gw, k // gw
        n = 0
        for dx, dy in ((d, 0), (-d, 0), (0, d), (0, -d)):
            xx, yy = x0 + dx, y0 + dy
            if 0 <= xx < gw and 0 <= yy < gh and isbg[yy * gw + xx]:
                n += 1
        return n

    # g の文字の色 = 「地の色に囲まれた、地と違う色」で一番多いもの (貼り済みの画像は囲まれていないので除外)
    cand, hist2 = [], {}
    for k, c in enumerate(px):
        if c is None or _dist(c, bg) < 60 or bg_around(k) < 2:
            continue
        cand.append(k)
        q = (c[0] >> 4, c[1] >> 4, c[2] >> 4)
        hist2[q] = hist2.get(q, 0) + 1
    if not hist2:
        info["reason"] = "g が見えません (暗い・色が近い)"
        return out, info
    gq = max(hist2, key=hist2.get)
    gcs = [px[k] for k in cand if (px[k][0] >> 4, px[k][1] >> 4, px[k][2] >> 4) == gq]
    glyph = tuple(sum(c[i] for c in gcs) / len(gcs) for i in range(3))
    info["glyph"] = glyph
    mask = [k for k in cand if _dist(px[k], glyph) < _dist(px[k], bg)]
    if len(mask) < 0.002 * total:
        info["reason"] = "g がほとんど見えません"
        return out, info

    # 壁の実際のマス数 (ファイル名と違えば警告)
    wc, _ = _period([uv[k][0] for k in mask])
    wr, _ = _period([uv[k][1] for k in mask])
    if wc and wr:
        info["wall"] = (wc, wr)
        info["mismatch"] = (wc, wr) != (cols, rows)
    if info["mismatch"]:
        info["reason"] = "g の並びがファイルの枚数と違うため、補正しません"
        return out, info

    # マスごとの g の重心
    acc, bgc, tot = {}, {}, {}
    for k, c in enumerate(px):
        if c is None:
            continue
        cc, rr = int(uv[k][0] * cols), int(uv[k][1] * rows)
        tot[(cc, rr)] = tot.get((cc, rr), 0) + 1
        if isbg[k]:
            bgc[(cc, rr)] = bgc.get((cc, rr), 0) + 1
    for k in mask:
        gu, gv = uv[k][0] * cols - 0.5, uv[k][1] * rows - 0.5
        cc, rr = int(round(gu)), int(round(gv))
        if 0 <= cc < cols and 0 <= rr < rows and abs(gu - cc) < 0.42 and abs(gv - rr) < 0.42:
            a = acc.setdefault((cc, rr), [0, 0.0, 0.0])
            a[0] += 1; a[1] += ox + (k % gw) * step; a[2] += oy + (k // gw) * step
    found = {}
    for key, (n, sx, sy) in acc.items():
        t_ = tot.get(key, 1)
        if bgc.get(key, 0) / t_ >= 0.3 and 0.01 * t_ <= n <= 0.5 * t_:
            found[key] = (sx / n, sy / n)
    info["detected"] = len(found)
    if len(found) < 6:
        info["reason"] = f"g が {len(found)} 個しか見つからないため、補正しません"
        return [(x, y, (k % cols, k // cols) in found) for k, (x, y) in enumerate(geo)], info

    keys = list(found)
    H = None
    for _ in range(2):                         # 外れ値を除いて合わせ直す
        H = fit_h(keys, [found[k] for k in keys])
        good = [k for k in keys if math.dist(apply_h(H, *k), found[k]) < 0.35 * tile]
        if len(good) == len(keys) or len(good) < 6:
            break
        keys = good
    fitted = [apply_h(H, k % cols, k // cols) for k in range(cols * rows)]
    worst = max(math.dist(f, g) for f, g in zip(fitted, geo))
    if worst > 0.45 * tile:                    # 補正が4つの角と食い違いすぎる → 信用しない
        info["reason"] = "補正結果が4つの角と大きく違うため、4つの角を使います"
        return [(x, y, (k % cols, k // cols) in found) for k, (x, y) in enumerate(geo)], info
    info["mode"] = "refined"
    info["detected"] = len(keys)

    # g の向き (逆さまのマスを探す)
    def color_at(fx, fy):                          # 画面上の小数座標の色 (周り4点から補間)
        x0, y0 = int(fx), int(fy)
        ax, ay = fx - x0, fy - y0
        x0 -= ox; y0 -= oy
        if not (0 <= x0 < bw - 1 and 0 <= y0 < bh - 1):
            return bg
        out = [0.0, 0.0, 0.0]
        for dx, dy, wt in ((0, 0, (1 - ax) * (1 - ay)), (1, 0, ax * (1 - ay)), (0, 1, (1 - ax) * ay), (1, 1, ax * ay)):
            i = ((y0 + dy) * bw + x0 + dx) * 4
            out[0] += buf[i + 2] * wt; out[1] += buf[i + 1] * wt; out[2] += buf[i] * wt
        return out

    span = max(_dist(glyph, bg), 1.0)

    def glyphness(c):
        if _dist(c, glyph) > _dist(c, bg):
            return 0.0
        return min(1.0, max(0.0, (_dist(c, bg) - 0.15 * span) / (0.7 * span)))

    L = 13
    grid = [(-0.3 + 0.6 * i / (L - 1)) for i in range(L)]
    patches = {}
    for (c, r) in keys:
        # このマスの横・縦の向きと大きさ (隣のマスの位置から。斜め・あおりも自動で合う)
        ex = [(a_ - b_) / 2 for a_, b_ in zip(apply_h(H, c + 1, r), apply_h(H, c - 1, r))]
        ey = [(a_ - b_) / 2 for a_, b_ in zip(apply_h(H, c, r + 1), apply_h(H, c, r - 1))]
        cx, cy = found[(c, r)]
        for _ in range(2):                         # g の重心を細かく合わせる
            sw = sx = sy = 0.0
            for b_ in grid:
                for a_ in grid:
                    px_, py_ = cx + a_ * ex[0] + b_ * ey[0], cy + a_ * ex[1] + b_ * ey[1]
                    w_ = glyphness(color_at(px_, py_))
                    sw += w_; sx += w_ * px_; sy += w_ * py_
            if sw > 0:
                cx, cy = sx / sw, sy / sw
        patches[(c, r)] = [glyphness(color_at(cx + a_ * ex[0] + b_ * ey[0], cy + a_ * ex[1] + b_ * ey[1]))
                           for b_ in grid for a_ in grid]
    ori = _orientations(patches, L)
    info["flip"] = sorted(r * cols + c for (c, r), d in ori.items() if d == "down")
    info["orient"] = {r * cols + c: d for (c, r), d in ori.items()}
    info["orient_known"] = len(ori)
    return [(int(round(x)), int(round(y)), (k % cols, k // cols) in keys)
            for k, (x, y) in enumerate(fitted)], info


def patch_diff(a, b):
    """2枚の小さな撮影画像の違いの大きさ (0〜255 の平均)"""
    if not a or not b or len(a) != len(b):
        return 255.0
    n = len(a) // 4
    stp = max(1, n // 400)
    tot = cnt = 0
    for i in range(0, n, stp):
        j = i * 4
        tot += abs(a[j] - b[j]) + abs(a[j + 1] - b[j + 1]) + abs(a[j + 2] - b[j + 2])
        cnt += 3
    return tot / max(cnt, 1)


def to_ppm(buf, bw, bh, maxw, maxh):
    """BGRA → 縮小した PPM (確認画面の表示用)。(バイト列, 縮小率)"""
    f = max(bw / maxw, bh / maxh, 1.0)
    pw, ph = max(1, int(bw / f)), max(1, int(bh / f))
    out = bytearray()
    for yy in range(ph):
        row = int(yy * f) * bw
        for xx in range(pw):
            i = (row + int(xx * f)) * 4
            out += bytes((buf[i + 2], buf[i + 1], buf[i]))
    return b"P6 %d %d 255\n" % (pw, ph) + bytes(out), f


# ───────────── 画像の読み書き (PNG を Python 標準だけで扱う) ─────────────
def png_decode(path):
    """PNG を読み込んで (幅, 高さ, RGBA の bytearray) を返す。読めない形式なら ValueError"""
    with open(path, "rb") as f:
        data = f.read()
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError("PNG ではありません")
    pos, idat, plte, trns = 8, [], None, None
    w = h = bd = ct = il = None
    while pos < len(data):
        n = int.from_bytes(data[pos:pos + 4], "big")
        typ = data[pos + 4:pos + 8]
        body = data[pos + 8:pos + 8 + n]
        pos += 12 + n
        if typ == b"IHDR":
            w, h = int.from_bytes(body[0:4], "big"), int.from_bytes(body[4:8], "big")
            bd, ct, il = body[8], body[9], body[12]
        elif typ == b"PLTE":
            plte = body
        elif typ == b"tRNS":
            trns = body
        elif typ == b"IDAT":
            idat.append(body)
        elif typ == b"IEND":
            break
    if il:
        raise ValueError("インターレース PNG には未対応です")
    if bd not in (8, 16) and not (ct == 3 and bd in (1, 2, 4)):
        raise ValueError(f"未対応の PNG 形式です (bit={bd})")
    chans = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}[ct]
    bpp_bits = chans * bd
    stride = (w * bpp_bits + 7) // 8
    bpp = max(1, bpp_bits // 8)
    raw = zlib.decompress(b"".join(idat))
    out = bytearray(stride * h)
    prev = bytearray(stride)
    p_ = 0
    for y in range(h):
        ft = raw[p_]; line = bytearray(raw[p_ + 1:p_ + 1 + stride]); p_ += 1 + stride
        if ft == 1:
            for i in range(bpp, stride):
                line[i] = (line[i] + line[i - bpp]) & 255
        elif ft == 2:
            for i in range(stride):
                line[i] = (line[i] + prev[i]) & 255
        elif ft == 3:
            for i in range(stride):
                a = line[i - bpp] if i >= bpp else 0
                line[i] = (line[i] + ((a + prev[i]) >> 1)) & 255
        elif ft == 4:
            for i in range(stride):
                a = line[i - bpp] if i >= bpp else 0
                b = prev[i]
                c = prev[i - bpp] if i >= bpp else 0
                pa, pb, pc = abs(b - c), abs(a - c), abs(a + b - 2 * c)
                line[i] = (line[i] + (a if pa <= pb and pa <= pc else (b if pb <= pc else c))) & 255
        out[y * stride:(y + 1) * stride] = line
        prev = line
    rgba = bytearray(w * h * 4)
    if ct == 3:
        pal = [(plte[i * 3], plte[i * 3 + 1], plte[i * 3 + 2], trns[i] if trns and i < len(trns) else 255)
               for i in range(len(plte) // 3)]
        per = 8 // bd
        for y in range(h):
            row = y * stride
            for x in range(w):
                if bd == 8:
                    idx = out[row + x]
                else:
                    byte = out[row + x // per]
                    idx = (byte >> (8 - bd * (x % per + 1))) & ((1 << bd) - 1)
                o = (y * w + x) * 4
                rgba[o:o + 4] = bytes(pal[idx] if idx < len(pal) else (0, 0, 0, 255))
        return w, h, rgba
    step = 2 if bd == 16 else 1                 # 16bit は上位バイトだけ使う
    src = out[::step] if step == 2 else out
    n = w * h
    if ct == 6:
        rgba[:] = src[:n * 4]
    elif ct == 2:
        rgba[0::4] = src[0::3][:n]; rgba[1::4] = src[1::3][:n]; rgba[2::4] = src[2::3][:n]
        rgba[3::4] = b"\xff" * n
    elif ct == 0:
        g_ = src[:n]
        rgba[0::4] = g_; rgba[1::4] = g_; rgba[2::4] = g_; rgba[3::4] = b"\xff" * n
    elif ct == 4:
        g_ = src[0::2][:n]
        rgba[0::4] = g_; rgba[1::4] = g_; rgba[2::4] = g_; rgba[3::4] = src[1::2][:n]
    return w, h, rgba


def png_encode(w, h, rgba):
    """RGBA → PNG のバイト列 (不透明なら RGB にして軽くする)"""
    opaque = all(a == 255 for a in rgba[3::4])
    if opaque:
        rgb = bytearray(w * h * 3)
        rgb[0::3] = rgba[0::4]; rgb[1::3] = rgba[1::4]; rgb[2::3] = rgba[2::4]
        px, ct, ch = rgb, 2, 3
    else:
        px, ct, ch = rgba, 6, 4
    stride = w * ch
    raw = bytearray()
    for y in range(h):                          # Sub フィルタ (軽くて圧縮も効く)
        line = px[y * stride:(y + 1) * stride]
        f = bytearray(line)
        for i in range(stride - 1, ch - 1, -1):
            f[i] = (line[i] - line[i - ch]) & 255
        raw += b"\x01" + f

    def chunk(t, b):
        return len(b).to_bytes(4, "big") + t + b + (zlib.crc32(t + b) & 0xFFFFFFFF).to_bytes(4, "big")
    ihdr = w.to_bytes(4, "big") + h.to_bytes(4, "big") + bytes((8, ct, 0, 0, 0))
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IDAT", zlib.compress(bytes(raw), 9)) + chunk(b"IEND", b"")


def resize_box(w, h, rgba, nw, nh):
    """面積平均で縮小 (ギザギザになりにくい)"""
    if (nw, nh) == (w, h):
        return bytearray(rgba)
    out = bytearray(nw * nh * 4)
    for y in range(nh):
        y0, y1 = y * h // nh, max(y * h // nh + 1, (y + 1) * h // nh)
        for x in range(nw):
            x0, x1 = x * w // nw, max(x * w // nw + 1, (x + 1) * w // nw)
            s0 = s1 = s2 = s3 = 0
            for yy in range(y0, y1):
                row = (yy * w + x0) * 4
                for i in range(row, row + (x1 - x0) * 4, 4):
                    s0 += rgba[i]; s1 += rgba[i + 1]; s2 += rgba[i + 2]; s3 += rgba[i + 3]
            n = (y1 - y0) * (x1 - x0)
            o = (y * nw + x) * 4
            out[o] = s0 // n; out[o + 1] = s1 // n; out[o + 2] = s2 // n; out[o + 3] = s3 // n
    return out


def rotate180(rgba):
    a = array("I"); a.frombytes(bytes(rgba)); a.reverse()
    return bytearray(a.tobytes())


def fit_size(w, h, maxside):
    if not maxside or max(w, h) <= maxside:
        return w, h
    f = maxside / max(w, h)
    return max(1, round(w * f)), max(1, round(h * f))


def prepare_images(folder, files, maxside, flips, outdir, progress=None):
    """全画像を先に読み込んで確認し、縮小・回転したものを outdir に書き出す。
    戻り値: (サムネイル[(w,h,rgba) または None], 問題のメッセージ一覧, 元の合計サイズ, 新しい合計サイズ)"""
    os.makedirs(outdir, exist_ok=True)
    thumbs, problems = [], []
    before = after = 0
    for k, name in enumerate(files):
        src = os.path.join(folder, name)
        dst = os.path.join(outdir, name)
        before += os.path.getsize(src)
        try:
            if not name.lower().endswith(".png"):
                raise ValueError("PNG 以外はそのまま使います")
            w, h, rgba = png_decode(src)
            nw, nh = fit_size(w, h, maxside)
            small = resize_box(w, h, rgba, nw, nh)
            out = rotate180(small) if k in flips else small
            data = png_encode(nw, nh, out)
            if k not in flips and (nw, nh) == (w, h) and len(data) >= os.path.getsize(src):
                with open(src, "rb") as f:                 # 小さくならなければ元のまま
                    data = f.read()
            with open(dst, "wb") as f:
                f.write(data)
            after += len(data)
            tw, th = fit_size(nw, nh, 96)
            thumbs.append((tw, th, resize_box(nw, nh, small, tw, th)))   # 完成予想は正しい向きで
        except Exception as e:
            with open(src, "rb") as f:
                data = f.read()
            with open(dst, "wb") as f:
                f.write(data)
            after += len(data)
            thumbs.append(None)
            problems.append(f"{name}: {e}" + (" (回転できません)" if k in flips else ""))
        if progress:
            progress(k + 1, len(files))
    return thumbs, problems, before, after


def render_preview(buf, bw, bh, ox, oy, board, cols, rows, thumbs, maxw, maxh):
    """撮影画像のカードの上に、画像を斜めに合わせて貼った「完成予想」を PPM で作る"""
    f = max(bw / maxw, bh / maxh, 1.0)
    pw, ph = max(1, int(bw / f)), max(1, int(bh / f))
    Hi = inv3(fit_h(UNIT, board))
    out = bytearray()
    for yy in range(ph):
        sy = int(yy * f)
        for xx in range(pw):
            sx = int(xx * f)
            u, v = apply_h(Hi, ox + sx, oy + sy)
            col = None
            if 0 <= u < 1 and 0 <= v < 1:
                c, r = int(u * cols), int(v * rows)
                k = r * cols + c
                t = thumbs[k] if k < len(thumbs) else None
                if t:
                    tw, th, px = t
                    tx = min(tw - 1, int((u * cols - c) * tw)); ty = min(th - 1, int((v * rows - r) * th))
                    i = (ty * tw + tx) * 4
                    a = px[i + 3] / 255
                    j = (sy * bw + sx) * 4
                    col = (int(px[i] * a + buf[j + 2] * (1 - a)), int(px[i + 1] * a + buf[j + 1] * (1 - a)),
                           int(px[i + 2] * a + buf[j] * (1 - a)))
            if col is None:
                j = (sy * bw + sx) * 4
                col = (buf[j + 2], buf[j + 1], buf[j])
            out += bytes(col)
    return b"P6 %d %d 255\n" % (pw, ph) + bytes(out), f


# ───────────── エクスプローラーのファイルを UI Automation で探す ─────────────
# 1つの PowerShell を起動したままにして、ファイル名を送ると
#   横スクロールを左端に戻す → その名前の行を(隠れていれば)スクロールして表示 → 「名前」の欄の位置
# を返してもらう。画面の色は読まず、Windows が公開している名前と位置だけを使う。
_PS_LOOP = r"""
$ErrorActionPreference='Continue'
[Console]::InputEncoding=[Text.Encoding]::UTF8
[Console]::OutputEncoding=[Text.Encoding]::UTF8
Add-Type -AssemblyName UIAutomationClient,UIAutomationTypes
$AE=[System.Windows.Automation.AutomationElement]
$TS=[System.Windows.Automation.TreeScope]
$CT=[System.Windows.Automation.ControlType]
function Say($s){ [Console]::Out.WriteLine($s); [Console]::Out.Flush() }
function Cond($p,$v){ New-Object System.Windows.Automation.PropertyCondition($p,$v) }
function GetWin(){
  foreach($w in $AE::RootElement.FindAll($TS::Children,(Cond $AE::ClassNameProperty 'CabinetWClass'))){
    $n=$w.Current.Name; $l=$env:GOGH_LEAF
    if($n -eq $l -or $n -like "*\$l"){ return $w } }
  return $null }
while($true){
  $line=[Console]::In.ReadLine(); if($line -eq $null){ break }
  try{
    $p=$line.Split("`t"); $win=GetWin
    if(-not $win){ Say "NOWIN" }
    elseif($p[0] -eq 'HWND'){ Say ("HWND`t"+$win.Current.NativeWindowHandle) }
    elseif($p[0] -eq 'FIND'){
      $list=$win.FindFirst($TS::Descendants,(Cond $AE::ControlTypeProperty $CT::List))
      if(-not $list){ Say "NOLIST" } else {
        try{ $sp=$list.GetCurrentPattern([System.Windows.Automation.ScrollPattern]::Pattern)
             if($sp.Current.HorizontallyScrollable -and $sp.Current.HorizontalScrollPercent -gt 0){
               $sp.SetScrollPercent(0,[System.Windows.Automation.ScrollPattern]::NoScroll); Start-Sleep -Milliseconds 150 } }catch{}
        $it=$null
        foreach($nm in @($p[1],[IO.Path]::GetFileNameWithoutExtension($p[1]))){
          if($it){ break }
          try{ $ic=$list.GetCurrentPattern([System.Windows.Automation.ItemContainerPattern]::Pattern)
               $it=$ic.FindItemByProperty($null,$AE::NameProperty,$nm) }catch{}
          if(-not $it){
            $c=New-Object System.Windows.Automation.AndCondition((Cond $AE::ControlTypeProperty $CT::ListItem),(Cond $AE::NameProperty $nm))
            $it=$list.FindFirst($TS::Descendants,$c) } }
        if(-not $it){ Say "NOITEM" } else {
          try{ $it.GetCurrentPattern([System.Windows.Automation.VirtualizedItemPattern]::Pattern).Realize() }catch{}
          try{ $it.GetCurrentPattern([System.Windows.Automation.ScrollItemPattern]::Pattern).ScrollIntoView() }catch{}
          Start-Sleep -Milliseconds 120
          $lr=$list.Current.BoundingRectangle
          $nc=$it.FindFirst($TS::Children,(Cond $AE::AutomationIdProperty 'System.ItemNameDisplay'))
          if($nc){ $r=$nc.Current.BoundingRectangle } else { $r=$it.Current.BoundingRectangle }
          Say ("ITEM`t{0}`t{1}`t{2}`t{3}`t{4}`t{5}`t{6}`t{7}" -f [int]$r.X,[int]$r.Y,[int]$r.Width,[int]$r.Height,[int]$lr.X,[int]$lr.Y,[int]$lr.Width,[int]$lr.Height) } } }
  } catch { Say ("ERR`t"+$_.Exception.Message) }
  Say "END"
}
"""


class ExplorerFinder:
    def __init__(self, leaf):
        enc = base64.b64encode(_PS_LOOP.encode("utf-16-le")).decode()
        self.p = subprocess.Popen(
            ["powershell", "-NoProfile", "-NonInteractive", "-EncodedCommand", enc],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            creationflags=0x08000000, env=dict(os.environ, GOGH_LEAF=leaf))
        self.q = queue.Queue()
        threading.Thread(target=self._reader, daemon=True).start()

    def _reader(self):
        for raw in self.p.stdout:
            self.q.put(raw.decode("utf-8", "ignore").rstrip("\r\n"))

    def cmd(self, line, timeout=20):
        self.p.stdin.write((line + "\n").encode("utf-8"))
        self.p.stdin.flush()
        out = []
        end = time.time() + timeout
        while time.time() < end:
            try:
                s = self.q.get(timeout=0.5)
            except queue.Empty:
                continue
            if s == "END":
                return out
            out.append(s)
        raise RuntimeError("エクスプローラーからの応答がありません。")

    def hwnd(self):
        for s in self.cmd("HWND"):
            t = s.split("\t")
            if t[0] == "HWND" and t[1].lstrip("-").isdigit():
                return int(t[1])
        return None

    def find(self, filename):
        """(名前欄 x,y,w,h, 一覧 x,y,w,h) または エラー文字列"""
        res = self.cmd("FIND\t" + filename)
        for s in res:
            t = s.split("\t")
            if t[0] == "ITEM" and len(t) == 9:
                return tuple(int(v) for v in t[1:])
        return res[0] if res else "NORES"

    def close(self):
        try:
            self.p.stdin.close(); self.p.terminate()
        except Exception:
            pass


# ───────────── マウスと画面撮影 (Windows) ─────────────
class _MI(ctypes.Structure):
    _fields_ = [("dx", ctypes.c_long), ("dy", ctypes.c_long), ("mouseData", ctypes.c_ulong),
                ("dwFlags", ctypes.c_ulong), ("time", ctypes.c_ulong), ("dwExtraInfo", ctypes.c_size_t)]


class _IN(ctypes.Structure):
    class _U(ctypes.Union):
        _fields_ = [("mi", _MI)]
    _anonymous_ = ("u",)
    _fields_ = [("type", ctypes.c_ulong), ("u", _U)]


class Mouse:
    """SendInput で本物のマウス操作と同じ信号を送る"""

    def __init__(self):
        self.u = ctypes.windll.user32
        self.slow = 1.0

    def _send(self, flags, x=0, y=0, data=0):
        i = _IN(type=0, mi=_MI(x, y, data & 0xFFFFFFFF, flags, 0, 0))
        self.u.SendInput(1, ctypes.byref(i), ctypes.sizeof(_IN))

    def pos(self):
        class P(ctypes.Structure):
            _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]
        p = P()
        self.u.GetCursorPos(ctypes.byref(p))
        return p.x, p.y

    def move(self, x, y):
        vx, vy = self.u.GetSystemMetrics(76), self.u.GetSystemMetrics(77)
        vw, vh = self.u.GetSystemMetrics(78), self.u.GetSystemMetrics(79)
        self._send(0x0001 | 0x8000 | 0x4000,
                   int((x - vx) * 65535 / max(vw - 1, 1)), int((y - vy) * 65535 / max(vh - 1, 1)))

    def glide(self, x, y, sec=0.4, steps=30):
        x0, y0 = self.pos()
        sec *= self.slow
        for i in range(1, steps + 1):
            t = i / steps
            self.move(x0 + (x - x0) * t, y0 + (y - y0) * t)
            time.sleep(sec / steps)

    def wait(self, sec):
        time.sleep(sec * self.slow)

    def down(self):
        self._send(0x0002)

    def up(self):
        self._send(0x0004)

    def wheel(self, notches):          # 正 = 下へ
        self._send(0x0800, data=-120 * notches)

    def hwheel(self, notches):         # 負 = 左へ
        self._send(0x1000, data=120 * notches)

    def esc_pressed(self):
        return bool(self.u.GetAsyncKeyState(0x1B) & 0x8000)


def grab(x, y, w, h):
    """画面の一部を撮影して BGRA のバイト列を返す (GDI。保存も送信もしない)"""
    u, g = ctypes.windll.user32, ctypes.windll.gdi32
    vp = ctypes.c_void_p
    u.GetDC.restype = vp; u.GetDC.argtypes = [vp]
    u.ReleaseDC.argtypes = [vp, vp]
    g.CreateCompatibleDC.restype = vp; g.CreateCompatibleDC.argtypes = [vp]
    g.CreateCompatibleBitmap.restype = vp; g.CreateCompatibleBitmap.argtypes = [vp, ctypes.c_int, ctypes.c_int]
    g.SelectObject.restype = vp; g.SelectObject.argtypes = [vp, vp]
    g.BitBlt.argtypes = [vp, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, vp,
                         ctypes.c_int, ctypes.c_int, ctypes.c_ulong]
    g.GetDIBits.argtypes = [vp, vp, ctypes.c_uint, ctypes.c_uint, vp, vp, ctypes.c_uint]
    g.DeleteObject.argtypes = [vp]; g.DeleteDC.argtypes = [vp]

    class BIH(ctypes.Structure):
        _fields_ = [("biSize", ctypes.c_uint32), ("biWidth", ctypes.c_int32), ("biHeight", ctypes.c_int32),
                    ("biPlanes", ctypes.c_uint16), ("biBitCount", ctypes.c_uint16),
                    ("biCompression", ctypes.c_uint32), ("biSizeImage", ctypes.c_uint32),
                    ("biXPelsPerMeter", ctypes.c_int32), ("biYPelsPerMeter", ctypes.c_int32),
                    ("biClrUsed", ctypes.c_uint32), ("biClrImportant", ctypes.c_uint32)]

    hdc = u.GetDC(None)
    mdc = g.CreateCompatibleDC(hdc)
    bmp = g.CreateCompatibleBitmap(hdc, w, h)
    old = g.SelectObject(mdc, bmp)
    try:
        g.BitBlt(mdc, 0, 0, w, h, hdc, x, y, 0x00CC0020)
        bih = BIH(40, w, -h, 1, 32, 0, 0, 0, 0, 0, 0)
        buf = ctypes.create_string_buffer(w * h * 4)
        g.SelectObject(mdc, old)
        g.GetDIBits(mdc, bmp, 0, h, buf, ctypes.byref(bih), 0)
        return buf.raw
    finally:
        g.DeleteObject(bmp); g.DeleteDC(mdc); u.ReleaseDC(None, hdc)


class Cancelled(Exception):
    pass


# ───────────── 画面 ─────────────
class App:
    def __init__(self, root):
        self.root = root
        root.title(f"gogh 自動ドロップくん {VERSION}")
        root.geometry("640x760")
        self.mouse = Mouse()
        self.folder = ""
        self.files = []
        self.board = None       # カード(プロジェクター)の4つの角 [左上, 右上, 左下, 右下]
        self.targets = None     # 解析で決まった各マスの位置
        self.targets_key = None
        self.tile = 40
        self.flip = set()        # 逆さまの g のマス (この番号の画像は 180度回転して貼る)
        self.thumbs = None
        self.prepared_key = None
        self.running = False
        self.finder = None
        self._load()
        self._build()
        self._refresh()
        threading.Thread(target=self.check_update, daemon=True).start()

    # --- 設定 ---
    def _load(self):
        try:
            with open(SETTINGS, encoding="utf-8") as f:
                d = json.load(f)
            if d.get("board"):
                self.board = [tuple(p) for p in d["board"]]
        except Exception:
            pass

    def _save(self):
        try:
            with open(SETTINGS, "w", encoding="utf-8") as f:
                json.dump({"board": self.board}, f)
        except Exception:
            pass

    # --- UI ---
    def _build(self):
        r = self.root
        pad = {"padx": 12, "pady": 4}
        ttk.Label(r, text="gogh 自動ドロップくん", font=("Meiryo UI", 15, "bold")).pack(padx=12, pady=(8, 0))
        ttk.Label(r, text=f"バージョン {VERSION}", foreground="#555").pack()
        fu = ttk.Frame(r); fu.pack(pady=(0, 4))
        ttk.Button(fu, text="最新版に更新", command=self.do_update).pack(side="left", padx=6)
        self.lbl_upd = ttk.Label(fu, text="")
        self.lbl_upd.pack(side="left")

        f1 = ttk.LabelFrame(r, text="① 画像フォルダ (便利くんGの分割ZIPを展開したもの)")
        f1.pack(fill="x", **pad)
        ttk.Button(f1, text="フォルダを選ぶ", command=self.pick_folder).pack(side="left", padx=8, pady=6)
        self.lbl_folder = ttk.Label(f1, text="未選択")
        self.lbl_folder.pack(side="left", padx=4)

        f2 = ttk.LabelFrame(r, text="② マスの数 (カードの画像・フォルダ名・枚数から自動) と速さ")
        f2.pack(fill="x", **pad)
        self.cols = tk.IntVar(value=5)
        self.rows = tk.IntVar(value=8)
        ttk.Label(f2, text="横").pack(side="left", padx=(8, 2))
        ttk.Spinbox(f2, from_=1, to=50, width=4, textvariable=self.cols).pack(side="left")
        ttk.Label(f2, text="× 縦").pack(side="left", padx=(8, 2))
        ttk.Spinbox(f2, from_=1, to=50, width=4, textvariable=self.rows).pack(side="left")
        self.autogrid = tk.BooleanVar(value=True)
        ttk.Checkbutton(f2, text="自動", variable=self.autogrid).pack(side="left", padx=(4, 0))
        ttk.Label(f2, text="  開始番号").pack(side="left", padx=(12, 2))
        self.start = tk.IntVar(value=1)
        ttk.Spinbox(f2, from_=1, to=999, width=4, textvariable=self.start).pack(side="left")
        ttk.Label(f2, text="  待ち秒").pack(side="left", padx=(12, 2))
        self.wait = tk.DoubleVar(value=0.2)
        ttk.Spinbox(f2, from_=0.0, to=10, increment=0.1, width=4, textvariable=self.wait).pack(side="left")
        ttk.Label(f2, text="  ゆっくり度").pack(side="left", padx=(12, 2))
        self.slow = tk.DoubleVar(value=0.5)
        ttk.Spinbox(f2, from_=0.1, to=5, increment=0.1, width=4, textvariable=self.slow).pack(side="left")
        fq = ttk.Frame(r); fq.pack(anchor="w", padx=20)
        ttk.Label(fq, text="画質 (長い辺の最大px。小さいほどゲームの読み込みが速い):").pack(side="left")
        self.quality = tk.StringVar(value="512")
        ttk.Combobox(fq, textvariable=self.quality, width=8, state="readonly",
                     values=["元のまま", "1024", "768", "512", "384", "256"]).pack(side="left", padx=4)
        self.autoflip = tk.BooleanVar(value=True)
        ttk.Checkbutton(r, text="g が逆さまのマスは、画像を180度回転してから貼る",
                        variable=self.autoflip).pack(anchor="w", padx=20)
        self.autowait = tk.BooleanVar(value=True)
        ttk.Checkbutton(r, text="ゲームが画像を読み込み終わるまで待ってから次へ(おすすめ)",
                        variable=self.autowait).pack(anchor="w", padx=20)

        f3 = ttk.LabelFrame(r, text="③ プロジェクター(カード全体)の4つの角を教える")
        f3.pack(fill="x", **pad)
        row = ttk.Frame(f3); row.pack(fill="x", pady=3)
        ttk.Button(row, text="カードの4つの角を指定", command=self.calib).pack(side="left", padx=8)
        self.lbl_board = ttk.Label(row, text="")
        self.lbl_board.pack(side="left")
        row = ttk.Frame(f3); row.pack(fill="x", pady=3)
        ttk.Button(row, text="位置を確認(画像で解析)", command=self.analyze).pack(side="left", padx=8)
        self.lbl_an = ttk.Label(row, text="未確認")
        self.lbl_an.pack(side="left")
        ttk.Label(f3, text="※ g を4つの角にするのではなく、カード全体の外側の角をクリックします。\n"
                           "※ ファイルの場所はソフトが自動で探します(エクスプローラーも自動で開きます)。",
                  foreground="#555", justify="left").pack(anchor="w", padx=8, pady=2)

        f4 = ttk.LabelFrame(r, text="④ 実行 (止めるときは Esc キー)")
        f4.pack(fill="x", **pad)
        row = ttk.Frame(f4); row.pack(pady=6)
        ttk.Button(row, text="テスト(動くだけ)", command=lambda: self.run(True)).pack(side="left", padx=8)
        ttk.Button(row, text="1枚だけ試す(開始番号)", command=lambda: self.run(False, True)).pack(side="left", padx=8)
        ttk.Button(row, text="全部開始", command=lambda: self.run(False)).pack(side="left", padx=8)

        self.log = tk.Text(r, height=12, state="disabled", font=("Meiryo UI", 9))
        self.log.pack(fill="both", expand=True, **pad)
        self.say("使い方: ①フォルダを選ぶ → ②数を確認 → ③カードの4つの角 → 確認画面でOK → ④開始")

    def say(self, msg):
        def f():
            self.log.configure(state="normal")
            self.log.insert("end", msg + "\n")
            self.log.see("end")
            self.log.configure(state="disabled")
        self.root.after(0, f)

    def _refresh(self):
        self.lbl_board.config(text="設定済み ✔" if self.board else "未設定")

    # --- アップデート ---
    def check_update(self):
        try:
            rv = remote_version(fetch_latest())
            if ver_tuple(rv) > ver_tuple(VERSION):
                self.root.after(0, lambda: self.lbl_upd.config(text=f"新しい版があります: {rv}", foreground="#c00"))
            else:
                self.root.after(0, lambda: self.lbl_upd.config(text="最新版です ✔", foreground="#080"))
        except Exception:
            self.root.after(0, lambda: self.lbl_upd.config(text="(更新を確認できません)", foreground="#555"))

    def do_update(self):
        if getattr(sys, "frozen", False):
            return messagebox.showinfo("更新", "exe版は更新ボタンに未対応です。新しい exe をダウンロードしてください。")
        if self.running:
            return
        try:
            src = fetch_latest()
            compile(src, "gogh_autodrop_gui.py", "exec")          # 壊れたファイルで上書きしない
            rv = remote_version(src)
            if not rv:
                raise ValueError("バージョン情報が見つかりません")
            if rv == VERSION:
                return messagebox.showinfo("更新", f"すでに最新版です ({VERSION})")
            tmp = SELF + ".new"
            with open(tmp, "w", encoding="utf-8", newline="") as f:
                f.write(src)
            try:
                os.replace(SELF, SELF + ".bak")
            except OSError:
                pass
            os.replace(tmp, SELF)
        except Exception as e:
            return messagebox.showerror("更新", f"更新できませんでした。\n{e}")
        messagebox.showinfo("更新", f"{rv} に更新しました。ソフトを再起動します。")
        subprocess.Popen([sys.executable, SELF])
        self.root.destroy()

    # --- フォルダ ---
    def pick_folder(self):
        d = filedialog.askdirectory(title="分割した画像のフォルダを選んでください")
        if not d:
            return
        self.folder = d
        self.files = list_images(d)
        self.lbl_folder.config(text=f"{os.path.basename(d)}  ({len(self.files)}枚)")
        self.prepared_key = None; self.thumbs = None
        if self.autogrid.get():
            self._grid_from_files()
        n = self.cols.get() * self.rows.get()
        if len(self.files) != n:
            self.say(f"⚠ 画像は {len(self.files)} 枚ですが、マスは {n} 個です。数を確認してください。")
        if self.board:
            self.say("カードの4つの角は前回のものを使います。「位置を確認」でマス数も画像から数えます。")

    def _grid_from_files(self):
        """フォルダ名 (…_30x32) → だめならファイルの枚数とカードの形からマス数を決める"""
        g = parse_grid(self.folder) if self.folder else None
        if g and g[0] * g[1] == len(self.files):
            self.cols.set(g[0]); self.rows.set(g[1])
            self.say(f"フォルダ名から 横{g[0]}×縦{g[1]} にしました。")
            return
        if self.files:
            piece = png_size(os.path.join(self.folder, self.files[0]))
            aspect = board_aspect(self.board) if self.board else 16 / 9
            c, r = guess_grid(len(self.files), piece, aspect)
            self.cols.set(c); self.rows.set(r)
            self.say(f"画像 {len(self.files)} 枚とカードの形から 横{c}×縦{r} にしました。")

    # --- 4つの角: 画面全体を薄く覆ってクリックを拾う ---
    def calib(self):
        prompts = ["① カードの【左上の角】をクリック", "② カードの【右上の角】をクリック",
                   "③ カードの【左下の角】をクリック", "④ カードの【右下の角】をクリック"]
        self.root.iconify()
        ov = tk.Toplevel(self.root)
        ov.attributes("-fullscreen", True)
        ov.attributes("-topmost", True)
        ov.attributes("-alpha", 0.3)
        ov.configure(bg="black", cursor="crosshair")
        cv = tk.Canvas(ov, bg="black", highlightthickness=0)
        cv.pack(fill="both", expand=True)
        pts, local = [], []
        txt = cv.create_text(ov.winfo_screenwidth() // 2, 60, fill="white",
                             font=("Meiryo UI", 22, "bold"), text=prompts[0] + "  (Esc でやめる)")

        def click(e):
            pts.append((e.x_root, e.y_root)); local.append((e.x, e.y))
            cv.create_oval(e.x - 8, e.y - 8, e.x + 8, e.y + 8, outline="yellow", width=3)
            if len(local) >= 2:
                order = {2: [0, 1], 3: [0, 1, 2], 4: [0, 1, 3, 2, 0]}[len(local)]
                cv.create_line(*[c for i in order for c in local[i]], fill="yellow", width=2)
            if len(pts) == 4:
                ov.after(250, lambda: finish(True))
            else:
                cv.itemconfigure(txt, text=prompts[len(pts)] + "  (Esc でやめる)")

        def finish(ok):
            ov.destroy()
            if not ok:
                self.root.deiconify()
                return
            self.board = pts
            self._save(); self._refresh()
            if self.autogrid.get() and self.files:
                self._grid_from_files()
            self.say("カードの4つの角を保存しました。続けて位置を確認します…")
            self.root.after(300, self.analyze)

        cv.bind("<Button-1>", click)
        ov.bind("<Escape>", lambda e: finish(False))
        ov.focus_force()

    # --- 画像の準備 (縮小・回転して作業用フォルダへ) ---
    def _maxside(self):
        q = self.quality.get()
        return int(q) if q.isdigit() else 0

    def _ready_dir(self):
        leaf = os.path.basename(os.path.normpath(self.folder))
        return os.path.join(self.folder, leaf + "_gogh")

    def _flips(self):
        return set(self.flip) if self.autoflip.get() else set()

    def _prepare(self, progress=None):
        key = (self.folder, self._maxside(), tuple(sorted(self._flips())), tuple(self.files))
        if self.prepared_key == key and os.path.isdir(self._ready_dir()):
            return None
        res = prepare_images(self.folder, self.files, self._maxside(), self._flips(), self._ready_dir(), progress)
        self.thumbs = res[0]
        self.prepared_key = key
        return res

    # --- 位置の確認 (画像で g を探して補正) ---
    def analyze(self):
        if not self.board:
            return messagebox.showinfo("gogh 自動ドロップくん", "先に「カードの4つの角を指定」をしてください。")
        cols, rows = self.cols.get(), self.rows.get()
        self.root.iconify()                       # 自分の画面が写り込まないように隠す
        self.root.after(600, lambda: self._analyze2(cols, rows))

    def _analyze2(self, cols, rows):
        self.lbl_an.config(text="撮影中… (カメラを動かさないで)")
        x, y, w, h = capture_rect(self.board)

        def work():
            try:
                bufs = []
                for i in range(3):                   # 1秒おきに3回撮る (落ち葉対策)
                    if i:
                        time.sleep(1.0)
                    bufs.append(grab(x, y, w, h))
                self.root.after(0, lambda: (self.root.deiconify(), self.lbl_an.config(text="解析中…")))
                buf, targets, info, c_, r_ = analyze_multi(bufs, w, h, x, y, self.board, cols, rows,
                                                           self.autogrid.get())
                if info.get("disagree", 0) > 0.1 * c_ * r_:   # 判定が割れたマスが多い → 2枚足して5枚で
                    self.root.after(0, lambda: (self.root.iconify(),
                                                self.lbl_an.config(text="葉が多いので追加で撮影中…")))
                    time.sleep(0.6)
                    for _ in range(2):
                        time.sleep(1.0)
                        bufs.append(grab(x, y, w, h))
                    self.root.after(0, lambda: (self.root.deiconify(), self.lbl_an.config(text="解析中…")))
                    buf, targets, info, c_, r_ = analyze_multi(bufs, w, h, x, y, self.board, c_, r_,
                                                               self.autogrid.get())
                self.flip = set(info.get("flip", []))
                prep = None
                if self.files:
                    self.prepared_key = None
                    prep = self._prepare(lambda i, n: self.root.after(
                        0, lambda: self.lbl_an.config(text=f"画像を読み込み中… {i}/{n}")))
                pv = None
                if self.thumbs:
                    pv = render_preview(buf, w, h, x, y, self.board, c_, r_, self.thumbs, 640, 520)
                self.root.after(0, lambda: self._analyzed(buf, w, h, x, y, targets, info, c_, r_, prep, pv))
            except Exception as e:
                err = str(e)
                self.root.after(0, lambda: (self.root.deiconify(), self.lbl_an.config(text="失敗"),
                                            messagebox.showerror("確認", f"確認できませんでした: {err}")))
        threading.Thread(target=work, daemon=True).start()

    def _analyzed(self, buf, w, h, x, y, targets, info, cols, rows, prep, pv):
        self.lbl_an.config(text="確認待ち")
        if (self.cols.get(), self.rows.get()) != (cols, rows):
            self.cols.set(cols); self.rows.set(rows)
        if info.get("wall"):
            self.say(f"確認: カードの g は 横{info['wall'][0]}×縦{info['wall'][1]} マスと数えました。")
        if self.files and len(self.files) != cols * rows:
            self.say(f"⚠ マスは {cols * rows} 個ですが、画像は {len(self.files)} 枚です。"
                     f"便利くんGで 横{cols}×縦{rows} に分割すると、ぴったり合います。")
        if info["mode"] == "refined":
            self.say(f"確認: g を {info['detected']} 個見つけて、位置を補正しました。")
        else:
            self.say(f"確認: {info['reason']} → カードの4つの角を {cols}×{rows} に等分して位置を決めました。")
        if info.get("votes"):
            self.say(f"確認: {info['frames']}回撮影して落ち葉などを消し、向きを {info['votes']} 回判定して多数決しました"
                     + (f" (判定が割れたマス {info['disagree']} 個も多数決で確定)" if info["disagree"] else " (全部一致)") + "。")
        if info["flip"]:
            self.say(f"確認: g が逆さまのマス {len(info['flip'])} 個 → 番号 "
                     + ", ".join(str(k + 1) for k in info["flip"]) + " の画像を180度回転します。")
        if prep:
            _, problems, before, after = prep
            self.say(f"画像 {len(self.files)} 枚を読み込み確認: {before // 1024}KB → {after // 1024}KB")
            for msg in problems:
                self.say("  ⚠ " + msg)
        self._preview(buf, w, h, x, y, targets, info, cols, rows, pv)

    def _preview(self, buf, w, h, ox, oy, targets, info, cols, rows, pv=None):
        def photo(ppm, name):
            path = os.path.join(tempfile.gettempdir(), name)
            with open(path, "wb") as fp:
                fp.write(ppm)
            return tk.PhotoImage(file=path)

        ppm, f = to_ppm(buf, w, h, 640, 520)
        img_now = photo(ppm, "gogh_autodrop_now.ppm")
        img_done = photo(pv[0], "gogh_autodrop_done.ppm") if pv else None
        win = tk.Toplevel(self.root)
        win.title("確認: この番号のマスに 01.png, 02.png … を貼ります")
        win.attributes("-topmost", True)
        cv = tk.Canvas(win, width=img_now.width(), height=img_now.height(), highlightthickness=0)
        cv.pack(padx=8, pady=8)
        cv.imgs = (img_now, img_done)
        bg_id = cv.create_image(0, 0, image=img_done or img_now, anchor="nw")
        b = [((x - ox) / f, (y - oy) / f) for x, y in self.board]
        cv.create_polygon(*b[0], *b[1], *b[3], *b[2], outline="#00c8ff", fill="", width=2, tags="mark")
        flips = set(info.get("flip", []))
        for i, (tx, ty, ok) in enumerate(targets):
            px, py = (tx - ox) / f, (ty - oy) / f
            color = "#00e000" if ok else "#ff9900"
            if i in flips and self.autoflip.get():
                color = "#00c8ff"
            cv.create_oval(px - 10, py - 10, px + 10, py + 10, outline=color, width=2, fill="#000000", tags="mark")
            cv.create_text(px, py, text=("↻" if i in flips and self.autoflip.get() else str(i + 1)),
                           fill="white", font=("Meiryo UI", 8, "bold"), tags="mark")
        state = {"done": img_done is not None, "marks": True}

        def toggle_view():
            state["done"] = not state["done"] and img_done is not None
            cv.itemconfigure(bg_id, image=img_done if state["done"] else img_now)
            btn_v.config(text="今の画面を見る" if state["done"] else "完成予想を見る")

        def toggle_marks():
            state["marks"] = not state["marks"]
            cv.itemconfigure("mark", state="normal" if state["marks"] else "hidden")
        top = ttk.Frame(win); top.pack()
        btn_v = ttk.Button(top, text="今の画面を見る" if img_done else "(画像フォルダ未選択)", command=toggle_view)
        btn_v.pack(side="left", padx=4)
        ttk.Button(top, text="番号の表示/非表示", command=toggle_marks).pack(side="left", padx=4)
        if self.files and len(self.files) != cols * rows:
            tk.Label(win, fg="#c00", font=("Meiryo UI", 10, "bold"), justify="left",
                     text=f"⚠ カードは 横{cols}×縦{rows} = {cols * rows}マス、画像は {len(self.files)} 枚です。\n"
                          f"   便利くんGで 横{cols}×縦{rows} に分割し直すとぴったり合います。").pack(padx=8)
        if info.get("mismatch"):
            wc, wr = info["wall"]
            tk.Label(win, fg="#c00", font=("Meiryo UI", 10, "bold"), justify="left",
                     text=f"⚠ カードの g は 横{wc}×縦{wr} に見えます。ファイルは 横{cols}×縦{rows} です。\n"
                          f"   ファイルに合わせて {cols}×{rows} に等分した位置に貼ります。").pack(padx=8)
        mode = (f"g を {info['detected']} 個見つけて位置を補正しました。" if info["mode"] == "refined"
                else f"{info['reason']} → 4つの角を {cols}×{rows} に等分しました。")
        flip_txt = (f"\n水色の ↻ = g が逆さまのマス ({len(flips)}個)。画像を180度回転して貼ります。"
                    if flips and self.autoflip.get() else "")
        ttk.Label(win, text=mode + "\n緑 = g を確認したマス / オレンジ = 計算で決めたマス。" + flip_txt
                            + "\n最初は「完成予想」(画像を貼った後の見え方) を表示しています。"
                              "位置がズレていれば4つの角を指定し直してください。",
                  justify="left").pack(padx=8)
        bar = ttk.Frame(win); bar.pack(pady=8)

        def ok():
            self.targets = targets
            self.tile = info.get("tile", 40)
            self.targets_key = (cols, rows, tuple(map(tuple, self.board)))
            self.lbl_an.config(text="確認済み ✔ (" + ("画像で補正" if info["mode"] == "refined" else "4つの角で計算") + ")")
            win.destroy()

        ttk.Button(bar, text="この配置でOK", command=ok).pack(side="left", padx=8)
        ttk.Button(bar, text="やめる", command=win.destroy).pack(side="left", padx=8)

    def _target(self, k, cols, rows):
        key = (cols, rows, tuple(map(tuple, self.board)))
        if self.targets and self.targets_key == key and k < len(self.targets):
            return self.targets[k][:2]
        return geo_targets(self.board, cols, rows)[k]

    # --- 実行 ---
    def run(self, dry, single=False):
        if self.running:
            return
        n = self.cols.get() * self.rows.get()
        if not self.files:
            return messagebox.showinfo("gogh 自動ドロップくん", "先にフォルダを選んでください。")
        if not self.board:
            return messagebox.showinfo("gogh 自動ドロップくん", "③ でカードの4つの角を教えてください。")
        if not dry and not messagebox.askokcancel(
                "確認", f"{1 if single else min(n, len(self.files))}枚を貼り付けます。\n"
                        "この間マウスは自動で動きます。止めるときは Esc キーを押してください。"):
            return
        self.running = True
        self.root.iconify()
        threading.Thread(target=self._worker, args=(dry, single), daemon=True).start()

    def _open_explorer(self, m):
        """フォルダを開き、カードと重ならない側に配置する"""
        folder = self._ready_dir()
        leaf = os.path.basename(os.path.normpath(folder))
        if self.finder is None or self.finder.p.poll() is not None or getattr(self, "_leaf", None) != leaf:
            if self.finder:
                self.finder.close()
            self.finder = ExplorerFinder(leaf)
            self._leaf = leaf
        hwnd = self.finder.hwnd()
        if not hwnd:
            os.startfile(folder)
            for _ in range(10):
                time.sleep(0.8)
                hwnd = self.finder.hwnd()
                if hwnd:
                    break
        if not hwnd:
            raise RuntimeError("エクスプローラーのウィンドウが見つかりません。")
        u = m.u
        sw, sh = u.GetSystemMetrics(0), u.GetSystemMetrics(1)
        cx = sum(p[0] for p in self.board) / 4
        w = int(sw * 0.34)
        x = 0 if cx > sw / 2 else sw - w        # カードが右寄りなら左に置く
        u.ShowWindow(hwnd, 9)
        u.SetWindowPos(hwnd, 0, x, 0, w, int(sh * 0.92), 0x0040)
        u.SetForegroundWindow(hwnd)
        time.sleep(0.6)
        self._exp_rect = (x, 0, w, int(sh * 0.92))

    def _locate(self, m, filename):
        """ファイルの名前が画面に見える状態にして、つかむ位置を返す"""
        last = ""
        for attempt in range(6):
            self._check(m)
            r = self.finder.find(filename)
            if isinstance(r, tuple):
                nx, ny, nw, nh, lx, ly, lw, lh = r
                gx, gy = nx + min(18, max(nw // 2, 1)), ny + nh // 2
                # 名前が一覧の見えている範囲に入っているか (見出し行の下〜下端の上)
                if lx + 4 <= gx <= lx + lw - 20 and ly + 22 <= gy <= ly + lh - 20 and nw > 4 and nh > 4:
                    return gx, gy
                last = "名前が見えない位置にあります"
                ex, ey, ew, eh = self._exp_rect
                m.move(lx + lw // 2 if lw > 0 else ex + ew // 2, ly + lh // 2 if lh > 0 else ey + eh // 2)
                if gx < lx + 4 or gx > lx + lw - 20:
                    m.hwheel(-10)                # 横スクロールを左へ
                elif gy < ly + 22:
                    m.wheel(-2)
                else:
                    m.wheel(2)
                time.sleep(0.3)
            else:
                last = {"NOWIN": "エクスプローラーが閉じています", "NOLIST": "一覧が見つかりません",
                        "NOITEM": "その名前のファイルが見つかりません"}.get(r.split("\t")[0], r)
                if r == "NOWIN":
                    self._open_explorer(m)
                time.sleep(0.3)
        raise RuntimeError(f"{filename} をつかめません ({last})")

    # --- ゲームの読み込み待ち: 貼ったマスの見た目が変わって落ち着くまで待つ ---
    def _patch_rect(self, dst):
        r = max(6, int(self.tile * 0.3))
        return dst[0] - r, dst[1] - r, 2 * r, 2 * r

    def _wait_loaded(self, m, before, noise, rect):
        """'ok'(変わって落ち着いた) / 'nochange'(反応なし)"""
        th_change = max(4.0, noise * 3 + 3)       # 暗い画面でも反応を拾えるよう、ちらつきに合わせて決める
        th_stable = max(2.0, noise * 1.5 + 1)
        t0 = time.time()
        changed, prev, stable = False, None, 0
        while time.time() - t0 < 15:
            self._check(m)
            time.sleep(0.12)
            try:
                cur = grab(*rect)
            except Exception:
                return "ok"
            if not changed:
                if patch_diff(cur, before) > th_change:
                    changed, prev = True, cur
                elif time.time() - t0 > 4:
                    return "nochange"
                continue
            if patch_diff(cur, prev) < th_stable:
                stable += 1
                if stable >= 3:
                    return "ok"
            else:
                stable = 0
            prev = cur
        return "ok"

    def _worker(self, dry, single=False):
        m = self.mouse
        m.slow = self.slow.get()
        cols, rows = self.cols.get(), self.rows.get()
        n = min(cols * rows, len(self.files))
        try:
            prep = self._prepare(lambda i, n: self.say(f"  画像の準備 {i}/{n}") if i in (1, n) else None)
            if prep:
                self.say(f"画像を準備しました ({prep[2] // 1024}KB → {prep[3] // 1024}KB"
                         + (f"、{len(self._flips())}枚を180度回転" if self._flips() else "") + ")")
                for msg in prep[1]:
                    self.say("  ⚠ " + msg)
            time.sleep(0.5)
            self._open_explorer(m)
            if dry:
                self.say("テスト: 4隅のマスを順に指します…")
                for i in (0, cols - 1, cols * rows - cols, cols * rows - 1):
                    self._check(m); m.glide(*self._target(i, cols, rows), 0.6); time.sleep(0.5)
                for name in (self.files[0], self.files[n - 1]):
                    self.say(f"テスト: {name} を探して指します…")
                    m.glide(*self._locate(m, name), 0.6); time.sleep(0.6)
                self.say("テスト終了。")
                return
            start = max(1, self.start.get())
            for k in range(start - 1, start if single else n):
                self._check(m)
                self.say(f"{k + 1:02d}/{n}  {self.files[k]} → {k // cols + 1}行{k % cols + 1}列")
                dst = self._target(k, cols, rows)
                if self.autowait.get():
                    rect = self._patch_rect(dst)
                    before = grab(*rect)
                    time.sleep(0.1)
                    noise = patch_diff(before, grab(*rect))
                    for attempt in range(2):
                        src = self._locate(m, self.files[k])
                        self._drag(m, src, dst)
                        t1 = time.time()
                        if self._wait_loaded(m, before, noise, rect) == "ok":
                            self.say(f"      読み込み完了 ({time.time() - t1:.1f}秒)")
                            break
                        self.say("      反応がないので、もう一度入れます…" if attempt == 0
                                 else "      ⚠ 反応なし。このマスは後で確認してください。")
                else:
                    self._drag(m, self._locate(m, self.files[k]), dst)
                t0 = time.time()
                while time.time() - t0 < self.wait.get():
                    self._check(m); time.sleep(0.05)
            self.say("完了!")
        except Cancelled:
            m.up()
            self.say("Esc が押されたので中止しました。続きは「開始番号」を指定して再開できます。")
        except Exception as e:
            m.up()
            self.say(f"エラー: {e}")
        finally:
            self.running = False
            self.root.after(0, self.root.deiconify)

    def _drag(self, m, src, dst):
        """つかむ → 少し動かしてドラッグ開始 → カードへ運ぶ → 揺らす → 離す"""
        try:
            m.glide(*src, 0.12, 12)
            m.wait(0.08)
            m.down()
            m.wait(0.08)
            for k in range(1, 4):
                m.glide(src[0] + 10 * k, src[1] + 4 * k, 0.03, 2)
            m.glide(*dst, 0.3, 20)
            for dx, dy in ((3, 0), (-3, 2), (0, 0)):
                m.glide(dst[0] + dx, dst[1] + dy, 0.05, 2)
            m.wait(0.12)
        finally:
            m.up()
        m.wait(0.1)

    @staticmethod
    def _check(m):
        if m.esc_pressed():
            raise Cancelled()


def main():
    if sys.platform != "win32":
        print("このソフトは Windows 専用です。")
        return
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass
    root = tk.Tk()
    app = App(root)
    root.protocol("WM_DELETE_WINDOW", lambda: (app.finder and app.finder.close(), root.destroy()))
    root.mainloop()


if __name__ == "__main__":
    main()
