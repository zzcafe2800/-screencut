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
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

VERSION = "v0.11 (2026-11-21)"   # 更新するたびにここを上げる
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


def _period(vals, max_n=40):
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


def analyze_board(buf, bw, bh, ox, oy, board, cols, rows):
    """撮影画像 (BGRA, 左上が画面の ox,oy) から g を探し、位置を補正する。

    基準はあくまで「カードの4つの角」と「ファイル名の cols×rows」。
    - g が十分見つかれば、見つかった g 全部で射影変換を合わせ直す (クリックの誤差を補正)
    - 暗い・色が違う・g が少ない等で信用できなければ、4つの角の等分 (geometry) を使う
    戻り値: (targets[(x, y, gが見つかったか)], info)
    """
    geo = geo_targets(board, cols, rows)
    info = {"mode": "geometry", "reason": "", "detected": 0, "wall": None, "mismatch": False}
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
    if($w.Current.Name -like "*$($env:GOGH_LEAF)*"){ return $w } }
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
        root.geometry("580x680")
        self.mouse = Mouse()
        self.folder = ""
        self.files = []
        self.board = None       # カード(プロジェクター)の4つの角 [左上, 右上, 左下, 右下]
        self.targets = None     # 解析で決まった各マスの位置
        self.targets_key = None
        self.tile = 40
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

        f2 = ttk.LabelFrame(r, text="② マスの数 (フォルダ名の 5x8 などから自動) と速さ")
        f2.pack(fill="x", **pad)
        self.cols = tk.IntVar(value=5)
        self.rows = tk.IntVar(value=8)
        ttk.Label(f2, text="横").pack(side="left", padx=(8, 2))
        ttk.Spinbox(f2, from_=1, to=50, width=4, textvariable=self.cols).pack(side="left")
        ttk.Label(f2, text="× 縦").pack(side="left", padx=(8, 2))
        ttk.Spinbox(f2, from_=1, to=50, width=4, textvariable=self.rows).pack(side="left")
        ttk.Label(f2, text="  開始番号").pack(side="left", padx=(12, 2))
        self.start = tk.IntVar(value=1)
        ttk.Spinbox(f2, from_=1, to=999, width=4, textvariable=self.start).pack(side="left")
        ttk.Label(f2, text="  待ち秒").pack(side="left", padx=(12, 2))
        self.wait = tk.DoubleVar(value=0.2)
        ttk.Spinbox(f2, from_=0.0, to=10, increment=0.1, width=4, textvariable=self.wait).pack(side="left")
        ttk.Label(f2, text="  ゆっくり度").pack(side="left", padx=(12, 2))
        self.slow = tk.DoubleVar(value=0.5)
        ttk.Spinbox(f2, from_=0.1, to=5, increment=0.1, width=4, textvariable=self.slow).pack(side="left")
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
        g = parse_grid(d)
        if g:
            self.cols.set(g[0]); self.rows.set(g[1])
            self.say(f"フォルダ名から {g[0]}×{g[1]} を自動で設定しました。")
        n = self.cols.get() * self.rows.get()
        if len(self.files) != n:
            self.say(f"⚠ 画像は {len(self.files)} 枚ですが、マスは {n} 個です。数を確認してください。")

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
            self.say("カードの4つの角を保存しました。続けて位置を確認します…")
            self.root.after(300, self.analyze)

        cv.bind("<Button-1>", click)
        ov.bind("<Escape>", lambda e: finish(False))
        ov.focus_force()

    # --- 位置の確認 (画像で g を探して補正) ---
    def analyze(self):
        if not self.board:
            return messagebox.showinfo("gogh 自動ドロップくん", "先に「カードの4つの角を指定」をしてください。")
        cols, rows = self.cols.get(), self.rows.get()
        self.root.iconify()                       # 自分の画面が写り込まないように隠す
        self.root.after(600, lambda: self._analyze2(cols, rows))

    def _analyze2(self, cols, rows):
        try:
            x, y, w, h = capture_rect(self.board)
            buf = grab(x, y, w, h)
            targets, info = analyze_board(buf, w, h, x, y, self.board, cols, rows)
        except Exception as e:
            self.root.deiconify()
            return messagebox.showerror("確認", f"確認できませんでした: {e}")
        self.root.deiconify()
        if info["mode"] == "refined":
            self.say(f"確認: g を {info['detected']} 個見つけて、位置を補正しました。")
        else:
            self.say(f"確認: {info['reason']} → カードの4つの角を {cols}×{rows} に等分して位置を決めました。")
        self._preview(buf, w, h, x, y, targets, info, cols, rows)

    def _preview(self, buf, w, h, ox, oy, targets, info, cols, rows):
        ppm, f = to_ppm(buf, w, h, 640, 560)
        path = os.path.join(tempfile.gettempdir(), "gogh_autodrop_preview.ppm")
        with open(path, "wb") as fp:
            fp.write(ppm)
        win = tk.Toplevel(self.root)
        win.title("確認: この番号のマスに 01.png, 02.png … を貼ります")
        win.attributes("-topmost", True)
        img = tk.PhotoImage(file=path)
        cv = tk.Canvas(win, width=img.width(), height=img.height(), highlightthickness=0)
        cv.pack(padx=8, pady=8)
        cv.create_image(0, 0, image=img, anchor="nw")
        cv.image = img
        b = [((x - ox) / f, (y - oy) / f) for x, y in self.board]
        cv.create_polygon(*b[0], *b[1], *b[3], *b[2], outline="#00c8ff", fill="", width=2)
        for i, (tx, ty, ok) in enumerate(targets):
            px, py = (tx - ox) / f, (ty - oy) / f
            color = "#00e000" if ok else "#ff9900"
            cv.create_oval(px - 10, py - 10, px + 10, py + 10, outline=color, width=2, fill="#000000")
            cv.create_text(px, py, text=str(i + 1), fill="white", font=("Meiryo UI", 8, "bold"))
        if info.get("mismatch"):
            wc, wr = info["wall"]
            tk.Label(win, fg="#c00", font=("Meiryo UI", 10, "bold"), justify="left",
                     text=f"⚠ カードの g は 横{wc}×縦{wr} に見えます。ファイルは 横{cols}×縦{rows} です。\n"
                          f"   ファイルに合わせて {cols}×{rows} に等分した位置に貼ります。").pack(padx=8)
        mode = (f"g を {info['detected']} 個見つけて位置を補正しました。" if info["mode"] == "refined"
                else f"{info['reason']} → 4つの角を {cols}×{rows} に等分しました。")
        ttk.Label(win, text=mode + "\n緑 = g を確認したマス / オレンジ = 計算で決めたマス。"
                                   "番号の位置がズレていれば、4つの角を指定し直してください。",
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
        leaf = os.path.basename(os.path.normpath(self.folder))
        if self.finder is None or self.finder.p.poll() is not None or getattr(self, "_leaf", None) != leaf:
            if self.finder:
                self.finder.close()
            self.finder = ExplorerFinder(leaf)
            self._leaf = leaf
        hwnd = self.finder.hwnd()
        if not hwnd:
            os.startfile(self.folder)
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
            time.sleep(0.8)
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
