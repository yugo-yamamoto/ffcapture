"""synctest.mp4 を再生しながら録画したファイルを解析し、A/V ずれと実効 fps を測る。

    python tools/analyze_capture.py capture.mp4

やっていること:

  - 映像を小さなグレースケールに落として 1 フレームずつの平均輝度を取る
    → 毎秒のフラッシュが山になる
  - 音声を 8kHz モノラルに落として、映像 1 フレーム分の窓ごとの振幅を取る
    → 毎秒のビープが山になる
  - 2 つの波形を ±1 秒ずらしながら相関を取り、いちばん一致するずれ量を答えとする

フラッシュとビープは synctest.mp4 の中で同一時刻なので、ここで出るずれがそのまま
キャプチャ経路のずれになる。1 回のイベントではなく 29 回ぶんを使うので、
単発検出よりばらつきに強い。

負の値 = 音声が映像より先行（`-itsoffset` を正の方向に増やす）
正の値 = 音声が映像より遅延（`-itsoffset` を減らす）
"""

from __future__ import annotations

import array
import os
import subprocess
import sys

GW, GH = 64, 36          # 解析用の縮小サイズ（走査バーの動きを取りこぼさない程度）
AR = 8000                # 音声の解析サンプリングレート
MAX_LAG_S = 3.0          # 相関を取る最大ずれ（フラッシュが不等間隔なので広げても一意）


def run(argv: list[str]) -> bytes:
    r = subprocess.run(argv, capture_output=True)
    if r.returncode != 0:
        sys.exit("ffmpeg に失敗しました:\n" + r.stderr.decode("utf-8", "replace")[-500:])
    return r.stdout


def probe_fps(path: str) -> float:
    out = run(["ffprobe", "-v", "error", "-select_streams", "v:0",
               "-show_entries", "stream=avg_frame_rate", "-of", "csv=p=0", path])
    num, _, den = out.decode().strip().partition("/")
    return float(num) / float(den or 1)


def gray_frames(path: str) -> list[bytes]:
    raw = run(["ffmpeg", "-v", "error", "-i", path, "-an",
               "-vf", f"scale={GW}:{GH},format=gray", "-f", "rawvideo", "-"])
    n = GW * GH
    return [raw[i:i + n] for i in range(0, len(raw) - n + 1, n)]


def audio_series(path: str, frames: int, fps: float) -> list[float]:
    raw = run(["ffmpeg", "-v", "error", "-i", path, "-vn", "-ac", "1",
               "-ar", str(AR), "-f", "s16le", "-"])
    pcm = array.array("h")
    pcm.frombytes(raw[:len(raw) // 2 * 2])
    win = max(1, int(AR / fps))
    out = []
    for i in range(frames):
        seg = pcm[i * win:(i + 1) * win]
        out.append(max((abs(x) for x in seg), default=0))
    return out


def normalize(xs: list[float]) -> list[float]:
    lo, hi = min(xs), max(xs)
    if hi - lo < 1e-9:
        return [0.0] * len(xs)
    mid = sum(xs) / len(xs)
    return [(x - mid) / (hi - lo) for x in xs]


def best_lag(v: list[float], a: list[float], fps: float) -> tuple[int, float]:
    """a を lag フレームずらしたとき最も相関する lag を返す。"""
    max_lag = int(MAX_LAG_S * fps)
    best, best_lag_ = -2.0, 0
    for lag in range(-max_lag, max_lag + 1):
        s, n = 0.0, 0
        for i in range(len(v)):
            j = i + lag
            if 0 <= j < len(a):
                s += v[i] * a[j]
                n += 1
        if n:
            c = s / n
            if c > best:
                best, best_lag_ = c, lag
    return best_lag_, best


def effective_fps(frames: list[bytes], fps: float) -> float:
    """直前と同じ絵のフレームを除いた、実際に更新された回数から出す。

    平均輝度の差では走査バーのような小さな動きを取りこぼすので、画素単位の最大差で見る。
    """
    changed = sum(1 for a, b in zip(frames, frames[1:])
                  if max(abs(x - y) for x, y in zip(a, b)) >= 4)
    return changed / (len(frames) / fps) if frames else 0.0


def measure_offset_ms(path: str) -> tuple[float, float, list[bytes], list[float], float]:
    """1 ファイル分の (ずれms, 相関, フレーム列, 音声列, fps) を返す。"""
    fps = probe_fps(path)
    frames = gray_frames(path)
    if len(frames) < 10:
        sys.exit(f"映像フレームが読めませんでした: {path}")
    n = GW * GH
    luma = [sum(f) / n for f in frames]
    audio = audio_series(path, len(frames), fps)
    lag, corr = best_lag(normalize(luma), normalize(audio), fps)
    return lag / fps * 1000.0, corr, frames, audio, fps


def find_reference() -> str | None:
    """較正用の synctest.mp4 を探す。"""
    here = os.path.dirname(os.path.abspath(__file__))
    for c in ("synctest.mp4",
              os.path.join(os.getcwd(), "synctest.mp4"),
              os.path.join(here, "..", "synctest.mp4")):
        if os.path.exists(c):
            return os.path.abspath(c)
    return None


def main() -> int:
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    path = sys.argv[1]
    ms, corr, frames, audio, fps = measure_offset_ms(path)
    if max(audio) < 200:
        sys.exit("音声がほぼ無音です。ミュートや再生デバイスを確認してください "
                 f"(最大振幅 {max(audio)} / 32767)。")

    # 基準クリップ自身にも AAC のプライミング等で数十 ms のバイアスがあるので差し引く
    ref = None if "--no-calib" in sys.argv else find_reference()
    bias = 0.0
    if ref and os.path.abspath(ref) != os.path.abspath(path):
        bias, _, _, _, _ = measure_offset_ms(ref)
        ms -= bias

    print(f"ファイル      : {path}")
    print(f"フレーム数    : {len(frames)}  ({len(frames)/fps:.2f} 秒 / {fps:g} fps)")
    eff = effective_fps(frames, fps)
    print(f"画面が変化した回数: {eff:.1f} 回/秒"
          f"（{fps:g} fps に対して {eff/fps*100:.0f}%）")
    print("  ※ キャプチャ速度ではなく『映っている中身が変わった頻度』です。"
          "静止部分だけを矩形で切り取ると低く出ます。")
    print(f"音声最大振幅  : {max(audio)} / 32767")
    if ref and bias:
        print(f"基準クリップ  : {ref} で較正 (バイアス {bias:+.0f} ms を差し引き)")
    elif not ref:
        print("基準クリップ  : synctest.mp4 が見つからず未較正（数十 ms のバイアスが残ります）")
    print()
    print(f"A/V ずれ      : {ms:+.0f} ms  (相関 {corr:.3f})")
    if abs(ms) > MAX_LAG_S * 1000 - 100:
        print(f"  ※ 探索範囲 ±{MAX_LAG_S:g} 秒の端に張り付いています。"
              "実際のずれはこれより大きい可能性があります。")
    if corr < 0.02:
        print("  ※ 相関が低く、測定が不確かです。フラッシュが画面内に大きく写るように"
              "録り直してください。")
    if abs(ms) < 25:
        print("  → 十分に合っています。")
    elif ms < 0:
        print(f"  → 音声が {abs(ms):.0f} ms 先行しています。"
              f"「音声オフセット (ms)」を今より {abs(ms):.0f} ms 大きくしてください。")
    else:
        print(f"  → 音声が {ms:.0f} ms 遅れています。"
              f"「音声オフセット (ms)」を今より {ms:.0f} ms 小さくしてください。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
