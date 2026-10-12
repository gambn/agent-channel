"""launch_service 守护入口的文案分档。"""

from launch_service import _channel_banner


def test_launch_service_banner_unconfigured():
    """未配置：横幅走 unconfigured 档，kwargs 为空。"""
    banner, kwargs = _channel_banner({"active_source": ""})
    assert "未配置节点" in banner
    assert kwargs == {}


def test_launch_service_banner_unselected():
    """池未选出站：横幅走 unselected 档（不能盖成“未配置”）。"""
    a = {"type": "vless", "server": "a.com", "server_port": 443}
    b = {"type": "vless", "server": "b.com", "server_port": 443}
    banner, kwargs = _channel_banner(
        {"active_source": "json", "imported_nodes": [a, b]}
    )
    assert "尚未选择出站" in banner
    assert kwargs == {}


def test_launch_service_banner_pin_dead():
    """指定节点失效：横幅走 pin_dead 档。"""
    a = {"type": "vless", "server": "a.com", "server_port": 443}
    b = {"type": "vless", "server": "b.com", "server_port": 443}
    banner, kwargs = _channel_banner(
        {
            "active_source": "json",
            "imported_nodes": [a, b],
            "outbound_selections": {
                "json": {"mode": "pinned", "node_key": "vless|gone.com|443"}
            },
        }
    )
    assert "已失效" in banner
    assert kwargs == {}


def test_launch_service_banner_ok_uses_source():
    """ok 状态横幅报 source（mode 已退役，不能打 mode=None）。"""
    node = {"type": "vless", "server": "a.com", "server_port": 443}
    banner, kwargs = _channel_banner(
        {"active_source": "json", "imported_nodes": [node]}
    )
    assert "source=json" in banner
    assert "mode=" not in banner
    assert kwargs == {"nodes": [node]}
