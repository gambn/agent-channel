"""PyInstaller 打包入口。

- 无参数启动：桌面模式。Windows 托盘常驻，macOS/Linux 起一个原生外壳窗口；
  两者都打开控制面板，关闭窗口/托盘退出都会停通道并还原系统代理；
- 带参数启动：转发给 CLI，与 pip 安装的 `agent-channel` 命令行一致。

同一个可执行文件因此既能双击当桌面程序，也能当命令行工具。
"""

import os
import sys


def main() -> int:
    # 冻结产物的 stdout 是块缓冲：不设行缓冲，面板地址要等进程退出才可见（实测踩过）
    from agent_channel.utils import configure_stdio

    configure_stdio()

    if len(sys.argv) > 1:
        from agent_channel.cli import main as cli_main

        return cli_main()

    # 无参数：桌面模式。Windows 走托盘；其余平台走原生外壳窗口（关闭窗口即退出并清理）。
    try:
        if sys.platform == "win32":
            from agent_channel.tray import run_tray

            return run_tray(open_panel=True)
        from agent_channel.config import DEFAULT_UI_PORT
        from agent_channel.window import run_window

        # 面板端口可选：默认端口被别的实例占着时，用户/验证脚本可以指定一个
        raw_port = os.environ.get("AGENT_CHANNEL_UI_PORT", "").strip()
        port = int(raw_port) if raw_port.isdigit() else DEFAULT_UI_PORT

        # 自检钩子：CI/真机验证时让窗口到点自动关一次（走的正是红叉那条路径）。
        # 平时不设这个环境变量，行为与用户点关闭完全一样。
        raw = os.environ.get("AGENT_CHANNEL_WINDOW_SELFTEST_MS", "").strip()
        close_after = int(raw) if raw.isdigit() else None
        # 自检时不自动开浏览器：这条路径是给无人值守的验证跑的
        return run_window(
            port=port, open_panel=close_after is None, close_after_ms=close_after
        )
    except Exception as exc:  # 界面起不来时退化为只开面板，保证程序仍可用
        print(f"[agent-channel] 界面不可用({exc})，改为直接打开控制面板")
        from agent_channel.config import DEFAULT_UI_PORT
        from agent_channel.server import launch_ui

        launch_ui(port=DEFAULT_UI_PORT, open_browser=True, block=True)
        return 0


if __name__ == "__main__":
    sys.exit(main())
