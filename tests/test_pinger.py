import socket

from agent_channel.pinger import ping_node, rank_nodes


def test_ping_node_reachable():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        s.listen(1)
        port = s.getsockname()[1]
        node = {"server": "127.0.0.1", "server_port": port, "tag": "test"}
        rtt = ping_node(node, timeout=0.5)
        assert rtt is not None
        assert rtt >= 0.0


def test_rank_nodes_orders_reachable_first():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        s.listen(1)
        port = s.getsockname()[1]
        alive = {"server": "127.0.0.1", "server_port": port, "tag": "alive"}
        dead = {"server": "127.0.0.1", "server_port": 59997, "tag": "dead"}
        ranked = rank_nodes([dead, alive], timeout=0.5)
        assert len(ranked) == 2
        assert ranked[0]["tag"] == "alive"
        assert ranked[1]["tag"] == "dead"


def test_rank_nodes_empty():
    assert rank_nodes([]) == []


def test_rank_nodes_measures_anytls_over_tcp():
    """anytls 实测是 TCP+TLS，必须参与 TCP 握手测速，不能被当成 UDP-only 排到最后。"""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        s.listen(1)
        port = s.getsockname()[1]
        alive_anytls = {
            "type": "anytls",
            "server": "127.0.0.1",
            "server_port": port,
            "tag": "anytls",
        }
        dead_tcp = {
            "type": "trojan",
            "server": "127.0.0.1",
            "server_port": 59996,
            "tag": "tcp-dead",
        }
        # 输入顺序故意把死 TCP 放前面：若 anytls 被跳过测速，就会按输入顺序排在死节点后面
        ranked = rank_nodes([dead_tcp, alive_anytls], timeout=0.5)
        assert ranked[0]["tag"] == "anytls", [n["tag"] for n in ranked]


def test_rank_nodes_retains_udp_only_and_unreachable():
    """UDP-only 与不可达节点都必须保留（rank_nodes 承诺“仍保留”），且保持输入序。

    这条守的是 rank_nodes 末尾那个 `for node in nodes` 追加循环——一旦有人把它改成
    遍历 tcp_nodes，hysteria2/tuic 会静默消失、候选池少一半。
    """
    hy2 = {
        "type": "hysteria2",
        "server": "127.0.0.1",
        "server_port": 59995,
        "tag": "hy2",
    }
    dead = {
        "type": "trojan",
        "server": "127.0.0.1",
        "server_port": 59994,
        "tag": "dead",
    }
    ranked = rank_nodes([hy2, dead], timeout=0.2)
    assert [n["tag"] for n in ranked] == ["hy2", "dead"]
