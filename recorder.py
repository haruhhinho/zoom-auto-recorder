"""Windows の画面（ffmpeg gdigrab）と PC 音声（WASAPI ループバック）の録画。"""
from __future__ import annotations

import ctypes
import glob
import os
import shutil
import subprocess
import threading
import time
import wave
from pathlib import Path
from typing import Callable

CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0
Log = Callable[[str], None]


def find_ffmpeg(custom: str = "") -> str | None:
    here = Path(__file__).resolve().parent
    candidates = [custom] if custom else []
    candidates += [str(here / "ffmpeg.exe"), str(here / "ffmpeg" / "bin" / "ffmpeg.exe")]
    which = shutil.which("ffmpeg")
    if which:
        candidates.append(which)
    local = os.environ.get("LOCALAPPDATA", "")
    if local:
        candidates.append(os.path.join(local, "Microsoft", "WinGet", "Links", "ffmpeg.exe"))
        candidates += glob.glob(os.path.join(local, "Microsoft", "WinGet", "Packages", "*FFmpeg*", "**", "bin", "ffmpeg.exe"), recursive=True)
    for c in candidates:
        if c and os.path.isfile(c):
            return c
    return None


def primary_screen_size() -> tuple[int, int]:
    user32 = ctypes.windll.user32
    return user32.GetSystemMetrics(0), user32.GetSystemMetrics(1)


# ---------------------------------------------------------------- PC 音声
class LoopbackAudio:
    """既定の再生デバイスの音（=会議の音声）を WAV に録音する。"""

    CHUNK = 1024

    def __init__(self, path: Path, log: Log):
        self.path = path
        self.log = log
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []
        self.started_at: float | None = None

    def start(self) -> None:
        try:
            import pyaudiowpatch as pyaudio
        except ImportError as e:
            raise RuntimeError("PyAudioWPatch が未インストールです（setup.bat を実行してください）") from e
        self.pyaudio = pyaudio
        self.pa = pyaudio.PyAudio()
        wasapi = self.pa.get_host_api_info_by_type(pyaudio.paWASAPI)
        out_dev = self.pa.get_device_info_by_index(wasapi["defaultOutputDevice"])
        loop = out_dev if out_dev.get("isLoopbackDevice") else None
        if loop is None:
            for d in self.pa.get_loopback_device_info_generator():
                if out_dev["name"] in d["name"]:
                    loop = d
                    break
        if loop is None:
            raise RuntimeError("再生デバイスのループバックが見つかりません")
        self.out_dev, self.loop_dev = out_dev, loop
        self.channels = int(loop["maxInputChannels"])
        self.rate = int(loop["defaultSampleRate"])
        self.log(f"音声デバイス: {out_dev['name']}（{self.rate}Hz / {self.channels}ch）")

        for target in (self._keep_alive, self._capture):
            t = threading.Thread(target=target, daemon=True)
            t.start()
            self._threads.append(t)
        for _ in range(50):  # 録音開始を最大5秒待つ
            if self.started_at:
                break
            time.sleep(0.1)

    def _keep_alive(self) -> None:
        # 無音でもループバックのデータが途切れないよう、同じデバイスに無音を流し続ける
        try:
            ch = int(self.out_dev["maxOutputChannels"]) or 2
            stream = self.pa.open(format=self.pyaudio.paInt16, channels=ch, rate=self.rate, output=True,
                                  output_device_index=self.out_dev["index"], frames_per_buffer=self.CHUNK)
            silence = b"\x00" * (self.CHUNK * ch * 2)
            while not self._stop.is_set():
                stream.write(silence)
            stream.stop_stream()
            stream.close()
        except Exception as e:  # 無くても録音自体は可能（無音区間は下で補完）
            self.log(f"（参考）無音キープ用ストリームを開けませんでした: {e}")

    def _capture(self) -> None:
        frame_bytes = self.channels * 2
        try:
            stream = self.pa.open(format=self.pyaudio.paInt16, channels=self.channels, rate=self.rate, input=True,
                                  input_device_index=self.loop_dev["index"], frames_per_buffer=self.CHUNK)
            with wave.open(str(self.path), "wb") as wf:
                wf.setnchannels(self.channels)
                wf.setsampwidth(2)
                wf.setframerate(self.rate)
                written = 0
                self.started_at = time.time()
                while not self._stop.is_set():
                    data = stream.read(self.CHUNK, exception_on_overflow=False)
                    n = len(data) // frame_bytes
                    # データが途切れていた分を無音で埋めて映像とのズレを防ぐ
                    expected = int((time.time() - self.started_at) * self.rate) - n
                    gap = expected - written
                    if gap > self.rate * 0.2:
                        wf.writeframes(b"\x00" * gap * frame_bytes)
                        written += gap
                    wf.writeframes(data)
                    written += n
            stream.stop_stream()
            stream.close()
        except Exception as e:
            self.log(f"音声録音エラー: {e}")

    def stop(self) -> None:
        self._stop.set()
        for t in self._threads:
            t.join(timeout=5)
        try:
            self.pa.terminate()
        except Exception:
            pass


# ---------------------------------------------------------------- 画面＋音声
class ScreenRecorder:
    def __init__(self, ffmpeg: str, out_path: Path, log: Log, fps: int = 15, crf: int = 28,
                 capture: str = "primary", audio: bool = True, audio_offset: float = 0.0):
        self.ffmpeg, self.out_path, self.log = ffmpeg, Path(out_path), log
        self.fps, self.crf, self.capture = fps, crf, capture
        self.use_audio, self.audio_offset = audio, audio_offset
        self.work = self.out_path.parent / f".tmp_{self.out_path.stem}"
        self.video_path = self.work / "video.mkv"
        self.audio_path = self.work / "audio.wav"
        self.ffmpeg_log = self.work / "ffmpeg.log"
        self.proc: subprocess.Popen | None = None
        self.audio: LoopbackAudio | None = None
        self.video_started_at = 0.0

    def _video_cmd(self) -> list[str]:
        cmd = [self.ffmpeg, "-hide_banner", "-y", "-f", "gdigrab", "-framerate", str(self.fps), "-draw_mouse", "1"]
        if self.capture == "primary":
            w, h = primary_screen_size()
            cmd += ["-offset_x", "0", "-offset_y", "0", "-video_size", f"{w}x{h}"]
        cmd += ["-i", "desktop",
                "-vf", "pad=ceil(iw/2)*2:ceil(ih/2)*2",
                "-c:v", "libx264", "-preset", "ultrafast", "-crf", str(self.crf), "-pix_fmt", "yuv420p",
                str(self.video_path)]
        return cmd

    def start(self) -> None:
        self.work.mkdir(parents=True, exist_ok=True)
        self._logf = open(self.ffmpeg_log, "wb")
        self.video_started_at = time.time()
        self.proc = subprocess.Popen(self._video_cmd(), stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                                     stderr=self._logf, creationflags=CREATE_NO_WINDOW)
        if self.use_audio:
            try:
                self.audio = LoopbackAudio(self.audio_path, self.log)
                self.audio.start()
            except Exception as e:
                self.log(f"音声なしで録画を続けます: {e}")
                self.audio = None
        time.sleep(1.5)
        if self.proc.poll() is not None:
            raise RuntimeError("ffmpeg が起動直後に終了しました:\n" + self._log_tail())
        self.log(f"録画開始（{'メインモニター' if self.capture == 'primary' else '全モニター'} / {self.fps}fps）")

    def _log_tail(self, n: int = 8) -> str:
        try:
            lines = self.ffmpeg_log.read_text(encoding="utf-8", errors="replace").splitlines()
            return "\n".join(lines[-n:])
        except OSError:
            return ""

    def is_running(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def stop(self) -> Path:
        if self.audio:
            self.audio.stop()
        if self.proc and self.proc.poll() is None:
            try:
                self.proc.stdin.write(b"q")
                self.proc.stdin.flush()
            except OSError:
                pass
            try:
                self.proc.wait(timeout=60)
            except subprocess.TimeoutExpired:
                self.proc.terminate()
                self.proc.wait(timeout=10)
        try:
            self._logf.close()
        except Exception:
            pass
        self.log("録画停止。ファイルを書き出しています…")
        return self._mux()

    def _mux(self) -> Path:
        if not self.video_path.exists() or self.video_path.stat().st_size == 0:
            raise RuntimeError("映像ファイルが作られませんでした:\n" + self._log_tail())
        has_audio = self.audio_path.exists() and self.audio_path.stat().st_size > 1024
        cmd = [self.ffmpeg, "-hide_banner", "-y", "-i", str(self.video_path)]
        if has_audio:
            offset = (self.audio.started_at or self.video_started_at) - self.video_started_at if self.audio else 0.0
            offset += self.audio_offset
            cmd += ["-itsoffset", f"{offset:.3f}", "-i", str(self.audio_path),
                    "-map", "0:v:0", "-map", "1:a:0", "-c:v", "copy", "-c:a", "aac", "-b:a", "160k"]
        else:
            cmd += ["-c", "copy"]
        cmd += ["-movflags", "+faststart", str(self.out_path)]
        r = subprocess.run(cmd, capture_output=True, creationflags=CREATE_NO_WINDOW)
        if r.returncode != 0 or not self.out_path.exists():
            fallback = self.out_path.with_suffix(".mkv")
            shutil.copy2(self.video_path, fallback)
            self.log("mp4 への変換に失敗したため、映像のみの mkv を保存しました。作業フォルダも残しています。")
            return fallback
        shutil.rmtree(self.work, ignore_errors=True)
        return self.out_path
