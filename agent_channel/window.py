"""macOS / Linux 的原生外壳窗口（标准库 tkinter）。

为什么需要它：托盘那套是 Windows 专属（`tray.py` 用 ctypes 调 Shell_NotifyIcon），
非 Windows 平台上这个常驻程序原先只是"开浏览器 + 前台阻塞"——用户既没有一个看得见的
窗口，也没有一条正规的退出路径（面板里没有退出入口，CLI 的 `stop` 停的是通道不是进程）。

窗口的职责只有两件：让程序有一个看得见、关得掉的入口，把关闭动作接到与托盘「退出」
同一套清理（`core.shutdown_app`）上。面板本身仍然是浏览器里的那份，这里不做第二套 UI。

红叉走 `WM_DELETE_WINDOW`——macOS 上就是标题栏那个红色按钮（Tk 9 实测：黄灯最小化、
绿灯缩放也都通）。Cmd-Q 单独接：Tk 9 已移除 `::tk::mac::Quit`，所以这里自己建一个
最小应用菜单，把「退出」指到同一处理器，否则用户按 Cmd-Q 会没有反应。
"""

import json
import urllib.request
import webbrowser
from typing import Any

from .config import DEFAULT_UI_PORT
from .core import get_channel_status, install_exit_handlers, shutdown_app
from .server import launch_ui
from .utils import warn_stderr

# 状态轮询间隔（毫秒）：状态卡要跟着通道启停变化，但没必要更密
_STATUS_INTERVAL_MS = 1000


def existing_instance_url(port: int = DEFAULT_UI_PORT, timeout: float = 1.0) -> str | None:
    """若本机已经有一个面板在跑，返回它的地址，否则 None。

    再点一次图标不该起第二个窗口加第二个面板（内核端口会撞）。判据是"那个端口上确实
    有一个像我们的 `/api/status`"，而不是"端口被占着"——那个端口可能是别的程序在用。
    """
    url = f"http://127.0.0.1:{port}"
    try:
        with urllib.request.urlopen(f"{url}/api/status", timeout=timeout) as resp:
            if resp.status != 200:
                return None
            payload = json.loads(resp.read().decode("utf-8"))
    except Exception:
        return None
    if not isinstance(payload, dict) or "status" not in payload:
        return None
    return url


def status_text(core_status: dict[str, Any], core_version: str) -> str:
    """窗口里那行状态文字（纯函数，便于测试）。"""
    state = str(core_status.get("status") or "stopped")
    label = {"running": "通道运行中", "stopped": "通道已停止"}.get(state, state)
    parts = [label]
    tag = str(core_status.get("node_tag") or "").strip()
    if state == "running" and tag:
        parts.append(tag)
    parts.append(f"内核 {core_version}")
    return " · ".join(parts)


def _open_panel_window(
    port: int, open_panel: bool, close_after_ms: int | None = None
) -> int:
    """起窗口并常驻到用户关掉它；返回进程退出码。"""
    import tkinter as tk

    url = launch_ui(port=port, open_browser=open_panel, block=False)

    root = tk.Tk()
    root.title("agent-channel 通道代理")
    root.geometry("440x210")
    root.minsize(380, 190)

    frame = tk.Frame(root, padx=18, pady=16)
    frame.pack(fill="both", expand=True)
    tk.Label(frame, text="agent-channel 通道代理", font=("Helvetica", 15, "bold")).pack(
        anchor="w"
    )
    status_var = tk.StringVar(value="正在读取状态…")
    tk.Label(frame, textvariable=status_var, font=("Helvetica", 12)).pack(
        anchor="w", pady=(10, 0)
    )
    tk.Label(frame, text=f"控制面板: {url}", font=("Helvetica", 10), fg="#666666").pack(
        anchor="w", pady=(8, 0)
    )
    tk.Label(
        frame,
        text="关闭本窗口即停止通道、还原系统代理并退出",
        font=("Helvetica", 10),
        fg="#666666",
    ).pack(anchor="w", pady=(6, 12))

    buttons = tk.Frame(frame)
    buttons.pack(anchor="w")

    def quit_now() -> None:
        root.destroy()

    tk.Button(
        buttons, text="打开控制面板", width=14, command=lambda: webbrowser.open(url)
    ).pack(side="left")
    tk.Button(buttons, text="退出", width=8, command=quit_now).pack(side="left", padx=8)

    # macOS：Tk 9 不再有 ::tk::mac::Quit，自己给 Cmd-Q 一个去处
    try:
        menu = tk.Menu(root)
        app_menu = tk.Menu(menu, tearoff=0)
        app_menu.add_command(label="退出 agent-channel", command=quit_now)
        menu.add_cascade(label="agent-channel", menu=app_menu)
        root.config(menu=menu)
    except Exception as exc:  # 菜单建不出来不影响主流程：红叉仍然可用
        warn_stderr(f"应用菜单不可用（忽略）: {exc}")

    # 红叉 / 关闭窗口：先结束主循环，再由这里统一收尾
    root.protocol("WM_DELETE_WINDOW", quit_now)

    def refresh() -> None:
        from .binary import get_local_core_version

        try:
            status_var.set(status_text(get_channel_status(), get_local_core_version()))
        except Exception as exc:
            status_var.set(f"状态读取失败: {exc}")
        root.after(_STATUS_INTERVAL_MS, refresh)

    root.after(0, refresh)
    if close_after_ms is not None:
        # 自检用：过 N 毫秒自动关一次窗口，等于替用户点一下红叉（走的是同一条路径）
        root.after(close_after_ms, quit_now)
    root.mainloop()

    # 走到这里说明窗口被关掉了（红叉 / 菜单退出 / 退出按钮）：清理在主线程里同步跑完，
    # 不能直接 os._exit —— 还原系统代理挂在清理路径上。
    # `shutdown_app()` 在主线程里以 SystemExit 结束调用方（对"进程该退出了"最直白），
    # 这里把它转成返回值，好让外层（自检脚本、测试）能接着断言清理结果。
    try:
        shutdown_app()
    except SystemExit as exc:
        return int(exc.code or 0)
    return 0


QUIT_HINT = "退出：面板底部的「退出程序」按钮，或按 Ctrl-C（关掉浏览器标签不会退出进程）"


def print_quit_hint() -> None:
    """没有原生窗口时给一句明白话。

    没有窗口的平台（Linux，或任何 tcl/tk / 显示环境缺失的场合）用户唯一看得见的是浏览器
    标签——关掉它进程并不会退出（面板服务与通道都还在跑）。这句话就是补上这个认知缺口。
    """
    print(f"[agent-channel通道代理] {QUIT_HINT}", flush=True)


def run_window(
    port: int = DEFAULT_UI_PORT,
    open_panel: bool = True,
    close_after_ms: int | None = None,
) -> int:
    """起外壳窗口并常驻；返回进程退出码。

    面板服务跑在后台线程，窗口只显示状态与接收关闭动作。关闭时先 `destroy()` 结束
    mainloop，再走 `shutdown_app()`——在 Tk 回调里直接抛 SystemExit 会被 Tkinter 吞掉
    （它只当作一次回调异常打印出来，主循环照跑）。

    `close_after_ms` 只给自检用：过 N 毫秒自动关窗，走的正是红叉那条路径。
    已经有一个实例在跑时不再起第二个：打开它的面板并退出（内核端口只有一个）。
    没有 tcl/tk 或没有可用显示时，退化回"只开控制面板"，不让程序起不来。
    """
    # 信号处理在此装好：窗口路径与"没有 tcl/tk / 没有显示"的退化路径都要能正规退出
    install_exit_handlers()

    existing = existing_instance_url(port)
    if existing is not None:
        print(f"[agent-channel通道代理] 已有一个面板在运行: {existing}")
        webbrowser.open(existing)
        return 0

    try:
        return _open_panel_window(port, open_panel, close_after_ms)
    except ImportError as exc:
        warn_stderr(f"界面组件不可用（{exc}），改为只开控制面板")
        print_quit_hint()
    except Exception as exc:  # 无显示环境（如纯终端 Linux）会在这里
        warn_stderr(f"窗口起不来（{exc}），改为只开控制面板")
        print_quit_hint()
    launch_ui(port=port, open_browser=open_panel, block=True)
    return 0
