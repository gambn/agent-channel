import platform
from unittest.mock import patch

from agent_channel.sysproxy import (
    get_system_proxy_status,
    set_system_proxy,
    unset_system_proxy,
)


def test_sysproxy_basic():
    # 只读状态查询应返回 dict，无副作用
    status = get_system_proxy_status()
    assert "enabled" in status
    assert "server" in status

    if platform.system().lower() == "windows":
        original = {
            "ProxyEnable": 0,
            "ProxyServer": "127.0.0.1:12334",
            "ProxyOverride": "<local>;我的自定义绕过列表",
        }
        restored: dict = {}
        with (
            patch(
                "agent_channel.sysproxy._set_windows_proxy", return_value=True
            ) as mock_set,
            patch(
                "agent_channel.sysproxy._snapshot_proxy", return_value=dict(original)
            ),
            patch(
                "agent_channel.sysproxy._write_windows_raw",
                side_effect=lambda values: bool(restored.update(values)) or True,
            ),
            patch("agent_channel.sysproxy._ORIGINAL_PROXY", None),
        ):
            assert set_system_proxy(port=54321)
            mock_set.assert_called_once_with(54321, "127.0.0.1")

            assert unset_system_proxy()

        # 关闭时必须把用户原有的值还原回去，而不是只把开关关掉
        assert restored == original, restored


def test_system_proxy_status_is_cached_and_self_invalidated():
    """3 秒轮询不得每次去拉子进程；自己开关过代理之后必须立刻重读。

    面板每 3 秒打一次 /api/status，而读系统代理在 Linux 要起三个 gsettings、macOS 要
    起 networksetup —— 不缓存就是白烧。反过来，缓存若不在自己的写操作后失效，用户点完
    开关会看到旧状态。
    """
    from agent_channel import sysproxy

    calls = {"n": 0}

    def counter():
        calls["n"] += 1
        return {"enabled": False, "server": ""}

    sysproxy._invalidate_status_cache()
    try:
        with (
            patch("agent_channel.sysproxy.get_os_type", lambda: "linux"),
            patch("agent_channel.sysproxy._get_linux_status", counter),
            patch("agent_channel.sysproxy._ORIGINAL_PROXY", None),
        ):
            sysproxy.get_system_proxy_status()
            sysproxy.get_system_proxy_status()
            assert calls["n"] == 1, "TTL 内的重复读取应命中缓存"

            # 自己的写操作必须让缓存失效（_unset_linux_proxy 在测试里是 no-op，
            # 这里只验“缓存被清掉、下次会重新问系统”）
            sysproxy.unset_system_proxy()
            sysproxy.get_system_proxy_status()
            assert calls["n"] == 2, "开关系统代理后应重新读取，不得发旧值"
    finally:
        sysproxy._invalidate_status_cache()


def test_heal_stale_system_proxy_matches_port_exactly():
    """自愈的端口判据必须**精确**匹配，并认得 Windows 的多协议串。

    旧实现是 `str(listen_port) in server` 子串判据 —— 查 888 会命中 8888，把用户自己
    指向 8888 的代理误关。这里覆盖：子串不得命中、精确命中、多协议串形态、非回环不认。
    """
    from agent_channel import sysproxy

    cases = (
        ("http=127.0.0.1:8888;https=127.0.0.1:8888", 888, False),
        ("http=127.0.0.1:8888;https=127.0.0.1:8888", 8888, True),
        ("127.0.0.1:20849", 20849, True),
        ("localhost:20849", 20849, True),
        ("1.2.3.4:888", 888, False),
        ("", 20849, False),
    )
    for server, port, expected in cases:
        with (
            patch("agent_channel.sysproxy.get_os_type", lambda: "windows"),
            patch(
                "agent_channel.sysproxy._get_windows_status",
                lambda s=server: {"enabled": True, "server": s},
            ),
        ):
            got = sysproxy.heal_stale_system_proxy(port)
        assert bool(got) == expected, f"server={server!r} port={port} → {got}"
