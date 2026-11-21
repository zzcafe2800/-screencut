"""
gogh 自動ドロップくん (Windows 用 / 画面つき)

便利くんGで分割した画像 (01.png 〜) を、goghの画面上の g タイルへ
「左上 → 右 → 下の段」の順に、マウス操作でドラッグ&ドロップして自動で貼ります。

■ 安全性
  - 外部ライブラリなし (Python 標準のみ)。インターネット通信は一切しません。
  - ゲームのメモリやファイルには触りません。人間と同じようにマウスを動かすだけです。
  - 管理者権限は不要。ファイルは設定ファイル (gogh_autodrop_settings.json) 以外書き込みません。
  - Esc キーを押すといつでも中止できます。
"""
import ctypes
import json
import os
import re
import sys
import threading
import time
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

IMG_EXT = (".png", ".jpg", ".jpeg", ".gif", ".webp")
SETTINGS = os.path.join(
    os.path.dirname(sys.executable if getattr(sys, "frozen", False) else os.path.abspath(__file__)),
    "gogh_autodrop_settings.json",
)


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


# ───────────── マウス操作 (Windows) ─────────────
class Mouse:
    def __init__(self):
        self.u = ctypes.windll.user32

    def pos(self):
        class P(ctypes.Structure):
            _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]
        p = P()
        self.u.GetCursorPos(ctypes.byref(p))
        return p.x, p.y

    def move(self, x, y):
        self.u.SetCursorPos(int(x), int(y))

    def glide(self, x, y, sec=0.4, steps=25):
        x0, y0 = self.pos()
        for i in range(1, steps + 1):
            t = i / steps
            self.move(x0 + (x - x0) * t, y0 + (y - y0) * t)
            time.sleep(sec / steps)

    def down(self):
        self.u.mouse_event(0x0002, 0, 0, 0, 0)

    def up(self):
        self.u.mouse_event(0x0004, 0, 0, 0, 0)

    def esc_pressed(self):
        return bool(self.u.GetAsyncKeyState(0x1B) & 0x8000)


class Cancelled(Exception):
    pass


# ───────────── 画面 ─────────────
class App:
    def __init__(self, root):
        self.root = root
        root.title("gogh 自動ドロップくん")
        root.geometry("560x620")
        self.mouse = Mouse()
        self.folder = ""
        self.files = []
        self.corners = None   # 4隅のタイル中心
        self.fpts = None      # ファイル先頭/末尾 (見えている範囲)
        self.running = False
        self._load()
        self._build()
        self._refresh()

    # --- 設定の保存 ---
    def _load(self):
        try:
            with open(SETTINGS, encoding="utf-8") as f:
                d = json.load(f)
            self.corners = [tuple(p) for p in d["corners"]] if d.get("corners") else None
            self.fpts = [tuple(p) for p in d["fpts"]] if d.get("fpts") else None
        except Exception:
            pass

    def _save(self):
        try:
            with open(SETTINGS, "w", encoding="utf-8") as f:
                json.dump({"corners": self.corners, "fpts": self.fpts}, f)
        except Exception:
            pass

    # --- UI ---
    def _build(self):
        r = self.root
        pad = {"padx": 12, "pady": 4}
        ttk.Label(r, text="gogh 自動ドロップくん", font=("Meiryo UI", 15, "bold")).pack(**pad)

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
        self.wait = tk.DoubleVar(value=1.5)
        ttk.Spinbox(f2, from_=0.3, to=10, increment=0.5, width=4, textvariable=self.wait).pack(side="left")

        f3 = ttk.LabelFrame(r, text="③ 位置を教える (goghとフォルダを並べて表示してから)")
        f3.pack(fill="x", **pad)
        row = ttk.Frame(f3); row.pack(fill="x", pady=3)
        ttk.Button(row, text="gのタイル4隅を指定", command=self.calib_tiles).pack(side="left", padx=8)
        self.lbl_tiles = ttk.Label(row, text="")
        self.lbl_tiles.pack(side="left")
        row = ttk.Frame(f3); row.pack(fill="x", pady=3)
        ttk.Button(row, text="ファイルの位置を指定", command=self.calib_files).pack(side="left", padx=8)
        self.lbl_files = ttk.Label(row, text="")
        self.lbl_files.pack(side="left")
        row = ttk.Frame(f3); row.pack(fill="x", pady=3)
        ttk.Button(row, text="フォルダをエクスプローラーで開く", command=self.open_folder).pack(side="left", padx=8)

        f4 = ttk.LabelFrame(r, text="④ 実行 (止めるときは Esc キー)")
        f4.pack(fill="x", **pad)
        row = ttk.Frame(f4); row.pack(pady=6)
        ttk.Button(row, text="テスト(動くだけ)", command=lambda: self.run(True)).pack(side="left", padx=8)
        self.btn_go = ttk.Button(row, text="開始", command=lambda: self.run(False))
        self.btn_go.pack(side="left", padx=8)

        self.log = tk.Text(r, height=12, state="disabled", font=("Meiryo UI", 9))
        self.log.pack(fill="both", expand=True, **pad)
        self.say("使い方: ①フォルダを選ぶ → ②数を確認 → ③位置を教える → ④テスト → 開始")

    def say(self, msg):
        def f():
            self.log.configure(state="normal")
            self.log.insert("end", msg + "\n")
            self.log.see("end")
            self.log.configure(state="disabled")
        self.root.after(0, f)

    def _refresh(self):
        self.lbl_tiles.config(text="設定済み ✔" if self.corners else "未設定")
        self.lbl_files.config(text="設定済み ✔" if self.fpts else "未設定")

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

    def open_folder(self):
        if self.folder:
            os.startfile(self.folder)
            self.say("エクスプローラーを「表示 → 詳細」「名前順」にして、gogh のタイルを隠さない位置に置いてください。")
        else:
            messagebox.showinfo("gogh 自動ドロップくん", "先にフォルダを選んでください。")

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
            self.say("タイル位置を保存しました。")
        self.collect_clicks([
            "① 左上のgタイルの中心をクリック",
            "② 右上のgタイルの中心をクリック",
            "③ 左下のgタイルの中心をクリック",
            "④ 右下のgタイルの中心をクリック",
        ], done)

    def calib_files(self):
        def done(p):
            self.fpts = p
            self._save(); self._refresh()
            self.say("ファイル位置を保存しました。")
        self.collect_clicks([
            "エクスプローラーの【一番上のファイル(01)】をクリック",
            "エクスプローラーの【一番下に見えているファイル】をクリック",
        ], done)

    # --- 実行 ---
    def run(self, dry):
        if self.running:
            return
        n = self.cols.get() * self.rows.get()
        if not self.files:
            return messagebox.showinfo("gogh 自動ドロップくん", "先にフォルダを選んでください。")
        if not self.corners or not self.fpts:
            return messagebox.showinfo("gogh 自動ドロップくん", "③ で位置を教えてください。")
        if not dry and not messagebox.askokcancel(
                "確認", f"{min(n, len(self.files))}枚を貼り付けます。\n"
                        "この間マウスは自動で動きます。止めるときは Esc キーを押してください。\n\n"
                        "goghの画面とフォルダが見えていますか?"):
            return
        self.running = True
        self.root.iconify()
        threading.Thread(target=self._worker, args=(dry,), daemon=True).start()

    def _ask_main(self, msg):
        ev = threading.Event()
        self.root.after(0, lambda: (self.root.deiconify(), messagebox.showinfo("gogh 自動ドロップくん", msg), ev.set()))
        ev.wait()

    def _worker(self, dry):
        m = self.mouse
        cols, rows = self.cols.get(), self.rows.get()
        n = min(cols * rows, len(self.files))
        # ファイルの縦間隔: 先頭と末尾の2点から。見えている数は「末尾までの個数」=
        # 画像数 n が見えている前提。入りきらない場合は下で分けて処理
        (fx, fy0), (_, fy1) = self.fpts
        try:
            time.sleep(2.0)
            if dry:
                self.say("テスト: 4隅のタイルを順に指します…")
                for i in (0, cols - 1, cols * rows - cols, cols * rows - 1):
                    self._check(m); m.glide(*tile_pos(self.corners, cols, rows, i), 0.6); time.sleep(0.5)
                self.say("テスト: 最初のファイルと最後のファイルを指します…")
                m.glide(fx, fy0, 0.6); time.sleep(0.5)
                m.glide(fx, fy1, 0.6)
                self.say("テスト終了。位置が合っていなければ③をやり直してください。")
                return
            start = max(1, self.start.get())
            step = (fy1 - fy0) / max(n - 1, 1)
            for k in range(start - 1, n):
                self._check(m)
                src = (fx, int(fy0 + step * k))
                dst = tile_pos(self.corners, cols, rows, k)
                self.say(f"{k + 1:02d}/{n}  {self.files[k]} → {k // cols + 1}行{k % cols + 1}列")
                m.glide(*src, 0.3)
                m.down(); time.sleep(0.15)
                m.glide(src[0] + 15, src[1] + 5, 0.1, 5)   # ドラッグ開始を認識させる
                m.glide(*dst, 0.6)
                time.sleep(0.3)
                m.glide(dst[0] + 2, dst[1] + 2, 0.05, 3)
                time.sleep(0.3)
                m.up()
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
