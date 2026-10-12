import base64
import json

from agent_channel.parsers import (
    extract_nodes_from_subscription,
    parse_node_uri,
    parse_singbox_json,
    parse_single_node,
)


def test_parse_vless_uri():
    uri = "vless://a1b2c3d4-e5f6-4a7b-8c9d-0e1f2a3b4c5d@hk.example.com:443?encryption=none&security=tls&sni=hk.example.com&type=ws&path=%2Fws#HK-Node"
    node = parse_node_uri(uri)
    assert node is not None
    assert node["type"] == "vless"
    assert node["server"] == "hk.example.com"
    assert node["server_port"] == 443
    assert node["uuid"] == "a1b2c3d4-e5f6-4a7b-8c9d-0e1f2a3b4c5d"
    assert node["tls"]["server_name"] == "hk.example.com"
    assert node["transport"]["path"] == "/ws"


def test_parse_trojan_uri():
    uri = "trojan://password123@us.example.com:443?sni=us.example.com#US-Trojan"
    node = parse_node_uri(uri)
    assert node is not None
    assert node["type"] == "trojan"
    assert node["server"] == "us.example.com"
    assert node["password"] == "password123"


def test_subscription_json_shares_the_import_type_list():
    """订阅返回的 sing-box JSON 必须与「导入 JSON」用同一份类型清单：
    此前这里有一份内联的 7 类小清单，naive / ssh / shadowtls / hysteria(v1) 会被静默丢掉。"""
    text = json.dumps(
        {
            "outbounds": [
                {
                    "type": "naive",
                    "tag": "n",
                    "server": "a.com",
                    "server_port": 443,
                    "username": "u",
                    "password": "p",
                    "tls": {"enabled": True, "server_name": "a.com"},
                }
            ]
        }
    )
    nodes, _errors = extract_nodes_from_subscription(text)
    assert len(nodes) == 1, nodes
    assert nodes[0]["type"] == "naive"


def test_subscription_json_gets_same_validation_and_tag_normalization():
    """订阅返回的 JSON 与「导入 JSON」共用同一份校验与归一化：
    缺凭据的拒收、tag 撞内置出站的被改名（不改名会让内核 duplicate tag 直接 FATAL）。"""
    text = json.dumps(
        {
            "outbounds": [
                {
                    "type": "vless",
                    "tag": "a",
                    "server": "1.2.3.4",
                    "server_port": 443,
                },
                {
                    "type": "vless",
                    "tag": "direct",
                    "server": "1.2.3.4",
                    "server_port": 443,
                    "uuid": "u",
                },
            ]
        }
    )
    nodes, _errors = extract_nodes_from_subscription(text)
    assert [n["tag"] for n in nodes] == ["imported-1"], nodes


def test_subscription_parse_never_returns_empty_without_a_reason():
    """订阅解析不允许再出现“空且无话”——每类坏输入都要说得出为什么。"""
    cases = [
        ("", "订阅内容为空"),
        ("<html>403 Forbidden</html>", "Base64"),
        ('{"outbounds": [{"type": "wireguard", "tag": "w"}]}', "类型不受支持"),
        (
            '{"outbounds": [{"type": "socks", "tag": "s","server": "1.2.3.4","server_port": 1080}]}',
            "类型不受支持",
        ),
        (
            '{"outbounds": [{"type": "vless", "tag": "a", "server": "1.2.3.4", "server_port": 443}]}',
            "缺字段",
        ),
    ]
    for raw, expect in cases:
        nodes, errors = extract_nodes_from_subscription(raw)
        assert nodes == [], f"raw={raw!r} nodes={nodes}"
        assert errors, f"{raw!r} 返回空却没有原因"
        assert any(expect in e for e in errors), f"raw={raw!r} errors={errors}"


def test_subscription_reports_how_many_lines_were_skipped():
    """成功识别一部分时，被跳过的行数要说明——静默丢弃必须变成可见。"""
    content = (
        "vless://id@s1.com:443?type=tcp#N1\n"
        "这不是节点\n"
        "vless://id@s2.com:443?type=tcp#N2"
    )
    nodes, errors = extract_nodes_from_subscription(
        base64.b64encode(content.encode()).decode()
    )
    assert len(nodes) == 2, nodes
    assert any("另有 1 行" in e for e in errors), errors


def test_extract_nodes_from_base64_subscription():
    content = (
        "vless://id@s1.com:443?type=tcp#Node1\nvless://id@s2.com:443?type=tcp#Node2"
    )
    encoded = base64.b64encode(content.encode("utf-8")).decode("utf-8")
    nodes, _errors = extract_nodes_from_subscription(encoded)
    assert len(nodes) == 2
    assert nodes[0]["server"] == "s1.com"


def test_parse_singbox_json_valid():
    text = json.dumps(
        {
            "outbounds": [
                {
                    "type": "vless",
                    "tag": "A",
                    "server": "a.com",
                    "server_port": 443,
                    "uuid": "u",
                    "tls": {"enabled": True, "server_name": "a.com"},
                    "transport": {"type": "ws", "path": "/x"},
                },
                {
                    "type": "trojan",
                    "tag": "B",
                    "server": "b.com",
                    "server_port": 443,
                    "password": "p",
                },
            ]
        }
    )
    nodes, errors, renamed = parse_singbox_json(text)
    assert len(nodes) == 2
    assert errors == []
    assert renamed == []
    # 嵌套结构必须原样透传，不挑字段重建
    assert nodes[0]["tls"] == {"enabled": True, "server_name": "a.com"}
    assert nodes[0]["transport"] == {"type": "ws", "path": "/x"}
    assert nodes[1]["password"] == "p"


def test_parse_singbox_json_skips_unsupported_type():
    text = json.dumps(
        {
            "outbounds": [
                {"type": "vless", "server": "a.com", "server_port": 443, "uuid": "u"},
                {"type": "direct", "tag": "direct"},
                {"type": "selector", "tag": "s"},
            ]
        }
    )
    nodes, errors, _ = parse_singbox_json(text)
    assert len(nodes) == 1
    assert any("direct" in e for e in errors)
    assert any("selector" in e for e in errors)


def test_parse_singbox_json_missing_fields():
    text = json.dumps(
        {
            "outbounds": [
                {"type": "vless", "server": "a.com", "server_port": 443},  # 缺 uuid
                {"type": "trojan", "server_port": 443, "password": "p"},  # 缺 server
            ]
        }
    )
    nodes, errors, _ = parse_singbox_json(text)
    assert nodes == []
    assert len(errors) == 2
    assert any("uuid" in e for e in errors)
    assert any("缺 server" in e for e in errors)


def test_parse_singbox_json_ssh_needs_auth():
    text = json.dumps(
        {
            "outbounds": [
                {"type": "ssh", "server": "a.com", "server_port": 22, "user": "root"},
                {
                    "type": "ssh",
                    "server": "a.com",
                    "server_port": 22,
                    "user": "root",
                    "private_key": "k",
                },
            ]
        }
    )
    nodes, errors, _ = parse_singbox_json(text)
    assert len(nodes) == 1
    assert any("password 或 private_key" in e for e in errors)


def test_parse_singbox_json_invalid_json():
    nodes, errors, _ = parse_singbox_json("{not json")
    assert nodes == []
    assert errors and "解析失败" in errors[0]


def test_parse_singbox_json_empty_outbounds():
    nodes, errors, _ = parse_singbox_json('{"outbounds": []}')
    assert nodes == []
    assert errors


def test_parse_singbox_json_tag_conflict():
    text = json.dumps(
        {
            "outbounds": [
                {
                    "type": "vless",
                    "tag": "direct",
                    "server": "a.com",
                    "server_port": 443,
                    "uuid": "u",
                },
                {
                    "type": "vless",
                    "tag": "direct",
                    "server": "b.com",
                    "server_port": 443,
                    "uuid": "u",
                },
            ]
        }
    )
    nodes, errors, renamed = parse_singbox_json(text)
    assert len(nodes) == 2
    assert errors == []
    assert nodes[0]["tag"] == "imported-1"
    assert nodes[1]["tag"] == "imported-2"
    assert len(renamed) == 2


def test_parse_singbox_json_missing_tls():
    """需要 tls 的协议（hysteria2 等）缺 tls 必须跳过并说明。"""
    text = json.dumps(
        {
            "outbounds": [
                {
                    "type": "hysteria2",
                    "server": "a.com",
                    "server_port": 443,
                    "password": "p",
                },
                {
                    "type": "hysteria2",
                    "server": "a.com",
                    "server_port": 443,
                    "password": "p",
                    "tls": {"enabled": True, "server_name": "a.com"},
                },
            ]
        }
    )
    nodes, errors, _ = parse_singbox_json(text)
    assert len(nodes) == 1
    assert any("tls" in e for e in errors)


def test_parse_vmess_uri():
    """vmess:// 支持 v2rayN 的 base64 JSON 形态，生成含 tls/transport 的 outbound。"""
    import base64

    payload = {
        "v": "2",
        "ps": "HK",
        "add": "hk.example.com",
        "port": "443",
        "id": "a1b2c3d4-e5f6-4a7b-8c9d-0e1f2a3b4c5d",
        "aid": "0",
        "scy": "auto",
        "net": "ws",
        "type": "none",
        "host": "hk.example.com",
        "path": "/ws",
        "tls": "tls",
        "sni": "hk.example.com",
    }
    uri = "vmess://" + base64.b64encode(json.dumps(payload).encode()).decode()
    node = parse_node_uri(uri)
    assert node is not None
    assert node["type"] == "vmess"
    assert node["server"] == "hk.example.com"
    assert node["server_port"] == 443
    assert node["uuid"] == "a1b2c3d4-e5f6-4a7b-8c9d-0e1f2a3b4c5d"
    assert node["tls"]["enabled"]
    assert node["tls"]["server_name"] == "hk.example.com"
    assert node["transport"]["type"] == "ws"
    assert node["transport"]["path"] == "/ws"


def test_parse_hysteria2_uri():
    """hysteria2:// 官方 URI 形态：auth 即 password，生成含 tls 的 outbound。"""
    uri = "hysteria2://secret@hy.example.com:8443/?sni=hy.example.com&insecure=1#HY2"
    node = parse_node_uri(uri)
    assert node is not None
    assert node["type"] == "hysteria2"
    assert node["server"] == "hy.example.com"
    assert node["server_port"] == 8443
    assert node["password"] == "secret"
    assert node["tls"]["enabled"]
    assert node["tls"]["server_name"] == "hy.example.com"
    assert node["tls"]["insecure"]


def test_parse_tuic_uri():
    """tuic:// 形态：uuid:password@host:port，生成含 tls 与 congestion_control 的 outbound。"""
    uri = (
        "tuic://a1b2c3d4-e5f6-4a7b-8c9d-0e1f2a3b4c5d:pw@tu.example.com:443/"
        "?sni=tu.example.com&congestion_control=bbr&alpn=h3#TUIC"
    )
    node = parse_node_uri(uri)
    assert node is not None
    assert node["type"] == "tuic"
    assert node["server"] == "tu.example.com"
    assert node["server_port"] == 443
    assert node["uuid"] == "a1b2c3d4-e5f6-4a7b-8c9d-0e1f2a3b4c5d"
    assert node["password"] == "pw"
    assert node["congestion_control"] == "bbr"
    assert node["tls"]["enabled"]
    assert node["tls"]["alpn"] == ["h3"]


def test_parse_singbox_json_rejects_local_socks():
    """socks/http 指向本地出口：明确拒绝并说明原因（那是被删掉的 BYO）。"""
    text = json.dumps(
        {
            "outbounds": [
                {
                    "type": "socks",
                    "tag": "s",
                    "server": "127.0.0.1",
                    "server_port": 1080,
                },
                {
                    "type": "http",
                    "tag": "h",
                    "server": "localhost",
                    "server_port": 8080,
                },
            ]
        }
    )
    nodes, errors, _ = parse_singbox_json(text)
    assert nodes == []
    assert len(errors) == 2
    assert all("本地" in e for e in errors), errors


def test_parse_single_node_accepts_json_and_uri():
    """单节点输入：{ 开头走 JSON（取 outbounds 第一条），否则走 URI。"""
    json_text = json.dumps(
        {
            "outbounds": [
                {
                    "type": "trojan",
                    "tag": "j1",
                    "server": "a.com",
                    "server_port": 443,
                    "password": "p",
                }
            ]
        }
    )
    node = parse_single_node(json_text)
    assert node is not None
    assert node["type"] == "trojan"
    assert node["server"] == "a.com"
    uri_node = parse_single_node("trojan://pw@b.com:443#B")
    assert uri_node is not None
    assert uri_node["type"] == "trojan"
    assert uri_node["server"] == "b.com"


def test_parse_singbox_json_accepts_bare_outbound():
    """裸单出站（{"type":"naive",...}，无 outbounds 外壳）应被当作单元素列表。"""
    text = json.dumps(
        {
            "type": "naive",
            "tag": "n",
            "server": "a.com",
            "server_port": 443,
            "username": "u",
            "password": "p",
            "tls": {"enabled": True, "server_name": "a.com"},
        }
    )
    nodes, errors, _ = parse_singbox_json(text)
    assert len(nodes) == 1, errors
    assert nodes[0]["type"] == "naive"


def test_parse_single_node_accepts_bare_outbound():
    """单节点输入也接受裸单出站（与导入路径一致）。"""
    text = json.dumps(
        {
            "type": "naive",
            "tag": "n",
            "server": "a.com",
            "server_port": 443,
            "username": "u",
            "password": "p",
            "tls": {"enabled": True, "server_name": "a.com"},
        }
    )
    node = parse_single_node(text)
    assert node is not None
    assert node["type"] == "naive"


def test_parse_singbox_json_accepts_naiveproxy_config():
    """naiveproxy 客户端配置（{"listen":…,"proxy":"https://user:pass@host:port"}）→ naive 出站。"""
    text = json.dumps(
        {
            "listen": "socks://127.0.0.1:1080",
            "proxy": "https://user:pw@v6.example.com:443",
        }
    )
    nodes, errors, _ = parse_singbox_json(text)
    assert len(nodes) == 1, errors
    node = nodes[0]
    assert node["type"] == "naive"
    assert node["server"] == "v6.example.com"
    assert node["server_port"] == 443
    assert node["username"] == "user"
    assert node["password"] == "pw"
    assert node["tls"]["enabled"]
    assert node["tls"]["server_name"] == "v6.example.com"


def test_parse_single_node_accepts_naiveproxy_config():
    """单节点输入也接受 naiveproxy 客户端配置（与导入路径一致）。"""
    text = json.dumps(
        {
            "listen": "socks://127.0.0.1:1080",
            "proxy": "https://user:pw@v6.example.com:443",
        }
    )
    node = parse_single_node(text)
    assert node is not None
    assert node["type"] == "naive"
    assert node["server"] == "v6.example.com"
