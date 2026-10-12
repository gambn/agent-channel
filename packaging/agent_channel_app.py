"""PyInstaller 打包入口。

- 无参数启动：桌面模式，托盘常驻并打开控制面板；
- 带参数启动：转发给 CLI，与 pip 安装的 `agent-channel` 命令行一致。

同一个可执行文件因此既能双击当桌面程序，也能当命令行工具。
"""

import sys


def main() -> int:
    if len(sys.argv) > 1:
        from agent_channel.cli import main as cli_main

        return cli_main()

    # 无参数：桌面模式。托盘常驻，双击图标或点菜单打开面板。
    try:
        from agent_channel.tray import run_tray

        return run_tray(open_panel=True)
    except Exception as exc:  # 托盘不可用时退化为只开面板，保证程序仍可用
        print(f"[agent-channel] 托盘不可用({exc})，改为直接打开控制面板")
        from agent_channel.config import DEFAULT_UI_PORT
        from agent_channel.server import launch_ui

        launch_ui(port=DEFAULT_UI_PORT, open_browser=True, block=True)
        return 0


if __name__ == "__main__":
    sys.exit(main())
