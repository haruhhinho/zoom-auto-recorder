"""Zoom 自動参加＆Windows 画面録画（GUI）。"""
from __future__ import annotations

import ctypes
import datetime as dt
import json
import os
import queue
import threading
import time
import tkinter as tk
import traceback
import uuid
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

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
DT_FMT = "%Y-%m-%d %H:%M"


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
    raise ValueError("開始日時は「2026-10-12 10:00」または「10:00」の形式で入力してください。")


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
            tracked = self._wait_join(assist, extra, s)
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
        self.geometry("880x660")
        self.minsize(760, 560)
        self.settings = {**DEFAULT_SETTINGS, **load_json(SETTINGS_FILE, {})}
        self.jobs: list[dict] = load_json(JOBS_FILE, [])
        for j in self.jobs:  # 前回、録画中のまま終了していたもの
            if j["status"] == ST_RUN:
                j["status"] = ST_FAIL
        self.worker: Worker | None = None
        self._queue: queue.Queue = queue.Queue()

        nb = ttk.Notebook(self)
        nb.pack(fill="both", expand=True, padx=8, pady=8)
        self._build_jobs_tab(nb)
        self._build_settings_tab(nb)
        self._build_log_tab(nb)
        self.status_var = tk.StringVar(value="待機中")
        ttk.Label(self, textvariable=self.status_var, anchor="w").pack(fill="x", padx=10, pady=(0, 6))

        self.refresh_jobs()
        self.protocol("WM_DELETE_WINDOW", self.on_close)
        self.after(200, self._pump)
        self.after(1000, self._tick)
        self._startup_checks()

    # ---------------------------------------------------------------- 予約タブ
    def _build_jobs_tab(self, nb):
        f = ttk.Frame(nb, padding=10)
        nb.add(f, text="予約")
        form = ttk.LabelFrame(f, text="会議を追加", padding=10)
        form.pack(fill="x")
        self.v_meeting, self.v_password = tk.StringVar(), tk.StringVar()
        self.v_start = tk.StringVar(value=(dt.datetime.now() + dt.timedelta(minutes=5)).strftime(DT_FMT))
        self.v_name = tk.StringVar(value=self.settings["display_name"])
        self.v_max = tk.StringVar(value=str(self.settings["default_max_min"]))
        self.v_camera = tk.BooleanVar(value=bool(self.settings["default_camera"]))
        self.v_mic = tk.BooleanVar(value=bool(self.settings["default_mic"]))
        rows = [
            ("招待URL または ミーティングID", self.v_meeting, 60),
            ("パスコード（URLに含まれていれば不要）", self.v_password, 30),
            ("開始日時（例 2026-10-12 10:00 / 10:00）", self.v_start, 22),
            ("表示名（空欄なら設定の値）", self.v_name, 30),
            ("最大録画時間（分）", self.v_max, 8),
        ]
        r = 0
        for r, (label, var, w) in enumerate(rows):
            ttk.Label(form, text=label).grid(row=r, column=0, sticky="w", pady=2)
            ttk.Entry(form, textvariable=var, width=w).grid(row=r, column=1, sticky="w", pady=2, padx=6)
        for label, var in (("カメラ", self.v_camera), ("マイク", self.v_mic)):
            r += 1
            ttk.Label(form, text=label).grid(row=r, column=0, sticky="w", pady=2)
            fr = ttk.Frame(form)
            ttk.Radiobutton(fr, text="オフ", value=False, variable=var).pack(side="left")
            ttk.Radiobutton(fr, text="オン", value=True, variable=var).pack(side="left", padx=10)
            fr.grid(row=r, column=1, sticky="w", padx=6, pady=2)
        btns = ttk.Frame(form)
        btns.grid(row=r + 1, column=0, columnspan=2, sticky="w", pady=(8, 0))
        ttk.Button(btns, text="予約に追加", command=self.add_job).pack(side="left")
        ttk.Button(btns, text="今すぐ参加して録画", command=self.run_now).pack(side="left", padx=6)

        lst = ttk.Frame(f)
        lst.pack(fill="both", expand=True, pady=(10, 0))
        cols = ("start", "meeting", "av", "status", "output")
        self.tree = ttk.Treeview(lst, columns=cols, show="headings", height=8)
        for c, label, w in zip(cols, ("開始日時", "会議", "カメラ/マイク", "状態", "保存先"), (130, 130, 90, 70, 360)):
            self.tree.heading(c, text=label)
            self.tree.column(c, width=w, anchor="w")
        self.tree.pack(side="left", fill="both", expand=True)
        sb = ttk.Scrollbar(lst, command=self.tree.yview)
        sb.pack(side="right", fill="y")
        self.tree.configure(yscrollcommand=sb.set)
        self.tree.bind("<Double-1>", lambda e: self.open_output())

        b2 = ttk.Frame(f)
        b2.pack(fill="x", pady=(6, 0))
        ttk.Button(b2, text="録画を停止", command=self.stop_current).pack(side="left")
        ttk.Button(b2, text="選択した予約を削除", command=self.delete_job).pack(side="left", padx=6)
        ttk.Button(b2, text="保存フォルダを開く", command=self.open_folder).pack(side="left")

    # ---------------------------------------------------------------- 設定タブ
    def _build_settings_tab(self, nb):
        f = ttk.Frame(nb, padding=10)
        nb.add(f, text="設定")
        s = self.settings
        self.sv = {k: tk.StringVar(value=str(s[k])) for k in
                   ("output_dir", "ffmpeg_path", "display_name", "fps", "crf", "audio_offset",
                    "join_timeout_min", "end_grace_sec", "default_max_min", "extra_classes")}
        self.sv_capture = tk.StringVar(value=s["capture"])
        self.sv_audio = tk.BooleanVar(value=bool(s["audio"]))
        self.sv_camera = tk.BooleanVar(value=bool(s["default_camera"]))
        self.sv_mic = tk.BooleanVar(value=bool(s["default_mic"]))
        self.sv_fullscreen = tk.BooleanVar(value=bool(s["fullscreen"]))
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
        row("既定の表示名", ttk.Entry(f, textvariable=self.sv["display_name"], width=30))
        av = ttk.Frame(f)
        ttk.Checkbutton(av, text="カメラをオン", variable=self.sv_camera).pack(side="left")
        ttk.Checkbutton(av, text="マイクをオン", variable=self.sv_mic).pack(side="left", padx=10)
        row("予約時の既定", av)
        cap = ttk.Frame(f)
        ttk.Radiobutton(cap, text="メインモニター", value="primary", variable=self.sv_capture).pack(side="left")
        ttk.Radiobutton(cap, text="全モニター", value="all", variable=self.sv_capture).pack(side="left", padx=8)
        row("録画範囲", cap)
        row("参加後に会議画面を全画面にする", ttk.Checkbutton(f, variable=self.sv_fullscreen))
        row("フレームレート（fps）", ttk.Entry(f, textvariable=self.sv["fps"], width=8))
        row("画質 CRF（小さいほど高画質 18〜32）", ttk.Entry(f, textvariable=self.sv["crf"], width=8))
        row("PCの音声を録音", ttk.Checkbutton(f, variable=self.sv_audio))
        row("音声の補正（秒、＋で音を遅らせる）", ttk.Entry(f, textvariable=self.sv["audio_offset"], width=8))
        row("会議に入れるまで待つ時間（分）", ttk.Entry(f, textvariable=self.sv["join_timeout_min"], width=8))
        row("会議終了判定までの猶予（秒）", ttk.Entry(f, textvariable=self.sv["end_grace_sec"], width=8))
        row("既定の最大録画時間（分）", ttk.Entry(f, textvariable=self.sv["default_max_min"], width=8))
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
            for k in ("fps", "crf", "join_timeout_min", "end_grace_sec", "default_max_min"):
                new[k] = int(new[k])
            new["audio_offset"] = float(new["audio_offset"])
        except ValueError:
            messagebox.showerror(APP_NAME, "数値の項目に数字以外が入っています。")
            return False
        new["capture"] = self.sv_capture.get()
        new["audio"] = self.sv_audio.get()
        new["default_camera"] = self.sv_camera.get()
        new["default_mic"] = self.sv_mic.get()
        new["fullscreen"] = self.sv_fullscreen.get()
        self.settings.update(new)
        save_json(SETTINGS_FILE, self.settings)
        if not quiet:
            self.log("設定を保存しました")
        return True

    # ---------------------------------------------------------------- ログタブ
    def _build_log_tab(self, nb):
        f = ttk.Frame(nb, padding=10)
        nb.add(f, text="ログ")
        self.log_text = tk.Text(f, height=20, state="disabled", wrap="word")
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
    def _job_from_form(self, start: dt.datetime) -> dict | None:
        try:
            m = zc.parse_meeting(self.v_meeting.get(), self.v_password.get())
            max_min = int(self.v_max.get())
        except ValueError as e:
            messagebox.showerror(APP_NAME, str(e) or "最大録画時間は数字で入力してください。")
            return None
        return {"id": uuid.uuid4().hex, "meeting": self.v_meeting.get().strip(),
                "password": self.v_password.get().strip(), "label": m.label(),
                "start": start.strftime(DT_FMT), "name": self.v_name.get().strip(),
                "camera": self.v_camera.get(), "mic": self.v_mic.get(),
                "max_min": max_min, "status": ST_WAIT, "output": ""}

    def add_job(self):
        try:
            start = parse_start(self.v_start.get())
        except ValueError as e:
            messagebox.showerror(APP_NAME, str(e))
            return
        if start < dt.datetime.now() - dt.timedelta(minutes=1):
            messagebox.showerror(APP_NAME, "過去の日時は指定できません。")
            return
        job = self._job_from_form(start)
        if job:
            self.jobs.append(job)
            self.save_jobs()
            self.log(f"予約しました: {job['start']}  {job['label']}（カメラ{onoff(job['camera'])} / マイク{onoff(job['mic'])}）")
            self.v_meeting.set("")
            self.v_password.set("")

    def run_now(self):
        if self.worker:
            messagebox.showwarning(APP_NAME, "録画中です。先に停止してください。")
            return
        job = self._job_from_form(dt.datetime.now())
        if job:
            self.jobs.append(job)
            self.save_jobs()
            self.start_job(job)

    def delete_job(self):
        sel = set(self.tree.selection())
        running = self.worker.job["id"] if self.worker else None
        if running in sel:
            messagebox.showwarning(APP_NAME, "実行中の予約は削除できません。先に停止してください。")
            return
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
        self.tree.delete(*self.tree.get_children())
        for j in sorted(self.jobs, key=lambda j: j["start"], reverse=True):
            av = f"{onoff(j.get('camera'))} / {onoff(j.get('mic'))}"
            self.tree.insert("", "end", iid=j["id"], values=(j["start"], j["label"], av, j["status"], j["output"]))
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

    def _tick(self):
        now = dt.datetime.now()
        changed = False
        for j in sorted(self.jobs, key=lambda j: j["start"]):
            if j["status"] != ST_WAIT:
                continue
            start = dt.datetime.strptime(j["start"], DT_FMT)
            if now < start:
                continue
            if now - start > LATE_LIMIT:
                j["status"] = ST_LATE
                changed = True
                self.log(f"予約時刻を過ぎていたため実行しませんでした: {j['start']} {j['label']}")
            elif self.worker is None:
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

    def _selected_output(self):
        sel = self.tree.selection()
        for j in self.jobs:
            if sel and j["id"] == sel[0]:
                return j["output"]
        return ""

    def open_output(self):
        p = self._selected_output()
        if p and os.path.exists(p):
            os.startfile(p)

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
