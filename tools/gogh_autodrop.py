"""
gogh 自動ドロップくん (Windows 用)

便利くんGの「分割」で作った ZIP を展開したフォルダの画像 (01.png, 02.png ...)
を、gogh の画面上の g タイルへ「左上→右へ、上の段→下の段」の順に
マウス操作でドラッグ&ドロップして自動で貼り付けます。

【準備】
  1. Python 3 を入れる (https://www.python.org/)
  2. コマンドプロンプトで:  pip install pyautogui numpy
  3. ZIP を展開する。エクスプローラーで展開先のフォルダを開き、
     ・表示 → 「詳細」(または一覧) ・名前順 に並べる
     ・ウィンドウを gogh のタイルを隠さない位置(左側など)に置く
     ・40 個ぜんぶが縦一列に見える大きさにする (見えない場合は --per-screen 参照)
  4. gogh は編集モードで、タイルが全部見える状態にしておく (カメラは動かさない)

【実行】
  python gogh_autodrop.py --cols 5 --rows 8
  (先に --dry-run を付けると、クリックせずマウスが動くだけなので位置確認できます)

【途中で止める】 マウスを画面の左上角へ思い切り動かす (pyautogui のフェイルセーフ)

【途中から再開】 --start 13  (13 番の画像から)
"""
import argparse
import sys
import time

import numpy as np
import pyautogui

pyautogui.FAILSAFE = True
pyautogui.PAUSE = 0.02


def ask_point(msg):
    input(f"\n{msg}\n  マウスをその位置に置いて、このウィンドウに戻って Enter...")
    p = pyautogui.position()
    print(f"  → ({p.x}, {p.y})")
    return (p.x, p.y)


def homography(src, dst):
    """src(4点) → dst(4点) の射影変換行列"""
    A, b = [], []
    for (x, y), (u, v) in zip(src, dst):
        A.append([x, y, 1, 0, 0, 0, -u * x, -u * y])
        A.append([0, 0, 0, x, y, 1, -v * x, -v * y])
        b += [u, v]
    h = np.linalg.solve(np.array(A, float), np.array(b, float))
    return np.append(h, 1).reshape(3, 3)


def apply_h(H, x, y):
    u, v, w = H @ np.array([x, y, 1.0])
    return u / w, v / w


def drag(src, dst, dry):
    pyautogui.moveTo(*src, duration=0.25)
    if dry:
        time.sleep(0.3)
        pyautogui.moveTo(*dst, duration=0.5)
        time.sleep(0.3)
        return
    pyautogui.mouseDown()
    time.sleep(0.15)
    # OS のドラッグ開始を認識させるため少しずつ動かす
    pyautogui.moveRel(15, 5, duration=0.1)
    pyautogui.moveTo(*dst, duration=0.6)
    time.sleep(0.25)
    pyautogui.moveRel(2, 2, duration=0.05)  # ゲーム側にホバーを認識させる
    time.sleep(0.25)
    pyautogui.mouseUp()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cols", type=int, default=5, help="タイルの横の数")
    ap.add_argument("--rows", type=int, default=8, help="タイルの縦の数")
    ap.add_argument("--start", type=int, default=1, help="何番の画像から始めるか")
    ap.add_argument("--wait", type=float, default=1.5, help="1枚ごとの待ち秒数")
    ap.add_argument("--dry-run", action="store_true", help="クリックせず動くだけ")
    ap.add_argument("--per-screen", type=int, default=0,
                    help="エクスプローラーに一度に見えるファイル数 (0=全部見えている)")
    a = ap.parse_args()
    n = a.cols * a.rows
    visible = a.per_screen or n

    print(f"タイル {a.cols}列 × {a.rows}行 = {n}枚。順番は 左上→右→下 です。")
    print("3秒後に位置合わせを始めます。gogh とエクスプローラーを並べて見えるようにしてください。")
    time.sleep(3)

    # --- タイル位置の校正: 4隅のタイルの中心 ---
    tl = ask_point("① 左上のタイルの中心")
    tr = ask_point("② 右上のタイルの中心")
    bl = ask_point("③ 左下のタイルの中心")
    br = ask_point("④ 右下のタイルの中心")
    # 単位正方形 → 画面 (斜めに見えるタイルにも対応する射影変換)
    H = homography([(0, 0), (1, 0), (0, 1), (1, 1)], [tl, tr, bl, br])

    def tile_pos(idx):
        r, c = divmod(idx, a.cols)
        u = c / (a.cols - 1) if a.cols > 1 else 0
        v = r / (a.rows - 1) if a.rows > 1 else 0
        x, y = apply_h(H, u, v)
        return int(x), int(y)

    # --- ファイル位置の校正: エクスプローラーの先頭と末尾の行 ---
    f1 = ask_point("⑤ エクスプローラーの先頭ファイル(01)の名前の上")
    fl = ask_point(f"⑥ エクスプローラーの {visible} 番目に見えているファイルの名前の上")
    step = (fl[1] - f1[1]) / max(visible - 1, 1)

    def file_pos(idx):
        row = idx % visible
        return int(f1[0]), int(f1[1] + step * row)

    # 動作確認: 4隅だけ動かして見せる
    print("\n位置確認: マウスが4隅のタイルを順に指します。")
    for i in (0, a.cols - 1, n - a.cols, n - 1):
        pyautogui.moveTo(*tile_pos(i), duration=0.5)
        time.sleep(0.4)
    if input("合っていれば y + Enter (違えばやり直し): ").strip().lower() != "y":
        sys.exit("やり直してください。")

    print(f"\n開始します ({'ドライラン' if a.dry_run else '本番'})。止めるときはマウスを画面の左上角へ。")
    time.sleep(2)
    for k in range(a.start - 1, n):
        # per-screen より多いときは、区切りごとにスクロールをお願いする
        if a.per_screen and k % visible == 0 and k >= a.start:
            input(f"\nエクスプローラーを {k + 1} 番目が先頭になるようスクロールして Enter...")
        print(f"{k + 1:02d}/{n}  → {k // a.cols + 1}行 {k % a.cols + 1}列")
        drag(file_pos(k), tile_pos(k), a.dry_run)
        time.sleep(a.wait)
    print("\n完了!")


if __name__ == "__main__":
    main()
