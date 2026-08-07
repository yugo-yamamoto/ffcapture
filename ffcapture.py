"""ffcapture - Windows 用 画面キャプチャ ffmpeg コマンドビルダー (Tkinter GUI)

依存は標準ライブラリのみ。uv は使わず python.exe / pythonw.exe で直接動かす。

    pythonw ffcapture.py            GUI を起動 (コンソールなし)
    python  ffcapture.py            GUI を起動 (コンソールあり)
    python  ffcapture.py --list     音声エンドポイントと DirectShow デバイスを一覧表示

映像:
    - ウィンドウタイトル指定 (gdigrab -i title=...)
    - 矩形指定 / 画面全体      (gdigrab -i desktop -offset_x/-offset_y/-video_size)
音声:
    - DirectShow の音声デバイスを -f dshow -i audio="..." で指定する（コマンドだけで完結）
    - PC の再生音を録るにはステレオミキサー等のループバック用デバイスが必要。
      Realtek 等が持っていても既定で無効化されていることが多いので、
      レジストリ (HKLM\\...\\MMDevices\\Audio\\Capture) から状態を検出して有効化できる。
"""

from __future__ import annotations

import argparse
import ctypes
import ctypes.wintypes as wt
import datetime as dt
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time

IS_WINDOWS = os.name == "nt"
CREATE_NO_WINDOW = 0x08000000 if IS_WINDOWS else 0
CREATE_NEW_PROCESS_GROUP = 0x00000200 if IS_WINDOWS else 0

# ループバック録音に使える名前のパターン（ステレオミキサー / 仮想デバイス）
LOOPBACK_NAME_RE = re.compile(
    r"ステレオ\s*ミキサ|stereo\s*mix|what\s*u\s*hear|wave\s*out\s*mix|"
    r"virtual-audio-capturer|CABLE\s*Output|VoiceMeeter\s*Output|ループバック",
    re.I)


# --------------------------------------------------------------------------------------
# DPI 対応 (gdigrab は物理ピクセルなので、プロセスを DPI aware にして座標系を一致させる)
# --------------------------------------------------------------------------------------


def enable_dpi_awareness() -> float:
    """DPI awareness を有効化し、システム DPI を返す。"""
    if not IS_WINDOWS:
        return 96.0
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)  # PER_MONITOR_DPI_AWARE
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass
    try:
        return float(ctypes.windll.user32.GetDpiForSystem())
    except Exception:
        return 96.0


# --------------------------------------------------------------------------------------
# Win32: ウィンドウ列挙 / 仮想デスクトップ矩形
# --------------------------------------------------------------------------------------

SM_XVIRTUALSCREEN, SM_YVIRTUALSCREEN = 76, 77
SM_CXVIRTUALSCREEN, SM_CYVIRTUALSCREEN = 78, 79
DWMWA_CLOAKED = 14


def virtual_screen_rect() -> tuple[int, int, int, int]:
    """(x, y, w, h) 仮想デスクトップ全体（マルチモニタ込み）。"""
    if not IS_WINDOWS:
        return (0, 0, 1920, 1080)
    g = ctypes.windll.user32.GetSystemMetrics
    return (g(SM_XVIRTUALSCREEN), g(SM_YVIRTUALSCREEN),
            g(SM_CXVIRTUALSCREEN), g(SM_CYVIRTUALSCREEN))


def _is_cloaked(hwnd: int) -> bool:
    """UWP の非表示ウィンドウなどを弾く。"""
    val = ctypes.c_int(0)
    try:
        hr = ctypes.windll.dwmapi.DwmGetWindowAttribute(
            wt.HWND(hwnd), ctypes.c_uint(DWMWA_CLOAKED),
            ctypes.byref(val), ctypes.sizeof(val))
    except Exception:
        return False
    return hr == 0 and val.value != 0


def enum_windows() -> list[dict]:
    """可視でタイトルを持つトップレベルウィンドウを列挙する。"""
    if not IS_WINDOWS:
        return []
    user32 = ctypes.windll.user32
    result: list[dict] = []
    self_pid = os.getpid()

    CB = ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)

    def cb(hwnd, _lparam):
        if not user32.IsWindowVisible(hwnd):
            return True
        length = user32.GetWindowTextLengthW(hwnd)
        if length <= 0:
            return True
        buf = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buf, length + 1)
        title = buf.value.strip()
        if not title or _is_cloaked(hwnd):
            return True
        pid = wt.DWORD(0)
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if pid.value == self_pid:
            return True
        rect = wt.RECT()
        user32.GetWindowRect(hwnd, ctypes.byref(rect))
        w, h = rect.right - rect.left, rect.bottom - rect.top
        if w <= 1 or h <= 1:
            return True
        result.append({"hwnd": hwnd, "title": title,
                       "x": rect.left, "y": rect.top, "w": w, "h": h})
        return True

    user32.EnumWindows(CB(cb), 0)
    # 同名タイトルは重複除去（gdigrab はタイトル文字列でしか指定できないため）
    seen: set[str] = set()
    uniq = []
    for it in result:
        if it["title"] in seen:
            continue
        seen.add(it["title"])
        uniq.append(it)
    uniq.sort(key=lambda d: d["title"].lower())
    return uniq


def window_rect_by_title(title: str) -> tuple[int, int, int, int] | None:
    for w in enum_windows():
        if w["title"] == title:
            return (w["x"], w["y"], w["w"], w["h"])
    return None


# --------------------------------------------------------------------------------------
# 音声エンドポイント (MMDevices レジストリ) — ステレオミキサーの検出と有効化
# --------------------------------------------------------------------------------------

MMDEV_CAPTURE = r"SOFTWARE\Microsoft\Windows\CurrentVersion\MMDevices\Audio\Capture"
PROP_DESC = "{a45c254e-df1c-4efd-8020-67d146a850e0},2"      # "ステレオ ミキサー"
PROP_FRIENDLY = "{b3f8fa53-0004-438e-9003-51a46e139bfc},6"  # "Realtek(R) Audio"

# DeviceState の下位 4bit は DEVICE_STATE_*、上位 0x10000000 が「ユーザーによる無効化」。
# （実機確認: 有効なエンドポイント = 0x00000001 / 無効化されたもの = 0x10000001）
STATE_MASK = 0x0000000F
FLAG_DISABLED = 0x10000000


def endpoint_state_name(state: int) -> str:
    low = state & STATE_MASK
    if low == 0x4:
        return "未接続"
    if low == 0x8:
        return "プラグ未挿入"
    if low == 0x2 or (state & FLAG_DISABLED):
        return "無効"
    return "有効"


def list_capture_endpoints() -> list[dict]:
    """録音エンドポイントを {guid, name, state, state_name} で列挙する。"""
    if not IS_WINDOWS:
        return []
    import winreg  # noqa: PLC0415

    out: list[dict] = []
    access = winreg.KEY_READ | getattr(winreg, "KEY_WOW64_64KEY", 0)
    try:
        root = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, MMDEV_CAPTURE, 0, access)
    except OSError:
        return []
    with root:
        i = 0
        while True:
            try:
                guid = winreg.EnumKey(root, i)
            except OSError:
                break
            i += 1
            try:
                with winreg.OpenKey(root, guid, 0, access) as k:
                    state = int(winreg.QueryValueEx(k, "DeviceState")[0])
                    with winreg.OpenKey(k, "Properties", 0, access) as pk:
                        def prop(name):
                            try:
                                return str(winreg.QueryValueEx(pk, name)[0])
                            except OSError:
                                return ""
                        desc, friendly = prop(PROP_DESC), prop(PROP_FRIENDLY)
            except OSError:
                continue
            name = f"{desc} ({friendly})" if desc and friendly else (desc or friendly)
            if not name:
                continue
            out.append({"guid": guid, "name": name, "desc": desc,
                        "state": state, "state_name": endpoint_state_name(state)})
    out.sort(key=lambda d: (d["state_name"] != "有効", d["name"]))
    return out


def find_loopback_endpoints() -> list[dict]:
    """ステレオミキサー等、PC の再生音を拾えるエンドポイントだけ抜き出す。"""
    seen: set[str] = set()
    out = []
    for e in list_capture_endpoints():
        if not LOOPBACK_NAME_RE.search(e["name"]):
            continue
        if e["state_name"] == "未接続":   # 過去のハードウェアの残骸
            continue
        if e["name"] in seen:
            continue
        seen.add(e["name"])
        out.append(e)
    return out


ENABLE_PS = r"""
$ErrorActionPreference = 'Stop'
$key = 'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\MMDevices\Audio\Capture\{guid}'
Set-ItemProperty -Path $key -Name DeviceState -Value {value} -Type DWord
Restart-Service -Name AudioEndpointBuilder -Force
"""


def enable_endpoint_elevated(guid: str, enable: bool = True) -> subprocess.CompletedProcess:
    """管理者権限の PowerShell でエンドポイントの有効/無効を切り替える。

    UAC のダイアログが出る。AudioEndpointBuilder の再起動で一瞬だけ音が途切れる。
    """
    inner = ENABLE_PS.format(guid=guid, value=1 if enable else 0x10000001)
    b64 = __import__("base64").b64encode(inner.encode("utf-16-le")).decode()
    launcher = (
        "$p = Start-Process powershell -Verb RunAs -Wait -PassThru "
        f"-ArgumentList '-NoProfile','-EncodedCommand','{b64}'; exit $p.ExitCode")
    return subprocess.run(
        ["powershell.exe", "-NoProfile", "-Command", launcher],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        creationflags=CREATE_NO_WINDOW)


def open_sound_control_panel():
    """サウンド コントロールパネルの「録音」タブを開く。"""
    subprocess.Popen(
        ["rundll32.exe", "shell32.dll,Control_RunDLL", "mmsys.cpl,,1"],
        creationflags=CREATE_NO_WINDOW)


# --------------------------------------------------------------------------------------
# ffmpeg 側のデバイス列挙
# --------------------------------------------------------------------------------------


def _run_ffmpeg(ffmpeg: str, args: list[str], timeout: int = 30):
    return subprocess.run([ffmpeg, "-hide_banner", *args],
                          capture_output=True, text=True, encoding="utf-8",
                          errors="replace", timeout=timeout,
                          creationflags=CREATE_NO_WINDOW)


def list_dshow_audio(ffmpeg: str) -> list[str]:
    """ffmpeg -list_devices から DirectShow の音声デバイス名を取り出す。"""
    try:
        r = _run_ffmpeg(ffmpeg, ["-list_devices", "true", "-f", "dshow", "-i", "dummy"], 20)
    except Exception:
        return []
    out = (r.stderr or "") + (r.stdout or "")
    names, seen = [], set()
    for m in re.finditer(r'"([^"]+)"\s*\(audio\)', out):
        if m.group(1) not in seen:
            seen.add(m.group(1))
            names.append(m.group(1))
    return names


def list_ffmpeg_encoders(ffmpeg: str) -> set[str]:
    try:
        r = _run_ffmpeg(ffmpeg, ["-encoders"])
    except Exception:
        return set()
    return {m.group(1) for m in re.finditer(r"^\s*[A-Z.]{6}\s+(\S+)", r.stdout, re.M)}


# --------------------------------------------------------------------------------------
# ffmpeg コマンド組み立て
# --------------------------------------------------------------------------------------

VIDEO_ENCODERS = {
    "libx264 (CPU/H.264)": {
        "name": "libx264",
        "presets": ["ultrafast", "superfast", "veryfast", "faster", "fast",
                    "medium", "slow"],
        "default_preset": "veryfast",
        "quality_flag": "crf", "quality_label": "CRF (低いほど高画質)",
    },
    "libx265 (CPU/H.265)": {
        "name": "libx265",
        "presets": ["ultrafast", "superfast", "veryfast", "faster", "fast",
                    "medium", "slow"],
        "default_preset": "veryfast",
        "quality_flag": "crf", "quality_label": "CRF (低いほど高画質)",
    },
    "h264_nvenc (NVIDIA)": {
        "name": "h264_nvenc",
        "presets": ["p1", "p2", "p3", "p4", "p5", "p6", "p7"],
        "default_preset": "p5",
        "quality_flag": "cq", "quality_label": "CQ (低いほど高画質)",
    },
    "hevc_nvenc (NVIDIA/H.265)": {
        "name": "hevc_nvenc",
        "presets": ["p1", "p2", "p3", "p4", "p5", "p6", "p7"],
        "default_preset": "p5",
        "quality_flag": "cq", "quality_label": "CQ (低いほど高画質)",
    },
    "h264_qsv (Intel)": {
        "name": "h264_qsv",
        "presets": ["veryfast", "faster", "fast", "medium", "slow"],
        "default_preset": "fast",
        "quality_flag": "global_quality", "quality_label": "品質 (低いほど高画質)",
    },
    "h264_amf (AMD)": {
        "name": "h264_amf",
        "presets": ["speed", "balanced", "quality"],
        "default_preset": "balanced",
        "quality_flag": "qp", "quality_label": "QP (低いほど高画質)",
    },
}

AUDIO_ENCODERS = ["aac", "libopus", "libmp3lame", "pcm_s16le"]


class Config:
    """GUI の入力値をまとめた素の設定オブジェクト。"""

    def __init__(self):
        self.ffmpeg = "ffmpeg"
        # video source
        self.source = "window"          # window | region | fullscreen
        self.title = ""
        self.x = self.y = 0
        self.w, self.h = 1280, 720
        self.fps = 30
        self.draw_mouse = True
        # audio
        self.audio_device = ""          # 空なら音声なし
        self.audio_buffer = "50"
        # encode
        self.vcodec_key = "libx264 (CPU/H.264)"
        self.preset = "veryfast"
        self.quality = 23
        self.scale = "そのまま"
        self.pix_fmt = "yuv420p"
        self.acodec = "aac"
        self.abitrate = "192k"
        # output
        self.outfile = ""
        self.overwrite = True


def _scale_filter(scale: str) -> str:
    """出力解像度フィルタ。幅・高さは必ず偶数に丸める。"""
    if scale == "そのまま":
        return "scale=trunc(iw/2)*2:trunc(ih/2)*2"
    if scale.endswith("%"):
        r = int(scale[:-1]) / 100.0
        return f"scale=trunc(iw*{r}/2)*2:trunc(ih*{r}/2)*2"
    height = {"1080p": 1080, "720p": 720, "480p": 480}[scale]
    return f"scale=-2:{height}"


def build_args(cfg: Config) -> list[str]:
    """ffmpeg の argv を組み立てる。"""
    a: list[str] = [cfg.ffmpeg, "-hide_banner"]
    a += ["-y"] if cfg.overwrite else ["-n"]

    # ---- 入力 0: 映像 (gdigrab) ----
    a += ["-f", "gdigrab",
          "-framerate", str(cfg.fps),
          "-draw_mouse", "1" if cfg.draw_mouse else "0",
          "-thread_queue_size", "1024"]
    if cfg.source == "window":
        a += ["-i", f"title={cfg.title}"]
    else:
        if cfg.source == "region":
            a += ["-offset_x", str(cfg.x), "-offset_y", str(cfg.y),
                  "-video_size", f"{cfg.w}x{cfg.h}"]
        a += ["-i", "desktop"]

    # ---- 入力 1: 音声 (DirectShow) ----
    has_audio = bool(cfg.audio_device)
    if has_audio:
        a += ["-f", "dshow",
              "-rtbufsize", "256M",
              "-thread_queue_size", "1024"]
        if cfg.audio_buffer.strip():
            a += ["-audio_buffer_size", cfg.audio_buffer.strip()]
        a += ["-i", f"audio={cfg.audio_device}"]
        a += ["-map", "0:v:0", "-map", "1:a:0"]
    else:
        a += ["-map", "0:v:0"]

    # ---- 映像エンコード ----
    enc = VIDEO_ENCODERS[cfg.vcodec_key]
    a += ["-c:v", enc["name"]]
    if enc["name"] == "h264_amf":
        a += ["-rc", "cqp",
              "-qp_i", str(cfg.quality), "-qp_p", str(cfg.quality),
              "-quality", cfg.preset]
    else:
        a += ["-preset", cfg.preset]
        if enc["quality_flag"] == "crf":
            a += ["-crf", str(cfg.quality)]
        elif enc["quality_flag"] == "cq":
            a += ["-rc", "vbr", "-cq", str(cfg.quality), "-b:v", "0"]
        else:  # global_quality
            a += ["-global_quality", str(cfg.quality)]
    if enc["name"] == "libx265":
        a += ["-tag:v", "hvc1"]
    a += ["-vf", _scale_filter(cfg.scale), "-pix_fmt", cfg.pix_fmt]

    # ---- 音声エンコード ----
    if has_audio:
        a += ["-c:a", cfg.acodec]
        if cfg.acodec != "pcm_s16le":
            a += ["-b:a", cfg.abitrate]
        # 取りこぼしがあっても時刻を保つ
        a += ["-af", "aresample=async=1:first_pts=0"]

    if cfg.outfile.lower().endswith(".mp4"):
        a += ["-movflags", "+faststart"]
    a += [cfg.outfile]
    return a


def quote_win(arg: str) -> str:
    if arg == "":
        return '""'
    if re.search(r'[\s"^&|<>()]', arg):
        return '"' + arg.replace('"', r"\"") + '"'
    return arg


def command_line(cfg: Config) -> str:
    return " ".join(quote_win(x) for x in build_args(cfg))


# --------------------------------------------------------------------------------------
# GUI
# --------------------------------------------------------------------------------------

NO_AUDIO = "（音声なし）"


def run_gui() -> int:
    import tkinter as tk
    from tkinter import filedialog, font as tkfont, messagebox, ttk

    dpi = enable_dpi_awareness()
    root = tk.Tk()
    root.title("ffcapture - 画面キャプチャ ffmpeg コマンドビルダー")
    try:
        root.tk.call("tk", "scaling", dpi / 72.0)
    except Exception:
        pass

    # 日本語対応フォント（欧文フォントへのフォールバック防止）
    families = set(tkfont.families(root))
    jp = next((f for f in ("Yu Gothic UI", "游ゴシック", "Meiryo UI", "Meiryo",
                           "BIZ UDGothic", "MS Gothic") if f in families), None)
    if jp:
        for nm in ("TkDefaultFont", "TkTextFont", "TkMenuFont", "TkHeadingFont"):
            tkfont.nametofont(nm).configure(family=jp, size=10)
    mono = next((f for f in ("BIZ UDGothic", "MS Gothic", "Consolas")
                 if f in families), "Courier")

    state: dict = {"proc": None, "loopback": None}

    # ---------------- 変数 ----------------
    v_ffmpeg = tk.StringVar(value=shutil.which("ffmpeg") or "ffmpeg")
    v_source = tk.StringVar(value="window")
    v_title = tk.StringVar()
    v_x, v_y = tk.StringVar(value="0"), tk.StringVar(value="0")
    v_w, v_h = tk.StringVar(value="1280"), tk.StringVar(value="720")
    v_fps = tk.StringVar(value="30")
    v_mouse = tk.BooleanVar(value=True)

    v_audio = tk.StringVar(value=NO_AUDIO)
    v_abuf = tk.StringVar(value="50")
    v_mixinfo = tk.StringVar(value="")

    v_vcodec = tk.StringVar(value="libx264 (CPU/H.264)")
    v_preset = tk.StringVar(value="veryfast")
    v_quality = tk.StringVar(value="23")
    v_qlabel = tk.StringVar(value="CRF (低いほど高画質)")
    v_scale = tk.StringVar(value="そのまま")
    v_pix = tk.StringVar(value="yuv420p")
    v_acodec = tk.StringVar(value="aac")
    v_abr = tk.StringVar(value="192k")

    default_out = os.path.join(
        os.path.expanduser("~"), "Videos",
        "capture_" + dt.datetime.now().strftime("%Y%m%d_%H%M%S") + ".mp4")
    v_out = tk.StringVar(value=default_out)
    v_overwrite = tk.BooleanVar(value=True)
    v_status = tk.StringVar(value="待機中")

    # ---------------- ログ ----------------
    def log(msg: str):
        def _append():
            txt_log.configure(state="normal")
            txt_log.insert("end", f"[{dt.datetime.now():%H:%M:%S}] {msg}\n")
            txt_log.see("end")
            txt_log.configure(state="disabled")
        try:
            root.after(0, _append)
        except Exception:
            pass

    # ---------------- 設定収集 ----------------
    def as_int(sv: tk.StringVar, fallback: int) -> int:
        try:
            return int(str(sv.get()).strip())
        except Exception:
            return fallback

    def collect() -> Config:
        c = Config()
        c.ffmpeg = v_ffmpeg.get().strip() or "ffmpeg"
        c.source = v_source.get()
        c.title = v_title.get().strip()
        c.x, c.y = as_int(v_x, 0), as_int(v_y, 0)
        c.w, c.h = max(2, as_int(v_w, 1280)), max(2, as_int(v_h, 720))
        c.w -= c.w % 2
        c.h -= c.h % 2
        c.fps = max(1, as_int(v_fps, 30))
        c.draw_mouse = v_mouse.get()

        dev = v_audio.get().strip()
        c.audio_device = "" if dev in ("", NO_AUDIO) else dev
        c.audio_buffer = v_abuf.get().strip()

        c.vcodec_key = v_vcodec.get()
        c.preset = v_preset.get()
        c.quality = as_int(v_quality, 23)
        c.scale = v_scale.get()
        c.pix_fmt = v_pix.get()
        c.acodec = v_acodec.get()
        c.abitrate = v_abr.get().strip() or "192k"
        c.outfile = v_out.get().strip()
        c.overwrite = v_overwrite.get()
        return c

    def validate(c: Config) -> str | None:
        if c.source == "window" and not c.title:
            return "ウィンドウタイトルを選択してください。"
        if not c.outfile:
            return "出力ファイルを指定してください。"
        return None

    def refresh_preview(*_):
        txt_cmd.configure(state="normal")
        txt_cmd.delete("1.0", "end")
        txt_cmd.insert("1.0", command_line(collect()))
        txt_cmd.configure(state="disabled")

    # ---------------- レイアウト ----------------
    root.columnconfigure(0, weight=1)
    root.rowconfigure(1, weight=1)

    main = ttk.Frame(root, padding=8)
    main.grid(row=0, column=0, sticky="ew")
    main.columnconfigure(0, weight=1)
    main.columnconfigure(1, weight=1)

    # --- キャプチャ対象 ---
    f_src = ttk.LabelFrame(main, text="キャプチャ対象", padding=8)
    f_src.grid(row=0, column=0, sticky="nsew", padx=(0, 4), pady=(0, 6))
    f_src.columnconfigure(1, weight=1)

    ttk.Radiobutton(f_src, text="ウィンドウ", variable=v_source, value="window",
                    command=lambda: on_source()).grid(row=0, column=0, sticky="w")
    cb_win = ttk.Combobox(f_src, textvariable=v_title, state="readonly", width=42)
    cb_win.grid(row=0, column=1, sticky="ew", padx=4)

    def refresh_windows():
        wins = enum_windows()
        cb_win["values"] = [w["title"] for w in wins]
        log(f"ウィンドウを {len(wins)} 件検出しました。")
        refresh_preview()

    ttk.Button(f_src, text="更新", width=6,
               command=refresh_windows).grid(row=0, column=2, padx=2)

    ttk.Radiobutton(f_src, text="矩形指定", variable=v_source, value="region",
                    command=lambda: on_source()).grid(row=1, column=0, sticky="w",
                                                      pady=(6, 0))
    f_rect = ttk.Frame(f_src)
    f_rect.grid(row=1, column=1, columnspan=2, sticky="ew", pady=(6, 0))
    for i, (lbl, var) in enumerate((("X", v_x), ("Y", v_y), ("幅", v_w), ("高さ", v_h))):
        ttk.Label(f_rect, text=lbl).grid(row=0, column=i * 2, padx=(4 if i else 0, 2))
        e = ttk.Entry(f_rect, textvariable=var, width=6)
        e.grid(row=0, column=i * 2 + 1)
        e.bind("<KeyRelease>", refresh_preview)

    ttk.Radiobutton(f_src, text="画面全体", variable=v_source, value="fullscreen",
                    command=lambda: on_source()).grid(row=2, column=0, sticky="w",
                                                      pady=(6, 0))
    f_misc = ttk.Frame(f_src)
    f_misc.grid(row=2, column=1, columnspan=2, sticky="ew", pady=(6, 0))
    ttk.Label(f_misc, text="fps").grid(row=0, column=0)
    e_fps = ttk.Entry(f_misc, textvariable=v_fps, width=5)
    e_fps.grid(row=0, column=1, padx=(2, 10))
    e_fps.bind("<KeyRelease>", refresh_preview)
    ttk.Checkbutton(f_misc, text="マウスカーソルを含める", variable=v_mouse,
                    command=refresh_preview).grid(row=0, column=2)

    f_pick = ttk.Frame(f_src)
    f_pick.grid(row=3, column=0, columnspan=3, sticky="ew", pady=(8, 0))
    btn_drag = ttk.Button(f_pick, text="画面をドラッグして範囲選択")
    btn_drag.grid(row=0, column=0, padx=(0, 6))
    btn_wrect = ttk.Button(f_pick, text="選択ウィンドウ → 矩形指定に変換")
    btn_wrect.grid(row=0, column=1)

    ttk.Label(f_src, wraplength=430, foreground="#666",
              text="※ ウィンドウ指定は gdigrab がウィンドウ DC を BitBlt する方式のため、"
                   "ハードウェア描画のアプリ (ブラウザ・Electron 等) では黒画面や文字欠けに"
                   "なります。その場合は上のボタンで矩形指定に変換してください。"
              ).grid(row=4, column=0, columnspan=3, sticky="w", pady=(6, 0))

    # --- 音声 ---
    f_aud = ttk.LabelFrame(main, text="音声ソース (DirectShow)", padding=8)
    f_aud.grid(row=0, column=1, sticky="nsew", padx=(4, 0), pady=(0, 6))
    f_aud.columnconfigure(0, weight=1)

    cb_audio = ttk.Combobox(f_aud, textvariable=v_audio, state="readonly", width=44)
    cb_audio.grid(row=0, column=0, sticky="ew")
    ttk.Button(f_aud, text="更新", width=6,
               command=lambda: refresh_audio(True)).grid(row=0, column=1, padx=(4, 0))

    f_abuf = ttk.Frame(f_aud)
    f_abuf.grid(row=1, column=0, columnspan=2, sticky="w", pady=(4, 0))
    ttk.Label(f_abuf, text="audio_buffer_size (ms)").grid(row=0, column=0)
    e_abuf = ttk.Entry(f_abuf, textvariable=v_abuf, width=6)
    e_abuf.grid(row=0, column=1, padx=4)
    e_abuf.bind("<KeyRelease>", refresh_preview)

    ttk.Label(f_aud, textvariable=v_mixinfo, wraplength=390, foreground="#666"
              ).grid(row=2, column=0, columnspan=2, sticky="w", pady=(6, 0))

    f_mix = ttk.Frame(f_aud)
    f_mix.grid(row=3, column=0, columnspan=2, sticky="w", pady=(4, 0))
    btn_enable = ttk.Button(f_mix, text="ステレオミキサーを有効化（管理者）")
    btn_enable.grid(row=0, column=0, padx=(0, 6))
    ttk.Button(f_mix, text="サウンド設定を開く",
               command=lambda: (open_sound_control_panel(),
                                log("サウンド コントロールパネル（録音タブ）を開きました。"))
               ).grid(row=0, column=1)

    # --- エンコード ---
    f_enc = ttk.LabelFrame(main, text="エンコード", padding=8)
    f_enc.grid(row=1, column=0, sticky="nsew", padx=(0, 4))
    f_enc.columnconfigure(1, weight=1)

    ttk.Label(f_enc, text="映像コーデック").grid(row=0, column=0, sticky="w")
    cb_vc = ttk.Combobox(f_enc, textvariable=v_vcodec, state="readonly",
                         values=list(VIDEO_ENCODERS), width=26)
    cb_vc.grid(row=0, column=1, sticky="ew", padx=4, pady=2)

    ttk.Label(f_enc, text="プリセット").grid(row=1, column=0, sticky="w")
    cb_preset = ttk.Combobox(f_enc, textvariable=v_preset, state="readonly", width=26)
    cb_preset.grid(row=1, column=1, sticky="ew", padx=4, pady=2)

    ttk.Label(f_enc, textvariable=v_qlabel).grid(row=2, column=0, sticky="w")
    sp_q = ttk.Spinbox(f_enc, from_=0, to=51, textvariable=v_quality, width=6,
                       command=refresh_preview)
    sp_q.grid(row=2, column=1, sticky="w", padx=4, pady=2)
    sp_q.bind("<KeyRelease>", refresh_preview)

    ttk.Label(f_enc, text="出力サイズ").grid(row=3, column=0, sticky="w")
    ttk.Combobox(f_enc, textvariable=v_scale, state="readonly", width=26,
                 values=["そのまま", "1080p", "720p", "480p", "75%", "50%"]
                 ).grid(row=3, column=1, sticky="ew", padx=4, pady=2)

    ttk.Label(f_enc, text="pix_fmt").grid(row=4, column=0, sticky="w")
    ttk.Combobox(f_enc, textvariable=v_pix, state="readonly", width=26,
                 values=["yuv420p", "yuv444p", "nv12"]
                 ).grid(row=4, column=1, sticky="ew", padx=4, pady=2)

    ttk.Label(f_enc, text="音声コーデック").grid(row=5, column=0, sticky="w")
    ttk.Combobox(f_enc, textvariable=v_acodec, state="readonly", width=26,
                 values=AUDIO_ENCODERS).grid(row=5, column=1, sticky="ew", padx=4, pady=2)

    ttk.Label(f_enc, text="音声ビットレート").grid(row=6, column=0, sticky="w")
    ttk.Combobox(f_enc, textvariable=v_abr, width=26,
                 values=["96k", "128k", "160k", "192k", "256k", "320k"]
                 ).grid(row=6, column=1, sticky="ew", padx=4, pady=2)

    # --- 出力 ---
    f_out = ttk.LabelFrame(main, text="出力", padding=8)
    f_out.grid(row=1, column=1, sticky="nsew", padx=(4, 0))
    f_out.columnconfigure(0, weight=1)

    e_out = ttk.Entry(f_out, textvariable=v_out)
    e_out.grid(row=0, column=0, sticky="ew")
    e_out.bind("<KeyRelease>", refresh_preview)

    def choose_out():
        p = filedialog.asksaveasfilename(
            title="保存先", defaultextension=".mp4",
            initialfile=os.path.basename(v_out.get()),
            initialdir=os.path.dirname(v_out.get()) or None,
            filetypes=[("MP4", "*.mp4"), ("Matroska", "*.mkv"),
                       ("MOV", "*.mov"), ("すべて", "*.*")])
        if p:
            v_out.set(p)
            refresh_preview()

    ttk.Button(f_out, text="参照…", command=choose_out).grid(row=0, column=1, padx=(4, 0))
    ttk.Checkbutton(f_out, text="既存ファイルを上書きする (-y)", variable=v_overwrite,
                    command=refresh_preview).grid(row=1, column=0, sticky="w", pady=(4, 0))
    ttk.Label(f_out, text="※ MP4 は正常終了が必要です。中断が心配なら .mkv を推奨。",
              foreground="#666", wraplength=380).grid(row=2, column=0, columnspan=2,
                                                      sticky="w", pady=(2, 0))

    ttk.Label(f_out, text="ffmpeg のパス").grid(row=3, column=0, sticky="w", pady=(8, 0))
    e_ff = ttk.Entry(f_out, textvariable=v_ffmpeg)
    e_ff.grid(row=4, column=0, sticky="ew")
    e_ff.bind("<KeyRelease>", refresh_preview)

    def choose_ffmpeg():
        p = filedialog.askopenfilename(
            title="ffmpeg.exe", filetypes=[("実行ファイル", "*.exe"), ("すべて", "*.*")])
        if p:
            v_ffmpeg.set(p)
            refresh_preview()

    ttk.Button(f_out, text="参照…", command=choose_ffmpeg).grid(row=4, column=1, padx=(4, 0))

    # --- コマンドプレビュー + ログ ---
    lower = ttk.Frame(root, padding=(8, 0, 8, 8))
    lower.grid(row=1, column=0, sticky="nsew")
    lower.columnconfigure(0, weight=1)
    lower.rowconfigure(1, weight=1)
    lower.rowconfigure(3, weight=2)

    bar_cmd = ttk.Frame(lower)
    bar_cmd.grid(row=0, column=0, columnspan=2, sticky="ew")
    ttk.Label(bar_cmd, text="生成されたコマンド").pack(side="left")
    ttk.Button(bar_cmd, text="再生成", command=refresh_preview).pack(side="right", padx=2)

    txt_cmd = tk.Text(lower, height=5, wrap="word", font=(mono, 9), state="disabled")
    txt_cmd.grid(row=1, column=0, columnspan=2, sticky="nsew", pady=(2, 6))

    bar_log = ttk.Frame(lower)
    bar_log.grid(row=2, column=0, columnspan=2, sticky="ew")
    ttk.Label(bar_log, text="ログ").pack(side="left")
    ttk.Button(bar_log, text="クリア",
               command=lambda: (txt_log.configure(state="normal"),
                                txt_log.delete("1.0", "end"),
                                txt_log.configure(state="disabled"))
               ).pack(side="right", padx=2)

    txt_log = tk.Text(lower, height=12, wrap="none", font=(mono, 9), state="disabled")
    txt_log.grid(row=3, column=0, sticky="nsew", pady=(2, 0))
    sb = ttk.Scrollbar(lower, orient="vertical", command=txt_log.yview)
    sb.grid(row=3, column=1, sticky="ns", pady=(2, 0))
    txt_log.configure(yscrollcommand=sb.set)

    # --- 操作バー ---
    bar = ttk.Frame(root, padding=(8, 0, 8, 8))
    bar.grid(row=2, column=0, sticky="ew")
    bar.columnconfigure(3, weight=1)

    def copy_text(widget, what: str):
        root.clipboard_clear()
        root.clipboard_append(widget.get("1.0", "end-1c"))
        log(f"{what}をクリップボードにコピーしました。")

    ttk.Button(bar, text="コマンドをコピー",
               command=lambda: copy_text(txt_cmd, "コマンド")).grid(row=0, column=0, padx=2)
    ttk.Button(bar, text="ログをコピー",
               command=lambda: copy_text(txt_log, "ログ")).grid(row=0, column=1, padx=2)
    btn_start = ttk.Button(bar, text="録画開始")
    btn_start.grid(row=0, column=2, padx=(16, 2))
    btn_stop = ttk.Button(bar, text="停止", state="disabled")
    btn_stop.grid(row=0, column=3, sticky="w", padx=2)
    ttk.Label(bar, textvariable=v_status).grid(row=0, column=4, sticky="e")

    # ---------------- 状態遷移 ----------------
    def on_source(*_):
        s = v_source.get()
        cb_win.configure(state="readonly" if s == "window" else "disabled")
        for child in f_rect.winfo_children():
            if isinstance(child, ttk.Entry):
                child.configure(state="normal" if s == "region" else "disabled")
        # 「矩形に変換」はウィンドウ選択中でも押せる必要があるので常時有効
        refresh_preview()

    def on_vcodec(*_):
        enc = VIDEO_ENCODERS[v_vcodec.get()]
        cb_preset["values"] = enc["presets"]
        if v_preset.get() not in enc["presets"]:
            v_preset.set(enc["default_preset"])
        v_qlabel.set(enc["quality_label"])
        refresh_preview()

    cb_vc.bind("<<ComboboxSelected>>", on_vcodec)
    for cb in (cb_win, cb_audio, cb_preset):
        cb.bind("<<ComboboxSelected>>", refresh_preview)
    for var in (v_scale, v_pix, v_acodec, v_abr):
        var.trace_add("write", lambda *_: refresh_preview())

    # ---------------- 音声デバイス列挙 ----------------
    def refresh_audio(verbose: bool = False):
        names = list_dshow_audio(v_ffmpeg.get())
        cb_audio["values"] = [NO_AUDIO] + names
        if v_audio.get() not in cb_audio["values"]:
            v_audio.set(NO_AUDIO)
        # ループバック用デバイスがあれば自動で選ぶ
        if v_audio.get() == NO_AUDIO:
            pref = next((n for n in names if LOOPBACK_NAME_RE.search(n)), None)
            if pref:
                v_audio.set(pref)
                log(f"ループバック可能なデバイスを自動選択: {pref}")
        if verbose:
            log(f"DirectShow 音声デバイス: {len(names)} 件"
                + (" — " + " / ".join(names) if names else ""))

        # ステレオミキサーの状態をレジストリから判定
        eps = find_loopback_endpoints()
        state["loopback"] = None
        if any(LOOPBACK_NAME_RE.search(n) for n in names):
            v_mixinfo.set("PC の再生音を拾えるデバイスが利用可能です。")
            btn_enable.state(["disabled"])
        elif eps:
            target = next((e for e in eps if e["state_name"] == "無効"), eps[0])
            state["loopback"] = target
            v_mixinfo.set(
                f"『{target['name']}』が {target['state_name']} 状態です"
                f" (DeviceState=0x{target['state']:08X})。"
                "有効化すると DirectShow に現れ、コマンドラインだけで PC の再生音を録れます。")
            btn_enable.state(["!disabled"] if target["state_name"] == "無効"
                             else ["disabled"])
        else:
            v_mixinfo.set(
                "ステレオミキサーが見つかりません。サウンドドライバが持っていない構成です。"
                "VB-CABLE や screen-capture-recorder (virtual-audio-capturer) を"
                "導入すると同じ方法で録音できます。")
            btn_enable.state(["disabled"])
        if verbose:
            for e in list_capture_endpoints():
                if LOOPBACK_NAME_RE.search(e["name"]):
                    log(f"  録音エンドポイント: {e['name']} = {e['state_name']} "
                        f"(0x{e['state']:08X})")
        refresh_preview()

    def enable_stereo_mix():
        target = state.get("loopback")
        if not target:
            return
        if not messagebox.askyesno(
                "ステレオミキサーの有効化",
                f"『{target['name']}』を有効化します。\n\n"
                "・管理者権限が必要です（UAC のダイアログが出ます）\n"
                "・オーディオサービスを再起動するため、音が数秒途切れます\n\n"
                "続行しますか？"):
            return
        btn_enable.state(["disabled"])
        log(f"『{target['name']}』を有効化します（DeviceState=1 + AudioEndpointBuilder 再起動）")

        def worker():
            try:
                r = enable_endpoint_elevated(target["guid"], True)
                for line in (r.stdout + r.stderr).splitlines():
                    if line.strip():
                        log(line.rstrip())
                if r.returncode == 0:
                    log("有効化しました。デバイスを再列挙します。")
                else:
                    log(f"有効化に失敗しました (exit={r.returncode})。"
                        "「サウンド設定を開く」から手動で有効化してください。")
            except Exception as e:
                log(f"有効化の実行に失敗: {e}")
            time.sleep(2.0)   # サービス再起動の落ち着き待ち
            root.after(0, lambda: refresh_audio(True))

        threading.Thread(target=worker, daemon=True).start()

    btn_enable.configure(command=enable_stereo_mix)

    def probe_encoders():
        avail = list_ffmpeg_encoders(v_ffmpeg.get())
        if not avail:
            log("ffmpeg を実行できませんでした。パスを確認してください。")
            return
        usable = [k for k, v in VIDEO_ENCODERS.items() if v["name"] in avail]
        missing = [k for k in VIDEO_ENCODERS if k not in usable]
        root.after(0, lambda: cb_vc.configure(values=usable or list(VIDEO_ENCODERS)))
        log("利用可能な映像エンコーダ: "
            + ", ".join(VIDEO_ENCODERS[k]["name"] for k in usable))
        if missing:
            log("この ffmpeg では使えないもの: "
                + ", ".join(VIDEO_ENCODERS[k]["name"] for k in missing))

    # ---------------- 矩形選択オーバーレイ ----------------
    def pick_region():
        vx, vy, vw, vh = virtual_screen_rect()
        ov = tk.Toplevel(root)
        ov.overrideredirect(True)
        ov.attributes("-topmost", True)
        ov.attributes("-alpha", 0.3)
        ov.geometry(f"{vw}x{vh}+{vx}+{vy}")
        cv = tk.Canvas(ov, bg="black", highlightthickness=0, cursor="cross")
        cv.pack(fill="both", expand=True)
        info = cv.create_text(20, 20, anchor="nw", fill="#ffffff",
                              font=(jp or "TkDefaultFont", 14),
                              text="ドラッグして範囲を選択 / Esc でキャンセル")
        st = {"x0": 0, "y0": 0, "rect": None}

        def press(e):
            st["x0"], st["y0"] = e.x_root, e.y_root
            if st["rect"]:
                cv.delete(st["rect"])
            st["rect"] = cv.create_rectangle(e.x, e.y, e.x, e.y,
                                             outline="#00d0ff", width=2)

        def drag(e):
            if not st["rect"]:
                return
            cv.coords(st["rect"], st["x0"] - vx, st["y0"] - vy, e.x, e.y)
            w, h = abs(e.x_root - st["x0"]), abs(e.y_root - st["y0"])
            cv.itemconfigure(info, text=f"{w} x {h}   (Esc でキャンセル)")

        def release(e):
            x0, y0 = min(st["x0"], e.x_root), min(st["y0"], e.y_root)
            w, h = abs(e.x_root - st["x0"]), abs(e.y_root - st["y0"])
            ov.destroy()
            if w < 8 or h < 8:
                log("選択範囲が小さすぎます。")
                return
            w -= w % 2
            h -= h % 2
            v_x.set(str(x0))
            v_y.set(str(y0))
            v_w.set(str(w))
            v_h.set(str(h))
            v_source.set("region")
            on_source()
            log(f"範囲を選択: {x0},{y0} {w}x{h}")

        cv.bind("<ButtonPress-1>", press)
        cv.bind("<B1-Motion>", drag)
        cv.bind("<ButtonRelease-1>", release)
        ov.bind("<Escape>", lambda _e: ov.destroy())
        ov.focus_force()
        cv.focus_set()

    def use_window_rect():
        t = v_title.get().strip()
        if not t:
            log("先にウィンドウを選択してください。")
            return
        r = window_rect_by_title(t)
        if not r:
            log("ウィンドウが見つかりません。『更新』を押してください。")
            return
        x, y, w, h = r
        w -= w % 2
        h -= h % 2
        v_x.set(str(x))
        v_y.set(str(y))
        v_w.set(str(w))
        v_h.set(str(h))
        v_source.set("region")
        on_source()
        log(f"ウィンドウ矩形を取り込み、矩形指定に切り替えました: {x},{y} {w}x{h}")

    btn_drag.configure(command=pick_region)
    btn_wrect.configure(command=use_window_rect)

    # ---------------- 録画 ----------------
    def set_running(running: bool):
        btn_start.configure(state="disabled" if running else "normal")
        btn_stop.configure(state="normal" if running else "disabled")
        v_status.set("録画中…" if running else "待機中")

    def start():
        if state["proc"] is not None:
            return
        c = collect()
        err = validate(c)
        if err:
            messagebox.showwarning("入力を確認してください", err)
            return
        outdir = os.path.dirname(c.outfile)
        if outdir and not os.path.isdir(outdir):
            os.makedirs(outdir, exist_ok=True)
        argv = build_args(c)
        log("実行: " + command_line(c))

        try:
            proc = subprocess.Popen(
                argv,
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                creationflags=CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP)
        except Exception as e:
            messagebox.showerror("起動できません", f"ffmpeg を起動できませんでした:\n{e}")
            return
        state["proc"] = proc
        set_running(True)

        def reader():
            for raw in iter(proc.stderr.readline, b""):
                line = raw.decode("utf-8", "replace").rstrip()
                if line:
                    log(line)
            log(f"ffmpeg 終了 (exit={proc.wait()})")
            root.after(0, finished)

        threading.Thread(target=reader, daemon=True).start()

    def finished():
        state["proc"] = None
        set_running(False)

    def stop():
        proc = state["proc"]
        if proc is None:
            return
        v_status.set("停止処理中…")
        try:
            if proc.stdin:
                proc.stdin.write(b"q")     # ffmpeg の対話コマンドで正常終了
                proc.stdin.flush()
                proc.stdin.close()
        except Exception:
            pass

        def watchdog():
            try:
                proc.wait(timeout=5)
                return
            except Exception:
                pass
            if IS_WINDOWS:
                log("'q' で終わらないため CTRL_BREAK を送ります。")
                try:
                    os.kill(proc.pid, signal.CTRL_BREAK_EVENT)  # SIGINT 相当
                except Exception:
                    pass
            try:
                proc.wait(timeout=5)
            except Exception:
                log("正常終了しないため強制終了します。ファイルが壊れる可能性があります。")
                try:
                    proc.kill()
                except Exception:
                    pass

        threading.Thread(target=watchdog, daemon=True).start()

    btn_start.configure(command=start)
    btn_stop.configure(command=stop)

    def on_close():
        if state["proc"] is not None:
            if not messagebox.askyesno("確認", "録画中です。停止して終了しますか？"):
                return
            stop()
            time.sleep(1.0)
        root.destroy()

    root.protocol("WM_DELETE_WINDOW", on_close)

    # ---------------- 初期化 ----------------
    if not IS_WINDOWS:
        log("警告: このツールは Windows 専用です (gdigrab / DirectShow)。")
    log(f"python: {sys.executable}")
    on_vcodec()
    on_source()
    refresh_windows()
    refresh_audio(True)
    threading.Thread(target=probe_encoders, daemon=True).start()
    refresh_preview()
    log("準備完了。対象を選んで『録画開始』を押してください。")

    root.mainloop()
    return 0


# --------------------------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(description="Windows 画面キャプチャ ffmpeg コマンドビルダー")
    ap.add_argument("--list", action="store_true",
                    help="音声エンドポイントと DirectShow デバイスを一覧表示して終了")
    ap.add_argument("--ffmpeg", default=shutil.which("ffmpeg") or "ffmpeg")
    args = ap.parse_args()

    if args.list:
        print("== 録音エンドポイント (レジストリ) ==")
        for e in list_capture_endpoints():
            print(f"  [{e['state_name']:<6}] 0x{e['state']:08X}  {e['name']}")
        print("== DirectShow 音声デバイス (ffmpeg) ==")
        for n in list_dshow_audio(args.ffmpeg):
            print(f"  {n}")
        return 0
    return run_gui()


if __name__ == "__main__":
    sys.exit(main())
