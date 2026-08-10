"""A/V 同期とコマ落ちを目視・実測で確かめるためのテストクリップを作る。

    python tools/make_synctest.py [出力ファイル名]

作られるクリップ (既定 synctest.mp4, 1280x720 / 60fps / 30秒):

  - 画面全体が 0.1 秒だけ白くフラッシュする（**不等間隔**に 22 回）
  - **まったく同じ時刻に** 1kHz のビープが 0.1 秒鳴る（同一の式で生成しているのでサンプル単位で一致）
    間隔を不等にしてあるのは、等間隔だと相関のピークが 1 秒ごとに繰り返して
    ずれ量を一意に決められなくなるため
  - 画面下部をシアンのバーが 1 秒で左から右へ走査する（コマ落ちすると動きが階段状になる）
  - 経過時刻とフレーム番号を表示する

これを再生しながら ffcapture で録画し、録画結果を tools/analyze_capture.py に渡すと、
音声が映像より何ミリ秒ずれているかと、実効フレームレートが数値で出る。
"""

from __future__ import annotations

import os
import subprocess
import sys

W, H, FPS, SECONDS = 1280, 720, 60, 30

# フラッシュを出す時刻（秒）。**わざと不等間隔**にしてある。
# 等間隔だと相関が 1 秒ごとに同じ値を取り、ずれ量が周期の整数倍だけ食い違っても
# 区別できない（±1 秒のエイリアシング）ため。
FLASH_AT = [1.0, 2.4, 3.3, 5.1, 6.0, 7.8, 8.5, 10.2, 11.7, 12.4,
            14.3, 15.0, 16.9, 18.2, 19.1, 20.6, 22.3, 23.0, 24.8, 26.1, 27.4, 28.9]
FLASH_LEN = 0.1

# フラッシュとビープを支配する共通の式。両方これを掛けるので厳密に同時刻になる
GATE = "+".join(f"between(t,{t},{t + FLASH_LEN})" for t in FLASH_AT)

FONT_CANDIDATES = [
    "/usr/share/fonts/truetype/noto/NotoSans-Regular.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "C:/Windows/Fonts/consola.ttf",
    "C:/Windows/Fonts/arial.ttf",
]


def find_font() -> str | None:
    return next((f for f in FONT_CANDIDATES if os.path.exists(f)), None)


def build_filter() -> str:
    # 走査バー: 1 秒で画面を横切る。コマ落ちすると動きが飛ぶので目視で分かる
    layers = [
        f"drawbox=x='(iw-48)*mod(t,1)':y=ih-160:w=48:h=120:color=cyan:t=fill",
        f"drawbox=x=0:y=0:w=iw:h=ih:color=white:t=fill:enable='{GATE}'",
    ]
    font = find_font()
    if font:
        esc = font.replace(":", "\\:")
        layers.insert(0, (
            f"drawtext=fontfile='{esc}':text='%{{pts\\:hms}}   frame %{{n}}'"
            f":x=48:y=64:fontsize=56:fontcolor=white:box=1:boxcolor=black@0.6:boxborderw=16"))
        layers.insert(1, (
            f"drawtext=fontfile='{esc}':text='ffcapture sync test  -  flash and beep are simultaneous'"
            f":x=48:y=160:fontsize=32:fontcolor=0xaaaaaa"))
    return ",".join(layers)


def main() -> int:
    out = sys.argv[1] if len(sys.argv) > 1 else "synctest.mp4"
    argv = [
        "ffmpeg", "-hide_banner", "-y",
        "-f", "lavfi", "-i", f"color=c=0x101010:s={W}x{H}:r={FPS}:d={SECONDS}",
        "-f", "lavfi",
        "-i", f"aevalsrc='0.7*sin(2*PI*1000*t)*{GATE}':d={SECONDS}:s=48000",
        "-vf", build_filter(),
        "-c:v", "libx264", "-preset", "medium", "-crf", "18", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "192k",
        "-movflags", "+faststart", out,
    ]
    print(" ".join(argv))
    r = subprocess.run(argv)
    if r.returncode != 0:
        return r.returncode
    size = os.path.getsize(out)
    print(f"\n{out} を作成しました ({size/1e6:.1f} MB, {W}x{H} {FPS}fps {SECONDS}秒)")
    if not find_font():
        print("※ フォントが見つからなかったので、時刻とフレーム番号の表示は省略しました。")
    print("再生しながら録画し、録れたファイルを次に渡してください:")
    print("    python tools/analyze_capture.py <録画したファイル>")
    return 0


if __name__ == "__main__":
    sys.exit(main())
