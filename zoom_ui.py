"""Zoom の画面を UI Automation で操作する。

- 参加前: 「参加」「ビデオなしで参加」「コンピューターオーディオで参加」などを自動で押す
- 参加後: カメラ・マイクを予約どおりのオン/オフに合わせる
- 会議に入ったかどうかを、ツールバーのボタン（参加者・ミュート・ビデオ）から判定する
"""
from __future__ import annotations

import ctypes
import os
import re
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import zoom_control as zc

Log = Callable[[str], None]
_local = threading.local()

CONTROL_TYPES = ("Button", "CheckBox", "MenuItem", "Hyperlink", "ListItem", "TabItem", "Text")
HOME_CLASSES = ("ZPPTMainFrmWndClassEx",)
HOME_HINTS = ("newmeeting", "新規ミーティング", "スケジュール", "schedule")

# ボタン名（空白を除き小文字にした形で比較）
JOIN_WITH_VIDEO = ("joinwithvideo", "ビデオ付きで参加", "ビデオありで参加", "ビデオをオンにして参加")
JOIN_WITHOUT_VIDEO = ("joinwithoutvideo", "ビデオなしで参加", "ビデオをオフにして参加")
JOIN_EXACT = {"join", "joinmeeting", "joinnow", "参加", "参加する", "ミーティングに参加", "ミーティングに参加する", "今すぐ参加"}
COMPUTER_AUDIO = ("joinwithcomputeraudio", "joinaudiobycomputer", "コンピューターオーディオで参加",
                  "コンピュータオーディオで参加", "コンピューターのオーディオで参加", "コンピューターでオーディオに参加",
                  "コンピューターオーディオに参加", "コンピューターオーディオに接続")
JOIN_AUDIO_PREFIX = ("joinaudio", "オーディオに参加", "オーディオに接続")
CONSENT_EXACT = {"gotit", "了解", "了解しました", "continue", "続行"}
CHK_VIDEO_OFF = ("turnoffmyvideo", "ビデオをオフにする", "自分のビデオをオフ")
CHK_NO_AUDIO = ("dontconnecttoaudio", "オーディオに接続しない")
# 会議が終わったときに出るメッセージ
END_TEXTS = ("endedbyhost", "meetinghasbeenended", "thismeetinghasended", "removedfromthemeeting",
             "ホストによって終了", "ホストがミーティングを終了", "ミーティングは終了しました", "ミーティングが終了しました",
             "ミーティングから削除されました")
CLOSE_EXACT = {"ok", "閉じる", "close"}
FULLSCREEN_NAMES = ("enterfullscreen", "全画面表示の開始", "全画面表示に切り替え", "全画面表示にする", "全画面表示", "fullscreen")
STATE_EXCLUDE = ("all", "すべて", "全員", "notification", "通知", "speaker", "スピーカー", "share", "共有", "setting", "設定")


def norm(s: str) -> str:
    return re.sub(r"[\s　'’`]", "", (s or "")).lower()


def _segments(n: str) -> list[str]:
    """Zoom のボタン名は「動作、現在の状態, ショートカット, 説明」のように区切られているので分割する。"""
    return [p for p in re.split(r"[,、，]", n) if p]


# ボタン名に含まれる「現在の状態」の表現（動作の表現より優先して見る）
MIC_NOW_ON = ("現在ミュート解除中", "現在ミュートを解除", "currentlyunmuted")
MIC_NOW_OFF = ("現在ミュート中", "現在ミュートしています", "currentlymuted")
VIDEO_NOW_ON = ("現在ビデオ送信中", "現在ビデオを送信", "現在ビデオを開始しています", "videoiscurrentlyon")
VIDEO_NOW_OFF = ("現在ビデオ停止中", "現在ビデオを停止しています", "videoiscurrentlyoff")


def _toggle_word(segs: list[str], subjects: tuple) -> bool | None:
    """「オーディオ, オフにする, …」形式（プレビュー画面）: オフにする=今オン、オンにする=今オフ。"""
    if len(segs) >= 2 and segs[0] in subjects:
        if segs[1] in ("オフにする", "turnoff", "off"):
            return True
        if segs[1] in ("オンにする", "turnon", "on"):
            return False
    return None


def video_state(n: str) -> bool | None:
    """ボタン名からカメラの現在の状態を推定（True=オン）。"""
    segs = _segments(n)
    if not segs or any(x in segs[0] for x in STATE_EXCLUDE):
        return None
    if any(k in n for k in VIDEO_NOW_OFF):
        return False
    if any(k in n for k in VIDEO_NOW_ON):
        return True
    st = _toggle_word(segs, ("ビデオ", "カメラ", "video", "camera"))
    if st is not None:
        return st
    a = segs[0]  # 押したときの動作
    if any(k in a for k in ("stopvideo", "stopmyvideo", "ビデオの停止", "ビデオを停止", "turnoffvideo", "ビデオをオフ")):
        return True
    if any(k in a for k in ("startvideo", "startmyvideo", "ビデオの開始", "ビデオを開始", "turnonvideo", "ビデオをオン")):
        return False
    return None


def mic_state(n: str) -> bool | None:
    """ボタン名からマイクの現在の状態を推定（True=オン＝ミュートされていない）。"""
    segs = _segments(n)
    if not segs or any(x in segs[0] for x in STATE_EXCLUDE):
        return None
    if any(k in n for k in MIC_NOW_ON):
        return True
    if any(k in n for k in MIC_NOW_OFF):
        return False
    st = _toggle_word(segs, ("オーディオ", "マイク", "audio", "microphone", "mic"))
    if st is not None:
        return st
    a = segs[0]  # 押したときの動作
    if any(k in a for k in ("unmute", "ミュート解除", "ミュートを解除", "turnonmic", "マイクをオン")):
        return False
    if a.startswith(("mute", "ミュート")) or any(
            k in a for k in ("muteaudio", "mutemyaudio", "mutemic", "turnoffmic", "マイクをオフ", "オーディオのミュート", "をミュート")):
        return True
    return None


def is_participants(n: str) -> bool:
    return n.startswith(("participants", "参加者", "manageparticipants")) or "参加者の管理" in n


# ---------------------------------------------------------------- 基本操作
def available() -> bool:
    try:
        import pywinauto  # noqa: F401
        return True
    except Exception:
        return False


def init_thread() -> None:
    """UI Automation（COM）を使うスレッドで最初に呼ぶ。"""
    if getattr(_local, "com", False):
        return
    try:
        import comtypes
        comtypes.CoInitializeEx(comtypes.COINIT_APARTMENTTHREADED)
    except Exception:
        pass
    _local.com = True


@dataclass
class Elem:
    hwnd: int
    win_title: str
    win_class: str
    control_type: str
    name: str
    n: str
    info: object


def scan() -> list[Elem]:
    """表示中の Zoom ウィンドウにあるボタン類を列挙する。"""
    init_thread()
    from pywinauto.uia_element_info import UIAElementInfo

    out: list[Elem] = []
    for w in zc.zoom_windows():
        if not w.visible or not w.hwnd:
            continue
        try:
            for d in UIAElementInfo(w.hwnd).descendants():
                ct = d.control_type
                if ct in CONTROL_TYPES and d.name:
                    out.append(Elem(w.hwnd, w.title, w.class_name, ct, d.name, norm(d.name), d))
        except Exception:
            continue
    return out


def _press(e: Elem, use_click: bool) -> bool:
    from pywinauto.controls.uiawrapper import UIAWrapper
    from pywinauto.uia_element_info import UIAElementInfo

    w = UIAWrapper(e.info)
    if not use_click:
        for action in ("invoke", "toggle", "select"):
            try:
                getattr(w, action)()
                return True
            except Exception:
                continue
    try:
        try:
            UIAWrapper(UIAElementInfo(e.hwnd)).set_focus()
        except Exception:
            pass
        w.click_input()
        return True
    except Exception:
        return False


def toggle_state(e: Elem) -> bool | None:
    from pywinauto.controls.uiawrapper import UIAWrapper

    try:
        return UIAWrapper(e.info).get_toggle_state() == 1
    except Exception:
        return None


def meeting_hwnds(elems: list[Elem]) -> set[int]:
    """ツールバーに「参加者」と「ミュート/ビデオ」ボタンがあるウィンドウ（=会議画面）。"""
    by_win: dict[int, list[Elem]] = {}
    for e in elems:
        by_win.setdefault(e.hwnd, []).append(e)
    found = set()
    for h, es in by_win.items():
        btn = [e.n for e in es if e.control_type == "Button"]
        if any(is_participants(n) for n in btn) and any(
                mic_state(n) is not None or video_state(n) is not None for n in btn):
            found.add(h)
    return found


def meeting_by_uia(elems: list[Elem]) -> bool:
    return bool(meeting_hwnds(elems))


def end_message(elems: list[Elem], exclude_hwnds=()) -> Elem | None:
    """「ホストによって終了されました」などのダイアログを探す（会議画面内のチャット等は対象外）。"""
    for e in elems:
        if e.hwnd not in exclude_hwnds and any(k in e.n for k in END_TEXTS):
            return e
    return None


def close_dialog(elems: list[Elem], hwnd: int) -> None:
    for e in elems:
        if e.hwnd == hwnd and e.control_type == "Button" and e.n in CLOSE_EXACT:
            _press(e, use_click=False)
            return


def click_fullscreen() -> bool:
    """画面上の「全画面表示」ボタン/メニューを押す。"""
    for e in scan():
        if e.control_type in ("Button", "MenuItem") and any(k in e.n for k in FULLSCREEN_NAMES) \
                and not any(x in e.n for x in ("終了", "exit", "解除")):
            return _press(e, use_click=False)
    return False


_last_wake = 0.0


def wake_toolbar(extra_classes: tuple = ()) -> None:
    """自動で隠れた会議ツールバーを表示させるため、会議画面の上でマウスを動かす。"""
    global _last_wake
    if os.name != "nt" or time.time() - _last_wake < 10:
        return
    _last_wake = time.time()
    wins = zc.meeting_windows(extra_classes)
    if not wins:
        return
    from ctypes import wintypes

    user32 = ctypes.windll.user32
    r = wintypes.RECT()
    if not user32.GetWindowRect(wins[0].hwnd, ctypes.byref(r)):
        return
    cx, y = (r.left + r.right) // 2, r.bottom - 60
    for dx in (0, 8, 0):
        user32.SetCursorPos(cx + dx, y)
        time.sleep(0.15)


def dump(path: Path) -> Path:
    """Zoom のウィンドウとボタン名をテキストに書き出す（うまく動かないときの調査用）。"""
    init_thread()
    from pywinauto.uia_element_info import UIAElementInfo

    lines = []
    for w in zc.zoom_windows():
        lines.append(f"== {'表示' if w.visible else '非表示'} | class={w.class_name} | title={w.title}")
        if not w.visible or not w.hwnd:
            continue
        try:
            for d in UIAElementInfo(w.hwnd).descendants():
                if d.name:
                    lines.append(f"    {d.control_type:<10} | {d.name}")
        except Exception as ex:
            lines.append(f"    （取得失敗: {ex}）")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) or "Zoom のウィンドウが見つかりません。", encoding="utf-8")
    return path


# ---------------------------------------------------------------- 自動操作
class Assistant:
    COOLDOWN = 3.5   # 同じボタンを押し直すまでの秒数（画面の反映待ち）
    MAX_TRIES = 6

    def __init__(self, camera_on: bool, mic_on: bool, log: Log, extra_classes: tuple = ()):
        self.camera_on, self.mic_on, self.log = camera_on, mic_on, log
        self.extra = extra_classes
        self.tries: dict[tuple, int] = {}
        self.last: dict[tuple, float] = {}
        self.warned: set[str] = set()
        self.cat_last: dict[str, float] = {}

    def _toggle(self, e: Elem, cat: str, why: str) -> bool:
        """カメラ/マイクの切り替え。押した後は反映を待つため、同じ種類は4秒間押さない（二重押しで元に戻るのを防ぐ）。"""
        if time.time() - self.cat_last.get(cat, 0) < 4.0:
            return True
        self.cat_last[cat] = time.time()
        return self._do(e, why)

    def _do(self, e: Elem, why: str) -> bool:
        key = (e.hwnd, e.n)
        now = time.time()
        if now - self.last.get(key, 0) < self.COOLDOWN:
            return True  # 押した直後。反映待ち
        n = self.tries.get(key, 0)
        if n >= self.MAX_TRIES:
            if e.n not in self.warned:
                self.warned.add(e.n)
                self.log(f"⚠ 「{e.name}」を押しても反映されませんでした")
            return False
        self.tries[key], self.last[key] = n + 1, now
        if n == 0:
            self.log(f"Zoom操作: 「{e.name}」を押します（{why}）")
        return _press(e, use_click=(n % 2 == 1))  # invoke とクリックを交互に試す

    @staticmethod
    def _windows(elems: list[Elem]) -> dict[int, list[Elem]]:
        by_win: dict[int, list[Elem]] = {}
        for e in elems:
            by_win.setdefault(e.hwnd, []).append(e)
        return by_win

    def _common(self, es: list[Elem]) -> int:
        acted = 0
        for e in es:
            if e.control_type == "Button" and (any(k in e.n for k in COMPUTER_AUDIO)
                                               or e.n.startswith(JOIN_AUDIO_PREFIX)):
                acted += self._do(e, "コンピューターオーディオに接続")
            elif e.control_type == "Button" and e.n in CONSENT_EXACT:
                acted += self._do(e, "確認ダイアログ")
        return acted

    def prejoin_step(self, elems: list[Elem]) -> int:
        """参加前の画面を進める。押したボタンの数を返す。"""
        acted = 0
        for es in self._windows(elems).values():
            if es[0].win_class in HOME_CLASSES or any(any(h in e.n for h in HOME_HINTS) for e in es):
                continue  # Zoom のホーム画面は触らない
            acted += self._common(es)
            toggled = 0
            for e in es:  # 古い参加ダイアログのチェックボックス
                if e.control_type != "CheckBox":
                    continue
                st = toggle_state(e)
                if any(k in e.n for k in CHK_VIDEO_OFF) and st is not None and st != (not self.camera_on):
                    toggled += self._do(e, "カメラ設定")
                elif any(k in e.n for k in CHK_NO_AUDIO) and st:
                    toggled += self._do(e, "オーディオに接続する")
            # プレビュー画面のカメラ/マイク切り替えボタン
            btn = [e for e in es if e.control_type == "Button"]
            cam = next(((e, s) for e in btn if (s := video_state(e.n)) is not None), None)
            mic = next(((e, s) for e in btn if (s := mic_state(e.n)) is not None), None)
            if cam and cam[1] != self.camera_on:
                toggled += self._toggle(cam[0], "cam", "カメラを" + ("オン" if self.camera_on else "オフ"))
            if mic and mic[1] != self.mic_on:
                toggled += self._toggle(mic[0], "mic", "マイクを" + ("オン" if self.mic_on else "オフ"))
            acted += toggled
            if toggled:
                continue  # 切り替えが反映されてから参加ボタンを押す
            first, second = (JOIN_WITH_VIDEO, JOIN_WITHOUT_VIDEO) if self.camera_on else (JOIN_WITHOUT_VIDEO, JOIN_WITH_VIDEO)
            target = (next((e for e in btn if any(k in e.n for k in first)), None)
                      or next((e for e in btn if any(k in e.n for k in second)), None)
                      or next((e for e in btn if e.n in JOIN_EXACT), None))
            if target:
                acted += self._do(target, "参加")
        return acted

    def enforce_step(self, elems: list[Elem]) -> bool:
        """会議中のカメラ/マイクを予約どおりにする。両方そろっていれば True。"""
        meeting = [es for es in self._windows(elems).values()
                   if any(is_participants(e.n) for e in es if e.control_type == "Button")]
        es = meeting[0] if meeting else elems
        self._common(es)
        btn = [e for e in es if e.control_type == "Button"]
        cam = next(((e, s) for e in btn if (s := video_state(e.n)) is not None), None)
        mic = next(((e, s) for e in btn if (s := mic_state(e.n)) is not None), None)
        ok = True
        if cam is None:
            ok = False
        elif cam[1] != self.camera_on:
            self._toggle(cam[0], "cam", "カメラを" + ("オン" if self.camera_on else "オフ"))
            ok = False
        if mic is None:
            ok = False
        elif mic[1] != self.mic_on:
            self._toggle(mic[0], "mic", "マイクを" + ("オン" if self.mic_on else "オフ"))
            ok = False
        if cam is None or mic is None:
            wake_toolbar(self.extra)
        return ok

    def misc_step(self, elems: list[Elem]) -> None:
        """会議中に出てくるオーディオ接続・録画同意などのダイアログを閉じる。"""
        for es in self._windows(elems).values():
            self._common(es)
