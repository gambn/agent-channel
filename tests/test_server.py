import json
import time
import urllib.error
import urllib.request
from unittest.mock import patch

from agent_channel.config import channel_state, load_config, save_config
from agent_channel.server import launch_ui
from agent_channel.utils import get_free_port


def _launch_panel(port: int) -> None:
    """测试里启动面板。

    打桩自愈：不这么做，测试会真的去查端口与进程、必要时 taskkill 本机正在
    运行的 sing-box —— 测试不该有这样的真实副作用（开发者本机跑一次测试就
    可能把正在用的通道杀掉）。
    """
    with patch("agent_channel.server.heal_stale_state", lambda _port: []):
        launch_ui(port=port, open_browser=False, block=False)


def _slots(source: str, mode: str = "", node_key: str | None = None) -> dict:
    """构造完整三槽 outbound_selections，指定 source 槽设置选择，其余槽未选择。"""
    return {
        s: (
            {"mode": mode, "node_key": node_key}
            if s == source
            else {"mode": "", "node_key": None}
        )
        for s in ("node", "sub", "json")
    }


def _get(path: str, base: str, timeout: float = 5.0) -> tuple[int, str, str]:
    """GET 请求，返回 (status, body, content_type)。"""

    def _fetch() -> tuple[int, str, str]:
        with urllib.request.urlopen(f"{base}{path}", timeout=timeout) as resp:
            return (
                resp.status,
                resp.read().decode("utf-8", errors="replace"),
                resp.headers.get("Content-Type", ""),
            )

    try:
        return _fetch()
    except urllib.error.HTTPError as e:
        return e.code, "", ""
    except Exception as exc:
        raise AssertionError(f"请求 {path} 失败: {exc}") from exc


def _get_json(path: str, base: str) -> dict:
    status, body, _ = _get(path, base)
    assert status == 200, f"{path} 返回 {status}"
    try:
        return json.loads(body)
    except ValueError as exc:
        raise AssertionError(f"{path} 返回非 JSON: {exc}") from exc


def _post_json(path: str, base: str, payload: dict) -> dict:
    req = urllib.request.Request(
        f"{base}{path}",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, ValueError) as exc:
        raise AssertionError(f"POST {path} 失败: {exc}") from exc


def _post_status(path: str, base: str, payload: dict) -> tuple[int, dict]:
    """POST 返回 (status, body)，用于断言 400 等错误响应。"""
    req = urllib.request.Request(
        f"{base}{path}",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode("utf-8"))
    except (urllib.error.URLError, ValueError) as exc:
        raise AssertionError(f"POST {path} 失败: {exc}") from exc


def _get_status(path: str, base: str) -> tuple[int, str]:
    """GET 返回 (status, body)，用于断言 400 等错误响应（保留错误 body）。"""
    try:
        with urllib.request.urlopen(f"{base}{path}", timeout=5) as resp:
            return resp.status, resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", errors="replace")
    except urllib.error.URLError as exc:
        raise AssertionError(f"GET {path} 失败: {exc}") from exc


def test_server_endpoints():
    test_port = get_free_port()
    _launch_panel(test_port)
    time.sleep(0.3)
    base = f"http://127.0.0.1:{test_port}"

    # 1. 首页 HTML 服务于更名后的标题
    status, html, ctype = _get("/", base)
    assert status == 200
    assert "text/html" in ctype
    assert "agent-channel通道代理" in html
    assert "极轻量·零驱动·无特权 AI 通道管理模块" not in html
    assert "同步为本地系统代理（开启 Chrome / Edge 浏览器支持）" in html
    # 前端已拆为外部静态文件，不再内联
    assert 'href="style.css"' in html
    assert 'src="app.js"' in html

    # 2. 静态资源可访问且 MIME 正确
    for path, expect in (
        ("/style.css", "text/css"),
        ("/app.js", "javascript"),
        ("/logo.svg", "svg"),
    ):
        status, body, ctype = _get(path, base)
        assert status == 200
        assert expect in ctype
        assert body

    # 3. 白名单之外的路径一律 404，不读取任意文件
    for path in ("/server.py", "/config.py", "/../launch_service.py"):
        status, _, _ = _get(path, base)
        assert status == 404, f"{path} 返回 {status}"

    # 4. API 端点
    st = _get_json("/api/status", base)
    assert "status" in st
    # 状态卡只读显示当前来源：/api/status 必须带回 source + source_label
    assert "source" in st and "source_label" in st, st
    assert len(_get_json("/api/uuid", base).get("uuid", "")) == 36
    assert "local_version" in _get_json("/api/core_info", base)

    # 5. 配置读写：先备份，测完还原，避免污染真实配置
    original = load_config()
    try:
        assert _post_json(
            "/api/config", base, {"node_uri": "vless://example.invalid:443"}
        ).get("success")
        saved = _get_json("/api/config", base)
        assert saved.get("node_uri") == "vless://example.invalid:443"
    finally:
        save_config(original)

    # 6. /api/enable 是“启用”，不是“改配置”：只传一个字段不能把别的字段清成空串。
    #    原先它无条件写这些字段（缺值即空），会把已配好的订阅清掉 —— 真发生过。
    backup = load_config()
    try:
        save_config(
            {
                "active_source": "sub",
                "subscription_url": "https://example.invalid/sub",
            }
        )
        with patch(
            "agent_channel.server.enable_channel",
            lambda **kwargs: {"status": "running", "mode": "standalone"},
        ):
            assert _post_json(
                "/api/enable",
                base,
                {"source": "node", "node_uri": "trojan://pw@example.invalid:443#N"},
            ).get("status")
        after = load_config()
        assert after.get("subscription_url") == "https://example.invalid/sub", after
        assert after.get("node_uri") == "trojan://pw@example.invalid:443#N", after
    finally:
        save_config(backup)


def test_benchmark_measures_current_source():
    """POST /api/benchmark 测“当前来源”：source=node 时只测单节点，不跨来源并导入节点。"""
    test_port = get_free_port()
    _launch_panel(test_port)
    time.sleep(0.3)
    base = f"http://127.0.0.1:{test_port}"

    backup = load_config()
    try:
        save_config(
            {
                "active_source": "node",
                "node_uri": "trojan://pw@example.invalid:443#N",
                "subscription_url": "",
                "imported_nodes": [
                    {
                        "type": "vless",
                        "tag": "imp",
                        "server": "127.0.0.1",
                        "server_port": 1,
                        "uuid": "secret",
                    }
                ],
            }
        )
        with patch("agent_channel.server.benchmark_nodes") as mock_bn:
            mock_bn.return_value = [
                {
                    "tag": "x",
                    "type": "trojan",
                    "server": "example.invalid",
                    "server_port": 443,
                    "tcp_ms": 12.0,
                    "tcp_reachable": True,
                    "forwarding": True,
                }
            ]
            data = _post_json("/api/benchmark", base, {})
            assert "results" in data
            kwargs = mock_bn.call_args.kwargs
            nodes = kwargs.get("nodes") or []
            servers = {n.get("server") for n in nodes}
            # 单节点来源：只有那一个，不含导入节点（不跨来源）
            assert servers == {"example.invalid"}, nodes
            assert not kwargs.get("node_uri")
            assert not kwargs.get("subscription_url")
    finally:
        save_config(backup)


def test_benchmark_caches_latency_into_outbound_options():
    """测速后延迟缓存进进程内，outbound_options 每项带 latency_ms（auto 项=本批最快），且不含凭据。"""
    test_port = get_free_port()
    _launch_panel(test_port)
    time.sleep(0.3)
    base = f"http://127.0.0.1:{test_port}"

    backup = load_config()
    imp = [
        {
            "type": "naive",
            "tag": "n1",
            "server": "a.com",
            "server_port": 443,
            "username": "u",
            "password": "secret-pw",
        },
        {
            "type": "vless",
            "tag": "n2",
            "server": "b.com",
            "server_port": 443,
            "uuid": "secret-uuid",
        },
    ]
    try:
        save_config(
            {
                "active_source": "json",
                "imported_nodes": imp,
                "outbound_selections": _slots("json", "auto"),
            }
        )
        with patch("agent_channel.server.benchmark_nodes") as mock_bn:
            mock_bn.return_value = [
                {
                    "tag": "n1",
                    "type": "naive",
                    "server": "a.com",
                    "server_port": 443,
                    "tcp_ms": 173.0,
                    "tcp_reachable": True,
                    "forwarding": True,
                },
                {
                    "tag": "n2",
                    "type": "vless",
                    "server": "b.com",
                    "server_port": 443,
                    "tcp_ms": 80.0,
                    "tcp_reachable": True,
                    "forwarding": True,
                },
            ]
            _post_json("/api/benchmark", base, {})

        data = _get_json("/api/outbound_options", base)
        assert data.get("source") == "json", data
        assert data.get("available_nodes") == 2, data
        opts = data.get("options", [])
        auto = next(o for o in opts if o.get("key") == "auto")
        n1 = next(o for o in opts if o.get("key") == "naive|a.com|443")
        n2 = next(o for o in opts if o.get("key") == "vless|b.com|443")
        assert auto.get("latency_ms") == 80.0, auto
        assert auto.get("fastest_tag") == "n2", auto
        assert n1.get("latency_ms") == 173.0, n1
        assert n2.get("latency_ms") == 80.0, n2
        # 红线：不含凭据
        raw = json.dumps(data)
        assert "secret-pw" not in raw and "secret-uuid" not in raw
    finally:
        save_config(backup)


def test_benchmark_uses_requested_source_not_active():
    """benchmark 按请求的 source 测，而非 active_source——
    在单节点标签启用过、切到 JSON/订阅标签再测速，不能测回旧来源。"""
    test_port = get_free_port()
    _launch_panel(test_port)
    time.sleep(0.3)
    base = f"http://127.0.0.1:{test_port}"

    backup = load_config()
    imp = [
        {"type": "vless", "tag": "j1", "server": "j1.example", "server_port": 443},
        {"type": "vless", "tag": "j2", "server": "j2.example", "server_port": 443},
    ]
    try:
        save_config(
            {
                "active_source": "node",
                "node_uri": "trojan://pw@node.example:443#N",
                "subscription_url": "",
                "imported_nodes": imp,
            }
        )
        with patch("agent_channel.server.benchmark_nodes") as mock_bn:
            mock_bn.return_value = []
            # 请求 source=json：应测 json 槽，不是 active_source=node
            data = _post_json("/api/benchmark", base, {"source": "json"})
            servers = {
                n.get("server") for n in (mock_bn.call_args.kwargs.get("nodes") or [])
            }
            assert servers == {"j1.example", "j2.example"}, servers
            # 响应回带 source：面板据此显示“来源：JSON”
            assert data.get("source") == "json", data
        with patch("agent_channel.server.benchmark_nodes") as mock_bn:
            mock_bn.return_value = []
            # 不传 source：回落 active_source=node
            _post_json("/api/benchmark", base, {})
            servers = {
                n.get("server") for n in (mock_bn.call_args.kwargs.get("nodes") or [])
            }
            assert servers == {"node.example"}, servers
    finally:
        save_config(backup)


def test_static_assets_are_not_cached():
    """面板静态资源必须禁缓存：否则升级后浏览器仍用旧 app.js（旧前端发不出 source），
    会出现“新后端 + 旧前端”的怪故障。"""
    import urllib.request

    test_port = get_free_port()
    _launch_panel(test_port)
    time.sleep(0.3)
    base = f"http://127.0.0.1:{test_port}"
    with urllib.request.urlopen(f"{base}/app.js", timeout=5) as resp:
        cc = resp.headers.get("Cache-Control", "")
    assert "no-cache" in cc or "no-store" in cc, cc


def test_record_benchmark_latency_fastest_skips_forwarding_failed():
    """ping 最短但转发失败的节点不能当「本批最快」。"""
    from agent_channel import server

    server._LAST_FASTEST.clear()
    server._record_benchmark_latency(
        [
            {
                "tag": "argo",
                "type": "vless",
                "server": "a.com",
                "server_port": 443,
                "tcp_ms": 30.0,
                "forwarding": False,
            },
            {
                "tag": "good",
                "type": "vless",
                "server": "b.com",
                "server_port": 443,
                "tcp_ms": 200.0,
                "forwarding": True,
            },
        ],
        "json",
    )
    ms, tag = server._LAST_FASTEST["json"]
    assert tag == "good", f"got ms={ms} tag={tag}"
    assert ms == 200.0
    # 全部转发失败：最快为空（宁可不显示也不撒谎）
    server._record_benchmark_latency(
        [
            {
                "tag": "x",
                "type": "vless",
                "server": "c.com",
                "server_port": 443,
                "tcp_ms": 10.0,
                "forwarding": False,
            }
        ],
        "json",
    )
    empty_ms, empty_tag = server._LAST_FASTEST["json"]
    assert empty_ms is None and empty_tag is None


def test_latency_cache_is_bounded():
    """测速延迟缓存必须有上限：键是节点身份，长期反复换节点/订阅只会越堆越多。

    回归背景：此前这张表没有上限，面板常驻时它只增不减。超限采取"整表清空"——
    它只是下拉里的展示缓存，下一轮测速会重新填，清掉没有正确性代价。
    """
    from agent_channel import server

    saved = dict(server._LATENCY_CACHE)
    server._LATENCY_CACHE.clear()
    try:
        server._record_benchmark_latency(
            [
                {
                    "tcp_ms": float(i),
                    "type": "vless",
                    "server": f"n{i}.invalid",
                    "server_port": 443,
                    "forwarding": True,
                    "tag": f"t{i}",
                }
                for i in range(server._LATENCY_CACHE_MAX + 1)
            ],
            "json",
        )
        assert len(server._LATENCY_CACHE) <= server._LATENCY_CACHE_MAX, (
            f"缓存超限未收敛：{len(server._LATENCY_CACHE)}"
        )
    finally:
        server._LATENCY_CACHE.clear()
        server._LATENCY_CACHE.update(saved)


def test_record_benchmark_latency_fastest_skips_unverified():
    """**未验证**转发（forwarding=None，如内核不在、没跑转发检查）也不能当最快。
    auto 项标签写的是“按延迟 + 转发验证挑最优”，未验证就不该说最优。"""
    from agent_channel import server

    server._LAST_FASTEST.clear()
    server._record_benchmark_latency(
        [
            {
                "tag": "unverified",
                "type": "vless",
                "server": "a.com",
                "server_port": 443,
                "tcp_ms": 20.0,
                "forwarding": None,
            },
            {
                "tag": "good",
                "type": "vless",
                "server": "b.com",
                "server_port": 443,
                "tcp_ms": 100.0,
                "forwarding": True,
            },
        ],
        "json",
    )
    ms, tag = server._LAST_FASTEST["json"]
    assert tag == "good", f"got ms={ms} tag={tag}"
    # 全部未验证：最快为空
    server._record_benchmark_latency(
        [
            {
                "tag": "x",
                "type": "vless",
                "server": "c.com",
                "server_port": 443,
                "tcp_ms": 5.0,
                "forwarding": None,
            }
        ],
        "json",
    )
    empty_ms, empty_tag = server._LAST_FASTEST["json"]
    assert empty_ms is None and empty_tag is None


def test_import_nodes_adds_and_reports():
    """导入 JSON：2 可用 + 1 direct + 1 缺字段 → 2 导入、2 跳过（带原因），响应不含凭据。"""
    test_port = get_free_port()
    _launch_panel(test_port)
    time.sleep(0.3)
    base = f"http://127.0.0.1:{test_port}"

    backup = load_config()
    try:
        save_config({"imported_nodes": []})
        text = json.dumps(
            {
                "outbounds": [
                    {
                        "type": "vless",
                        "tag": "n1",
                        "server": "127.0.0.1",
                        "server_port": 1,
                        "uuid": "secret-uuid",
                    },
                    {
                        "type": "trojan",
                        "tag": "n2",
                        "server": "127.0.0.1",
                        "server_port": 2,
                        "password": "secret-pw",
                    },
                    {"type": "direct", "tag": "direct"},
                    {
                        "type": "vless",
                        "tag": "bad",
                        "server": "127.0.0.1",
                        "server_port": 3,
                    },
                ]
            }
        )
        data = _post_json("/api/import_nodes", base, {"text": text})
        assert data["imported"] == 2, data
        assert len(data["errors"]) == 2, data
        assert any("direct" in e for e in data["errors"])
        assert any("uuid" in e for e in data["errors"])
        raw = json.dumps(data)
        assert "secret-uuid" not in raw
        assert "secret-pw" not in raw
        assert len(load_config()["imported_nodes"]) == 2
        # 红线：/api/config 响应不含 imported_nodes（凭据不回前端）
        cfg_resp = _get_json("/api/config", base)
        assert "imported_nodes" not in cfg_resp
    finally:
        save_config(backup)


def test_import_nodes_clear():
    """清空导入节点：导入后 clear，imported_nodes 归零。"""
    test_port = get_free_port()
    _launch_panel(test_port)
    time.sleep(0.3)
    base = f"http://127.0.0.1:{test_port}"

    backup = load_config()
    try:
        save_config(
            {
                "imported_nodes": [
                    {
                        "type": "vless",
                        "tag": "x",
                        "server": "127.0.0.1",
                        "server_port": 1,
                        "uuid": "u",
                    }
                ]
            }
        )
        data = _post_json("/api/import_nodes", base, {"action": "clear"})
        assert data.get("cleared")
        assert load_config()["imported_nodes"] == []
    finally:
        save_config(backup)


def test_import_nodes_deduplicates():
    """追加导入按 type+server+server_port 去重，不重复添加。"""
    test_port = get_free_port()
    _launch_panel(test_port)
    time.sleep(0.3)
    base = f"http://127.0.0.1:{test_port}"

    backup = load_config()
    try:
        save_config(
            {
                "imported_nodes": [
                    {
                        "type": "vless",
                        "tag": "n1",
                        "server": "a.com",
                        "server_port": 443,
                        "uuid": "u",
                    }
                ]
            }
        )
        text = json.dumps(
            {
                "outbounds": [
                    {
                        "type": "vless",
                        "tag": "n1-dup",
                        "server": "a.com",
                        "server_port": 443,
                        "uuid": "u",
                    },
                    {
                        "type": "trojan",
                        "tag": "n2",
                        "server": "b.com",
                        "server_port": 443,
                        "password": "p",
                    },
                ]
            }
        )
        data = _post_json("/api/import_nodes", base, {"text": text})
        assert data["imported"] == 1, data
        assert data["duplicates"] == 1, data
        assert len(load_config()["imported_nodes"]) == 2
    finally:
        save_config(backup)


def test_enable_respects_pinned_outbound():
    """池来源指定节点：/api/enable 只把指定节点交给 enable_channel（不并池）。"""
    test_port = get_free_port()
    _launch_panel(test_port)
    time.sleep(0.3)
    base = f"http://127.0.0.1:{test_port}"

    backup = load_config()
    a = {"type": "vless", "tag": "a", "server": "a.com", "server_port": 443}
    b = {"type": "vless", "tag": "b", "server": "b.com", "server_port": 443}
    try:
        save_config(
            {
                "active_source": "json",
                "imported_nodes": [a, b],
                "outbound_selections": _slots("json", "pinned", "vless|b.com|443"),
            }
        )
        with patch("agent_channel.server.enable_channel") as mock_en:
            mock_en.return_value = {"status": "running", "mode": "standalone"}
            _post_json("/api/enable", base, {"source": "json", "set_sys_proxy": False})
            kwargs = mock_en.call_args.kwargs
            assert kwargs.get("node") == b, kwargs
            assert kwargs.get("nodes") is None, kwargs
    finally:
        save_config(backup)


def test_enable_unselected_errors():
    """池来源未选择出站：话术必须与状态卡（channel_state.message）一致，不动 enable_channel。"""
    test_port = get_free_port()
    _launch_panel(test_port)
    time.sleep(0.3)
    base = f"http://127.0.0.1:{test_port}"

    backup = load_config()
    a = {"type": "vless", "tag": "a", "server": "a.com", "server_port": 443}
    b = {"type": "vless", "tag": "b", "server": "b.com", "server_port": 443}
    try:
        save_config(
            {
                "active_source": "json",
                "imported_nodes": [a, b],
                "outbound_selections": _slots("json", ""),
            }
        )
        with patch("agent_channel.server.enable_channel") as mock_en:
            status, data = _post_status(
                "/api/enable", base, {"source": "json", "set_sys_proxy": False}
            )
            assert status == 400, data
            # 同一状态不许两套说法：面板启用被拒的文案 == 状态卡的 message
            assert (
                data.get("error") == channel_state(config=load_config())["message"]
            ), data
            assert not mock_en.called
    finally:
        save_config(backup)


def test_enable_dead_pinned_errors():
    """指定节点已失效：话术必须与状态卡一致，不换别人。"""
    test_port = get_free_port()
    _launch_panel(test_port)
    time.sleep(0.3)
    base = f"http://127.0.0.1:{test_port}"

    backup = load_config()
    a = {"type": "vless", "tag": "a", "server": "a.com", "server_port": 443}
    b = {"type": "vless", "tag": "b", "server": "b.com", "server_port": 443}
    try:
        save_config(
            {
                "active_source": "json",
                "imported_nodes": [a, b],
                "outbound_selections": _slots("json", "pinned", "vless|gone.com|443"),
            }
        )
        with patch("agent_channel.server.enable_channel") as mock_en:
            status, data = _post_status(
                "/api/enable", base, {"source": "json", "set_sys_proxy": False}
            )
            assert status == 400, data
            assert (
                data.get("error") == channel_state(config=load_config())["message"]
            ), data
            assert not mock_en.called
    finally:
        save_config(backup)


def test_enable_requires_explicit_source_no_fallback():
    """启用必须以请求为准，禁止回落。无 source（输入框为空）必须拒绝并说明原因，
    不得静默用 imported_nodes 里的老来源启动。"""
    test_port = get_free_port()
    _launch_panel(test_port)
    time.sleep(0.3)
    base = f"http://127.0.0.1:{test_port}"

    backup = load_config()
    try:
        save_config(
            {
                "active_source": "json",
                "imported_nodes": [
                    {"type": "vless", "server": "a.com", "server_port": 443}
                ],
            }
        )
        with patch("agent_channel.server.enable_channel") as mock_en:
            status, data = _post_status("/api/enable", base, {})
            assert status == 400, data
            assert "请先在面板里选择来源" in data.get("error", ""), data
            assert not mock_en.called
            # 有 source 但对应地址为空也要拒绝（node 槽无 node_uri）
            status, data = _post_status("/api/enable", base, {"source": "node"})
            assert status == 400, data
            assert not mock_en.called
    finally:
        save_config(backup)


def test_failed_enable_keeps_active_source():
    """启用失败不得改写生效来源。

    回归背景：更早的实现先把 active_source 写盘、再跑 channel_state 校验 —— 于是在空的
    json 标签点「启动」，会先把生效来源改成 json，转头才报 400「未配置节点」：用户什么
    都没做成，配置却已经变了。校验通过才落盘，失败即原样不动。
    """
    test_port = get_free_port()
    _launch_panel(test_port)
    time.sleep(0.3)
    base = f"http://127.0.0.1:{test_port}"

    backup = load_config()
    try:
        save_config(
            {
                "active_source": "sub",
                "subscription_url": "https://example.invalid/sub",
                "imported_nodes": [],
            }
        )
        with patch("agent_channel.server.enable_channel") as mock_en:
            status, data = _post_status(
                "/api/enable", base, {"source": "json", "set_sys_proxy": False}
            )
            assert status == 400, data
            assert not mock_en.called
        assert load_config().get("active_source") == "sub", "失败的启用改了生效来源"
    finally:
        save_config(backup)


def test_outbound_options_and_select():
    """候选项只给摘要（不含凭据）；选中节点 → json 槽 pinned；可切回 auto。"""
    test_port = get_free_port()
    _launch_panel(test_port)
    time.sleep(0.3)
    base = f"http://127.0.0.1:{test_port}"

    backup = load_config()
    imp = [
        {
            "type": "naive",
            "tag": "n1",
            "server": "a.com",
            "server_port": 443,
            "username": "u",
            "password": "secret-pw",
        },
        {"type": "vless", "tag": "n2", "server": "b.com", "server_port": 443},
    ]
    try:
        # 清掉订阅/单节点，避免 /api/outbound_options 真去拉订阅（测试要离线自洽）
        save_config(
            {
                "active_source": "json",
                "imported_nodes": imp,
                "outbound_selections": _slots("json", "auto"),
            }
        )
        data = _get_json("/api/outbound_options", base)
        assert data.get("source") == "json", data
        assert data.get("available_nodes") == 2, data
        opts = data.get("options", [])
        assert any(o.get("key") == "auto" for o in opts), opts
        assert any(
            o.get("key") == "naive|a.com|443" and o.get("tag") == "n1" for o in opts
        ), opts
        raw = json.dumps(data)
        assert "secret-pw" not in raw, "候选项不得含凭据"

        data = _post_json(
            "/api/set_outbound", base, {"mode": "pinned", "node_key": "naive|a.com|443"}
        )
        assert data.get("success"), data
        cfg = load_config()
        assert cfg["outbound_selections"]["json"]["mode"] == "pinned", cfg
        assert cfg["outbound_selections"]["json"]["node_key"] == "naive|a.com|443", cfg

        assert _post_json("/api/set_outbound", base, {"mode": "auto"}).get("success")
        assert load_config()["outbound_selections"]["json"]["mode"] == "auto"
        assert load_config()["outbound_selections"]["json"]["node_key"] is None
    finally:
        save_config(backup)


def test_outbound_options_single_node_is_readonly():
    """单节点来源（1 个可用节点）：无 auto 项、只读显示那一个节点，selected=pinned。"""
    test_port = get_free_port()
    _launch_panel(test_port)
    time.sleep(0.3)
    base = f"http://127.0.0.1:{test_port}"

    backup = load_config()
    try:
        save_config(
            {
                "active_source": "node",
                "node_uri": "trojan://pw@a.com:443#N",
                "subscription_url": "",
                "imported_nodes": [],
            }
        )
        data = _get_json("/api/outbound_options", base)
        assert data.get("source") == "node", data
        assert data.get("available_nodes") == 1, data
        opts = data.get("options", [])
        assert len(opts) == 1, opts
        assert opts[0].get("key") == "trojan|a.com|443", opts
        assert "auto" not in [o.get("key") for o in opts], opts
        assert data.get("selected") == {
            "mode": "pinned",
            "node_key": "trojan|a.com|443",
        }, data
    finally:
        save_config(backup)


def test_geolocation_requires_running_channel():
    """通道未运行：探测落地 IP 返回 400「通道未运行」。"""
    test_port = get_free_port()
    _launch_panel(test_port)
    time.sleep(0.3)
    base = f"http://127.0.0.1:{test_port}"

    with patch("agent_channel.server.get_channel_status") as mock_gs:
        mock_gs.return_value = {"status": "stopped", "proxy_url": None}
        status, body = _get_status("/api/geolocation", base)
        assert status == 400, body
        # body 必须是 JSON：json.loads 抛错就让测试失败，这正是期望
        assert "通道未运行" in json.loads(body).get("error", ""), body


def test_geolocation_returns_geo_via_proxy():
    """通道运行：走代理探测落地，返回城市/国家/ISP/天气。"""
    test_port = get_free_port()
    _launch_panel(test_port)
    time.sleep(0.3)
    base = f"http://127.0.0.1:{test_port}"

    with (
        patch("agent_channel.server.get_channel_status") as mock_gs,
        patch("agent_channel.server.geolocate_via_proxy") as mock_geo,
    ):
        mock_gs.return_value = {
            "status": "running",
            "proxy_url": "http://127.0.0.1:20849",
        }
        mock_geo.return_value = {
            "ip": "198.51.100.7",
            "city": "示例城",
            "region": "示例地区",
            "country": "示例国",
            "isp": "示例ISP",
            "weather": {"temp_c": 23.5, "code": 3},
        }
        data = _get_json("/api/geolocation", base)
        assert data.get("city") == "示例城", data
        assert data.get("country") == "示例国", data
        assert data.get("weather", {}).get("temp_c") == 23.5, data
        mock_geo.assert_called_once_with("http://127.0.0.1:20849")


# ---------------------------------------------------------------- 写接口守卫
def _raw_post(base: str, path: str, body: bytes, headers: dict | None = None) -> int:
    """裸 socket POST，返回状态码。

    用裸 socket 而不是 urllib：要**精确构造** Origin / Content-Type / Content-Length，
    包括“声明 9 MiB 但只发一小段”这种 urllib 造不出来的形态。
    """
    import socket

    host, _, port = base.removeprefix("http://").partition(":")
    # 请求行必须有：漏了它，http.server 会把第一个头（Host:）当成方法名。
    # Host 也放进 head，再被调用方的 headers 覆盖 —— 模拟“用户手输 localhost:port 打开面板”时，
    # 浏览器发的 Host 与 Origin 都是 localhost:port，而不是 127.0.0.1。
    head = {
        "Host": f"{host}:{port}",
        "Connection": "close",
        "Content-Type": "application/json",
        "Content-Length": str(len(body)),
    }
    head.update(headers or {})
    lines = [f"POST {path} HTTP/1.1"]
    lines += [f"{key}: {value}" for key, value in head.items()]
    request = ("\r\n".join(lines) + "\r\n\r\n").encode() + body
    port_num = int(port)
    with socket.create_connection((host, port_num), timeout=5) as sock:
        sock.sendall(request)
        first = sock.recv(512)
    # 解析失败就该把测试搞红，不抑不掩
    return int(first.split(b" ")[1])


def test_post_rejects_cross_site_origin():
    """带外站 Origin 的写请求必须 403。

    这道挡的是“任意网页向 127.0.0.1 端口发简单请求”——浏览器不预检这类请求，
    而写接口能停通道、改订阅、换内核，副作用当场就发生。
    """
    test_port = get_free_port()
    _launch_panel(test_port)
    time.sleep(0.3)
    base = f"http://127.0.0.1:{test_port}"
    status = _raw_post(
        base,
        "/api/disable",
        b"{}",
        {"Content-Type": "application/json", "Origin": "https://evil.example"},
    )
    assert status == 403, f"外站 Origin 应 403，实际 {status}"


def test_post_rejects_form_encoded_body():
    """非 application/json 的写请求必须 415（表单编码的跨站请求走这条路）。"""
    test_port = get_free_port()
    _launch_panel(test_port)
    time.sleep(0.3)
    base = f"http://127.0.0.1:{test_port}"
    status = _raw_post(
        base,
        "/api/disable",
        b"action=stop",
        {"Content-Type": "application/x-www-form-urlencoded"},
    )
    assert status == 415, f"表单编码应 415，实际 {status}"


def test_post_rejects_oversize_body():
    """声明超过上限的 body 必须 413，且**不先把 body 读完**。

    只发头部、body 留空：服务端应当在校验 Content-Length 时就回错。
    """
    test_port = get_free_port()
    _launch_panel(test_port)
    time.sleep(0.3)
    base = f"http://127.0.0.1:{test_port}"
    status = _raw_post(
        base,
        "/api/disable",
        b"",
        {
            "Content-Type": "application/json",
            "Content-Length": str(9 * 1024 * 1024),
        },
    )
    assert status == 413, f"超大 body 应 413，实际 {status}"


def test_post_rejects_bad_json():
    """坏 JSON 必须 400 —— 旧行为是兜底成空 `{}` 继续执行。"""
    test_port = get_free_port()
    _launch_panel(test_port)
    time.sleep(0.3)
    base = f"http://127.0.0.1:{test_port}"
    status = _raw_post(
        base, "/api/disable", b"{not json", {"Content-Type": "application/json"}
    )
    assert status == 400, f"坏 JSON 应 400，实际 {status}"


def test_post_allows_localhost_origin_and_no_origin():
    """手输 `localhost:18888` 打开面板时同源 Origin 是 localhost —— 必须放行；
    非浏览器调用（无 Origin，如 curl / CLI / 托盘 / 守护）也必须放行。

    这条是“不能把正常路径一起挡掉”的反向守卫：按 Host 归一化比对、而不是写死
    `http://127.0.0.1:<port>`，正是为了让手输 localhost 的用户不被 403。
    """
    test_port = get_free_port()
    _launch_panel(test_port)
    time.sleep(0.3)
    base = f"http://127.0.0.1:{test_port}"
    with_origin = _raw_post(
        base,
        "/api/disable",
        b"{}",
        {
            # 手输 localhost:port 打开面板时，浏览器的 Host 与 Origin 都是 localhost:port
            "Host": f"localhost:{test_port}",
            "Origin": f"http://localhost:{test_port}",
        },
    )
    assert with_origin == 200, f"localhost 同源 Origin 应放行，实际 {with_origin}"
    without_origin = _raw_post(
        base, "/api/disable", b"{}", {"Content-Type": "application/json"}
    )
    assert without_origin == 200, f"无 Origin 应放行，实际 {without_origin}"


def test_write_operations_return_409_when_channel_is_busy():
    """启停锁被别的操作占着时：写接口当场回 409，而不是挂着等它跑完。

    弱网下一次启用（订阅拉取 + 逐候选转发验证）可以长达一分多钟；阻塞排队会让面板的
    "停止"按钮几十秒没有任何反馈。这里把锁占在后台线程里，验证两个写入口都是快速拒绝。
    """
    import threading

    from agent_channel import core
    from agent_channel.config import load_config

    test_port = get_free_port()
    _launch_panel(test_port)
    time.sleep(0.3)
    base = f"http://127.0.0.1:{test_port}"

    holding = threading.Event()
    release = threading.Event()

    def holder() -> None:
        with core._CHANNEL_LOCK:
            holding.set()
            release.wait(timeout=10)

    thread = threading.Thread(target=holder)
    thread.start()
    try:
        assert holding.wait(timeout=5), "后台线程没能拿到启停锁"

        status, data = _post_status("/api/disable", base, {})
        assert status == 409, (status, data)
        assert "进行中" in data.get("error", ""), data

        # 启用侧同样快速拒绝，且**忙时连配置都不动**：请求带的地址不得落盘
        before = load_config()
        status, data = _post_status(
            "/api/enable",
            base,
            {"source": "node", "node_uri": "vless://should-not-land"},
        )
        assert status == 409, (status, data)
        assert "进行中" in data.get("error", ""), data
        assert load_config().get("node_uri") == before.get("node_uri"), (
            "忙被拒时不该改持久状态"
        )
    finally:
        release.set()
        thread.join(timeout=5)

    # 忙态解除后必须能正常停止：确认 409 路径没有把锁一直扣着
    status, data = _post_status("/api/disable", base, {})
    assert status == 200, (status, data)


# ---------------------------------------------------------------- 一键测试诚实性
def test_test_ai_marks_direct_when_channel_stopped():
    """通道未运行时，一键测试必须标注为直连 + `channel_running=false`。

    注意是**标注而非拒绝**：直连结果仍有诊断价值。
    不标注的后果是健康检查说谎 —— 直连成功会被读成“通道没问题”。
    """
    test_port = get_free_port()
    _launch_panel(test_port)
    time.sleep(0.3)
    base = f"http://127.0.0.1:{test_port}"
    with (
        patch("agent_channel.server.get_channel_status") as mock_gs,
        patch("agent_channel.server.test_single_endpoint") as mock_ep,
    ):
        mock_gs.return_value = {"status": "stopped"}
        mock_ep.return_value = {"reachable": True, "latency_ms": 1.0, "via": "direct"}
        data = _post_json("/api/test_ai", base, {})

    assert not data.get("channel_running"), data
    assert data.get("via") == "direct", data
    assert data.get("note"), f"应带提示语：{data}"
    for call in mock_ep.call_args_list:
        assert call.args[1] is None, f"通道未运行时应以无代理直连：{call}"


def test_test_ai_marks_channel_when_running():
    """通道运行时标注 `via=channel`，且探测确实带上通道代理地址。"""
    test_port = get_free_port()
    _launch_panel(test_port)
    time.sleep(0.3)
    base = f"http://127.0.0.1:{test_port}"
    with (
        patch("agent_channel.server.get_channel_status") as mock_gs,
        patch("agent_channel.server.test_single_endpoint") as mock_ep,
    ):
        mock_gs.return_value = {
            "status": "running",
            "proxy_url": "http://127.0.0.1:20849",
        }
        mock_ep.return_value = {"reachable": True, "latency_ms": 1.0, "via": "channel"}
        data = _post_json("/api/test_ai", base, {})

    assert data.get("channel_running"), data
    assert data.get("via") == "channel", data
    assert "note" not in data, f"通道在跑时不该出现直连提示：{data}"
    for call in mock_ep.call_args_list:
        assert call.args[1] == "http://127.0.0.1:20849", f"应带通道代理：{call}"


# ---------------------------------------------------------------- 保存失败传播
def test_save_error_reports_reason_on_failure():
    """`save_config` 失败时 `save_error()` 必须给出原因（成功时为 None）。

    背景：`save_config` 原先把所有 OSError 吞成 `return False`，而几个调用点又
    不检查返回值 —— 磁盘满或目录只读时用户得到“导入成功”，下次启用读到的还是老配置。
    """
    from pathlib import Path as _Path

    from agent_channel import config

    with patch.object(config, "config_path", lambda: _Path("Z:/nonexistent/c.json")):
        ok = config.save_config({"mode": "node"})
    assert not ok, "写不进去时必须返回 False"
    assert config.save_error(), "失败原因必须可读"


def test_import_nodes_reports_save_failure():
    """导入结果写盘失败必须 500，而不是照报“导入成功”。"""
    test_port = get_free_port()
    _launch_panel(test_port)
    time.sleep(0.3)
    base = f"http://127.0.0.1:{test_port}"
    with (
        patch("agent_channel.server.parse_singbox_json", lambda _text: ([], [], [])),
        patch("agent_channel.server.save_config", lambda _updates: False),
        patch("agent_channel.server.save_error", lambda: "磁盘只读"),
    ):
        status, body = _post_status("/api/import_nodes", base, {"text": "{}"})
    assert status == 500, f"写盘失败应 500，实际 {status} {body}"
    assert "写入失败" in body.get("error", ""), body


def test_set_outbound_reports_save_failure():
    """设出站写盘失败必须 500。"""
    test_port = get_free_port()
    _launch_panel(test_port)
    time.sleep(0.3)
    base = f"http://127.0.0.1:{test_port}"
    backup = load_config()
    try:
        save_config(
            {
                "active_source": "json",
                "imported_nodes": [
                    {"type": "vless", "server": "a.com", "server_port": 443},
                    {"type": "vless", "server": "b.com", "server_port": 443},
                ],
                "outbound_selections": _slots("json", "auto"),
            }
        )
        with (
            patch("agent_channel.server.save_config", lambda _updates: False),
            patch("agent_channel.server.save_error", lambda: "磁盘只读"),
        ):
            status, body = _post_status("/api/set_outbound", base, {"mode": "auto"})
    finally:
        save_config(backup)
    assert status == 500, f"写盘失败应 500，实际 {status} {body}"


def test_enable_aborts_when_config_write_fails():
    """启用时配置写不进去必须中止启动（400），不得拿读回的老配置继续启用。

    这条正是 Linux 真机上“老配置堵在 JSON、出站还是老配置”的机制：`_handle_enable`
    先写 payload → 再 `load_config()` 读回 → 由读回的内容决定启用什么；写失败时读回的
    是老配置，而旧代码照样往下走。
    """
    test_port = get_free_port()
    _launch_panel(test_port)
    time.sleep(0.3)
    base = f"http://127.0.0.1:{test_port}"
    with (
        patch("agent_channel.server.save_config", lambda _updates: False),
        patch("agent_channel.server.save_error", lambda: "磁盘只读"),
        patch("agent_channel.server.enable_channel") as mock_enable,
    ):
        status, body = _post_status(
            "/api/enable",
            base,
            {"source": "node", "node_uri": "vless://example.invalid:443"},
        )
    assert status == 400, f"写盘失败应中止启动并 400，实际 {status} {body}"
    assert "中止启动" in body.get("error", ""), body
    mock_enable.assert_not_called()


def test_api_config_reports_save_failure_reason():
    """`POST /api/config` 本来就检查返回值，只是把失败原因一并回给前端。"""
    test_port = get_free_port()
    _launch_panel(test_port)
    time.sleep(0.3)
    base = f"http://127.0.0.1:{test_port}"
    with (
        patch("agent_channel.server.save_config", lambda _updates: False),
        patch("agent_channel.server.save_error", lambda: "磁盘只读"),
    ):
        data = _post_json("/api/config", base, {"mode": "node"})
    assert not data.get("success"), data
    assert data.get("error") == "磁盘只读", data


def test_quit_endpoint_responds_then_shuts_down():
    """`/api/quit` 必须先回响应再退出：在请求线程里先关服务，客户端拿不到反馈。

    真正的退出（停通道/还原系统代理/关面板）走 `core.shutdown_app`，这里把它打桩，
    只验"接口被正确触发、且确实排了一次退出"。
    """
    test_port = get_free_port()
    _launch_panel(test_port)
    time.sleep(0.3)
    base = f"http://127.0.0.1:{test_port}"

    calls: list[str] = []
    with patch("agent_channel.server.shutdown_app", lambda *a, **k: calls.append("quit")):
        status, payload = _post_status("/api/quit", base, {})
        assert status == 200, status
        assert payload.get("status") == "quitting", payload
        # 退出排在后台线程里（先给响应留出时间）
        for _ in range(20):
            if calls:
                break
            time.sleep(0.1)
    assert calls == ["quit"], f"退出没有被触发: {calls}"


def test_quit_endpoint_rejects_cross_site():
    """能终止程序的接口同样受跨站守卫保护：外站 Origin 必须 403。"""
    test_port = get_free_port()
    _launch_panel(test_port)
    time.sleep(0.3)
    base = f"http://127.0.0.1:{test_port}"

    req = urllib.request.Request(
        f"{base}/api/quit",
        data=b"{}",
        headers={
            "Content-Type": "application/json",
            "Origin": "https://evil.example",
            "Referer": "https://evil.example/",
        },
    )
    with patch("agent_channel.server.shutdown_app", lambda *a, **k: None):
        try:
            urllib.request.urlopen(req, timeout=5)
        except urllib.error.HTTPError as exc:
            assert exc.code == 403, exc.code
        else:
            raise AssertionError("跨站 Origin 调 /api/quit 必须被拒")
