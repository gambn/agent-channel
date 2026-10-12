"""系统托盘常驻（Windows）。

设计取舍：不引入 pystray/pillow 等运行时依赖，直接用 ctypes 调 Shell_NotifyIcon。
非 Windows 平台下 `run_tray()` 会退化为「直接打开面板并阻塞」，不做托盘。

托盘菜单：
- 打开控制面板
- 启动/停止通道（显示当前状态）
- 退出
双击托盘图标等同「打开控制面板」。
"""

import ctypes
import sys
import threading
import webbrowser
from contextlib import suppress
from ctypes import wintypes
from pathlib import Path

WM_DESTROY = 0x0002
WM_COMMAND = 0x0111
WM_LBUTTONDBLCLK = 0x0203
WM_RBUTTONUP = 0x0205
WM_APP = 0x8000
WM_TRAY = WM_APP + 1

NIM_ADD = 0x00000000
NIM_MODIFY = 0x00000001
NIM_DELETE = 0x00000002
NIF_MESSAGE = 0x00000001
NIF_ICON = 0x00000002
NIF_TIP = 0x00000004

IMAGE_ICON = 1
LR_LOADFROMFILE = 0x00000010
LR_DEFAULTSIZE = 0x00000040
TPM_RIGHTBUTTON = 0x0002

MF_STRING = 0x00000000
MF_SEPARATOR = 0x00000800

ID_OPEN = 1001
ID_TOGGLE = 1002
ID_EXIT = 1003

APP_TITLE = "agent-channel通道代理"

# 64 位下 LRESULT / 句柄都超出 32 位，ctypes 默认按 c_int 转换会 OverflowError，
# 所以下面所有涉及句柄的 Win32 调用都必须显式声明 argtypes。
LRESULT = ctypes.c_longlong if ctypes.sizeof(ctypes.c_void_p) == 8 else ctypes.c_long


def _icon_candidates() -> list[Path]:
    """托盘图标的候选位置，按优先级排列。

    优先用托盘专用图 tray.ico：它是透明底、且每一档尺寸单独绘制（不是从大图缩下来的），
    在 16px 的任务栏里比应用图标清晰得多；找不到时退回应用图标，再找不到用系统默认图标。
    """
    roots: list[Path] = []
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        roots.append(Path(meipass) / "assets")
    roots.append(Path(sys.executable).resolve().parent / "assets")
    roots.append(Path(__file__).resolve().parent.parent / "packaging" / "assets")
    return [root / name for root in roots for name in ("tray.ico", "icon.ico")]


def find_icon() -> Path | None:
    """定位托盘图标文件；找不到返回 None（此时用系统默认图标）。"""
    for candidate in _icon_candidates():
        if candidate.is_file():
            return candidate
    return None


def _hide_console() -> None:
    """隐藏本进程的控制台窗口，让托盘模式看起来像个桌面程序。"""
    try:
        handle = ctypes.windll.kernel32.GetConsoleWindow()
        if handle:
            ctypes.windll.user32.ShowWindow(handle, 0)  # SW_HIDE
    except (AttributeError, OSError):
        # 无控制台（例如以窗口模式打包）时没有可隐藏的窗口，也没有要补救的动作，
        # 静默跳过即可 —— 这里没有"需要记录"的东西。
        pass


def _toggle_channel() -> str:
    """启动或停止通道，返回给用户看的一句话结果。"""
    from .config import DEFAULT_LISTEN_PORT, channel_state, load_config
    from .core import (
        ChannelBusyError,
        disable_channel,
        enable_channel,
        get_channel_status,
    )

    status = get_channel_status()
    if status.get("status") == "running":
        try:
            disable_channel()
        except ChannelBusyError as exc:
            # 托盘消息回调里不能让异常逃出去：如实回一句
            return f"停止失败: {exc}"
        return "通道已停止"

    st = channel_state(config=load_config())
    if st["state"] != "ok":
        return st["message"]
    try:
        result = enable_channel(
            **st["kwargs"],
            auto_patch=True,
            set_sys_proxy=True,
            listen_port=DEFAULT_LISTEN_PORT,
        )
    except Exception as exc:
        return f"启动失败: {exc}"
    return f"通道已启动 ({result.get('node_tag') or '已连接'})"


if sys.platform != "win32":

    def run_tray(open_panel: bool = True) -> int:
        """非 Windows 平台没有本实现的托盘，退化为直接打开面板。"""
        from .config import DEFAULT_UI_PORT
        from .server import launch_ui

        launch_ui(port=DEFAULT_UI_PORT, open_browser=open_panel, block=True)
        return 0

else:

    class _WNDCLASSEX(ctypes.Structure):
        _fields_ = [
            ("cbSize", wintypes.UINT),
            ("style", wintypes.UINT),
            ("lpfnWndProc", ctypes.c_void_p),
            ("cbClsExtra", ctypes.c_int),
            ("cbWndExtra", ctypes.c_int),
            ("hInstance", wintypes.HINSTANCE),
            ("hIcon", wintypes.HANDLE),
            ("hCursor", wintypes.HANDLE),
            ("hbrBackground", wintypes.HANDLE),
            ("lpszMenuName", wintypes.LPCWSTR),
            ("lpszClassName", wintypes.LPCWSTR),
            ("hIconSm", wintypes.HANDLE),
        ]

    class _NOTIFYICONDATA(ctypes.Structure):
        _fields_ = [
            ("cbSize", wintypes.DWORD),
            ("hWnd", wintypes.HWND),
            ("uID", wintypes.UINT),
            ("uFlags", wintypes.UINT),
            ("uCallbackMessage", wintypes.UINT),
            ("hIcon", wintypes.HANDLE),
            ("szTip", wintypes.WCHAR * 128),
            ("dwState", wintypes.DWORD),
            ("dwStateMask", wintypes.DWORD),
            ("szInfo", wintypes.WCHAR * 256),
            ("uVersion", wintypes.UINT),
            ("szInfoTitle", wintypes.WCHAR * 64),
            ("dwInfoFlags", wintypes.DWORD),
            ("guidItem", ctypes.c_byte * 16),
            ("hBalloonIcon", wintypes.HANDLE),
        ]

    class TrayApp:
        """极简托盘：一个隐藏窗口 + 一个通知区图标 + 右键菜单。"""

        def __init__(self) -> None:
            self.user32 = ctypes.windll.user32
            self.shell32 = ctypes.windll.shell32
            self.kernel32 = ctypes.windll.kernel32
            self.hwnd: int = 0
            self.hicon: int = 0
            self._running = False
            self._panel_thread: threading.Thread | None = None
            self._bind_apis()
            self._wndproc = ctypes.WINFUNCTYPE(
                LRESULT,
                wintypes.HWND,
                wintypes.UINT,
                wintypes.WPARAM,
                wintypes.LPARAM,
            )(self._on_message)

        def _bind_apis(self) -> None:
            """显式声明参与调用的 Win32 函数签名。"""
            u = self.user32
            u.DefWindowProcW.argtypes = [
                wintypes.HWND,
                wintypes.UINT,
                wintypes.WPARAM,
                wintypes.LPARAM,
            ]
            u.DefWindowProcW.restype = LRESULT
            u.CreateWindowExW.argtypes = [
                wintypes.DWORD,
                wintypes.LPCWSTR,
                wintypes.LPCWSTR,
                wintypes.DWORD,
                ctypes.c_int,
                ctypes.c_int,
                ctypes.c_int,
                ctypes.c_int,
                wintypes.HWND,
                wintypes.HANDLE,
                wintypes.HINSTANCE,
                ctypes.c_void_p,
            ]
            u.CreateWindowExW.restype = wintypes.HWND
            u.DestroyWindow.argtypes = [wintypes.HWND]
            u.PostQuitMessage.argtypes = [ctypes.c_int]
            u.GetMessageW.argtypes = [
                ctypes.POINTER(wintypes.MSG),
                wintypes.HWND,
                wintypes.UINT,
                wintypes.UINT,
            ]
            u.TranslateMessage.argtypes = [ctypes.POINTER(wintypes.MSG)]
            u.DispatchMessageW.argtypes = [ctypes.POINTER(wintypes.MSG)]
            u.CreatePopupMenu.restype = wintypes.HANDLE
            u.AppendMenuW.argtypes = [
                wintypes.HANDLE,
                wintypes.UINT,
                ctypes.c_size_t,
                wintypes.LPCWSTR,
            ]
            u.TrackPopupMenu.argtypes = [
                wintypes.HANDLE,
                wintypes.UINT,
                ctypes.c_int,
                ctypes.c_int,
                ctypes.c_int,
                wintypes.HWND,
                ctypes.c_void_p,
            ]
            u.DestroyMenu.argtypes = [wintypes.HANDLE]
            u.SetForegroundWindow.argtypes = [wintypes.HWND]
            u.GetCursorPos.argtypes = [ctypes.POINTER(wintypes.POINT)]
            u.LoadImageW.argtypes = [
                wintypes.HINSTANCE,
                wintypes.LPCWSTR,
                wintypes.UINT,
                ctypes.c_int,
                ctypes.c_int,
                wintypes.UINT,
            ]
            u.LoadImageW.restype = wintypes.HANDLE
            u.LoadIconW.restype = wintypes.HANDLE
            self.shell32.Shell_NotifyIconW.argtypes = [
                wintypes.DWORD,
                ctypes.POINTER(_NOTIFYICONDATA),
            ]
            self.kernel32.GetModuleHandleW.restype = wintypes.HMODULE
            self.kernel32.GetConsoleWindow.restype = wintypes.HWND

        # ---- 窗口与图标 ------------------------------------------------

        def _create_window(self) -> None:
            instance = self.kernel32.GetModuleHandleW(None)
            self.hicon = self._load_icon()
            wc = _WNDCLASSEX()
            wc.cbSize = ctypes.sizeof(_WNDCLASSEX)
            wc.lpfnWndProc = ctypes.cast(self._wndproc, ctypes.c_void_p)
            wc.hInstance = instance
            wc.hIcon = self.hicon
            wc.lpszClassName = APP_TITLE
            if not self.user32.RegisterClassExW(ctypes.byref(wc)):
                raise OSError("RegisterClassExW 失败")
            # 只创建消息窗口，不显示
            self.hwnd = self.user32.CreateWindowExW(
                0, APP_TITLE, APP_TITLE, 0, 0, 0, 0, 0, 0, 0, instance, None
            )
            if not self.hwnd:
                raise OSError("CreateWindowExW 失败")

        def _load_icon(self) -> int:
            path = find_icon()
            if path is None:
                return self.user32.LoadIconW(0, 32512)  # IDI_APPLICATION
            handle = self.user32.LoadImageW(
                0, str(path), IMAGE_ICON, 0, 0, LR_LOADFROMFILE | LR_DEFAULTSIZE
            )
            return handle or self.user32.LoadIconW(0, 32512)

        def _notify(self, action: int) -> bool:
            data = _NOTIFYICONDATA()
            data.cbSize = ctypes.sizeof(_NOTIFYICONDATA)
            data.hWnd = self.hwnd
            data.uID = 1
            data.uFlags = NIF_ICON | NIF_MESSAGE | NIF_TIP
            data.uCallbackMessage = WM_TRAY
            data.hIcon = self.hicon
            data.szTip = APP_TITLE
            return bool(self.shell32.Shell_NotifyIconW(action, ctypes.byref(data)))

        # ---- 交互 ------------------------------------------------------

        def _show_menu(self) -> None:
            from .core import get_channel_status

            running = get_channel_status().get("status") == "running"
            menu = self.user32.CreatePopupMenu()
            self.user32.AppendMenuW(menu, MF_STRING, ID_OPEN, "打开控制面板")
            label = "停止通道" if running else "启动通道"
            self.user32.AppendMenuW(menu, MF_STRING, ID_TOGGLE, label)
            self.user32.AppendMenuW(menu, MF_SEPARATOR, 0, None)
            self.user32.AppendMenuW(menu, MF_STRING, ID_EXIT, "退出")

            point = wintypes.POINT()
            self.user32.GetCursorPos(ctypes.byref(point))
            # 必须先把窗口设为前台，否则菜单点不掉
            self.user32.SetForegroundWindow(self.hwnd)
            self.user32.TrackPopupMenu(
                menu, TPM_RIGHTBUTTON, point.x, point.y, 0, self.hwnd, None
            )
            self.user32.DestroyMenu(menu)

        def _open_panel(self) -> None:
            from .config import DEFAULT_UI_PORT
            from .server import launch_ui

            if self._panel_thread and self._panel_thread.is_alive():
                webbrowser.open(f"http://127.0.0.1:{DEFAULT_UI_PORT}")
                return
            # 面板跑在后台线程，托盘消息循环继续留在主线程
            self._panel_thread = threading.Thread(
                target=launch_ui,
                kwargs={
                    "port": DEFAULT_UI_PORT,
                    "open_browser": True,
                    "block": True,
                },
                daemon=True,
            )
            self._panel_thread.start()

        def _on_message(self, hwnd, msg, wparam, lparam):
            if msg == WM_TRAY:
                if lparam == WM_RBUTTONUP:
                    self._show_menu()
                elif lparam == WM_LBUTTONDBLCLK:
                    self._open_panel()
                return 0
            if msg == WM_COMMAND:
                command = wparam & 0xFFFF
                if command == ID_OPEN:
                    self._open_panel()
                elif command == ID_TOGGLE:
                    _toggle_channel()
                elif command == ID_EXIT:
                    # 退出前显式停通道：退出会还原系统代理与内核，不能只靠 atexit 兜底
                    from .core import disable_channel

                    with suppress(Exception):
                        disable_channel()
                    self.user32.DestroyWindow(hwnd)
                return 0
            if msg == WM_DESTROY:
                self._notify(NIM_DELETE)
                self.user32.PostQuitMessage(0)
                return 0
            return self.user32.DefWindowProcW(hwnd, msg, wparam, lparam)

        # ---- 生命周期 --------------------------------------------------

        def run(self, open_panel: bool = True) -> int:
            self._create_window()
            if not self._notify(NIM_ADD):
                raise OSError("Shell_NotifyIcon 注册托盘图标失败")
            _hide_console()
            self._running = True
            if open_panel:
                self._open_panel()

            message = wintypes.MSG()
            while self.user32.GetMessageW(ctypes.byref(message), 0, 0, 0) > 0:
                self.user32.TranslateMessage(ctypes.byref(message))
                self.user32.DispatchMessageW(ctypes.byref(message))
            self._running = False
            return 0

    def run_tray(open_panel: bool = True) -> int:
        """在系统托盘常驻运行，直到用户从菜单选择退出。"""
        from .binary import ensure_local_core

        ensure_local_core()
        return TrayApp().run(open_panel=open_panel)
