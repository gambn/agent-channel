"""配置文件的读写、三槽位模型迁移与损坏处理。"""

import json
import tempfile
import threading
import time
from pathlib import Path
from unittest.mock import patch

from agent_channel import config as cfgmod


def test_load_config_defaults_are_not_shared_with_module_default():
    """没配置文件时返回的默认值必须是深拷贝，调用方原地改不得污染模块级默认。

    `dict(DEFAULT_CONFIG)` 只拷一层：`imported_nodes` / `outbound_selections` 会与
    `DEFAULT_CONFIG` 共享同一个列表/字典。任何一个调用方原地 append 一次，本进程后续
    每一次"没有配置文件"的读取都会带着那条脏数据 —— 而且污染的是默认值，重启前一直存在。
    """
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "config.json"  # 不存在 → 走默认值分支
        with patch.object(cfgmod, "config_path", lambda: path):
            first = cfgmod.load_config()
            first["imported_nodes"].append({"type": "vless", "server": "polluted"})
            first["outbound_selections"]["node"]["mode"] = "auto"
            second = cfgmod.load_config()

        assert second["imported_nodes"] == [], second["imported_nodes"]
        assert second["outbound_selections"]["node"]["mode"] == ""
    # 模块级默认也必须干净
    assert cfgmod.DEFAULT_CONFIG["imported_nodes"] == []
    assert cfgmod.DEFAULT_CONFIG["outbound_selections"]["node"]["mode"] == ""


def test_broken_config_is_stashed_and_not_silently_lost():
    """配置读不动时：坏文件必须原样留证，且后续保存不能把内容抹掉。"""
    with tempfile.TemporaryDirectory() as tmp:
        tmp_dir = Path(tmp)
        path = tmp_dir / "config.json"
        good = '{"subscription_url": "https://example.invalid/sub", "mode": "sub"}'
        broken = good[: len(good) - 4]  # 去掉结尾，JSON 不完整但内容含订阅
        path.write_text(broken, encoding="utf-8")
        with patch.object(cfgmod, "config_path", lambda: path):
            loaded = cfgmod.load_config()
            assert loaded["active_source"] == "" or loaded == cfgmod.DEFAULT_CONFIG
            assert cfgmod.config_warning(), "读取失败必须被记下来"
            stashed = list(tmp_dir.glob("config.json.bad-*"))
            assert len(stashed) == 1, tmp_dir.iterdir()
            before = stashed[0].read_text(encoding="utf-8")
            assert before == broken, "坏文件必须原样留着"
            assert "example.invalid" in before, "暂存件里要保住用户已配的内容"

            # 关键：再保存一次，不能因为读到默认值就把订阅抹平（以前就是这么丢的）
            assert cfgmod.save_config({"active_source": "sub"})
            assert stashed[0].read_text(encoding="utf-8") == before, (
                "暂存件被后面的保存覆盖了"
            )


def test_save_keeps_previous_version_as_bak():
    """每次保存前留一份上一版，便于手滑后回退。"""
    with tempfile.TemporaryDirectory() as tmp:
        tmp_dir = Path(tmp)
        path = tmp_dir / "config.json"
        with patch.object(cfgmod, "config_path", lambda: path):
            assert cfgmod.save_config(
                {"active_source": "node", "node_uri": "vless://first"}
            )
            assert cfgmod.save_config({"node_uri": "vless://second"})
        bak = json.loads((tmp_dir / "config.json.bak").read_text(encoding="utf-8"))
        assert bak["node_uri"] == "vless://first", bak
        assert cfgmod.load_config  # 保持导入被使用，避免 lint 误报


def test_imported_nodes_persists():
    """导入的节点列表必须能写进 config.json 并读回。"""
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "config.json"
        with patch.object(cfgmod, "config_path", lambda: path):
            nodes = [
                {"type": "vless", "server": "a.com", "server_port": 443, "uuid": "u"},
            ]
            assert cfgmod.save_config({"imported_nodes": nodes})
            assert cfgmod.load_config()["imported_nodes"] == nodes


def test_concurrent_save_and_load_never_stashes_config():
    """并发读写不得把 config.json 判成损坏（半截写入回归）。

    背景：`save_config` 原用 `write_text`（先截断再写），而面板每 3 秒的
    `/api/status` 会 `load_config()` —— 截断窗口里的读解析失败，旧逻辑随即把
    config.json 改名成 `.bad-<时间戳>`，活动配置回落默认值，用户的订阅/节点只留在
    那个被改名的文件里。写入改成原子替换后，读者要么看到旧内容、要么看到新内容。

    这里多线程反复 save/load（**两个写者**：面板的多个写接口与 CLI 可能同时在写，
    共用一个临时文件名就会互相踩）：只要还出现一个 `.bad` 或一次读取告警，就说明
    半截文件又能被读者看见了（本用例在旧实现下必红）。
    """
    with tempfile.TemporaryDirectory() as tmp:
        tmp_dir = Path(tmp)
        path = tmp_dir / "config.json"
        with patch.object(cfgmod, "config_path", lambda: path):
            assert cfgmod.save_config({"node_uri": "vless://start"})
            stop = threading.Event()
            warnings: list[str] = []

            def writer(tag: str) -> None:
                n = 0
                while not stop.is_set():
                    n += 1
                    cfgmod.save_config({"node_uri": f"vless://{tag}-{n}"})

            def reader() -> None:
                while not stop.is_set():
                    cfgmod.load_config()
                    warning = cfgmod.config_warning()
                    if warning:
                        warnings.append(warning)

            threads = [
                threading.Thread(target=writer, args=("w1",)),
                threading.Thread(target=writer, args=("w2",)),
                threading.Thread(target=reader),
                threading.Thread(target=reader),
            ]
            for thread in threads:
                thread.start()
            time.sleep(0.3)
            stop.set()
            for thread in threads:
                thread.join()

            stashed = [p.name for p in tmp_dir.glob("config.json.bad-*")]
            assert not stashed, f"读者撞上占位/半截文件并把它暂存了: {stashed}"
            assert not warnings, warnings
            final = json.loads(path.read_text(encoding="utf-8"))
            assert final["node_uri"].startswith("vless://"), final


def test_save_replaces_target_only_after_temp_write_succeeds():
    """写入必须是"临时文件写好再替换"：替换失败时现有配置一个字节都不动。

    故障注入：让 `os.replace` 抛 OSError（磁盘满、Windows 上目标被占用都会走到这里）。
    旧实现是直接 `write_text` 到目标文件，这里会把目标截成半截或写坏；新实现下
    config.json 仍是上一版内容，且失败被如实报出来（`save_error`）。
    """
    with tempfile.TemporaryDirectory() as tmp:
        tmp_dir = Path(tmp)
        path = tmp_dir / "config.json"
        with patch.object(cfgmod, "config_path", lambda: path):
            assert cfgmod.save_config({"node_uri": "vless://keep-me"})
            with (
                patch.object(cfgmod.os, "replace", side_effect=OSError("替换失败")),
                patch.object(cfgmod, "_RETRY_SLEEP", 0),
            ):
                assert not cfgmod.save_config({"node_uri": "vless://lost"})
            assert cfgmod.save_error(), "写入失败原因要能报出来"
            kept = json.loads(path.read_text(encoding="utf-8"))
            assert kept["node_uri"] == "vless://keep-me", kept


def test_empty_config_is_not_stashed_but_reported():
    """空文件不暂存、不改名：它多半是写入中途的形态，搬走反而打断那次写入。"""
    with tempfile.TemporaryDirectory() as tmp:
        tmp_dir = Path(tmp)
        path = tmp_dir / "config.json"
        path.write_text("", encoding="utf-8")
        with (
            patch.object(cfgmod, "config_path", lambda: path),
            patch.object(cfgmod, "_RETRY_SLEEP", 0),  # 不让重试把测试拖慢
        ):
            loaded = cfgmod.load_config()
        assert loaded["active_source"] == "", loaded
        assert cfgmod.config_warning(), "空文件要如实报出来，不能装作没事"
        assert path.exists(), "空文件不该被改名搬走"
        assert not list(tmp_dir.glob("config.json.bad-*"))


def test_byo_mode_migrates_to_unconfigured_with_warning():
    """升级后读到老配置 mode=byo：丢弃 local_proxy、active_source 为空、给 warning、不抛异常。"""
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "config.json"
        path.write_text(
            json.dumps({"mode": "byo", "local_proxy": "http://127.0.0.1:12334"}),
            encoding="utf-8",
        )
        with patch.object(cfgmod, "config_path", lambda: path):
            cfg = cfgmod.load_config()
            assert cfg["active_source"] == "", cfg
            assert cfg.get("local_proxy", "") == "", cfg
            assert "mode" not in cfg, cfg
            assert cfgmod.config_warning(), "必须给出迁移提示"


def test_default_config_has_no_legacy_keys():
    """新模型默认配置不含 mode/outbound_mode/outbound_node_key，且 active_source 为空。"""
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "config.json"  # 不存在
        with patch.object(cfgmod, "config_path", lambda: path):
            cfg = cfgmod.load_config()
            assert cfg["active_source"] == "", cfg
            assert "mode" not in cfg, cfg
            assert "outbound_mode" not in cfg, cfg
            assert "outbound_node_key" not in cfg, cfg
            assert "local_proxy" not in cfg, cfg


def test_target_kwargs_rejects_local_proxy_forms():
    """删 BYO 后：纯数字端口与 127.0.0.1 URL 不再被当作代理目标。"""
    assert cfgmod.target_kwargs("7890") is None
    assert cfgmod.target_kwargs("http://127.0.0.1:7890") is None
    assert cfgmod.target_kwargs("127.0.0.1:7890") is None
    # 协议 URI 与订阅仍可用
    assert cfgmod.target_kwargs("vless://u@h:443") == {"node_uri": "vless://u@h:443"}
    assert cfgmod.target_kwargs("https://example.invalid/sub") == {
        "subscription_url": "https://example.invalid/sub"
    }


def test_node_key_format():
    """node_key 用 (type, server, server_port) 的稳定字符串，面板与后端共用同一套规则。"""
    node = {"type": "naive", "server": "a.com", "server_port": 443}
    assert cfgmod.node_key(node) == "naive|a.com|443"


def test_active_source_of_reads_field_and_falls_back():
    """active_source_of 读字段；空串=未配置；脏值兜底 node。"""
    assert cfgmod.active_source_of({"active_source": "sub"}) == "sub"
    assert cfgmod.active_source_of({"active_source": "json"}) == "json"
    assert cfgmod.active_source_of({"active_source": ""}) == ""
    assert cfgmod.active_source_of({"active_source": "byo"}) == "node"
    assert cfgmod.active_source_of({}) == ""


def test_slot_candidates_scoped_to_source():
    """并池只发生在槽位内部：json 只回 imported_nodes，node 只回 node_uri，无交叉无回退。"""
    node = {"type": "vless", "tag": "n", "server": "a.com", "server_port": 443}
    json_cfg = {
        "active_source": "json",
        "node_uri": "",
        "subscription_url": "",
        "imported_nodes": [node],
    }
    assert cfgmod.slot_candidates(json_cfg, "json") == [node]
    # 显式要 node 槽但 node_uri 为空：必须回空，不回退到 json 的 imported_nodes
    assert cfgmod.slot_candidates(json_cfg, "node") == []
    node_cfg = {
        "active_source": "node",
        "node_uri": "trojan://pw@a.com:443#N",
        "subscription_url": "",
        "imported_nodes": [node],
    }
    cands = cfgmod.slot_candidates(node_cfg, "node")
    assert len(cands) == 1 and cands[0].get("server") == "a.com", cands
    assert cands[0].get("type") == "trojan", (
        cands
    )  # 是 node_uri 解析的，不是 imported_nodes
    # json 槽有自己的 imported_nodes：各取各的，互不混入
    assert cfgmod.slot_candidates(node_cfg, "json") == [node]


def test_find_candidate_by_key():
    """find_candidate 按 node_key 在指定槽位候选里命中/未命中。"""
    node = {"type": "vless", "tag": "n", "server": "a.com", "server_port": 443}
    cfg = {
        "active_source": "json",
        "node_uri": "",
        "subscription_url": "",
        "imported_nodes": [node],
    }
    assert cfgmod.find_candidate(cfg, "vless|a.com|443") == node
    assert cfgmod.find_candidate(cfg, "vless|a.com|444") is None
    assert cfgmod.find_candidate(cfg, "vless|a.com|443", source="json") == node


def test_channel_state_unconfigured():
    """无来源：state=unconfigured，message 给出三来源提示。"""
    st = cfgmod.channel_state(config={"active_source": ""})
    assert st["state"] == "unconfigured"
    assert st["kwargs"] == {}
    assert "未配置节点" in st["message"]


def test_channel_state_single_node_uses_it_directly():
    """仅一个可用节点：不管选择，直接用那一个（退化为直接使用）。"""
    node = {"type": "vless", "tag": "n", "server": "a.com", "server_port": 443}
    cfg = {"active_source": "json", "imported_nodes": [node]}
    st = cfgmod.channel_state(config=cfg)
    assert st["state"] == "ok"
    assert st["kwargs"] == {"nodes": [node]}


def test_channel_state_unselected():
    """池来源（≥2 节点）未选择出站：state=unselected，message 含候选数。"""
    imported = [
        {"type": "vless", "tag": "a", "server": "a.com", "server_port": 443},
        {"type": "vless", "tag": "b", "server": "b.com", "server_port": 443},
    ]
    cfg = {"active_source": "json", "imported_nodes": imported}
    st = cfgmod.channel_state(config=cfg)
    assert st["state"] == "unselected"
    assert st["kwargs"] == {}
    assert "2 个候选节点" in st["message"]


def test_channel_state_pinned_uses_single_node():
    """池来源指定节点：只用指定的那一个，不并池。"""
    a = {"type": "vless", "tag": "a", "server": "a.com", "server_port": 443}
    b = {"type": "vless", "tag": "b", "server": "b.com", "server_port": 443}
    cfg = {
        "active_source": "json",
        "imported_nodes": [a, b],
        "outbound_selections": {
            "json": {"mode": "pinned", "node_key": "vless|b.com|443"}
        },
    }
    st = cfgmod.channel_state(config=cfg)
    assert st["state"] == "ok"
    assert st["kwargs"] == {"nodes": [b]}


def test_channel_state_pinned_dead_node():
    """指定节点已失效：state=pin_dead，不悄悄换成别人。"""
    a = {"type": "vless", "tag": "a", "server": "a.com", "server_port": 443}
    b = {"type": "vless", "tag": "b", "server": "b.com", "server_port": 443}
    cfg = {
        "active_source": "json",
        "imported_nodes": [a, b],
        "outbound_selections": {
            "json": {"mode": "pinned", "node_key": "vless|gone.com|443"}
        },
    }
    st = cfgmod.channel_state(config=cfg)
    assert st["state"] == "pin_dead"
    assert st["kwargs"] == {}
    assert "已失效" in st["message"]


def test_channel_state_auto_uses_source_pool():
    """池来源自动优选：返回该来源（json）的候选集，不跨来源。"""
    imported = [
        {"type": "vless", "tag": "a", "server": "a.com", "server_port": 443},
        {"type": "vless", "tag": "b", "server": "b.com", "server_port": 443},
    ]
    cfg = {
        "active_source": "json",
        "imported_nodes": imported,
        "outbound_selections": {"json": {"mode": "auto", "node_key": None}},
    }
    st = cfgmod.channel_state(config=cfg)
    assert st["state"] == "ok"
    assert st["kwargs"] == {"nodes": imported}


def test_channel_kwargs_thin_shell():
    """channel_kwargs 薄壳：ok 状态给 kwargs，非 ok 给空 dict。"""
    node = {"type": "vless", "tag": "n", "server": "a.com", "server_port": 443}
    assert cfgmod.channel_kwargs(
        {"active_source": "json", "imported_nodes": [node]}
    ) == {"nodes": [node]}
    assert cfgmod.channel_kwargs({"active_source": ""}) == {}


def _migrate(data: dict) -> dict:
    """把原始 dict 写成文件，走 load_config 完整迁移链，返回迁移后的 cfg。"""
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "config.json"
        path.write_text(json.dumps(data), encoding="utf-8")
        with patch.object(cfgmod, "config_path", lambda: path):
            return cfgmod.load_config()


def test_migrate_to_slots_unconfigured():
    """形态一：未配（mode=node 但无任何来源）→ active_source 空，旧键全删。"""
    cfg = _migrate({"mode": "node"})
    assert cfg["active_source"] == "", cfg
    for legacy in ("mode", "outbound_mode", "outbound_node_key"):
        assert legacy not in cfg, cfg


def test_migrate_to_slots_node():
    """形态二：node 来源 → active_source=node，旧键全删。"""
    cfg = _migrate({"mode": "node", "node_uri": "vless://u@h:443"})
    assert cfg["active_source"] == "node", cfg
    for legacy in ("mode", "outbound_mode", "outbound_node_key"):
        assert legacy not in cfg, cfg


def test_migrate_to_slots_sub_pinned():
    """形态三：sub+pinned → active_source=sub，pinned 落到 sub 槽。"""
    cfg = _migrate(
        {
            "mode": "sub",
            "subscription_url": "https://x/sub",
            "outbound_mode": "pinned",
            "outbound_node_key": "vless|a.com|443",
        }
    )
    assert cfg["active_source"] == "sub", cfg
    assert cfg["outbound_selections"]["sub"] == {
        "mode": "pinned",
        "node_key": "vless|a.com|443",
    }, cfg
    assert cfg["outbound_selections"]["node"]["mode"] == "", cfg
    for legacy in ("mode", "outbound_mode", "outbound_node_key"):
        assert legacy not in cfg, cfg


def test_migrate_to_slots_imported_only():
    """形态四：仅导入（无 mode）→ active_source=json。"""
    cfg = _migrate(
        {"imported_nodes": [{"type": "vless", "server": "a.com", "server_port": 443}]}
    )
    assert cfg["active_source"] == "json", cfg


def test_migrate_idempotent():
    """已迁移的配置（无旧键）再 load 不改变 outbound_selections，也不回写旧键。"""
    data = {
        "active_source": "json",
        "imported_nodes": [{"type": "vless", "server": "a.com", "server_port": 443}],
        "outbound_selections": {
            "json": {"mode": "auto", "node_key": None},
            "node": {"mode": "", "node_key": None},
            "sub": {"mode": "", "node_key": None},
        },
    }
    cfg = _migrate(data)
    assert cfg["active_source"] == "json"
    assert cfg["outbound_selections"]["json"]["mode"] == "auto"
    for legacy in ("mode", "outbound_mode", "outbound_node_key"):
        assert legacy not in cfg, cfg


def test_load_config_migrates_legacy_outbound_then_slots():
    """最旧的 outbound=pinned + pinned_node 先走 _migrate_outbound，再归槽。"""
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "config.json"
        node = {"type": "naive", "tag": "n", "server": "a.com", "server_port": 443}
        path.write_text(
            json.dumps(
                {
                    "mode": "node",
                    "node_uri": "vless://u@h:443",
                    "outbound": "pinned",
                    "pinned_node": node,
                }
            ),
            encoding="utf-8",
        )
        with patch.object(cfgmod, "config_path", lambda: path):
            cfg = cfgmod.load_config()
            assert cfg["active_source"] == "node", cfg
            assert cfg["outbound_selections"]["node"] == {
                "mode": "pinned",
                "node_key": "naive|a.com|443",
            }, cfg
            for legacy in (
                "mode",
                "outbound",
                "pinned_node",
                "outbound_mode",
                "outbound_node_key",
            ):
                assert legacy not in cfg, cfg


def test_load_config_unmappable_pinned_to_unselected():
    """旧 pinned_node 缺失 → 降级为未选择 + config_warning，不抛异常。"""
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "config.json"
        path.write_text(
            json.dumps(
                {
                    "mode": "node",
                    "node_uri": "vless://u@h:443",
                    "outbound": "pinned",
                    "pinned_node": None,
                }
            ),
            encoding="utf-8",
        )
        with patch.object(cfgmod, "config_path", lambda: path):
            cfg = cfgmod.load_config()
            assert cfg["outbound_selections"]["node"]["mode"] == "", cfg
            assert cfgmod.config_warning(), "不可映射必须给 config_warning"


def test_load_config_does_not_share_mutable_defaults():
    """读取到的可变容器不能与 DEFAULT_CONFIG 共用同一个对象（别名污染）。

    回归背景：`load_config` 此前是 `dict(DEFAULT_CONFIG)` 浅拷贝，`imported_nodes` 与
    `outbound_selections` 和模块级默认值指向同一对象 —— 调用方原地改一下，污染是全局且
    跨配置的：此后每一次 `load_config`（哪怕配置文件不存在）都会带着上次的改动。
    """
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "config.json"  # 不存在 → 整份取默认值
        with patch.object(cfgmod, "config_path", lambda: path):
            first = cfgmod.load_config()
            first["imported_nodes"].append(
                {"type": "vless", "server": "polluted.invalid", "server_port": 443}
            )
            first["outbound_selections"]["node"]["mode"] = "auto"

            assert cfgmod.DEFAULT_CONFIG["imported_nodes"] == [], "模块级默认值被污染"
            second = cfgmod.load_config()
            assert second["imported_nodes"] == [], second
            assert second["outbound_selections"]["node"]["mode"] == "", second


def test_disable_channel_does_not_touch_running_source_when_not_owned():
    """不拥有通道的进程调用 disable_channel（即 `agent-channel status` 等短命 CLI 的
    atexit 路径）**不得**清共享 config 的 running_source——曾因此把面板正在跑的
    来源记录抹成 None。"""
    from agent_channel import core

    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "config.json"
        with patch.object(cfgmod, "config_path", lambda: path):
            cfgmod.save_config({"active_source": "json", "running_source": "json"})
            saved_status = core._CURRENT_STATUS
            saved_runner = core._ACTIVE_RUNNER
            try:
                core._CURRENT_STATUS = {"status": "stopped"}
                core._ACTIVE_RUNNER = None
                core.disable_channel()  # owned=False
                assert cfgmod.load_config().get("running_source") == "json", (
                    "未拥有通道的进程不该动 running_source"
                )
            finally:
                core._CURRENT_STATUS = saved_status
                core._ACTIVE_RUNNER = saved_runner


def test_channel_state_empty_when_configured_but_unrecognized():
    """配了地址却一个节点都没认出来 → state=empty（区别于 unconfigured），且带原因。"""
    from agent_channel import core

    cfg = {"active_source": "sub", "subscription_url": "https://example.invalid/sub"}
    with patch.object(core, "_fetch_subscription", lambda url: "<html>403</html>"):
        st = cfgmod.channel_state(config=cfg)
    assert st["state"] == "empty", st
    assert "订阅" in st["message"], st
    # 没配地址时仍是 unconfigured——两者不能混为一谈
    assert (
        cfgmod.channel_state(config={"active_source": "sub", "subscription_url": ""})[
            "state"
        ]
        == "unconfigured"
    )
