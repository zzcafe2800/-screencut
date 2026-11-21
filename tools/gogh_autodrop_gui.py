"""
gogh 自動ドロップくん (Windows 用 / 画面つき)

便利くんGで分割した画像 (01.png 〜) を、goghの画面上の g タイルへ
「左上 → 右 → 下の段」の順に、マウス操作でドラッグ&ドロップして自動で貼ります。

■ 安全性
  - 外部ライブラリなし (Python 標準のみ)。通信は起動用 bat の自動アップデートのみ (no_update.txt で停止)。
  - ゲームのメモリやファイルには触りません。人間と同じようにマウスを動かすだけです。
  - g の位置確認と読み込み待ちのため、指定した範囲だけ画面を撮影します。撮影した画像はメモリ上で解析するだけで、送信しません(確認用の縮小画像だけ一時フォルダに置きます)。
  - 管理者権限は不要。ファイルは設定ファイル (gogh_autodrop_settings.json) 以外書き込みません。
  - Esc キーを押すといつでも中止できます。
"""
import base64
import ctypes
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import urllib.request
import time
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

VERSION = "v0.10 (2026-11-21)"   # 更新するたびにここを上げる
IMG_EXT = (".png", ".jpg", ".jpeg", ".gif", ".webp")
SETTINGS = os.path.join(
    os.path.dirname(sys.executable if getattr(sys, "frozen", False) else os.path.abspath(__file__)),
    "gogh_autodrop_settings.json",
)


UPDATE_URL = "https://raw.githubusercontent.com/zzcafe2800/-screencut/claude/gallant-heisenberg-4vjhkx/tools/gogh_autodrop_gui.py"
SELF = os.path.abspath(__file__)


def ver_tuple(text):
    m = re.search(r"v(\d+(?:\.\d+)*)", text or "")
    return tuple(int(x) for x in m.group(1).split(".")) if m else (0,)


def fetch_latest():
    """GitHub から最新版のソースを取得して返す (失敗したら例外)"""
    # GitHub の配信キャッシュ(数分間古い版が返る)を避けるため、毎回違う URL で取りに行く
    req = urllib.request.Request(f"{UPDATE_URL}?nocache={int(time.time())}",
                                 headers={"User-Agent": "gogh-autodrop", "Cache-Control": "no-cache"})
    with urllib.request.urlopen(req, timeout=10) as r:
        return r.read().decode("utf-8")


def remote_version(src):
    m = re.search(r'^VERSION = "([^"]+)"', src, re.M)
    return m.group(1) if m else ""


# ───────────── 計算 (OSに依存しない部分) ─────────────
def natural_key(s):
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", s)]


def list_images(folder):
    names = [f for f in os.listdir(folder) if f.lower().endswith(IMG_EXT)]
    return sorted(names, key=natural_key)


def parse_grid(folder):
    """フォルダ名の末尾が _5x8 なら (列, 行) = (5, 8) を返す (便利くんGの分割フォルダ名)"""
    m = re.search(r"_(\d+)x(\d+)$", os.path.basename(os.path.normpath(folder)))
    return (int(m.group(1)), int(m.group(2))) if m else None


def solve_linear(A, b):
    n = len(b)
    M = [row[:] + [b[i]] for i, row in enumerate(A)]
    for i in range(n):
        p = max(range(i, n), key=lambda r: abs(M[r][i]))
        M[i], M[p] = M[p], M[i]
        if abs(M[i][i]) < 1e-12:
            raise ValueError("4隅の位置が一直線上にあります。指定し直してください。")
        for r in range(i + 1, n):
            f = M[r][i] / M[i][i]
            for c in range(i, n + 1):
                M[r][c] -= f * M[i][c]
    x = [0.0] * n
    for i in range(n - 1, -1, -1):
        x[i] = (M[i][n] - sum(M[i][j] * x[j] for j in range(i + 1, n))) / M[i][i]
    return x


def make_homography(corners):
    """単位正方形 (0,0)(1,0)(0,1)(1,1) → 画面上の4点 (左上,右上,左下,右下)"""
    src = [(0, 0), (1, 0), (0, 1), (1, 1)]
    A, b = [], []
    for (x, y), (u, v) in zip(src, corners):
        A.append([x, y, 1, 0, 0, 0, -u * x, -u * y])
        A.append([0, 0, 0, x, y, 1, -v * x, -v * y])
        b += [u, v]
    h = solve_linear(A, b)
    return h + [1.0]


def apply_h(h, x, y):
    w = h[6] * x + h[7] * y + h[8]
    return (h[0] * x + h[1] * y + h[2]) / w, (h[3] * x + h[4] * y + h[5]) / w


def tile_pos(corners, cols, rows, idx):
    h = make_homography(corners)
    r, c = divmod(idx, cols)
    u = c / (cols - 1) if cols > 1 else 0.0
    v = r / (rows - 1) if rows > 1 else 0.0
    x, y = apply_h(h, u, v)
    return int(round(x)), int(round(y))


def fit_h(src, dst):
    """src→dst の射影変換を最小二乗で求める (4点以上)。9要素のリストを返す"""
    A, b = [], []
    for (x, y), (u, v) in zip(src, dst):
        A.append([x, y, 1, 0, 0, 0, -u * x, -u * y]); b.append(u)
        A.append([0, 0, 0, x, y, 1, -v * x, -v * y]); b.append(v)
    if len(A) == 8:
        return solve_linear(A, b) + [1.0]
    AtA = [[sum(A[k][i] * A[k][j] for k in range(len(A))) for j in range(8)] for i in range(8)]
    Atb = [sum(A[k][i] * b[k] for k in range(len(A))) for i in range(8)]
    return solve_linear(AtA, Atb) + [1.0]


def inv3(h):
    a, b, c, d, e, f, g, i_, j = h
    A_, B_, C_ = e * j - f * i_, -(d * j - f * g), d * i_ - e * g
    det = a * A_ + b * B_ + c * C_
    if abs(det) < 1e-12:
        raise ValueError("4隅の位置が正しくありません。")
    return [A_ / det, -(b * j - c * i_) / det, (b * f - c * e) / det,
            B_ / det, (a * j - c * g) / det, -(a * f - c * d) / det,
            C_ / det, -(a * i_ - b * g) / det, (a * e - b * d) / det]


def grid_corners(cols, rows):
    return [(0, 0), (cols - 1, 0), (0, rows - 1), (cols - 1, rows - 1)]


def quad_area(c):
    tl, tr, bl, br = c
    pts = [tl, tr, br, bl]
    return abs(sum(pts[i][0] * pts[(i + 1) % 4][1] - pts[(i + 1) % 4][0] * pts[i][1] for i in range(4))) / 2


def capture_rect(corners, cols, rows):
    """4隅から、撮影すべき画面の範囲 (x, y, w, h) を返す (タイル半分の余白つき)"""
    tile = (quad_area(corners) / max((cols - 1) * (rows - 1), 1)) ** 0.5
    m = int(tile * 0.8) + 4
    xs = [p[0] for p in corners]; ys = [p[1] for p in corners]
    x0, y0 = int(min(xs)) - m, int(min(ys)) - m
    return max(x0, 0), max(y0, 0), int(max(xs)) + m - max(x0, 0), int(max(ys)) + m - max(y0, 0)


def _dist(c1, c2):
    return abs(c1[0] - c2[0]) + abs(c1[1] - c2[1]) + abs(c1[2] - c2[2])


def _count_period(vals, max_f=40):
    """g の位置 (0〜1 に正規化) の並びから「マスの間隔の数」を数える。数えられなければ None"""
    if len(vals) < 30:
        return None
    import math
    vals = vals[:: max(1, len(vals) // 4000)]
    scores = {}
    for f in range(1, max_f + 1):
        scores[f] = sum(math.cos(2 * math.pi * f * v) for v in vals) / len(vals)
    best = max(scores.values())
    if best < 0.25:
        return None
    for f in range(1, max_f + 1):           # 倍数でも高くなるので、条件を満たす一番小さいものを選ぶ
        if scores[f] >= 0.75 * best:
            return f
    return None


def analyze_grid(buf, bw, bh, ox, oy, corners, cols, rows):
    """撮影した画像 (BGRA, 左上が画面の ox,oy) から g の位置を解析する。

    1. マス目の中で一番多い色 = タイルの地の色 (ピンク)
    2. 「地の色に囲まれた小さな模様」の色 = g の文字の色 (貼り済みの画像は地の色に囲まれていないので除外)
    3. g の並びの周期から、壁が実際に何列×何行あるかを数える
    4. マスごとに g の重心を求め、見つかった g 全部で射影変換を合わせ直す (クリックのズレを補正)
    5. g が無いマス (貼り済みなど) は、合わせ直した変換から位置を予測
    戻り値: (targets[(x, y, 見つかったか)] 画像の枚数分, info)
    """
    Hu = fit_h([(0, 0), (1, 0), (0, 1), (1, 1)], corners)   # 4隅のマス中心 → 0〜1
    Hiu = inv3(Hu)
    tile = (quad_area(corners) / max((cols - 1) * (rows - 1), 1)) ** 0.5
    step = max(1, int((bw * bh / 150000) ** 0.5))
    gw, gh = (bw + step - 1) // step, (bh + step - 1) // step
    mu, mv = 0.55 / max(cols - 1, 1), 0.55 / max(rows - 1, 1)
    cols_px = [None] * (gw * gh)
    uv = [None] * (gw * gh)
    hist = {}
    for gy in range(gh):
        y = gy * step
        for gx in range(gw):
            x = gx * step
            u, v = apply_h(Hiu, ox + x, oy + y)
            if not (-mu <= u <= 1 + mu and -mv <= v <= 1 + mv):
                continue
            i = (y * bw + x) * 4
            c = (buf[i + 2], buf[i + 1], buf[i])
            k = gy * gw + gx
            cols_px[k] = c; uv[k] = (u, v)
            b_ = (c[0] >> 4, c[1] >> 4, c[2] >> 4)
            hist[b_] = hist.get(b_, 0) + 1
    if not hist:
        raise ValueError("撮影範囲が空です。")
    top = max(hist, key=hist.get)
    cs = [c for c in cols_px if c and (c[0] >> 4, c[1] >> 4, c[2] >> 4) == top]
    bg = tuple(sum(c[i] for c in cs) / len(cs) for i in range(3))
    isbg = [c is not None and _dist(c, bg) < 70 for c in cols_px]
    d = max(1, int(0.22 * tile / step))

    def bg_around(k):
        gx, gy = k % gw, k // gw
        n = 0
        for dx, dy in ((d, 0), (-d, 0), (0, d), (0, -d)):
            xx, yy = gx + dx, gy + dy
            if 0 <= xx < gw and 0 <= yy < gh and isbg[yy * gw + xx]:
                n += 1
        return n

    # g の色: 地の色に2方向以上囲まれた、地ではない点で一番多い色
    hist2 = {}
    cand = []
    for k, c in enumerate(cols_px):
        if c is None or isbg[k] or _dist(c, bg) < 100:
            continue
        if bg_around(k) >= 2:
            cand.append(k)
            b_ = (c[0] >> 4, c[1] >> 4, c[2] >> 4)
            hist2[b_] = hist2.get(b_, 0) + 1
    info = {"bg": bg, "detected": 0, "fitted": False, "tile": tile,
            "wall": (cols, rows), "user": (cols, rows), "mismatch": False}

    def targets_from(H, wc, wr, found):
        t = []
        for k in range(cols * rows):
            c, r = k % cols, k // cols
            x, y = apply_h(H, c, r)
            t.append((int(round(x)), int(round(y)), (c, r) in found))
        extra = [tuple(int(round(z)) for z in apply_h(H, c, r))
                 for r in range(wr) for c in range(wc) if c >= cols or r >= rows]
        return t, extra

    H0 = fit_h(grid_corners(cols, rows), corners)
    if not hist2:
        t, info["extra"] = targets_from(H0, cols, rows, set())
        return t, info
    gb = max(hist2, key=hist2.get)
    gcs = [cols_px[k] for k in cand if (cols_px[k][0] >> 4, cols_px[k][1] >> 4, cols_px[k][2] >> 4) == gb]
    glyph = tuple(sum(c[i] for c in gcs) / len(gcs) for i in range(3))
    info["glyph"] = glyph
    mask = [k for k in cand if _dist(cols_px[k], glyph) < 90 and _dist(cols_px[k], glyph) < _dist(cols_px[k], bg)]

    # 壁が実際に何列×何行か (4隅をクリックしたマスの間を数える)
    fu = _count_period([uv[k][0] for k in mask])
    fv = _count_period([uv[k][1] for k in mask])
    wc = fu + 1 if fu else cols
    wr = fv + 1 if fv else rows
    info["wall"] = (wc, wr)
    info["mismatch"] = (wc, wr) != (cols, rows)

    # マスごとの g の重心と、地の色の割合
    acc, bgc, tot = {}, {}, {}
    for k, c in enumerate(cols_px):
        if c is None:
            continue
        gu, gv = uv[k][0] * (wc - 1), uv[k][1] * (wr - 1)
        cc, rr = int(round(gu)), int(round(gv))
        if not (0 <= cc < wc and 0 <= rr < wr) or abs(gu - cc) > 0.45 or abs(gv - rr) > 0.45:
            continue
        tot[(cc, rr)] = tot.get((cc, rr), 0) + 1
        if isbg[k]:
            bgc[(cc, rr)] = bgc.get((cc, rr), 0) + 1
    for k in mask:
        gu, gv = uv[k][0] * (wc - 1), uv[k][1] * (wr - 1)
        cc, rr = int(round(gu)), int(round(gv))
        if 0 <= cc < wc and 0 <= rr < wr and abs(gu - cc) < 0.42 and abs(gv - rr) < 0.42:
            a = acc.setdefault((cc, rr), [0, 0.0, 0.0])
            a[0] += 1; a[1] += ox + (k % gw) * step; a[2] += oy + (k // gw) * step
    found = {}
    for key, (n, sx, sy) in acc.items():
        t_ = tot.get(key, 1)
        if bgc.get(key, 0) / t_ >= 0.3 and 0.01 * t_ <= n <= 0.5 * t_:
            found[key] = (sx / n, sy / n)

    Hw = fit_h(grid_corners(wc, wr), corners)
    H = Hw
    if len(found) >= 6:
        keys = list(found)
        for _ in range(2):                  # 外れ値を除いて合わせ直す
            try:
                H = fit_h(keys, [found[k] for k in keys])
            except ValueError:
                H = Hw; break
            good = [k for k in keys if ((apply_h(H, *k)[0] - found[k][0]) ** 2
                                        + (apply_h(H, *k)[1] - found[k][1]) ** 2) ** 0.5 < 0.35 * tile]
            if len(good) == len(keys) or len(good) < 6:
                break
            keys = good
        found = {k: found[k] for k in keys}
        info["fitted"] = H is not Hw
    info["detected"] = len(found)
    t, info["extra"] = targets_from(H, wc, wr, found)
    return t, info


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
    """BGRA → 縮小した PPM 画像 (tk で表示する用)。(ppmバイト列, 縮小率) を返す"""
    f = max(bw / maxw, bh / maxh, 1.0)
    pw, ph = max(1, int(bw / f)), max(1, int(bh / f))
    out = bytearray()
    for yy in range(ph):
        row = int(yy * f) * bw
        for xx in range(pw):
            i = (row + int(xx * f)) * 4
            out += bytes((buf[i + 2], buf[i + 1], buf[i]))
    return b"P6 %d %d 255\n" % (pw, ph) + bytes(out), f


# ───────────── エクスプローラーのファイル位置を OS の UI Automation で取得 ─────────────
# 画面の色や画像を読むのではなく、Windows が公開している「ファイル名と枠の位置」を聞くだけ。
_PS = r"""
$ErrorActionPreference='Stop'
[Console]::OutputEncoding=[Text.Encoding]::UTF8
Add-Type -AssemblyName UIAutomationClient,UIAutomationTypes
$AE=[System.Windows.Automation.AutomationElement]
$leaf=$env:GOGH_LEAF
$wc=New-Object System.Windows.Automation.PropertyCondition($AE::ClassNameProperty,'CabinetWClass')
$win=$null
foreach($w in $AE::RootElement.FindAll('Children',$wc)){ if($w.Current.Name -like "*$leaf*"){ $win=$w; break } }
if(-not $win){ exit 0 }
"HWND`t$($win.Current.NativeWindowHandle)"
$lc=New-Object System.Windows.Automation.PropertyCondition($AE::ControlTypeProperty,[System.Windows.Automation.ControlType]::ListItem)
foreach($i in $win.FindAll('Descendants',$lc)){
  if($i.Current.IsOffscreen){ continue }
  $r=$i.Current.BoundingRectangle
  "{0}`t{1}`t{2}`t{3}`t{4}" -f $i.Current.Name,[int]$r.X,[int]$r.Y,[int]$r.Width,[int]$r.Height
}
"""


def uia_query(leaf):
    """(hwnd, {表示名: (x,y,w,h)}) を返す。窓が見つからなければ (None, {})"""
    enc = base64.b64encode(_PS.encode("utf-16-le")).decode()
    r = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-EncodedCommand", enc],
                       capture_output=True, timeout=40, creationflags=0x08000000,
                       env=dict(os.environ, GOGH_LEAF=leaf))
    hwnd, items = None, {}
    for line in r.stdout.decode("utf-8", "ignore").splitlines():
        t = line.strip("\r").split("\t")
        if t[0] == "HWND" and len(t) == 2 and t[1].isdigit():
            hwnd = int(t[1])
        elif len(t) == 5:
            try:
                items[t[0]] = tuple(int(v) for v in t[1:])
            except ValueError:
                pass
    return hwnd, items


def find_item(items, filename):
    """表示名は拡張子つき/なしどちらでも見つける"""
    stem = os.path.splitext(filename)[0]
    low = {k.lower(): v for k, v in items.items()}
    return low.get(filename.lower()) or low.get(stem.lower())


# ───────────── マウス操作 (Windows) ─────────────
class _MI(ctypes.Structure):
    _fields_ = [("dx", ctypes.c_long), ("dy", ctypes.c_long), ("mouseData", ctypes.c_ulong),
                ("dwFlags", ctypes.c_ulong), ("time", ctypes.c_ulong), ("dwExtraInfo", ctypes.c_size_t)]


class _IN(ctypes.Structure):
    class _U(ctypes.Union):
        _fields_ = [("mi", _MI)]
    _anonymous_ = ("u",)
    _fields_ = [("type", ctypes.c_ulong), ("u", _U)]


class Mouse:
    """SendInput を使い、本物のマウス操作と同じ信号を送る (SetCursorPos より確実にドラッグが始まる)"""

    def __init__(self):
        self.u = ctypes.windll.user32
        self.slow = 1.0  # ゆっくり度 (1.0 = 標準)

    def _send(self, flags, x=0, y=0):
        i = _IN(type=0, mi=_MI(x, y, 0, flags, 0, 0))
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
        ax = int((x - vx) * 65535 / max(vw - 1, 1))
        ay = int((y - vy) * 65535 / max(vh - 1, 1))
        self._send(0x0001 | 0x8000 | 0x4000, ax, ay)   # MOVE | ABSOLUTE | VIRTUALDESK

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

    def wheel(self, notches):
        i = _IN(type=0, mi=_MI(0, 0, (-120 * notches) & 0xFFFFFFFF, 0x0800, 0, 0))
        self.u.SendInput(1, ctypes.byref(i), ctypes.sizeof(_IN))

    def esc_pressed(self):
        return bool(self.u.GetAsyncKeyState(0x1B) & 0x8000)


def grab(x, y, w, h):
    """画面の一部を撮影して BGRA のバイト列を返す (Windows 標準の GDI。保存も送信もしない)"""
    u, g = ctypes.windll.user32, ctypes.windll.gdi32
    vp = ctypes.c_void_p
    u.GetDC.restype = vp; u.GetDC.argtypes = [vp]
    u.ReleaseDC.argtypes = [vp, vp]
    g.CreateCompatibleDC.restype = vp; g.CreateCompatibleDC.argtypes = [vp]
    g.CreateCompatibleBitmap.restype = vp; g.CreateCompatibleBitmap.argtypes = [vp, ctypes.c_int, ctypes.c_int]
    g.SelectObject.restype = vp; g.SelectObject.argtypes = [vp, vp]
    g.BitBlt.argtypes = [vp, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, vp, ctypes.c_int, ctypes.c_int, ctypes.c_ulong]
    g.GetDIBits.argtypes = [vp, vp, ctypes.c_uint, ctypes.c_uint, vp, vp, ctypes.c_uint]
    g.DeleteObject.argtypes = [vp]; g.DeleteDC.argtypes = [vp]

    class BIH(ctypes.Structure):
        _fields_ = [("biSize", ctypes.c_uint32), ("biWidth", ctypes.c_int32), ("biHeight", ctypes.c_int32),
                    ("biPlanes", ctypes.c_uint16), ("biBitCount", ctypes.c_uint16), ("biCompression", ctypes.c_uint32),
                    ("biSizeImage", ctypes.c_uint32), ("biXPelsPerMeter", ctypes.c_int32),
                    ("biYPelsPerMeter", ctypes.c_int32), ("biClrUsed", ctypes.c_uint32), ("biClrImportant", ctypes.c_uint32)]

    hdc = u.GetDC(None)
    mdc = g.CreateCompatibleDC(hdc)
    bmp = g.CreateCompatibleBitmap(hdc, w, h)
    old = g.SelectObject(mdc, bmp)
    try:
        g.BitBlt(mdc, 0, 0, w, h, hdc, x, y, 0x00CC0020)       # SRCCOPY
        bih = BIH(40, w, -h, 1, 32, 0, 0, 0, 0, 0, 0)            # 上から下へ・32bit
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
        root.geometry("560x620")
        self.mouse = Mouse()
        self.folder = ""
        self.files = []
        self.corners = None   # 4隅のタイル中心
        self.running = False
        self._cache = {}
        self.targets = None   # 解析で決まった各マスの位置 [(x, y, 見つかったか)]
        self.tile = 40
        self._load()
        self._build()
        self._refresh()
        threading.Thread(target=self.check_update, daemon=True).start()

    # --- 設定の保存 ---
    def _load(self):
        try:
            with open(SETTINGS, encoding="utf-8") as f:
                d = json.load(f)
            self.corners = [tuple(p) for p in d["corners"]] if d.get("corners") else None
        except Exception:
            pass

    def _save(self):
        try:
            with open(SETTINGS, "w", encoding="utf-8") as f:
                json.dump({"corners": self.corners}, f)
        except Exception:
            pass

    # --- UI ---
    def _build(self):
        r = self.root
        pad = {"padx": 12, "pady": 4}
        ttk.Label(r, text="gogh 自動ドロップくん", font=("Meiryo UI", 15, "bold")).pack(padx=12, pady=(8, 0))
        ttk.Label(r, text=f"バージョン {VERSION}", foreground="#555").pack(pady=(0, 2))
        fu = ttk.Frame(r); fu.pack(pady=(0, 4))
        self.btn_upd = ttk.Button(fu, text="最新版に更新", command=self.do_update)
        self.btn_upd.pack(side="left", padx=6)
        self.lbl_upd = ttk.Label(fu, text="")
        self.lbl_upd.pack(side="left")

        f1 = ttk.LabelFrame(r, text="① 画像フォルダ (便利くんGの分割ZIPを展開したもの)")
        f1.pack(fill="x", **pad)
        ttk.Button(f1, text="フォルダを選ぶ", command=self.pick_folder).pack(side="left", padx=8, pady=6)
        self.lbl_folder = ttk.Label(f1, text="未選択")
        self.lbl_folder.pack(side="left", padx=4)

        f2 = ttk.LabelFrame(r, text="② タイルの数")
        f2.pack(fill="x", **pad)
        self.cols = tk.IntVar(value=5)
        self.rows = tk.IntVar(value=8)
        ttk.Label(f2, text="横").pack(side="left", padx=(8, 2))
        ttk.Spinbox(f2, from_=1, to=50, width=4, textvariable=self.cols, command=self._refresh).pack(side="left")
        ttk.Label(f2, text="× 縦").pack(side="left", padx=(8, 2))
        ttk.Spinbox(f2, from_=1, to=50, width=4, textvariable=self.rows, command=self._refresh).pack(side="left")
        ttk.Label(f2, text="  開始番号").pack(side="left", padx=(12, 2))
        self.start = tk.IntVar(value=1)
        ttk.Spinbox(f2, from_=1, to=999, width=4, textvariable=self.start).pack(side="left")
        ttk.Label(f2, text="  待ち秒").pack(side="left", padx=(12, 2))
        self.wait = tk.DoubleVar(value=0.3)
        ttk.Spinbox(f2, from_=0.0, to=10, increment=0.1, width=4, textvariable=self.wait).pack(side="left")
        ttk.Label(f2, text="  ゆっくり度(小=速い)").pack(side="left", padx=(12, 2))
        self.slow = tk.DoubleVar(value=0.5)
        ttk.Spinbox(f2, from_=0.1, to=5, increment=0.1, width=4, textvariable=self.slow).pack(side="left")
        self.autowait = tk.BooleanVar(value=True)
        ttk.Checkbutton(r, text="ゲームが画像を読み込み終わるまで待つ(おすすめ)", variable=self.autowait).pack(anchor="w", padx=20)

        f3 = ttk.LabelFrame(r, text="③ gのタイルの位置を教える (gogh を編集モードにして、タイルが全部見える状態で)")
        f3.pack(fill="x", **pad)
        row = ttk.Frame(f3); row.pack(fill="x", pady=3)
        ttk.Button(row, text="gのタイル4隅を指定", command=self.calib_tiles).pack(side="left", padx=8)
        self.lbl_tiles = ttk.Label(row, text="")
        self.lbl_tiles.pack(side="left")
        row = ttk.Frame(f3); row.pack(fill="x", pady=3)
        ttk.Button(row, text="gの位置を解析(画像で確認)", command=self.analyze).pack(side="left", padx=8)
        self.lbl_an = ttk.Label(row, text="未解析")
        self.lbl_an.pack(side="left")
        ttk.Label(f3, text="※ ファイルの場所はソフトが自動で見つけます(エクスプローラーも自動で開きます)",
                  foreground="#555").pack(anchor="w", padx=8, pady=2)

        f4 = ttk.LabelFrame(r, text="④ 実行 (止めるときは Esc キー)")
        f4.pack(fill="x", **pad)
        row = ttk.Frame(f4); row.pack(pady=6)
        ttk.Button(row, text="テスト(動くだけ)", command=lambda: self.run(True)).pack(side="left", padx=8)
        ttk.Button(row, text="1枚だけ試す(開始番号)", command=lambda: self.run(False, True)).pack(side="left", padx=8)
        self.btn_go = ttk.Button(row, text="全部開始", command=lambda: self.run(False))
        self.btn_go.pack(side="left", padx=8)

        self.log = tk.Text(r, height=12, state="disabled", font=("Meiryo UI", 9))
        self.log.pack(fill="both", expand=True, **pad)
        self.say("使い方: ①フォルダを選ぶ → ②数を確認 → ③gの4隅を教える(自動で解析) → ④テスト → 開始")

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
                os.replace(SELF, SELF + ".bak")                     # 元のファイルはバックアップ
            except OSError:
                pass
            os.replace(tmp, SELF)
        except Exception as e:
            return messagebox.showerror("更新", f"更新できませんでした。\n{e}")
        messagebox.showinfo("更新", f"{rv} に更新しました。ソフトを再起動します。")
        subprocess.Popen([sys.executable, SELF])
        self.root.destroy()

    def say(self, msg):
        def f():
            self.log.configure(state="normal")
            self.log.insert("end", msg + "\n")
            self.log.see("end")
            self.log.configure(state="disabled")
        self.root.after(0, f)

    def _refresh(self):
        self.lbl_tiles.config(text="設定済み ✔" if self.corners else "未設定")

    def pick_folder(self):
        d = filedialog.askdirectory(title="分割した画像のフォルダを選んでください")
        if not d:
            return
        self.folder = d
        self.files = list_images(d)
        self.lbl_folder.config(text=f"{os.path.basename(d)}  ({len(self.files)}枚)")
        g = parse_grid(d)
        if g:
            self.cols.set(g[0]); self.rows.set(g[1])
            self.say(f"フォルダ名から {g[0]}×{g[1]} を自動で設定しました。")
        n = self.cols.get() * self.rows.get()
        if len(self.files) != n:
            self.say(f"⚠ 画像は {len(self.files)} 枚ですが、タイルは {n} 枚です。数を確認してください。")

    # --- 位置指定: 画面全体を薄く覆ってクリックを拾う ---
    def collect_clicks(self, prompts, done):
        ov = tk.Toplevel(self.root)
        ov.attributes("-fullscreen", True)
        ov.attributes("-topmost", True)
        ov.attributes("-alpha", 0.3)
        ov.configure(bg="black", cursor="crosshair")
        cv = tk.Canvas(ov, bg="black", highlightthickness=0)
        cv.pack(fill="both", expand=True)
        pts = []
        txt = cv.create_text(ov.winfo_screenwidth() // 2, 60, fill="white",
                             font=("Meiryo UI", 22, "bold"), text=prompts[0])

        def click(e):
            pts.append((e.x_root, e.y_root))
            cv.create_oval(e.x - 8, e.y - 8, e.x + 8, e.y + 8, outline="yellow", width=3)
            if len(pts) == len(prompts):
                ov.destroy()
                done(pts)
            else:
                cv.itemconfigure(txt, text=prompts[len(pts)])

        cv.bind("<Button-1>", click)
        ov.bind("<Escape>", lambda e: ov.destroy())
        ov.focus_force()

    def calib_tiles(self):
        def done(p):
            self.corners = p
            self._save(); self._refresh()
            self.say("タイル位置を保存しました。続けて画像で解析します…")
            self.root.after(300, self.analyze)
        self.collect_clicks([
            "① 左上のgタイルの中心をクリック",
            "② 右上のgタイルの中心をクリック",
            "③ 左下のgタイルの中心をクリック",
            "④ 右下のgタイルの中心をクリック",
        ], done)

    # --- 画像で g の位置を解析 ---
    def analyze(self):
        if not self.corners:
            return messagebox.showinfo("gogh 自動ドロップくん", "先に「gのタイル4隅を指定」をしてください。")
        cols, rows = self.cols.get(), self.rows.get()
        if cols < 2 or rows < 2:
            return messagebox.showinfo("gogh 自動ドロップくん", "解析は 横2以上・縦2以上 のときに使えます。")
        self.root.iconify()                       # 自分の画面が写り込まないように隠す
        self.root.after(600, lambda: self._analyze2(cols, rows))

    def _analyze2(self, cols, rows):
        try:
            x, y, w, h = capture_rect(self.corners, cols, rows)
            buf = grab(x, y, w, h)
            targets, info = analyze_grid(buf, w, h, x, y, self.corners, cols, rows)
        except Exception as e:
            self.root.deiconify()
            return messagebox.showerror("解析", f"解析できませんでした: {e}")
        self.root.deiconify()
        self.say(f"解析: 壁は 横{info['wall'][0]}×縦{info['wall'][1]} マス、g を {info['detected']} マスで発見。"
                 + ("全体のズレを補正しました。" if info["fitted"] else "見つかった数が少ないため4隅の位置から計算します。"))
        self._preview(buf, w, h, x, y, targets, info, cols)

    def _preview(self, buf, w, h, ox, oy, targets, info, cols):
        ppm, f = to_ppm(buf, w, h, 640, 560)
        path = os.path.join(tempfile.gettempdir(), "gogh_autodrop_preview.ppm")
        with open(path, "wb") as fp:
            fp.write(ppm)
        win = tk.Toplevel(self.root)
        win.title("解析結果: この順番で貼ります")
        win.attributes("-topmost", True)
        img = tk.PhotoImage(file=path)
        cv = tk.Canvas(win, width=img.width(), height=img.height(), highlightthickness=0)
        cv.pack(padx=8, pady=8)
        cv.create_image(0, 0, image=img, anchor="nw")
        cv.image = img
        for tx, ty in info.get("extra", []):          # 画像が足りず使わないマス
            px, py = (tx - ox) / f, (ty - oy) / f
            cv.create_line(px - 6, py - 6, px + 6, py + 6, fill="#888", width=2)
            cv.create_line(px - 6, py + 6, px + 6, py - 6, fill="#888", width=2)
        for i, (tx, ty, ok) in enumerate(targets):
            px, py = (tx - ox) / f, (ty - oy) / f
            color = "#00e000" if ok else "#ff9900"
            cv.create_oval(px - 9, py - 9, px + 9, py + 9, outline=color, width=2)
            cv.create_text(px, py, text=str(i + 1), fill="white", font=("Meiryo UI", 8, "bold"))
        if info.get("mismatch"):
            wc, wr = info["wall"]; uc, ur = info["user"]
            tk.Label(win, fg="#c00", font=("Meiryo UI", 10, "bold"), justify="left",
                     text=f"⚠ 壁の g は 横{wc}×縦{wr} マスに見えます。設定(画像)は 横{uc}×縦{ur} です。\n"
                          f"   便利くんGで {wc}×{wr} に分割し直すのがおすすめです。(このままなら左上から詰めて貼り、×のマスは空きます)").pack(padx=8)
        ttk.Label(win, text=f"緑 = g を見つけた位置 ({info['detected']}個) / オレンジ = 周りから予測した位置\n"
                            "番号が 01.png, 02.png … の貼る先です。ズレていれば4隅を指定し直してください。").pack(padx=8)
        bar = ttk.Frame(win); bar.pack(pady=8)

        def ok():
            self.targets = targets
            self.tile = info.get("tile", 40)
            self.targets_key = (cols, self.rows.get(), tuple(map(tuple, self.corners)))
            self.lbl_an.config(text=f"解析済み ✔ (壁 {info['wall'][0]}×{info['wall'][1]} / g発見 {info['detected']})")
            win.destroy()

        ttk.Button(bar, text="この配置でOK", command=ok).pack(side="left", padx=8)
        ttk.Button(bar, text="やめる", command=win.destroy).pack(side="left", padx=8)

    def _target(self, k, cols, rows):
        key = (cols, rows, tuple(map(tuple, self.corners)))
        if self.targets and getattr(self, "targets_key", None) == key and k < len(self.targets):
            return self.targets[k][:2]
        return tile_pos(self.corners, cols, rows, k)

    # --- ゲームの読み込み待ち: 貼ったマスの見た目が変わって落ち着くまで待つ ---
    def _patch_rect(self, dst):
        r = max(6, int(self.tile * 0.3))
        return dst[0] - r, dst[1] - r, 2 * r, 2 * r

    def _wait_loaded(self, m, before, rect):
        """戻り値: 'ok'(変わって落ち着いた) / 'nochange'(反応なし)"""
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
                if patch_diff(cur, before) > 10:
                    changed, prev = True, cur
                elif time.time() - t0 > 4:
                    return "nochange"
                continue
            if patch_diff(cur, prev) < 2.5:
                stable += 1
                if stable >= 3:
                    return "ok"
            else:
                stable = 0
            prev = cur
        return "ok"

    # --- 実行 ---
    def run(self, dry, single=False):
        if self.running:
            return
        n = self.cols.get() * self.rows.get()
        if not self.files:
            return messagebox.showinfo("gogh 自動ドロップくん", "先にフォルダを選んでください。")
        if not self.corners:
            return messagebox.showinfo("gogh 自動ドロップくん", "③ で位置を教えてください。")
        if not dry and not messagebox.askokcancel(
                "確認", f"{1 if single else min(n, len(self.files))}枚を貼り付けます。\n"
                        "この間マウスは自動で動きます。止めるときは Esc キーを押してください。\n\n"
                        "goghの画面が見えていますか?(エクスプローラーは自動で開きます)"):
            return
        self.running = True
        self.root.iconify()
        threading.Thread(target=self._worker, args=(dry, single), daemon=True).start()

    def _ask_main(self, msg):
        ev = threading.Event()
        self.root.after(0, lambda: (self.root.deiconify(), messagebox.showinfo("gogh 自動ドロップくん", msg), ev.set()))
        ev.wait()

    # --- エクスプローラーの準備 ---
    def _open_explorer(self, m):
        """フォルダを開き、g のタイルと重ならない側に配置して、(hwnd) を返す"""
        leaf = os.path.basename(os.path.normpath(self.folder))
        hwnd, _ = uia_query(leaf)
        if not hwnd:
            os.startfile(self.folder)
            for _ in range(8):
                time.sleep(1.0)
                hwnd, _ = uia_query(leaf)
                if hwnd:
                    break
        if not hwnd:
            raise RuntimeError("エクスプローラーのウィンドウが見つかりません。")
        u = m.u
        sw, sh = u.GetSystemMetrics(0), u.GetSystemMetrics(1)
        xs = [p[0] for p in self.corners]
        w = int(sw * 0.36)
        # タイルが右寄りなら左に、左寄りなら右に置く
        x = 0 if (sum(xs) / len(xs)) > sw / 2 else sw - w
        u.ShowWindow(hwnd, 9)                                   # 最大化されていれば元に戻す
        u.SetWindowPos(hwnd, 0, x, 0, w, int(sh * 0.92), 0x0040)
        u.SetForegroundWindow(hwnd)
        time.sleep(0.8)
        self._exp_rect = (x, 0, w, int(sh * 0.92))
        return leaf

    def _scroll(self, m, notches):
        x, y, w, h = self._exp_rect
        m.move(x + w // 2, y + h // 2)
        time.sleep(0.1)
        m.wheel(notches)
        time.sleep(0.6)

    def _locate(self, m, leaf, filename):
        """ファイルの『つかむ位置』を返す。見えていなければスクロールして探す"""
        for attempt in range(25):
            r = find_item(self._cache, filename)            # 位置は使い回す (毎回聞くと遅い)
            if not r:
                _, self._cache = uia_query(leaf)
                r = find_item(self._cache, filename)
            if r:
                x, y, w, h = r
                return x + min(40, w // 2), y + h // 2         # 名前とアイコンの上
            self._check(m)
            self._scroll(m, -5 if attempt < 12 else 5)
            self._cache = {}
        raise RuntimeError(f"{filename} がエクスプローラーで見つかりません。")

    def _worker(self, dry, single=False):
        m = self.mouse
        m.slow = self.slow.get()
        cols, rows = self.cols.get(), self.rows.get()
        n = min(cols * rows, len(self.files))
        try:
            time.sleep(1.0)
            leaf = self._open_explorer(m)
            self._scroll(m, 100)                                # 先頭までスクロール
            self._cache = {}
            if dry:
                self.say("テスト: 4隅のタイルを順に指します…")
                for i in (0, cols - 1, cols * rows - cols, cols * rows - 1):
                    self._check(m); m.glide(*self._target(i, cols, rows), 0.6); time.sleep(0.5)
                self.say("テスト: 最初のファイルを探して指します…")
                m.glide(*self._locate(m, leaf, self.files[0]), 0.6)
                self.say("テスト終了。4隅が合っていなければ③をやり直してください。")
                return
            start = max(1, self.start.get())
            for k in range(start - 1, start if single else n):
                self._check(m)
                self.say(f"{k + 1:02d}/{n}  {self.files[k]} → {k // cols + 1}行{k % cols + 1}列")
                src = self._locate(m, leaf, self.files[k])
                dst = self._target(k, cols, rows)
                if self.autowait.get():
                    rect = self._patch_rect(dst)
                    before = grab(*rect)
                    for attempt in range(2):
                        self._drag(m, src, dst)
                        t1 = time.time()
                        res = self._wait_loaded(m, before, rect)
                        if res == "ok":
                            self.say(f"      読み込み完了 ({time.time() - t1:.1f}秒)")
                            break
                        self.say("      反応がないので、もう一度入れます…" if attempt == 0
                                 else "      ⚠ 反応なし。このマスは後で確認してください。")
                else:
                    self._drag(m, src, dst)
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
        """つかむ → 少し動かして『ドラッグ開始』を認識させる → ゲーム上へ運ぶ → 揺らす → 離す
        (時間はすべて「速さ」の倍率で伸び縮みします)"""
        try:
            m.glide(*src, 0.12, 12)
            m.wait(0.08)
            m.down()
            m.wait(0.08)
            for k in range(1, 4):                      # ドラッグ開始のしきい値(数px)を超える
                m.glide(src[0] + 10 * k, src[1] + 4 * k, 0.03, 2)
            m.glide(*dst, 0.3, 20)
            for dx, dy in ((3, 0), (-3, 2), (0, 0)):   # 落とす先にいると伝える
                m.glide(dst[0] + dx, dst[1] + dy, 0.05, 2)
            m.wait(0.12)
        finally:
            m.up()                                     # 何があっても必ず離す
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
        ctypes.windll.shcore.SetProcessDpiAwareness(2)  # 画面の拡大率に関わらず座標を一致させる
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass
    root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
