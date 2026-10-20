"""Zoom への自動参加と、会議ウィンドウの検出。"""
from __future__ import annotations

import ctypes
import os
import re
import time
import urllib.parse
from dataclasses import dataclass

IS_WINDOWS = os.name == "nt"

# Zoom の会議中ウィンドウのクラス名（デスクトップ版）
DEFAULT_MEETING_CLASSES = ("ZPContentViewWndClass", "ConfMultiTabContentWndClass")
# 会議ウィンドウのタイトル（英語版/日本語版）
DEFAULT_MEETING_TITLES = ("Zoom Meeting", "Zoom ミーティング", "Zoom 会議", "Zoom Webinar", "Zoom ウェビナー")

_ID_RE = re.compile(r"^\d{9,12}$")
_PATH_RE = re.compile(r"/(?:j|s|w|wc|wc/join)/(\d{9,12})(?:/|$)")


@dataclass
class Meeting:
    meeting_id: str
    password: str = ""
    host: str = "zoom.us"

    def label(self) -> str:
        i = self.meeting_id
        if len(i) == 11:
            return f"{i[:3]} {i[3:7]} {i[7:]}"
        if len(i) == 10:
            return f"{i[:3]} {i[3:6]} {i[6:]}"
        return i


def parse_meeting(text: str, password: str = "") -> Meeting:
    """招待URL または ミーティングID（＋パスワード）を解析する。"""
    s = (text or "").strip()
    pw = (password or "").strip()
    if not s:
        raise ValueError("招待URLまたはミーティングIDを入力してください。")

    compact = re.sub(r"[\s\-]", "", s)
    if _ID_RE.match(compact):
        return Meeting(compact, pw)

    url = s if "://" in s else "https://" + s
    u = urllib.parse.urlparse(url)
    q = urllib.parse.parse_qs(u.query)

    if u.scheme in ("zoommtg", "zoomus"):
        confno = (q.get("confno") or [""])[0]
        if not _ID_RE.match(confno):
            raise ValueError("zoommtg リンクにミーティングIDが含まれていません。")
        return Meeting(confno, (q.get("pwd") or [pw])[0], u.hostname or "zoom.us")

    host = (u.hostname or "").lower()
    if "zoom" not in host:
        raise ValueError("Zoom の招待URLではないようです。")
    m = _PATH_RE.search(u.path)
    if not m:
        raise ValueError(
            "URLからミーティングIDを読み取れませんでした。\n"
            "（パーソナルリンク名のURLなどは非対応です。ミーティングIDとパスワードで入力してください）"
        )
    url_pwd = (q.get("pwd") or [""])[0]
    return Meeting(m.group(1), url_pwd or pw, host)


def build_join_uri(meeting: Meeting, display_name: str = "") -> str:
    params = {"action": "join", "confno": meeting.meeting_id}
    if meeting.password:
        params["pwd"] = meeting.password
    if display_name:
        params["uname"] = display_name
    query = urllib.parse.urlencode(params, quote_via=urllib.parse.quote)
    return f"zoommtg://{meeting.host}/join?{query}"


def zoom_installed() -> bool:
    """zoommtg:// プロトコルが登録されているか（=Zoomデスクトップアプリがあるか）。"""
    if not IS_WINDOWS:
        return False
    import winreg

    try:
        winreg.CloseKey(winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, "zoommtg"))
        return True
    except OSError:
        return False


def launch_join(meeting: Meeting, display_name: str = "") -> str:
    uri = build_join_uri(meeting, display_name)
    if not IS_WINDOWS:
        raise RuntimeError("Windows でのみ動作します。")
    os.startfile(uri)  # type: ignore[attr-defined]
    return uri


# ---------------------------------------------------------------- ウィンドウ検出
@dataclass
class WindowInfo:
    process: str
    class_name: str
    title: str
    visible: bool
    hwnd: int = 0


def list_windows() -> list[WindowInfo]:
    if not IS_WINDOWS:
        return []
    from ctypes import wintypes

    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    result: list[WindowInfo] = []
    name_cache: dict[int, str] = {}

    def proc_name(pid: int) -> str:
        if pid in name_cache:
            return name_cache[pid]
        name = ""
        h = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if h:
            buf = ctypes.create_unicode_buffer(1024)
            size = wintypes.DWORD(1024)
            if kernel32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
                name = os.path.basename(buf.value)
            kernel32.CloseHandle(h)
        name_cache[pid] = name
        return name

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def enum_proc(hwnd, _lparam):
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        cls = ctypes.create_unicode_buffer(256)
        user32.GetClassNameW(hwnd, cls, 256)
        length = user32.GetWindowTextLengthW(hwnd)
        title = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, title, length + 1)
        result.append(WindowInfo(proc_name(pid.value), cls.value, title.value, bool(user32.IsWindowVisible(hwnd)), int(hwnd or 0)))
        return True

    user32.EnumWindows(enum_proc, 0)
    return result


def zoom_windows() -> list[WindowInfo]:
    return [w for w in list_windows() if w.process.lower() == "zoom.exe"]


def meeting_windows(extra_classes: tuple[str, ...] = ()) -> list[WindowInfo]:
    classes = set(DEFAULT_MEETING_CLASSES) | {c for c in extra_classes if c}
    return [w for w in zoom_windows() if w.visible and
            (w.class_name in classes or any(w.title.startswith(t) for t in DEFAULT_MEETING_TITLES))]


def is_in_meeting(extra_classes: tuple[str, ...] = ()) -> bool:
    """ウィンドウのクラス名・タイトルから会議画面の有無を判定する（簡易判定）。"""
    return bool(meeting_windows(extra_classes))


# ---------------------------------------------------------------- ウィンドウ操作（全画面・生存確認）
_U32 = None


def _user32():
    global _U32
    if _U32 is None:
        from ctypes import wintypes

        u = ctypes.windll.user32
        u.IsWindow.argtypes = u.IsWindowVisible.argtypes = u.IsIconic.argtypes = [wintypes.HWND]
        u.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
        u.MonitorFromWindow.argtypes = [wintypes.HWND, wintypes.DWORD]
        u.MonitorFromWindow.restype = ctypes.c_void_p
        u.GetMonitorInfoW.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        u.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
        u.SetForegroundWindow.argtypes = u.BringWindowToTop.argtypes = [wintypes.HWND]
        u.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int,
                                   ctypes.c_int, ctypes.c_int, wintypes.UINT]
        u.GetForegroundWindow.restype = wintypes.HWND
        _U32 = u
    return _U32


def window_alive(hwnd: int) -> bool:
    """ウィンドウが存在し、表示されているか。"""
    if not IS_WINDOWS or not hwnd:
        return False
    u = _user32()
    return bool(u.IsWindow(hwnd) and u.IsWindowVisible(hwnd))


def _rect(hwnd: int):
    from ctypes import wintypes

    r = wintypes.RECT()
    _user32().GetWindowRect(hwnd, ctypes.byref(r))
    return r


def _monitor(hwnd: int):
    from ctypes import wintypes

    class MONITORINFO(ctypes.Structure):
        _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", wintypes.RECT),
                    ("rcWork", wintypes.RECT), ("dwFlags", wintypes.DWORD)]

    u = _user32()
    mi = MONITORINFO()
    mi.cbSize = ctypes.sizeof(MONITORINFO)
    u.GetMonitorInfoW(u.MonitorFromWindow(hwnd, 2), ctypes.byref(mi))
    return mi


def is_fullscreen(hwnd: int) -> bool:
    if not window_alive(hwnd):
        return False
    r, m = _rect(hwnd), _monitor(hwnd).rcMonitor
    return r.left <= m.left + 1 and r.top <= m.top + 1 and r.right >= m.right - 1 and r.bottom >= m.bottom - 1


def any_zoom_fullscreen() -> bool:
    return any(w.visible and is_fullscreen(w.hwnd) for w in zoom_windows())


def main_window(hwnds) -> int:
    """候補の中で表示中かつ一番大きいウィンドウ（=会議のメイン画面）。"""
    best, area = 0, -1
    for h in hwnds:
        if window_alive(h):
            r = _rect(h)
            a = (r.right - r.left) * (r.bottom - r.top)
            if a > area:
                best, area = h, a
    return best


def focus_window(hwnd: int) -> bool:
    u = _user32()
    if u.IsIconic(hwnd):
        u.ShowWindow(hwnd, 9)  # SW_RESTORE
    u.keybd_event(0x12, 0, 0, 0)  # Alt を一瞬押すと、別アプリからでも前面に出せる
    u.keybd_event(0x12, 0, 2, 0)
    u.SetForegroundWindow(hwnd)
    u.BringWindowToTop(hwnd)
    time.sleep(0.3)
    return u.GetForegroundWindow() == hwnd


def alt_key(vk: int) -> None:
    """Alt+キー を送る（Zoom のショートカット用。例: 0x46=F 全画面, 0x48=H チャット）。"""
    u = _user32()
    u.keybd_event(0x12, 0, 0, 0)
    u.keybd_event(vk, 0, 0, 0)
    u.keybd_event(vk, 0, 2, 0)
    u.keybd_event(0x12, 0, 2, 0)


def _alt_f() -> None:
    alt_key(0x46)


def make_fullscreen(hwnd: int, primary_only: bool = True, uia_click=None) -> str:
    """Zoom の会議画面を全画面にする。戻り値: "full"（全画面）/"max"（最大化で代用）/"none"。"""
    if not window_alive(hwnd):
        return "none"
    if is_fullscreen(hwnd) or any_zoom_fullscreen():
        return "full"
    u = _user32()
    if primary_only and not (_monitor(hwnd).dwFlags & 1):
        # 録画するメインモニターへ移動
        u.ShowWindow(hwnd, 9)
        u.SetWindowPos(hwnd, 0, 80, 80, 0, 0, 0x0001 | 0x0004)  # SWP_NOSIZE | SWP_NOZORDER
        time.sleep(0.5)
    # 1) Zoom のショートカット Alt+F
    before = _rect(hwnd)
    if focus_window(hwnd):
        _alt_f()
        time.sleep(2)
        if is_fullscreen(hwnd) or any_zoom_fullscreen():
            return "full"
    after = _rect(hwnd)
    changed = (before.right - before.left, before.bottom - before.top) != (after.right - after.left, after.bottom - after.top)
    # 2) 画面上の「全画面表示」ボタン
    if not changed and uia_click:
        try:
            if uia_click():
                time.sleep(2)
                if is_fullscreen(hwnd) or any_zoom_fullscreen():
                    return "full"
        except Exception:
            pass
    # 3) 最大化で代用
    u.ShowWindow(hwnd, 3)  # SW_MAXIMIZE
    return "max"


def window_rect(hwnd: int) -> tuple[int, int, int, int]:
    r = _rect(hwnd)
    return r.left, r.top, r.right, r.bottom


def overlap_area(a: tuple, b: tuple) -> int:
    w = min(a[2], b[2]) - max(a[0], b[0])
    h = min(a[3], b[3]) - max(a[1], b[1])
    return max(0, w) * max(0, h)


def minimize_window(hwnd: int) -> bool:
    """ウィンドウを最小化する（SW_SHOWMINNOACTIVE: 他のウィンドウの前面は奪わない）。"""
    u = _user32()
    u.ShowWindow(hwnd, 7)
    time.sleep(0.3)
    return not window_alive(hwnd) or bool(u.IsIconic(hwnd))
