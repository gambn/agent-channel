"""
agent-channel通道代理 模块入口点
`python -m agent_channel`（无参数）启动本地控制面板；
`python -m agent_channel <命令>` 转发给 CLI，与 `agent-channel` console script 一致。
"""

import sys

from .config import DEFAULT_UI_PORT
from .server import launch_ui


def main() -> int:
    # 与打包入口 packaging/agent_channel_app.py 同一套分发：带参数走 CLI，
    # 无参数才起面板。否则 `python -m agent_channel status` 这类命令会被吞掉、
    # 面板起来并阻塞，还占着 18888 干扰后续步骤。
    if len(sys.argv) > 1:
        from .cli import main as cli_main

        return cli_main()

    print("=" * 60)
    print(" agent-channel通道代理 控制与测试中心")
    print(" 按 Ctrl+C 可停止本地服务")
    print("=" * 60)
    launch_ui(port=DEFAULT_UI_PORT, open_browser=True, block=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
