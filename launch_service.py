"""常驻守护进程：启动控制面板，并按配置文件自动接通代理通道。

节点配置只从 config.json 读取（由人用面板写入一次），本文件不内置任何订阅、
节点或凭据。未配置时只提供面板，不会崩溃。
"""

import time

from agent_channel.config import (
    DEFAULT_LISTEN_PORT,
    DEFAULT_UI_PORT,
    channel_state,
    load_config,
)
from agent_channel.core import enable_channel, get_channel_status
from agent_channel.server import launch_ui


def _channel_banner(config: dict) -> tuple[str, dict]:
    """守护接通前的横幅 + kwargs。

    非 ok 状态（未配置/未选出站/指定节点失效）横幅走 channel_state 的三档 message，
    不再都盖成一句“未配置”；ok 状态横幅报当前来源（mode 已退役，改用 source）。
    """
    st = channel_state(config=config)
    if st["state"] != "ok":
        return st["message"], {}
    source = config.get("active_source") or "-"
    return f"正在按配置接通通道 (source={source})...", st["kwargs"]


def main() -> None:
    # 1. 启动控制面板（非阻塞后台线程）
    launch_ui(port=DEFAULT_UI_PORT, open_browser=False, block=False)
    time.sleep(0.5)

    # 2. 按配置文件接通通道
    config = load_config()
    banner, kwargs = _channel_banner(config)
    print(f"[agent-channel] {banner}")
    if not kwargs:
        print(f"[agent-channel] 控制面板: http://127.0.0.1:{DEFAULT_UI_PORT}")
    else:
        try:
            enable_channel(
                **kwargs,
                auto_patch=True,
                set_sys_proxy=True,
                listen_port=DEFAULT_LISTEN_PORT,
            )
            status = get_channel_status()
            sp = status.get("system_proxy") or {}
            print("[agent-channel] 代理通道接通成功!")
            print(f"  - 本地监听: {status.get('proxy_url')}")
            print(f"  - 活跃节点: {status.get('node_tag')}")
            print(
                f"  - 系统代理: {sp.get('server') or '-'} "
                f"({'已开启' if sp.get('enabled') else '未开启'})"
            )
        except Exception as exc:
            print(f"[agent-channel] 通道接通失败: {exc}")
        print(f"[agent-channel] Web控制面板就绪: http://127.0.0.1:{DEFAULT_UI_PORT}")

    # 3. 阻塞主线程保持守护
    try:
        while True:
            time.sleep(3600)
    except (KeyboardInterrupt, SystemExit):
        pass


if __name__ == "__main__":
    main()
