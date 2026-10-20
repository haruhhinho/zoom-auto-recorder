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
                name = d.name or ""
                if name or ct == "Edit":
                    out.append(Elem(w.hwnd, w.title, w.class_name, ct, name, norm(name), d))
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
    ok_wins = {e.hwnd for e in elems if e.control_type == "Button" and e.n in CLOSE_EXACT}
    for e in elems:
        # 終了ダイアログには「OK」ボタンがある。チャット欄の書き込みで誤検知しないよう、それも条件にする
        if e.hwnd not in exclude_hwnds and e.hwnd in ok_wins and any(k in e.n for k in END_TEXTS):
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
            if es[0].win_class in HOME_CLASSES or any(
                    e.control_type == "Button" and any(h in e.n for h in HOME_HINTS) for e in es):
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


# ---------------------------------------------------------------- チャット（URL・ファイル）
URL_RE = re.compile(r"https?://[^\s<>\"'（）「」『』【】、。]+", re.I)
URL_TRAIL = ".,;:!?)]}>…"
# Zoom 自身のリンク（参加リンクを開くと今の会議から抜けてしまう・画面上のヘルプ等）は開かない
URL_SKIP_RE = re.compile(r"^https?://([\w-]+\.)*(zoom\.us|zoom\.com|zoomgov\.com)(?=[/:?#]|$)|zoommtg:|zoomus:", re.I)
DOWNLOAD_WORDS = ("ダウンロード", "download")
DOWNLOAD_SKIP = ("ダウンロード済", "ダウンロード完了", "downloaded", "update", "アップデート", "最新", "zoomを", "zoomworkplace")
CHAT_INPUT_SKIP = ("検索", "search", "名前", "name", "パスコード", "passcode", "ミーティングid", "meetingid")
SAVE_DIALOG_CLASS = "#32770"


def extract_urls(elems: list[Elem]) -> list[str]:
    """チャット欄などに表示されている URL を、出てきた順に重複なしで返す。"""
    seen, out = set(), []
    for e in elems:
        if e.control_type == "Edit":
            continue  # 入力中の文字は対象外
        for m in URL_RE.finditer(e.name):
            url = m.group(0).rstrip(URL_TRAIL)
            if len(url) < 12 or URL_SKIP_RE.search(url) or url in seen:
                continue
            seen.add(url)
            out.append(url)
    return out


def _elem_key(e: Elem) -> tuple:
    try:
        rid = tuple(e.info.runtime_id)
        if rid:
            return ("rid", rid)
    except Exception:
        pass
    try:
        r = e.info.rectangle
        return ("pos", e.hwnd, e.n, r.left, r.top)
    except Exception:
        return ("name", e.hwnd, e.n)


def download_buttons(elems: list[Elem]) -> list[Elem]:
    return [e for e in elems
            if e.control_type in ("Button", "Hyperlink", "MenuItem")
            and any(w in e.n for w in DOWNLOAD_WORDS)
            and not any(x in e.n for x in DOWNLOAD_SKIP)]


def chat_is_open(elems: list[Elem], meeting_hwnds_: set[int]) -> bool:
    """チャットの入力欄があればチャット欄は開いている（ポップアウトしたチャット窓も含む）。"""
    for e in elems:
        if e.control_type != "Edit":
            continue
        if any(x in e.n for x in CHAT_INPUT_SKIP):
            continue
        if e.hwnd in meeting_hwnds_ or "チャット" in e.win_title or "chat" in e.win_title.lower():
            return True
    return False


class ChatWatcher:
    """会議中のチャットを見張り、新しい URL とファイルを拾う。"""

    def __init__(self, log: Log, open_urls: bool, download: bool, auto_open_panel: bool):
        self.log = log
        self.open_urls, self.download, self.auto_open_panel = open_urls, download, auto_open_panel
        self.opened: set[str] = set()
        self.clicked: set[tuple] = set()
        self.panel_tries, self.panel_last, self.panel_warned = 0, 0.0, False
        self.saved_dialogs: set[int] = set()

    def ensure_panel(self, elems: list[Elem], main_hwnd: int, meeting_hwnds_: set[int]) -> None:
        """チャット欄が閉じていれば Alt+H で開く（チャットを読むには表示されている必要がある）。"""
        if not self.auto_open_panel or not main_hwnd or chat_is_open(elems, meeting_hwnds_):
            return
        if self.panel_tries >= 3:
            if not self.panel_warned:
                self.panel_warned = True
                self.log("⚠ チャット欄を開けませんでした。URLやファイルを拾うには、チャット欄を手動で開いてください")
            return
        if time.time() - self.panel_last < 15:
            return
        self.panel_tries += 1
        self.panel_last = time.time()
        if zc.focus_window(main_hwnd):
            zc.alt_key(0x48)  # Alt+H: チャット欄の表示
            if self.panel_tries == 1:
                self.log("チャット欄を開きました（URL・ファイルの読み取りのため）")

    def new_urls(self, elems: list[Elem]) -> list[str]:
        if not self.open_urls:
            return []
        urls = [u for u in extract_urls(elems) if u not in self.opened]
        self.opened.update(urls)
        return urls

    def click_downloads(self, elems: list[Elem]) -> int:
        if not self.download:
            return 0
        n = 0
        for e in download_buttons(elems):
            key = _elem_key(e)
            if key in self.clicked:
                continue
            self.clicked.add(key)
            if _press(e, use_click=False) or _press(e, use_click=True):
                n += 1
                self.log(f"チャットのファイルをダウンロード: 「{e.name[:60]}」")
        return n

    def handle_save_dialog(self, elems: list[Elem]) -> None:
        """ダウンロード時に「名前を付けて保存」が出たら、そのまま保存する。"""
        if not self.download:
            return
        for e in elems:
            if e.win_class == SAVE_DIALOG_CLASS and e.control_type == "Button" \
                    and (e.n.startswith("保存") or e.n in ("save", "save(s)", "&save")):
                if e.hwnd not in self.saved_dialogs:
                    self.saved_dialogs.add(e.hwnd)
                    _press(e, use_click=False)
                    self.log("保存ダイアログで「保存」を押しました")
                return


# ---------------------------------------------------------------- 参加者の映像の小窓
# 画面共有中などに、会議画面の上に重なって出る「参加者の映像」の小窓を隠す
HIDE_PANEL_NAMES = ("ビデオパネルを最小化", "ビデオパネルを非表示", "ビデオを最小化", "ビデオを非表示",
                    "minimizevideopanel", "hidevideopanel", "minimizevideo", "hidevideo")
HIDE_PANEL_GENERIC = ("最小化", "minimize")  # 小窓の中だけで使う（会議画面本体の「最小化」は押さない）
OVERLAY_SKIP_CLASSES = ("#32770",)
OVERLAY_KEEP_WORDS = ("チャット", "chat", "参加者", "participants")  # 自分で開いたパネル等は隠さない


class OverlayHider:
    def __init__(self, log: Log):
        self.log = log
        self.tries: dict[int, int] = {}
        self.last: dict[int, float] = {}
        self.reported: set[str] = set()

    def step(self, elems: list[Elem], main_hwnd: int, keep_hwnds: set[int]) -> int:
        """会議画面に重なっている Zoom の小窓を探して隠す。隠した数を返す。"""
        if not main_hwnd or not zc.window_alive(main_hwnd):
            return 0
        main_rect = zc.window_rect(main_hwnd)
        main_area = max(1, (main_rect[2] - main_rect[0]) * (main_rect[3] - main_rect[1]))
        by_win: dict[int, list[Elem]] = {}
        for e in elems:
            by_win.setdefault(e.hwnd, []).append(e)
        done = 0
        for w in zc.zoom_windows():
            h = w.hwnd
            if not w.visible or h == main_hwnd or h in keep_hwnds or w.class_name in OVERLAY_SKIP_CLASSES:
                continue
            if any(k in (w.title or "").lower() for k in OVERLAY_KEEP_WORDS):
                continue
            rect = zc.window_rect(h)
            area = (rect[2] - rect[0]) * (rect[3] - rect[1])
            if area <= 0 or area > main_area * 0.6 or zc.overlap_area(rect, main_rect) <= 0:
                continue
            es = by_win.get(h, [])
            # ダイアログ（OK・確認ボタンがある）やチャットの入力欄がある窓は対象外
            if any(e.control_type == "Edit" for e in es) or any(
                    e.control_type == "Button" and (e.n in CLOSE_EXACT or e.n in CONSENT_EXACT
                                                   or any(k in e.n for k in COMPUTER_AUDIO)) for e in es):
                continue
            if time.time() - self.last.get(h, 0) < 10 or self.tries.get(h, 0) >= 5:
                continue
            self.last[h], self.tries[h] = time.time(), self.tries.get(h, 0) + 1
            btn = next((e for e in es if e.control_type == "Button"
                        and any(k in e.n for k in HIDE_PANEL_NAMES)), None) \
                or next((e for e in es if e.control_type == "Button"
                         and any(e.n.startswith(k) for k in HIDE_PANEL_GENERIC)), None)
            ok = bool(btn) and _press(btn, use_click=False)
            if not ok:
                ok = zc.minimize_window(h)
            key = f"{w.class_name}|{w.title}"
            if key not in self.reported:
                self.reported.add(key)
                how = f"（「{btn.name}」を押しました）" if btn else ""
                self.log(f"会議画面に重なる参加者の映像の小窓を最小化しました{how}（{w.class_name}「{w.title}」）"
                         + ("" if ok else " ※うまく隠れませんでした"))
            done += ok
        return done
