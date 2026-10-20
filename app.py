"""Zoom 自動参加＆Windows 画面録画（GUI）。"""
from __future__ import annotations

import ctypes
import datetime as dt
import json
import os
import queue
import re
import unicodedata
import threading
import time
import tkinter as tk
import traceback
import uuid
import shutil
import webbrowser
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from tkinter import font as tkfont

import zoom_control as zc
import zoom_ui as zui
from recorder import ScreenRecorder, find_ffmpeg

APP_NAME = "Zoom 自動録画"
DATA_DIR = Path(os.environ.get("APPDATA", Path.home())) / "ZoomAutoRecorder"
JOBS_FILE = DATA_DIR / "jobs.json"
SETTINGS_FILE = DATA_DIR / "settings.json"
LATE_LIMIT = dt.timedelta(minutes=10)  # 予約時刻をこれ以上過ぎていたら実行しない
ENFORCE_SEC = 120                      # 参加後、カメラ/マイクを合わせ続ける時間

DEFAULT_SETTINGS = {
    "output_dir": str(Path.home() / "Videos" / "ZoomRecordings"),
    "ffmpeg_path": "",
    "display_name": "",
    "default_camera": False,
    "default_mic": False,
    "font_size": "標準",           # 文字の大きさ
    "hide_overlays": True,         # 会議画面に重なる参加者の映像の小窓を最小化する
    "chat_open_urls": True,        # チャットの URL を既定のブラウザで開く
    "chat_download": True,         # チャットのファイルをダウンロードする
    "chat_open_panel": True,       # チャット欄を自動で開く（読み取りに必要）
    "download_dirs": "",           # 会議中に保存された資料を探す追加フォルダ（; 区切り）
    "fullscreen": True,            # 参加後に会議画面を全画面にする
    "fps": 15,
    "crf": 28,
    "capture": "primary",          # primary / all
    "audio": True,
    "audio_offset": 0.0,
    "join_timeout_min": 30,        # 会議に入れるまで待つ時間（待機室・ホスト待ち含む）
    "end_grace_sec": 15,           # 会議画面が消えてから停止するまでの猶予
    "default_max_min": 180,
    "extra_classes": "",
}

ST_WAIT, ST_RUN, ST_DONE, ST_FAIL, ST_LATE, ST_CANCEL = "予約中", "録画中", "完了", "失敗", "期限切れ", "中止"
ST_END = "繰り返し終了"
DT_FMT = "%Y-%m-%d %H:%M"
DAY_NAMES = "月火水木金土日"
FONT_SIZES = {"小": 9, "標準": 11, "大": 13, "特大": 16}  # ポイント


def job_days(job: dict) -> list[int]:
    """繰り返す曜日（0=月〜6=日）。以前の「毎日」「平日」の予約もここで曜日に直す。"""
    rep = job.get("repeat", "none")
    if rep == "daily":
        return list(range(7))
    if rep == "weekdays":
        return [0, 1, 2, 3, 4]
    if rep == "weekly":
        return sorted(job.get("days", []))
    return []


def days_text(days: list[int]) -> str:
    if len(days) == 7:
        return "毎日"
    if days == [0, 1, 2, 3, 4]:
        return "平日（月〜金）"
    return "毎週 " + "・".join(DAY_NAMES[d] for d in days)


def md(d) -> str:
    return f"{d.month}/{d.day}（{DAY_NAMES[d.weekday()]}）"


def repeat_text(job: dict) -> str:
    days = job_days(job)
    if not days:
        return ""
    return days_text(days)


def parse_until(text: str) -> str:
    s = text.strip()
    try:
        return dt.datetime.strptime(s, "%Y-%m-%d").date().isoformat()
    except ValueError:
        raise ValueError("繰り返しの最終日は数字8桁で入力してください（例: 20261225 → 2026-12-25）。")


def next_occurrence(job: dict, after: dt.datetime) -> dt.datetime | None:
    """繰り返し予約の、after より後の次の開始日時（なければ None）。回数の上限は呼び出し側で数える。"""
    days = set(job_days(job))
    if not days:
        return None
    t = dt.datetime.strptime(job["start"], DT_FMT)
    until = dt.date.fromisoformat(job["until"]) if job.get("until") else None
    for i in range(0, 15):
        day = after.date() + dt.timedelta(days=i)
        if until and day > until:
            return None
        cand = dt.datetime.combine(day, dt.time(t.hour, t.minute))
        if cand > after and day.weekday() in days:
            return cand
    return None


def recording_dates(job: dict, after: dt.datetime, limit: int) -> list[dt.datetime]:
    """これから録画が始まる日時（選んだ曜日だけ。回数・最終日の指定も反映）を limit 件まで。"""
    out: list[dt.datetime] = []
    if job.get("remaining"):
        limit = min(limit, int(job["remaining"]))
    t = after
    while len(out) < limit:
        nxt = next_occurrence(job, t)
        if nxt is None:
            break
        out.append(nxt)
        t = nxt
    return out


# ---------------------------------------------------------------- 入力欄の自動整形
def format_digits(digits: str, groups: tuple, seps: tuple, trailing: bool = False) -> str:
    """数字を区切り付きの形にする。例: 202610211000 → 2026-10-21 10:00
    trailing=True のときは、区切りの位置まで打ち終えた時点で次の区切り文字も付ける（例: 2026 → 2026-）。"""
    digits = digits[:sum(groups)]
    out, i = [], 0
    for gi, g in enumerate(groups):
        part = digits[i:i + g]
        if not part:
            break
        out.append(part)
        i += len(part)
        if gi < len(seps) and (i < len(digits) or (trailing and len(part) == g)):
            out.append(seps[gi])
        if len(part) < g:
            break
    return "".join(out)


def fmt_datetime(text: str, trailing: bool) -> str:
    return format_digits(re.sub(r"\D", "", text), (4, 2, 2, 2, 2), ("-", "-", " ", ":"), trailing)


def fmt_date(text: str, trailing: bool) -> str:
    return format_digits(re.sub(r"\D", "", text), (4, 2, 2), ("-", "-"), trailing)


def fmt_meeting(text: str, trailing: bool, final: bool = False) -> str | None:
    """ミーティングIDなら「123 4567 8901」の形にする。URL を貼った場合は触らない（None）。"""
    if not text.strip() or re.search(r"[^\d\s\-]", text):
        return None
    d = re.sub(r"\D", "", text)[:11]
    if final:  # 入力し終えたら桁数に合わせた区切りに直す
        groups = {9: (3, 3, 3), 10: (3, 3, 4)}.get(len(d), (3, 4, 4))
        return format_digits(d, groups, (" ", " "))
    return format_digits(d, (3, 4, 4), (" ", " "), trailing)


def fmt_passcode(text: str, trailing: bool) -> str:
    return re.sub(r"\s", "", text)  # パスコードに空白は入らないので取り除く


def fmt_number(text: str, trailing: bool) -> str:
    return re.sub(r"\D", "", text)[:4]


def attach_format(entry: ttk.Entry, var: tk.StringVar, fmt, final_fmt=None) -> None:
    """入力に合わせて区切り文字を自動で入れる。
    キー操作ではなく「欄の中身が変わったとき」に動くので、日本語入力（IME）の確定や貼り付けでも働く。
    全角数字は半角に直す。カーソル位置は数字の数を基準に保つ。"""
    state = {"last": var.get(), "busy": False, "pending": False}

    def apply(final: bool = False):
        state["pending"] = False
        text = var.get()
        norm = unicodedata.normalize("NFKC", text)  # ２０２６ → 2026
        try:
            cur = entry.index("insert")
        except tk.TclError:
            cur = len(text)
        at_end = cur >= len(text)
        grew = len(norm) > len(state["last"])
        f = final_fmt if (final and final_fmt) else fmt
        # 打ち足したときだけ末尾に区切りを付ける（消しているときに付けると消せなくなる）
        new = f(norm, at_end and grew and not final)
        if new is None:
            new = norm
        if new != text:
            n_before = sum(c.isdigit() for c in norm[:cur])
            state["busy"] = True
            var.set(new)
            state["busy"] = False
            if at_end:
                entry.icursor("end")
            else:
                pos = cnt = 0
                for k, ch in enumerate(new):
                    if cnt >= n_before:
                        break
                    if ch.isdigit():
                        cnt += 1
                    pos = k + 1
                entry.icursor(pos)
        state["last"] = var.get()

    def on_write(*_):
        if state["busy"] or state["pending"]:
            return
        state["pending"] = True
        entry.after_idle(apply)  # 入力の処理が終わってから整形する

    var.trace_add("write", on_write)
    entry.bind("<FocusOut>", lambda e: apply(final=True), add="+")


def documents_dir() -> Path:
    """「ドキュメント」フォルダ（OneDrive に移動している場合も考慮）。"""
    if os.name == "nt":
        try:
            buf = ctypes.create_unicode_buffer(260)
            if ctypes.windll.shell32.SHGetFolderPathW(None, 5, None, 0, buf) == 0:  # CSIDL_PERSONAL
                return Path(buf.value)
        except Exception:
            pass
    return Path.home() / "Documents"


SKIP_SUFFIXES = (".crdownload", ".part", ".partial", ".tmp", ".download", ".opdownload")


def collect_files(since: float, until: float, dest: Path, extra_dirs: str, exclude: Path) -> list[Path]:
    """会議中に保存されたファイル（チャットの資料・ブラウザでのダウンロード）を dest にコピーする。"""
    dirs = [documents_dir() / "Zoom", Path.home() / "Downloads"]
    dirs += [Path(d.strip()) for d in extra_dirs.split(";") if d.strip()]
    exclude = exclude.resolve()
    copied: list[Path] = []
    seen: set[Path] = set()
    for d in dirs:
        if not d.is_dir():
            continue
        for p in d.rglob("*"):
            try:
                if not p.is_file() or p.suffix.lower() in SKIP_SUFFIXES or p.name.startswith(("~$", ".")) \
                        or p.name.lower() == "desktop.ini":
                    continue
                rp = p.resolve()
                if rp in seen or exclude in rp.parents:
                    continue
                st = p.stat()
                if not (since - 5 <= st.st_mtime <= until + 5) or st.st_size > 1024 ** 3:
                    continue
                seen.add(rp)
                dest.mkdir(parents=True, exist_ok=True)
                target = dest / p.name
                k = 1
                while target.exists():
                    target = dest / f"{p.stem} ({k}){p.suffix}"
                    k += 1
                shutil.copy2(p, target)
                copied.append(target)
            except OSError:
                continue
    return copied


def onoff(v) -> str:
    return "オン" if v else "オフ"


def load_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def save_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def parse_start(text: str) -> dt.datetime:
    s = text.strip().replace("：", ":")
    now = dt.datetime.now()
    for fmt in ("%Y-%m-%d %H:%M", "%Y/%m/%d %H:%M", "%m/%d %H:%M", "%H:%M"):
        try:
            t = dt.datetime.strptime(s, fmt)
        except ValueError:
            continue
        if fmt == "%H:%M":
            t = now.replace(hour=t.hour, minute=t.minute, second=0, microsecond=0)
            if t < now - dt.timedelta(minutes=1):
                t += dt.timedelta(days=1)
        elif fmt == "%m/%d %H:%M":
            t = t.replace(year=now.year)
        return t
    raise ValueError("開始日時が途中までしか入力されていません。\n数字12桁で入力してください（例: 202610211000 → 2026-10-21 10:00）。")


def keep_awake(on: bool) -> None:
    """予約がある間は PC のスリープと画面オフを防ぐ。"""
    if os.name != "nt":
        return
    ES_CONTINUOUS, ES_SYSTEM_REQUIRED, ES_DISPLAY_REQUIRED = 0x80000000, 0x00000001, 0x00000002
    flags = ES_CONTINUOUS | (ES_SYSTEM_REQUIRED | ES_DISPLAY_REQUIRED if on else 0)
    ctypes.windll.kernel32.SetThreadExecutionState(flags)


class Cancelled(Exception):
    pass


# ======================================================================== 実行
class Worker(threading.Thread):
    """1件の予約を実行: Zoom起動 → 参加ボタン等を自動で押す → 会議に入ったら録画 → 終了検知で停止。"""

    def __init__(self, app: "App", job: dict):
        super().__init__(daemon=True)
        self.app, self.job = app, job
        self.stop_event = threading.Event()
        self.uia = zui.available()
        self.chat = None
        self.hider = zui.OverlayHider(self.log)

    def log(self, msg: str) -> None:
        self.app.post(lambda: self.app.log(msg))

    def _scan(self):
        if not self.uia:
            return None
        try:
            return zui.scan()
        except Exception as e:
            self.log(f"（参考）Zoom画面の読み取りに失敗: {e}")
            return None

    def run(self) -> None:
        s = dict(self.app.settings)
        job = self.job
        cam, mic = bool(job.get("camera", False)), bool(job.get("mic", False))
        status, output, rec = ST_FAIL, "", None
        try:
            if self.uia:
                zui.init_thread()
            else:
                self.log("⚠ pywinauto が未インストールのため、参加ボタンの自動クリックができません（setup.bat を実行してください）")
            meeting = zc.parse_meeting(job["meeting"], job.get("password", ""))
            ffmpeg = find_ffmpeg(s["ffmpeg_path"])
            if not ffmpeg:
                raise RuntimeError("ffmpeg が見つかりません（設定タブで場所を指定してください）")
            extra = tuple(c.strip() for c in s["extra_classes"].split(",") if c.strip())
            elems = self._scan()
            if zc.is_in_meeting(extra) or (elems and zui.meeting_by_uia(elems)):
                raise RuntimeError("既に別の Zoom 会議に参加中のため実行しませんでした")

            self.log(f"Zoom に参加します: {meeting.label()}（カメラ{onoff(cam)} / マイク{onoff(mic)}）")
            zc.launch_join(meeting, job.get("name") or s["display_name"])
            assist = zui.Assistant(cam, mic, self.log, extra) if self.uia else None
            self.chat = zui.ChatWatcher(self.log, bool(s["chat_open_urls"]), bool(s["chat_download"]),
                                        bool(s["chat_open_panel"])) if self.uia else None
            tracked = self._wait_join(assist, extra, s)
            joined_at = time.time()
            if s["fullscreen"]:
                self._fullscreen(tracked, assist, s, first=True)

            out_dir = Path(s["output_dir"])
            out_dir.mkdir(parents=True, exist_ok=True)
            stamp = dt.datetime.now().strftime("%Y%m%d_%H%M")
            out_path = out_dir / f"zoom_{stamp}_{meeting.meeting_id}.mp4"
            rec = ScreenRecorder(ffmpeg, out_path, self.log, fps=int(s["fps"]), crf=int(s["crf"]),
                                 capture=s["capture"], audio=bool(s["audio"]),
                                 audio_offset=float(s["audio_offset"]))
            rec.start()

            reason = self._watch(rec, assist, tracked, extra, s)
            self.log(f"停止理由: {reason}")
            path = rec.stop()
            rec = None
            output, status = str(path), ST_DONE
            self.log(f"保存しました: {path}")
            if s["chat_download"]:
                self._collect(joined_at, path, s)
        except Cancelled:
            status = ST_CANCEL
            self.log("参加前に中止しました")
        except Exception as e:
            self.log(f"エラー: {e}")
            traceback.print_exc()
            if rec is not None:
                try:
                    output = str(rec.stop())
                    self.log(f"途中までの録画を保存しました: {output}")
                except Exception as e2:
                    self.log(f"録画の書き出しにも失敗しました: {e2}")
        finally:
            self.app.post(lambda: self.app.job_finished(job["id"], status, output))

    def _collect(self, joined_at: float, video: Path, s: dict) -> None:
        """会議中にダウンロードされた資料を、録画ファイルの横の「〜_資料」フォルダにまとめる。"""
        time.sleep(3)  # 終了直前に始まったダウンロードの完了待ち
        dest = Path(video).with_name(Path(video).stem + "_資料")
        try:
            files = collect_files(joined_at, time.time(), dest, s["download_dirs"], Path(s["output_dir"]))
        except Exception as e:
            self.log(f"資料の回収に失敗しました: {e}")
            return
        if files:
            self.log(f"会議中の資料 {len(files)} 件をまとめました: {dest}")
            for f in files[:20]:
                self.log(f"  ・{f.name}")
        else:
            self.log("会議中にダウンロードされた資料はありませんでした")

    def _open_urls(self, urls: list[str], tracked: set[int]) -> None:
        """チャットの URL を既定のブラウザで開き、録画が隠れないよう Zoom を前面に戻す。"""
        for u in urls:
            try:
                webbrowser.open(u, new=2)
                self.log(f"チャットのURLを開きました: {u}")
            except Exception as e:
                self.log(f"URLを開けませんでした: {u}（{e}）")
            time.sleep(0.5)
        for _ in range(2):  # ブラウザが後から前面に出てくることがあるので2回戻す
            time.sleep(2)
            main = zc.main_window(tracked)
            if main:
                zc.focus_window(main)

    def _wait_join(self, assist, extra: tuple, s: dict) -> set[int]:
        """参加前の画面を自動で進め、会議に入るまで待つ（ここではまだ録画しない）。会議画面のウィンドウを返す。"""
        t0 = time.time()
        timeout = int(s["join_timeout_min"]) * 60
        class_since = None
        waiting_logged = False
        while True:
            if self.stop_event.wait(1.5):
                raise Cancelled()
            elems = self._scan()
            acted = 0
            if elems is not None:
                hw = zui.meeting_hwnds(elems)
                if hw:
                    self.log("会議に参加しました")
                    return hw
                acted = assist.prejoin_step(elems)
            if zc.is_in_meeting(extra) and not acted:
                class_since = class_since or time.time()
                # ボタンで判定できない場合でも、会議画面が出続けていれば参加済みとみなす
                if elems is None or time.time() - class_since > 45:
                    self.log("会議画面を検出しました")
                    return {w.hwnd for w in zc.meeting_windows(extra)}
            else:
                class_since = None
            elapsed = time.time() - t0
            if elapsed > 60 and not acted and not waiting_logged:
                waiting_logged = True
                self.log("ホストの開始・待機室からの許可を待っています…")
            if elapsed > timeout:
                raise RuntimeError(f"{timeout // 60}分待っても会議に参加できませんでした（録画はしていません）")

    def _chat_step(self, elems, tracked: set[int]) -> None:
        chat = self.chat
        try:
            meet = zui.meeting_hwnds(elems) | {h for h in tracked if zc.window_alive(h)}
            chat.ensure_panel(elems, zc.main_window(tracked), meet)
            chat.handle_save_dialog(elems)
            chat.click_downloads(elems)
            urls = chat.new_urls(elems)
            if urls:
                self._open_urls(urls, tracked)
        except Exception as e:
            self.log(f"（参考）チャットの処理でエラー: {e}")

    def _fullscreen(self, tracked: set[int], assist, s: dict, first: bool) -> str:
        """会議画面を全画面にする（録画が見やすいように）。"""
        main = zc.main_window(tracked)
        if not main:
            return "none"
        if first and assist is not None:
            # オーディオ接続ダイアログ等が残っていると全画面にできないので先に閉じる
            elems = self._scan()
            if elems:
                assist.misc_step(elems)
                time.sleep(1)
        uia_click = (lambda: zui.click_fullscreen()) if self.uia else None
        result = zc.make_fullscreen(main, primary_only=(s["capture"] == "primary"), uia_click=uia_click)
        if result == "full":
            self.log("会議画面を全画面表示にしました")
        elif result == "max" and first:
            self.log("⚠ 全画面表示にできなかったため、最大化しました")
        return result

    def _watch(self, rec: ScreenRecorder, assist, tracked: set[int], extra: tuple, s: dict) -> str:
        t0 = time.time()
        max_sec = int(self.job.get("max_min") or s["default_max_min"]) * 60
        grace = int(s["end_grace_sec"])
        tracked = set(tracked)
        gone_since = None
        ok_count, enforcing = 0, assist is not None
        last_scan, last_fs, fs_fail = 0.0, time.time(), 0
        class_seen = False
        while True:
            if self.stop_event.wait(2):
                return "手動で停止"
            if not rec.is_running():
                return "ffmpeg が予期せず終了"
            now = time.time()
            if now - t0 > max_sec:
                return f"最大録画時間（{max_sec // 60}分）に到達"
            # 終了判定: 会議ウィンドウ（クラス名/タイトル）が見つかる環境ではそれだけで判定する。
            # 会議後も残ることがあるウィンドウ（ハンドル）やボタンの有無には頼らない。
            by_class = zc.is_in_meeting(extra)
            if by_class and not class_seen:
                class_seen = True
                names = ", ".join(sorted({f"{w.class_name}「{w.title}」" for w in zc.meeting_windows(extra)}))
                self.log(f"会議ウィンドウで終了を判定します: {names}")
            if class_seen:
                alive = by_class
            else:
                if not self.uia:
                    tracked |= {w.hwnd for w in zc.meeting_windows(extra)}
                alive = any(zc.window_alive(h) for h in tracked)

            # カメラ・マイクの設定中や、会議画面が見当たらないときは毎回、それ以外は5秒ごとに画面を読む
            if self.uia and (enforcing or not alive or now - last_scan >= 5):
                if not alive:
                    zui.wake_toolbar(extra)
                elems = self._scan()
                last_scan = now
                if elems is not None:
                    new = zui.meeting_hwnds(elems) - tracked
                    if new and not class_seen:  # ブレイクアウトルーム移動などで会議画面が作り直された
                        tracked |= new
                        alive = True
                    end = zui.end_message(elems, exclude_hwnds=tracked)
                    if end:
                        zui.close_dialog(elems, end.hwnd)
                        return f"会議の終了を検知（{end.name[:40]}）"
                    if enforcing:
                        if assist.enforce_step(elems):
                            ok_count += 1
                            if ok_count >= 2:
                                enforcing = False
                                self.log(f"カメラ{onoff(assist.camera_on)}・マイク{onoff(assist.mic_on)}に設定しました")
                        else:
                            ok_count = 0
                        if enforcing and now - t0 > ENFORCE_SEC:
                            enforcing = False
                            self.log("⚠ カメラ/マイクの状態を確認できませんでした。設定タブの「Zoomの画面要素を書き出す」で調査できます")
                    else:
                        assist.misc_step(elems)
                        if self.chat is not None and alive:
                            self._chat_step(elems, tracked)
                    if s["hide_overlays"] and alive:
                        try:
                            keep = zui.meeting_hwnds(elems) | {h for h in tracked if zc.window_alive(h)}
                            self.hider.step(elems, zc.main_window(tracked), keep)
                        except Exception as e:
                            self.log(f"（参考）小窓の処理でエラー: {e}")

            if alive:
                gone_since = None
            else:
                gone_since = gone_since or now
                if now - gone_since >= grace:
                    return "会議画面が閉じたため、会議の終了と判断"
                continue

            # 画面共有の開始などで全画面が解除されたら戻す
            if s["fullscreen"] and fs_fail < 3 and now - last_fs >= 20:
                last_fs = now
                main = zc.main_window(tracked)
                if main and not zc.any_zoom_fullscreen():
                    fs_fail = 0 if self._fullscreen(tracked, assist, s, first=False) == "full" else fs_fail + 1


# ======================================================================== 画面
class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(APP_NAME)
        self.geometry("960x760")
        self.minsize(860, 640)
        self.settings = {**DEFAULT_SETTINGS, **load_json(SETTINGS_FILE, {})}
        self.jobs: list[dict] = load_json(JOBS_FILE, [])
        for j in self.jobs:
            if j["status"] == ST_RUN:  # 前回、録画中のまま終了していたもの
                j["status"] = ST_FAIL
            if j.get("repeat") in ("daily", "weekdays"):  # 以前の「毎日」「平日」は曜日指定に直す
                j["days"], j["repeat"] = job_days(j), "weekly"
        self.worker: Worker | None = None
        self._queue: queue.Queue = queue.Queue()

        # 文字の大きさ（右上にいつでも押せるように置く）
        top = ttk.Frame(self)
        top.pack(fill="x", padx=10, pady=(8, 0))
        self.v_font = tk.StringVar(value=self.settings.get("font_size", "標準"))
        for name in reversed(list(FONT_SIZES)):
            ttk.Radiobutton(top, text=name, value=name, variable=self.v_font, style="Toolbutton",
                            command=self._change_font).pack(side="right", padx=1)
        ttk.Label(top, text="文字の大きさ：").pack(side="right", padx=(0, 4))
        self._apply_font(self.v_font.get())

        nb = ttk.Notebook(self)
        nb.pack(fill="both", expand=True, padx=8, pady=8)
        self._build_jobs_tab(nb)
        self._build_settings_tab(nb)
        self._build_log_tab(nb)
        self.status_var = tk.StringVar(value="待機中")
        ttk.Label(self, textvariable=self.status_var, anchor="w").pack(fill="x", padx=10, pady=(0, 6))

        self.refresh_jobs()
        self.after(100, self._fit_window)
        self.protocol("WM_DELETE_WINDOW", self.on_close)
        self.after(200, self._pump)
        self.after(1000, self._tick)
        self._startup_checks()

    # ---------------------------------------------------------------- 文字の大きさ
    def _apply_font(self, name: str) -> None:
        size = FONT_SIZES.get(name, FONT_SIZES["標準"])
        for fname in ("TkDefaultFont", "TkTextFont", "TkMenuFont", "TkHeadingFont", "TkCaptionFont",
                      "TkSmallCaptionFont", "TkIconFont", "TkTooltipFont", "TkFixedFont"):
            try:
                tkfont.nametofont(fname).configure(size=size)
            except tk.TclError:
                pass
        style = ttk.Style(self)
        line = tkfont.nametofont("TkDefaultFont").metrics("linespace")
        style.configure("Treeview", rowheight=int(line * 1.5))
        style.configure("Treeview.Heading", font=tkfont.nametofont("TkHeadingFont"))
        style.configure("Toolbutton", padding=(8, 2))

    def _change_font(self) -> None:
        name = self.v_font.get()
        self._apply_font(name)
        self.settings["font_size"] = name
        save_json(SETTINGS_FILE, self.settings)
        self._fit_window()

    def _fit_window(self) -> None:
        """文字が大きくなって入りきらない分だけ、ウィンドウを広げる（画面からははみ出さない）。"""
        self.update_idletasks()
        w = min(max(self.winfo_width(), self.winfo_reqwidth()), self.winfo_screenwidth() - 40)
        h = min(max(self.winfo_height(), self.winfo_reqheight()), self.winfo_screenheight() - 80)
        self.geometry(f"{w}x{h}")

    # ---------------------------------------------------------------- 予約タブ
    def _build_jobs_tab(self, nb):
        f = ttk.Frame(nb, padding=10)
        nb.add(f, text="予約")
        self.form_box = form = ttk.LabelFrame(f, text="会議を追加", padding=10)
        form.pack(fill="x")
        self.editing_id: str | None = None
        self.v_meeting, self.v_password = tk.StringVar(), tk.StringVar()
        self.v_start = tk.StringVar(value=(dt.datetime.now() + dt.timedelta(minutes=5)).strftime(DT_FMT))
        self.v_name = tk.StringVar(value=self.settings["display_name"])
        self.v_max = tk.StringVar(value=str(self.settings["default_max_min"]))
        self.v_camera = tk.BooleanVar(value=bool(self.settings["default_camera"]))
        self.v_mic = tk.BooleanVar(value=bool(self.settings["default_mic"]))
        self.v_repeat_on = tk.BooleanVar(value=False)
        self.v_days = [tk.BooleanVar(value=False) for _ in range(7)]
        self.v_end = tk.StringVar(value="none")   # none / count / until
        self.v_count = tk.StringVar()
        self.v_until = tk.StringVar()
        form.columnconfigure(2, weight=1)
        rows = [  # ラベル, 変数, 幅, 整形, 入力し終えたときの整形
            ("招待URL または ミーティングID", self.v_meeting, 40, lambda t, tr: fmt_meeting(t, tr),
             lambda t, tr: fmt_meeting(t, tr, final=True)),
            ("パスコード", self.v_password, 24, fmt_passcode, None),
            ("開始日時", self.v_start, 18, fmt_datetime, None),
            ("表示名（空欄ならZoomの登録名）", self.v_name, 24, None, None),
            ("最大録画時間（分）", self.v_max, 8, fmt_number, None),
        ]
        self.hints: dict[str, ttk.Label] = {}

        def hint_label(key, row, col=2, span=1):
            self.hints[key] = ttk.Label(form, foreground="gray40", wraplength=200, justify="left")
            self.hints[key].grid(row=row, column=col, columnspan=span, sticky="w", padx=(0 if col == 2 else 6, 0))

        hint_keys = ["meeting", "password", "start", None, "max"]
        r = 0
        for r, (label, var, w, fmt, final) in enumerate(rows):
            ttk.Label(form, text=label).grid(row=r, column=0, sticky="w", pady=2)
            ent = ttk.Entry(form, textvariable=var, width=w)
            ent.grid(row=r, column=1, sticky="w", pady=2, padx=6)
            if fmt:
                attach_format(ent, var, fmt, final)
            if hint_keys[r]:
                hint_label(hint_keys[r], r)
        for label, var in (("カメラ", self.v_camera), ("マイク", self.v_mic)):
            r += 1
            ttk.Label(form, text=label).grid(row=r, column=0, sticky="w", pady=2)
            fr = ttk.Frame(form)
            ttk.Radiobutton(fr, text="オフ", value=False, variable=var).pack(side="left")
            ttk.Radiobutton(fr, text="オン", value=True, variable=var).pack(side="left", padx=10)
            fr.grid(row=r, column=1, sticky="w", padx=6, pady=2)

        # 繰り返し: オン/オフ。オンのときだけ曜日と繰り返し方を出す
        r += 1
        ttk.Label(form, text="繰り返し").grid(row=r, column=0, sticky="w", pady=2)
        fr = ttk.Frame(form)
        ttk.Radiobutton(fr, text="オフ", value=False, variable=self.v_repeat_on).pack(side="left")
        ttk.Radiobutton(fr, text="オン", value=True, variable=self.v_repeat_on).pack(side="left", padx=10)
        fr.grid(row=r, column=1, sticky="w", padx=6, pady=2)

        self.repeat_rows: list[tk.Widget] = []  # 繰り返しがオフのときは隠す
        r += 1
        lbl = ttk.Label(form, text="　曜日")
        lbl.grid(row=r, column=0, sticky="w", pady=2)
        fr = ttk.Frame(form)
        for i, d in enumerate(DAY_NAMES):
            ttk.Checkbutton(fr, text=d, variable=self.v_days[i]).pack(side="left", padx=(0, 4))
        fr.grid(row=r, column=1, sticky="w", padx=6, pady=2)
        hint_label("days", r)
        self.repeat_rows += [lbl, fr, self.hints["days"]]

        r += 1
        lbl = ttk.Label(form, text="　繰り返し方")
        lbl.grid(row=r, column=0, sticky="w", pady=2)
        self.repeat_rows.append(lbl)
        fr = ttk.Frame(form)
        self.repeat_rows.append(fr)
        self.end_widgets = []
        rb = ttk.Radiobutton(fr, text="ずっと", value="none", variable=self.v_end)
        rb.pack(side="left")
        self.end_widgets.append(rb)
        rb = ttk.Radiobutton(fr, text="回数で", value="count", variable=self.v_end)
        rb.pack(side="left", padx=(12, 2))
        self.end_widgets.append(rb)
        self.count_entry = ttk.Entry(fr, textvariable=self.v_count, width=4)
        self.count_entry.pack(side="left")
        attach_format(self.count_entry, self.v_count, fmt_number)
        ttk.Label(fr, text="回").pack(side="left", padx=(2, 0))
        rb = ttk.Radiobutton(fr, text="最終日を指定", value="until", variable=self.v_end)
        rb.pack(side="left", padx=(12, 2))
        self.end_widgets.append(rb)
        self.until_entry = ttk.Entry(fr, textvariable=self.v_until, width=11)
        self.until_entry.pack(side="left")
        attach_format(self.until_entry, self.v_until, fmt_date)
        ttk.Label(fr, text="まで").pack(side="left", padx=(2, 0))
        fr.grid(row=r, column=1, columnspan=2, sticky="w", padx=6, pady=2)
        # 録画が始まる日（選んだ曜日の日付だけを並べる）
        r += 1
        ttk.Label(form, text="録画日").grid(row=r, column=0, sticky="nw", pady=2)
        hint_label("dates", r, col=1, span=2)
        # 入力欄をクリックしたら、その選択肢を選んだことにする
        self.count_entry.bind("<FocusIn>", lambda e: self.v_end.set("count"), add="+")
        self.until_entry.bind("<FocusIn>", lambda e: self.v_end.set("until"), add="+")

        for v in (self.v_meeting, self.v_password, self.v_start, self.v_max, self.v_end, self.v_count, self.v_until):
            v.trace_add("write", lambda *a: self._update_hints())
        for v in self.v_days + [self.v_repeat_on]:
            v.trace_add("write", lambda *a: self._update_repeat_widgets())

        def fit_hints(_e=None):  # 説明文は、右側に残っている幅で折り返す
            for lbl in self.hints.values():
                avail = form.winfo_width() - lbl.winfo_x() - 24
                if avail > 120 and abs(int(lbl.cget("wraplength")) - avail) > 4:
                    lbl.configure(wraplength=avail)
        form.bind("<Configure>", fit_hints, add="+")

        btns = ttk.Frame(form)
        btns.grid(row=r + 1, column=0, columnspan=3, sticky="w", pady=(8, 0))
        self.add_btn = ttk.Button(btns, text="予約に追加", command=self.add_job)
        self.add_btn.pack(side="left")
        self.now_btn = ttk.Button(btns, text="今すぐ参加して録画", command=self.run_now)
        self.now_btn.pack(side="left", padx=6)
        self.cancel_btn = ttk.Button(btns, text="編集をやめる", command=self.cancel_edit)
        self._update_repeat_widgets()

        lst = ttk.Frame(f)
        lst.pack(fill="both", expand=True, pady=(10, 0))
        cols = ("start", "repeat", "meeting", "av", "status", "output")
        self.tree = ttk.Treeview(lst, columns=cols, show="headings", height=8)
        for c, label, w in zip(cols, ("開始日時", "繰り返し", "会議", "カメラ/マイク", "状態", "保存先"),
                                (125, 150, 115, 85, 80, 280)):
            self.tree.heading(c, text=label)
            self.tree.column(c, width=w, anchor="w")
        self.tree.pack(side="left", fill="both", expand=True)
        sb = ttk.Scrollbar(lst, command=self.tree.yview)
        sb.pack(side="right", fill="y")
        self.tree.configure(yscrollcommand=sb.set)
        self.tree.bind("<Double-1>", lambda e: self.on_double_click())

        b2 = ttk.Frame(f)
        b2.pack(fill="x", pady=(6, 0))
        ttk.Button(b2, text="録画を停止", command=self.stop_current).pack(side="left")
        ttk.Button(b2, text="編集", command=self.edit_job).pack(side="left", padx=(12, 0))
        ttk.Button(b2, text="複製", command=self.duplicate_job).pack(side="left", padx=6)
        ttk.Button(b2, text="削除", command=self.delete_job).pack(side="left")
        ttk.Button(b2, text="保存フォルダを開く", command=self.open_folder).pack(side="left", padx=(12, 0))

    def _hint(self, key: str, text: str, error: bool = False, color: str = "gray40") -> None:
        self.hints[key].configure(text=text, foreground="#c0392b" if error else color)

    def _form_days(self) -> list[int]:
        if not self.v_repeat_on.get():
            return []
        return [i for i, v in enumerate(self.v_days) if v.get()]

    def _update_hints(self) -> None:
        """入力欄の横に、入力の仕方や、入力した内容がどう読み取られたかを表示する。"""
        if not getattr(self, "hints", None) or "days" not in self.hints:
            return
        m = self.v_meeting.get().strip()
        url_pwd = False
        digits = re.sub(r"\D", "", m)
        if not m:
            self._hint("meeting", "URLを貼るか、IDの数字を入力（空白は自動）")
        elif not re.search(r"[^\d\s\-]", m) and len(digits) < 9:
            self._hint("meeting", f"IDは9〜11桁です（いま{len(digits)}桁）")
        else:
            try:
                mt = zc.parse_meeting(m, "")
                url_pwd = bool(mt.password)
                self._hint("meeting", f"✓ ID {mt.label()}" + ("（パスコード入り）" if url_pwd else ""))
            except ValueError as e:
                self._hint("meeting", str(e).splitlines()[0], error=True)
        if url_pwd:
            self._hint("password", "URLに含まれているので不要です")
        else:
            self._hint("password", "招待メールのものをそのまま入力（URLを貼った場合は不要）")

        days = self._form_days()
        start = None
        d = re.sub(r"\D", "", self.v_start.get())
        if len(d) < 12:
            self._hint("start", "数字だけでOK（例 202610211000）。区切りは自動")
        else:
            try:
                start = dt.datetime.strptime(self.v_start.get(), DT_FMT)
            except ValueError:
                self._hint("start", "存在しない日時です", error=True)
            else:
                if not days and start < dt.datetime.now() - dt.timedelta(minutes=1):
                    self._hint("start", "過去の日時になっています", error=True)
                else:
                    self._hint("start", f"✓ {start:%H:%M} 開始")
        self._dates_hint(start, days)

        mx = self.v_max.get()
        ok = mx.isdigit() and int(mx) > 0
        self._hint("max", "" if ok else "1以上の数字を入力してください", error=not ok)

        if self.v_repeat_on.get():
            self._hint("days", days_text(days) if days else "曜日を選んでください（毎日なら月〜日、平日なら月〜金）",
                       error=False)

    def _dates_hint(self, start: dt.datetime | None, days: list[int]) -> None:
        """実際に録画が始まる日（繰り返しなら選んだ曜日の日だけ）を並べて表示する。"""
        if start is None:
            self._hint("dates", "開始日時を入れると、ここに録画日が出ます")
            return
        if self.v_repeat_on.get() and not days:
            self._hint("dates", "繰り返す曜日を選ぶと、ここに録画日が出ます")
            return
        if not days:
            if start >= dt.datetime.now() - dt.timedelta(minutes=1):
                self._hint("dates", f"{md(start)} {start:%H:%M}", color="black")
            else:
                self._hint("dates", "開始日時が過ぎています", error=True)
            return
        job = {"repeat": "weekly", "days": days, "start": start.strftime(DT_FMT)}
        end = self.v_end.get()
        if end == "count" and self.v_count.get().isdigit() and int(self.v_count.get()) > 0:
            job["remaining"] = int(self.v_count.get())
        elif end == "until":
            try:
                job["until"] = parse_until(self.v_until.get())
            except ValueError:
                pass
        after = max(start, dt.datetime.now()) - dt.timedelta(minutes=1)
        dates = recording_dates(job, after, 5)
        if not dates:
            self._hint("dates", "この条件だと、録画する日がありません", error=True)
            return
        more = len(recording_dates(job, after, 6)) > 5
        self._hint("dates", "  ".join(md(x) for x in dates) + ("  …" if more else ""), color="black")

    def _update_repeat_widgets(self):
        show = self.v_repeat_on.get()
        for w in self.repeat_rows:
            if show:
                w.grid()
            else:
                w.grid_remove()
        on = bool(self._form_days())
        for w in self.end_widgets + [self.count_entry, self.until_entry]:
            w.state(["!disabled"] if on else ["disabled"])
        self._update_hints()

    # ---------------------------------------------------------------- 設定タブ
    def _build_settings_tab(self, nb):
        f = ttk.Frame(nb, padding=10)
        nb.add(f, text="設定")
        s = self.settings
        self.sv = {k: tk.StringVar(value=str(s[k])) for k in
                   ("output_dir", "ffmpeg_path", "fps", "crf", "audio_offset",
                    "join_timeout_min", "end_grace_sec", "extra_classes", "download_dirs")}
        self.sv_capture = tk.StringVar(value=s["capture"])
        self.sv_audio = tk.BooleanVar(value=bool(s["audio"]))
        self.sv_fullscreen = tk.BooleanVar(value=bool(s["fullscreen"]))
        self.sv_chat = {k: tk.BooleanVar(value=bool(s[k])) for k in ("chat_open_urls", "chat_download", "chat_open_panel")}
        self.sv_hide = tk.BooleanVar(value=bool(s["hide_overlays"]))
        r = 0

        def row(label, widget):
            nonlocal r
            ttk.Label(f, text=label).grid(row=r, column=0, sticky="w", pady=3)
            widget.grid(row=r, column=1, sticky="w", padx=6, pady=3)
            r += 1

        def path_row(label, key, is_dir):
            fr = ttk.Frame(f)
            ttk.Entry(fr, textvariable=self.sv[key], width=55).pack(side="left")
            ttk.Button(fr, text="参照", command=lambda: self._browse(key, is_dir)).pack(side="left", padx=4)
            row(label, fr)

        path_row("保存先フォルダ", "output_dir", True)
        path_row("ffmpeg.exe（空欄で自動検出）", "ffmpeg_path", False)
        cap = ttk.Frame(f)
        ttk.Radiobutton(cap, text="メインモニター", value="primary", variable=self.sv_capture).pack(side="left")
        ttk.Radiobutton(cap, text="全モニター", value="all", variable=self.sv_capture).pack(side="left", padx=8)
        row("録画範囲", cap)
        row("参加後に会議画面を全画面にする", ttk.Checkbutton(f, variable=self.sv_fullscreen))
        row("画面に重なる参加者の映像の小窓を最小化する", ttk.Checkbutton(f, variable=self.sv_hide))
        ch = ttk.Frame(f)
        ttk.Checkbutton(ch, text="URLをブラウザで開く", variable=self.sv_chat["chat_open_urls"]).pack(side="left")
        ttk.Checkbutton(ch, text="ファイルをダウンロード", variable=self.sv_chat["chat_download"]).pack(side="left", padx=10)
        ttk.Checkbutton(ch, text="チャット欄を自動で開く", variable=self.sv_chat["chat_open_panel"]).pack(side="left")
        row("会議中のチャット", ch)
        row("資料を探す追加フォルダ（; 区切り）", ttk.Entry(f, textvariable=self.sv["download_dirs"], width=55))
        row("フレームレート（fps）", ttk.Entry(f, textvariable=self.sv["fps"], width=8))
        row("画質 CRF（小さいほど高画質 18〜32）", ttk.Entry(f, textvariable=self.sv["crf"], width=8))
        row("PCの音声を録音", ttk.Checkbutton(f, variable=self.sv_audio))
        row("音声の補正（秒、＋で音を遅らせる）", ttk.Entry(f, textvariable=self.sv["audio_offset"], width=8))
        row("会議に入れるまで待つ時間（分）", ttk.Entry(f, textvariable=self.sv["join_timeout_min"], width=8))
        row("会議終了判定までの猶予（秒）", ttk.Entry(f, textvariable=self.sv["end_grace_sec"], width=8))
        row("追加の会議ウィンドウクラス（カンマ区切り）", ttk.Entry(f, textvariable=self.sv["extra_classes"], width=40))
        bf = ttk.Frame(f)
        bf.grid(row=r, column=0, columnspan=2, sticky="w", pady=(12, 0))
        ttk.Button(bf, text="設定を保存", command=self.save_settings).pack(side="left")
        ttk.Button(bf, text="10秒テスト録画", command=self.test_record).pack(side="left", padx=6)
        ttk.Button(bf, text="会議中かチェック", command=self.check_meeting).pack(side="left")
        ttk.Button(bf, text="Zoomの画面要素を書き出す", command=self.dump_zoom).pack(side="left", padx=6)

    def _browse(self, key, is_dir):
        p = filedialog.askdirectory() if is_dir else filedialog.askopenfilename(filetypes=[("ffmpeg", "ffmpeg.exe")])
        if p:
            self.sv[key].set(p)

    def save_settings(self, quiet=False) -> bool:
        try:
            new = {k: v.get().strip() for k, v in self.sv.items()}
            for k in ("fps", "crf", "join_timeout_min", "end_grace_sec"):
                new[k] = int(new[k])
            new["audio_offset"] = float(new["audio_offset"])
        except ValueError:
            messagebox.showerror(APP_NAME, "数値の項目に数字以外が入っています。")
            return False
        new["capture"] = self.sv_capture.get()
        new["audio"] = self.sv_audio.get()
        new["fullscreen"] = self.sv_fullscreen.get()
        new["hide_overlays"] = self.sv_hide.get()
        for k, v in self.sv_chat.items():
            new[k] = v.get()
        self.settings.update(new)
        save_json(SETTINGS_FILE, self.settings)
        if not quiet:
            self.log("設定を保存しました")
        return True

    # ---------------------------------------------------------------- ログタブ
    def _build_log_tab(self, nb):
        f = ttk.Frame(nb, padding=10)
        nb.add(f, text="ログ")
        self.log_text = tk.Text(f, height=20, state="disabled", wrap="word", font="TkTextFont")
        self.log_text.pack(fill="both", expand=True)

    def log(self, msg: str) -> None:
        line = f"[{dt.datetime.now():%H:%M:%S}] {msg}"
        self.log_text.configure(state="normal")
        self.log_text.insert("end", line + "\n")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")
        self.status_var.set(msg.splitlines()[0])
        try:
            DATA_DIR.mkdir(parents=True, exist_ok=True)
            with open(DATA_DIR / "app.log", "a", encoding="utf-8") as fp:
                fp.write(f"{dt.datetime.now():%Y-%m-%d} {line}\n")
        except OSError:
            pass

    # ---------------------------------------------------------------- 予約操作
    def _job_from_form(self, start: dt.datetime, allow_repeat: bool = True) -> dict | None:
        try:
            m = zc.parse_meeting(self.v_meeting.get(), re.sub(r"\s", "", self.v_password.get()))
            try:
                max_min = int(self.v_max.get())
                if max_min <= 0:
                    raise ValueError
            except ValueError:
                raise ValueError("最大録画時間は1以上の数字で入力してください。")
            days = self._form_days() if allow_repeat else []
            if allow_repeat and self.v_repeat_on.get() and not days:
                raise ValueError("繰り返しがオンになっています。繰り返す曜日を選ぶか、繰り返しをオフにしてください。")
            rep = "weekly" if days else "none"
            end = self.v_end.get() if days else "none"
            until = parse_until(self.v_until.get()) if end == "until" else ""
            remaining = None
            if end == "count":
                c = self.v_count.get()
                if not (c.isdigit() and int(c) > 0):
                    raise ValueError("繰り返す回数を、1以上の数字で入力してください。")
                remaining = int(c)
        except ValueError as e:
            messagebox.showerror(APP_NAME, str(e))
            return None
        job = {"id": uuid.uuid4().hex, "meeting": self.v_meeting.get().strip(),
               "password": re.sub(r"\s", "", self.v_password.get()), "label": m.label(),
               "start": start.strftime(DT_FMT), "name": self.v_name.get().strip(),
               "camera": self.v_camera.get(), "mic": self.v_mic.get(),
               "repeat": rep, "days": days, "until": until,
               "max_min": max_min, "status": ST_WAIT, "output": ""}
        if remaining:
            job["remaining"] = remaining
        if rep != "none":
            # 最初の回: 入力した日時以降で、条件に合う最初の日時
            first = next_occurrence(job, max(start, dt.datetime.now()) - dt.timedelta(minutes=1))
            if first is None:
                messagebox.showerror(APP_NAME, "最終日までに、選んだ曜日の日がありません。")
                return None
            job["start"] = first.strftime(DT_FMT)
        return job

    def _remember_form(self, job: dict) -> None:
        """表示名・カメラ・マイク・最大録画時間は、次に予約するときの初期値として覚えておく。"""
        self.settings.update(display_name=job["name"], default_camera=job["camera"],
                             default_mic=job["mic"], default_max_min=job["max_min"])
        save_json(SETTINGS_FILE, self.settings)

    def _load_form(self, job: dict) -> None:
        m = job.get("meeting", "")
        self.v_meeting.set(fmt_meeting(m, False, final=True) or m)
        self.v_password.set(job.get("password", ""))
        self.v_start.set(job.get("start", ""))
        self.v_name.set(job.get("name", ""))
        self.v_max.set(str(job.get("max_min", self.settings["default_max_min"])))
        self.v_camera.set(bool(job.get("camera")))
        self.v_mic.set(bool(job.get("mic")))
        days = set(job_days(job))
        self.v_repeat_on.set(bool(days))
        for i, v in enumerate(self.v_days):
            v.set(i in days)
        self.v_count.set(str(job["remaining"]) if job.get("remaining") else "")
        self.v_until.set(job.get("until", ""))
        self.v_end.set("count" if job.get("remaining") else "until" if job.get("until") else "none")
        self._update_repeat_widgets()

    def _clear_form(self) -> None:
        self.v_meeting.set("")
        self.v_password.set("")
        self.v_repeat_on.set(False)
        for v in self.v_days:
            v.set(False)
        self.v_end.set("none")
        self.v_count.set("")
        self.v_until.set("")
        self._update_repeat_widgets()

    def _set_edit_mode(self, job_id: str | None) -> None:
        self.editing_id = job_id
        if job_id:
            self.form_box.configure(text="予約を編集中")
            self.add_btn.configure(text="変更を保存")
            self.now_btn.pack_forget()
            self.cancel_btn.pack(side="left", padx=6)
        else:
            self.form_box.configure(text="会議を追加")
            self.add_btn.configure(text="予約に追加")
            self.cancel_btn.pack_forget()
            self.now_btn.pack(side="left", padx=6)

    def _selected_job(self) -> dict | None:
        sel = self.tree.selection()
        if not sel:
            messagebox.showinfo(APP_NAME, "一覧から予約を選んでください。")
            return None
        return next((j for j in self.jobs if j["id"] == sel[0]), None)

    def add_job(self):
        try:
            start = parse_start(self.v_start.get())
        except ValueError as e:
            messagebox.showerror(APP_NAME, str(e))
            return
        repeating = bool(self._form_days())
        if not repeating and start < dt.datetime.now() - dt.timedelta(minutes=1):
            messagebox.showerror(APP_NAME, "過去の日時は指定できません。")
            return
        job = self._job_from_form(start)
        if not job:
            return
        if self.editing_id:
            old = next((j for j in self.jobs if j["id"] == self.editing_id), None)
            if old is None or old["status"] != ST_WAIT:
                messagebox.showwarning(APP_NAME, "この予約はすでに実行されたため、変更できません。")
                self.cancel_edit()
                return
            job["id"] = old["id"]
            old.clear()
            old.update(job)
            verb = "変更しました"
        else:
            self.jobs.append(job)
            verb = "予約しました"
        self._remember_form(job)
        self.save_jobs()
        rep = f"  {repeat_text(job)}" if job["repeat"] != "none" else ""
        self.log(f"{verb}: {job['start']}  {job['label']}{rep}（カメラ{onoff(job['camera'])} / マイク{onoff(job['mic'])}）")
        self._set_edit_mode(None)
        self._clear_form()

    def edit_job(self):
        job = self._selected_job()
        if not job:
            return
        if job["status"] != ST_WAIT:
            messagebox.showinfo(APP_NAME, "編集できるのは「予約中」の予約だけです。\n終わった予約をもう一度使うときは「複製」を使ってください。")
            return
        self._load_form(job)
        self._set_edit_mode(job["id"])
        self.log(f"編集中: {job['start']} {job['label']}（内容を直して「変更を保存」を押してください）")

    def duplicate_job(self):
        job = self._selected_job()
        if not job:
            return
        src = dict(job)
        start = dt.datetime.strptime(src["start"], DT_FMT)
        if start < dt.datetime.now() and src.get("repeat", "none") == "none":
            # 過去の予約は、同じ時刻の次の日付を初期値にする
            nxt = dt.datetime.combine(dt.date.today(), start.time())
            if nxt <= dt.datetime.now():
                nxt += dt.timedelta(days=1)
            src["start"] = nxt.strftime(DT_FMT)
        self._set_edit_mode(None)
        self._load_form(src)
        self.log("予約を複製しました。開始日時などを確認して「予約に追加」を押してください")

    def cancel_edit(self):
        self._set_edit_mode(None)
        self._clear_form()

    def on_double_click(self):
        sel = self.tree.selection()
        job = next((j for j in self.jobs if sel and j["id"] == sel[0]), None)
        if not job:
            return
        if job.get("output") and os.path.exists(job["output"]):
            os.startfile(job["output"])
        elif job["status"] == ST_WAIT:
            self.edit_job()

    def run_now(self):
        if self.worker:
            messagebox.showwarning(APP_NAME, "録画中です。先に停止してください。")
            return
        job = self._job_from_form(dt.datetime.now(), allow_repeat=False)
        if job:
            self._remember_form(job)
            self.jobs.append(job)
            self.save_jobs()
            self.start_job(job)

    def delete_job(self):
        sel = set(self.tree.selection())
        if not sel:
            messagebox.showinfo(APP_NAME, "一覧から予約を選んでください。")
            return
        running = self.worker.job["id"] if self.worker else None
        if running in sel:
            messagebox.showwarning(APP_NAME, "実行中の予約は削除できません。先に停止してください。")
            return
        if any(j["id"] in sel and j.get("repeat", "none") != "none" and j["status"] == ST_WAIT for j in self.jobs):
            if not messagebox.askyesno(APP_NAME, "繰り返し予約を削除すると、今後の回もすべて実行されなくなります。削除しますか？"):
                return
        if self.editing_id in sel:
            self.cancel_edit()
        self.jobs = [j for j in self.jobs if j["id"] not in sel]
        self.save_jobs()

    def stop_current(self):
        if self.worker:
            self.log("停止しています…")
            self.worker.stop_event.set()

    def save_jobs(self):
        save_json(JOBS_FILE, self.jobs)
        self.refresh_jobs()

    def refresh_jobs(self):
        sel = self.tree.selection()
        self.tree.delete(*self.tree.get_children())
        # 予約中（近い順）→ それ以外（新しい順）
        waiting = sorted((j for j in self.jobs if j["status"] in (ST_WAIT, ST_RUN)), key=lambda j: j["start"])
        others = sorted((j for j in self.jobs if j["status"] not in (ST_WAIT, ST_RUN)), key=lambda j: j["start"], reverse=True)
        for j in waiting + others:
            av = f"{onoff(j.get('camera'))} / {onoff(j.get('mic'))}"
            self.tree.insert("", "end", iid=j["id"], values=(j["start"], repeat_text(j), j["label"], av,
                                                            j["status"], j["output"]))
        keep = [i for i in sel if self.tree.exists(i)]
        if keep:
            self.tree.selection_set(keep)
        keep_awake(any(j["status"] in (ST_WAIT, ST_RUN) for j in self.jobs))

    def start_job(self, job: dict):
        self.save_settings(quiet=True)
        job["status"] = ST_RUN
        self.save_jobs()
        self.worker = Worker(self, job)
        self.worker.start()

    def job_finished(self, job_id: str, status: str, output: str):
        for j in self.jobs:
            if j["id"] == job_id:
                j["status"], j["output"] = status, output
        self.worker = None
        self.save_jobs()

    def _occurrence(self, template: dict, status: str) -> dict:
        """繰り返し予約から、今回の1回分の記録を作る。"""
        inst = dict(template)
        inst.update(id=uuid.uuid4().hex, repeat="none", days=[], until="", parent=template["id"],
                    status=status, output="")
        inst.pop("remaining", None)
        return inst

    def _advance(self, template: dict, now: dt.datetime) -> None:
        if template.get("remaining"):  # 回数指定: 今回の分を数える
            template["remaining"] -= 1
            if template["remaining"] <= 0:
                template["status"] = ST_END
                self.log(f"指定の回数を録画したので、繰り返し予約を終了しました: {template['label']}")
                return
        nxt = next_occurrence(template, max(now, dt.datetime.strptime(template["start"], DT_FMT)))
        if nxt:
            template["start"] = nxt.strftime(DT_FMT)
            self.log(f"次回の予約: {template['start']} {template['label']}（{repeat_text(template)}）")
        else:
            template["status"] = ST_END
            self.log(f"繰り返し予約が終了しました: {template['label']}")

    def _tick(self):
        now = dt.datetime.now()
        changed = False
        for j in sorted(self.jobs, key=lambda j: j["start"]):
            if j["status"] != ST_WAIT or j["id"] == self.editing_id:
                continue
            start = dt.datetime.strptime(j["start"], DT_FMT)
            if now < start:
                continue
            repeating = j.get("repeat", "none") != "none"
            if now - start > LATE_LIMIT:
                self.log(f"予約時刻を過ぎていたため実行しませんでした: {j['start']} {j['label']}")
                if repeating:
                    self.jobs.append(self._occurrence(j, ST_LATE))
                    self._advance(j, now)
                else:
                    j["status"] = ST_LATE
                changed = True
            elif self.worker is None:
                if repeating:
                    inst = self._occurrence(j, ST_WAIT)
                    self.jobs.append(inst)
                    self._advance(j, now)
                    self.start_job(inst)
                else:
                    self.start_job(j)
        if changed:
            self.save_jobs()
        self.after(1000, self._tick)

    # ---------------------------------------------------------------- その他
    def post(self, fn):
        self._queue.put(fn)

    def _pump(self):
        while not self._queue.empty():
            self._queue.get()()
        self.after(200, self._pump)

    def open_folder(self):
        d = Path(self.settings["output_dir"])
        d.mkdir(parents=True, exist_ok=True)
        os.startfile(d)

    def _in_thread(self, fn):
        def run():
            try:
                fn()
            except Exception as e:
                self.post(lambda: messagebox.showerror(APP_NAME, f"失敗しました: {e}"))
        threading.Thread(target=run, daemon=True).start()

    def check_meeting(self):
        def run():
            extra = tuple(c.strip() for c in self.settings["extra_classes"].split(",") if c.strip())
            by_class = zc.is_in_meeting(extra)
            lines = [f"ウィンドウで判定: {'会議中' if by_class else '会議外'}"]
            if zui.available():
                elems = zui.scan()
                lines.append(f"ボタンで判定: {'会議中' if zui.meeting_by_uia(elems) else '会議外'}")
                btn = [e for e in elems if e.control_type == "Button"]
                cam = next((s for e in btn if (s := zui.video_state(e.n)) is not None), None)
                mic = next((s for e in btn if (s := zui.mic_state(e.n)) is not None), None)
                lines.append(f"カメラ: {'不明' if cam is None else onoff(cam)} / マイク: {'不明' if mic is None else onoff(mic)}")
            else:
                lines.append("pywinauto 未インストール（ボタンでの判定・自動クリック不可）")
            text = "\n".join(lines)
            self.post(lambda: messagebox.showinfo("会議中かチェック", text))
        self._in_thread(run)

    def dump_zoom(self):
        if not zui.available():
            messagebox.showerror(APP_NAME, "pywinauto が未インストールです。setup.bat を実行してください。")
            return
        path = DATA_DIR / f"zoom_ui_{dt.datetime.now():%Y%m%d_%H%M%S}.txt"

        def run():
            zui.dump(path)
            self.post(lambda: (self.log(f"Zoom の画面要素を書き出しました: {path}"), os.startfile(path)))
        self._in_thread(run)

    def test_record(self):
        if self.worker:
            messagebox.showwarning(APP_NAME, "録画中です。")
            return
        if not self.save_settings(quiet=True):
            return
        ffmpeg = find_ffmpeg(self.settings["ffmpeg_path"])
        if not ffmpeg:
            messagebox.showerror(APP_NAME, "ffmpeg が見つかりません。")
            return
        s = self.settings
        out = Path(s["output_dir"]) / f"test_{dt.datetime.now():%Y%m%d_%H%M%S}.mp4"
        out.parent.mkdir(parents=True, exist_ok=True)

        def run():
            log = lambda m: self.post(lambda: self.log(m))
            try:
                rec = ScreenRecorder(ffmpeg, out, log, fps=int(s["fps"]), crf=int(s["crf"]), capture=s["capture"],
                                     audio=bool(s["audio"]), audio_offset=float(s["audio_offset"]))
                rec.start()
                log("テスト録画中（10秒）… 何か音を鳴らしてみてください")
                time.sleep(10)
                log(f"テスト録画を保存しました: {rec.stop()}")
            except Exception as e:
                log(f"テスト録画に失敗: {e}")

        threading.Thread(target=run, daemon=True).start()

    def _startup_checks(self):
        if os.name != "nt":
            self.log("※ このアプリは Windows 専用です")
            return
        if not zc.zoom_installed():
            self.log("⚠ Zoom デスクトップアプリが見つかりません。インストールしてください。")
        if not zui.available():
            self.log("⚠ pywinauto が見つかりません。参加ボタンを自動で押すには setup.bat をもう一度実行してください。")
        ff = find_ffmpeg(self.settings["ffmpeg_path"])
        self.log(f"ffmpeg: {ff}" if ff else "⚠ ffmpeg が見つかりません。setup.bat を実行するか、設定で指定してください。")
        n = sum(j["status"] == ST_WAIT for j in self.jobs)
        if n:
            self.log(f"予約 {n} 件を待機中。アプリは開いたままにしてください。")

    def on_close(self):
        waiting = any(j["status"] == ST_WAIT for j in self.jobs)
        if self.worker and not messagebox.askyesno(APP_NAME, "実行中です。停止して終了しますか？"):
            return
        if waiting and not self.worker and not messagebox.askyesno(
                APP_NAME, "アプリを閉じると予約は実行されません。終了しますか？"):
            return
        if self.worker:
            self.worker.stop_event.set()
            self.worker.join(timeout=90)
        keep_awake(False)
        self.destroy()


def main():
    if os.name == "nt":
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(2)  # 高DPI環境で正しい画面サイズ・座標を取得
        except Exception:
            try:
                ctypes.windll.user32.SetProcessDPIAware()
            except Exception:
                pass
    App().mainloop()


if __name__ == "__main__":
    main()
