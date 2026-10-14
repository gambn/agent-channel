"""配置读写：两个外壳（Agent CLI 与人类面板）共用的唯一契约。

配置文件位置：
- Windows: %LOCALAPPDATA%\\agent_channel\\config.json
- 其他平台: ~/.agent_channel/config.json

设计要点：
- 节点的配置只走这个文件，不走对话。人用面板写一次，Agent 壳直接读，
  这样 Agent 侧接入通道是零 token 的。
- 保存是增量的，只覆盖显式传入的键，不会用空值清空其它字段。
"""

import copy
import json
import os
import threading
import time
from contextlib import suppress
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

# 本地代理监听端口，与接入指南保持一致
DEFAULT_LISTEN_PORT = 20849

# 控制面板默认端口
DEFAULT_UI_PORT = 18888

# 三个来源槽位：node（单节点）/ sub（订阅）/ json（导入的 sing-box JSON）。
_VALID_SOURCES = ("node", "sub", "json")

# 来源槽位的中文标签（面板与文案共用这一份，别各写一套）
SOURCE_LABEL = {"node": "单节点", "sub": "订阅", "json": "JSON"}

# 配置默认值。
DEFAULT_CONFIG: dict[str, Any] = {
    # 当前激活来源：node/sub/json；空串表示尚未配置任何来源。
    "active_source": "",
    # 通道运行时落笔的来源快照（enable 成功时与 active_source 同值，停止时清 null）。
    # 状态卡优先显示它，好让「启动后又切了标签」时仍显示“现在跑的是哪条线”。
    "running_source": None,
    "node_uri": "",
    "subscription_url": "",
    # 面板“导入 JSON 文件”导入的 sing-box 节点列表（作为 json 来源的候选）
    "imported_nodes": [],
    # 每个来源槽位的出站选择：mode=""（未选择）/ "auto"（并池优选）/ "pinned"（指定节点）。
    # node_key 只在 pinned 时有值，用 (type, server, server_port) 的稳定字符串；
    # 选择按槽独立保存，互不干扰（取代旧的单一 outbound_mode/outbound_node_key）。
    "outbound_selections": {
        "node": {"mode": "", "node_key": None},
        "sub": {"mode": "", "node_key": None},
        "json": {"mode": "", "node_key": None},
    },
    # 面板“跳过此版本”记住的版本号：只压住这一个版本，将来有更新的版本仍会提示。
    "dismissed_version": "",
}


def config_path() -> Path:
    """返回配置文件绝对路径，父目录自动创建。"""
    local_app_data = os.environ.get("LOCALAPPDATA")
    base = (
        Path(local_app_data) / "agent_channel"
        if local_app_data
        else Path.home() / ".agent_channel"
    )
    base.mkdir(parents=True, exist_ok=True)
    return base / "config.json"


# 上一次读取配置失败的原因（正常时为 None）。面板/CLI 用它提示用户，
# 而不是静默把订阅丢掉。
LAST_LOAD_ERROR: str | None = None

# 上一次保存失败的原因（正常时为 None）。见 save_error()。
LAST_SAVE_ERROR: str | None = None


def node_key(node: dict[str, Any]) -> str:
    """节点的稳定匹配键：(type, server, server_port) 的字符串形式。

    面板与后端共用同一套生成规则；字段微调一次不会让 pin 静默失联。
    """
    return f"{node.get('type')}|{node.get('server')}|{node.get('server_port')}"


def active_source_of(config: dict[str, Any]) -> str:
    """读当前激活来源；空串=未配置，脏值兜底 node。"""
    src = config.get("active_source", "")
    if src in _VALID_SOURCES:
        return src
    return "node" if src else ""


def _slot_candidates_with_errors(
    config: dict[str, Any], source: str
) -> tuple[list[dict[str, Any]], list[str]]:
    """显式按槽位取候选，并带上“为什么没有”的说明。

    node 只看 node_uri，sub 只看 subscription_url，json 只看 imported_nodes——
    无交叉、无回退链。只有 sub 槽会产生说明（拉取失败 / 内容不认）。
    """
    if source == "node":
        from .parsers import parse_single_node

        raw = str(config.get("node_uri") or "").strip()
        if not raw:
            return [], []
        parsed = parse_single_node(raw)
        return ([parsed] if parsed else []), ([] if parsed else ["节点链接无法解析"])
    if source == "sub":
        url = str(config.get("subscription_url") or "").strip()
        if not url:
            return [], []
        from .core import _fetch_subscription
        from .parsers import extract_nodes_from_subscription

        try:
            return extract_nodes_from_subscription(_fetch_subscription(url))
        except Exception as exc:
            return [], [f"订阅拉取失败: {exc}"]
    if source == "json":
        return list(config.get("imported_nodes") or []), []
    return [], []


def slot_candidates(config: dict[str, Any], source: str) -> list[dict[str, Any]]:
    """显式按槽位取候选（公开薄壳：只要节点列表）。"""
    return _slot_candidates_with_errors(config, source)[0]


def _slot_has_address(config: dict[str, Any], source: str) -> bool:
    """该槽位是否“配了地址”——用于把 unconfigured（没配）与 empty（配了但没认出来）分开。"""
    if source == "node":
        return bool(str(config.get("node_uri") or "").strip())
    if source == "sub":
        return bool(str(config.get("subscription_url") or "").strip())
    if source == "json":
        return bool(config.get("imported_nodes"))
    return False


def _find_by_key(
    candidates: list[dict[str, Any]], key: str | None
) -> dict[str, Any] | None:
    """在候选里按 node_key 找节点；找不到（已失效）返回 None。"""
    if not key:
        return None
    for node in candidates:
        if node_key(node) == key:
            return node
    return None


def slot_selection(config: dict[str, Any], source: str) -> dict[str, Any]:
    """读某槽位的出站选择；缺失时补默认（未选择）。"""
    selections = config.get("outbound_selections") or {}
    sel = selections.get(source) or {}
    return {"mode": sel.get("mode", ""), "node_key": sel.get("node_key")}


def updated_selections(
    config: dict[str, Any], source: str, mode: str, node_key: str | None = None
) -> dict[str, Any]:
    """返回把某槽位选择更新为 (mode, node_key) 后的 outbound_selections（不动其它槽）。"""
    selections = {s: dict(slot_selection(config, s)) for s in _VALID_SOURCES}
    selections[source] = {"mode": mode, "node_key": node_key}
    return selections


def find_candidate(
    config: dict[str, Any], key: str | None, source: str | None = None
) -> dict[str, Any] | None:
    """在指定来源槽位（缺省取 active_source）里按 node_key 找节点。"""
    if source is None:
        source = active_source_of(config)
    return _find_by_key(slot_candidates(config, source), key)


def _migrate_outbound(cfg: dict[str, Any], data: dict[str, Any]) -> None:
    """旧 outbound/pinned_node、strategy/custom_node → outbound_mode/outbound_node_key。

    无法映射（pinned 但节点缺失/缺字段）→ 降级为未选择 + config_warning，不抛异常。
    """
    global LAST_LOAD_ERROR
    old_mode = None
    old_pinned = None
    if "outbound" in data:
        old_mode = data.get("outbound")
        old_pinned = data.get("pinned_node")
    elif "strategy" in data:
        old_mode = "pinned" if data.get("strategy") == "custom" else "auto"
        old_pinned = data.get("custom_node")

    if old_mode == "pinned":
        if (
            isinstance(old_pinned, dict)
            and old_pinned.get("type")
            and old_pinned.get("server")
            and old_pinned.get("server_port") is not None
        ):
            cfg["outbound_mode"] = "pinned"
            cfg["outbound_node_key"] = node_key(old_pinned)
        else:
            cfg["outbound_mode"] = ""
            cfg["outbound_node_key"] = None
            if LAST_LOAD_ERROR is None:
                LAST_LOAD_ERROR = "旧的指定节点无法迁移，已重置为未选择，请重新选择出站"
    elif old_mode == "auto":
        cfg["outbound_mode"] = "auto"
        cfg["outbound_node_key"] = None

    cfg.pop("outbound", None)
    cfg.pop("pinned_node", None)
    cfg.pop("strategy", None)
    cfg.pop("custom_node", None)


def _infer_legacy_source(cfg: dict[str, Any]) -> str | None:
    """旧 source_of 的推断逻辑（迁移用）：URI→node、{→json、订阅→sub、仅导入→json、空→None。"""
    mode = cfg.get("mode", "node")
    if mode == "sub":
        if str(cfg.get("subscription_url") or "").strip():
            return "sub"
    else:
        node_uri = str(cfg.get("node_uri") or "").strip()
        if node_uri:
            return "json" if node_uri.startswith("{") else "node"
    if cfg.get("imported_nodes"):
        return "json"
    return None


def _migrate_to_slots(cfg: dict[str, Any], data: dict[str, Any]) -> None:
    """旧模型（mode/outbound_mode/outbound_node_key）→ 新三槽位模型。幂等。

    靠「data 里是否还有旧键」判定：没有旧键（已迁移或全新）就跳过；有旧键则把语义
    归位到 active_source + outbound_selections，并删除旧键。
    """
    legacy_keys = ("mode", "outbound_mode", "outbound_node_key")
    # 幂等：data 里已有 active_source（新模型或已迁移）就跳过；否则推断并归位旧键。
    # 不能只看「有没有旧键」——更早的配置可能只有 imported_nodes 而没有 mode（仅导入形态），
    # 那也需要推断出 active_source=json。
    if "active_source" in data:
        return
    old_src = _infer_legacy_source(cfg)
    cfg["active_source"] = old_src or ""
    selections = {s: {"mode": "", "node_key": None} for s in _VALID_SOURCES}
    if old_src:
        selections[old_src] = {
            "mode": cfg.get("outbound_mode", ""),
            "node_key": cfg.get("outbound_node_key"),
        }
    cfg["outbound_selections"] = selections
    for key in legacy_keys:
        cfg.pop(key, None)


def _stash_broken_config(path: Path) -> Path | None:
    """把读不动的配置原样挪到 `<名字>.bad-<时间戳>`；挪不动返回 None。"""
    try:
        target = path.with_name(f"{path.name}.bad-{time.strftime('%Y%m%d-%H%M%S')}")
        path.replace(target)
        return target
    except OSError:
        return None


# 读写重试：写入方走原子替换（见 `_replace_atomic`），读者正常看不到半截文件；
# 这几拍兜的是"旧版本 / 外部工具正在改这个文件"以及替换瞬间的竞争。
_RETRY_ATTEMPTS = 3
_RETRY_SLEEP = 0.05


def _replace_atomic(path: Path, data: bytes) -> None:
    """同目录临时文件 + `os.replace`：读者要么看到旧内容、要么看到新内容，绝无半截。

    必须同目录：跨目录 / 跨盘的 replace 不是原子操作（Windows 上还会直接失败）。

    临时文件名带 pid 与线程 id：面板是 ThreadingHTTPServer，CLI 也可能与守护进程同时
    保存。若共用一个固定 `.tmp`，两个写者会互相踩——一个刚写一半，另一个就把这份半截
    文件 replace 成正式配置，读者又能看到坏内容。崩溃留下的临时文件不会被任何读取路径
    使用，也不影响后续保存。

    替换可能撞上"另一个进程正打开着目标文件"（Windows 上 MoveFileEx 会以共享冲突
    失败），所以对 PermissionError 重试几拍；重试用尽仍失败就如实抛给调用方——
    `save_config` 会把它变成一次可见的"写入失败"，而不是悄悄丢内容。
    """
    tmp = path.with_name(f"{path.name}.{os.getpid()}-{threading.get_ident()}.tmp")
    tmp.write_bytes(data)
    for attempt in range(_RETRY_ATTEMPTS):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            if attempt + 1 >= _RETRY_ATTEMPTS:
                raise
            time.sleep(_RETRY_SLEEP)


def _try_read_config(path: Path) -> tuple[dict[str, Any] | None, str, str]:
    """读一次并解析：返回 (数据, 原文, 失败原因)。失败原因是给人看的一句话。"""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        return None, "", f"{path.name} 读取失败（{exc}）"
    if not text.strip():
        return None, text, f"{path.name} 是空文件（可能正在写入）"
    try:
        data = json.loads(text)
    except ValueError as exc:
        return None, text, f"{path.name} 读取失败（{exc}）"
    if not isinstance(data, dict):
        return None, text, f"{path.name} 内容不是一个 JSON 对象"
    return data, text, ""


def load_config() -> dict[str, Any]:
    """读取配置；文件不存在时用默认值，缺失键自动补齐。

    文件存在但读不动（损坏 / 写入被中断）时**不再静默丢内容**：把坏文件原样
    另存为 `config.json.bad-<时间戳>`，再用默认值继续，失败原因记进
    `LAST_LOAD_ERROR`。否则紧随其后的 save_config 会拿默认值把用户配好的
    订阅/节点连证据一起抹平（以前就是这样）。

    两种情况要区别对待，别把"正在写入"误判成"已损坏"：

    - 读失败先重试几拍——写入方是原子替换（`_replace_atomic`），读者正常看不到
      半截文件，这里兜的是旧版本 / 外部工具正在改这个文件的窗口；
    - 只有**非空且读不动**的文件才暂存留证。空文件没有内容可留，而且它大概率正是
      写入中途的形态，把它改名搬走会打断正在进行的那次写入。
    """
    global LAST_LOAD_ERROR
    # 深拷贝：DEFAULT_CONFIG 里的 imported_nodes / outbound_selections 是可变对象，
    # `dict(...)` 只拷一层，返回值与模块级默认值会共享同一个列表/字典 —— 任何一个
    # 调用方原地改一下，就污染了本进程后续所有"没有配置文件"的读取。
    cfg = copy.deepcopy(DEFAULT_CONFIG)
    path = config_path()
    if not path.exists():
        LAST_LOAD_ERROR = None
        return cfg

    data: dict[str, Any] | None = None
    text = ""
    reason = ""
    for attempt in range(_RETRY_ATTEMPTS):
        data, text, reason = _try_read_config(path)
        if data is not None:
            break
        if attempt + 1 < _RETRY_ATTEMPTS:
            time.sleep(_RETRY_SLEEP)

    if data is None:
        LAST_LOAD_ERROR = reason
        if text.strip():
            _stash_broken_config(path)
        return cfg

    cfg.update(data)
    if cfg.get("mode") == "byo":
        # 升级迁移：“本地已有代理”（BYO）模式已移除，改到单节点口
        cfg["mode"] = "node"
        cfg.pop("local_proxy", None)
        LAST_LOAD_ERROR = "升级后已移除“本地已有代理”模式，请改配订阅或节点"
    else:
        LAST_LOAD_ERROR = None
    # 出站模型迁移：旧 outbound/pinned_node、strategy/custom_node → outbound_mode/outbound_node_key
    _migrate_outbound(cfg, data)
    # 三槽位模型迁移：mode/outbound_mode/outbound_node_key → active_source/outbound_selections
    _migrate_to_slots(cfg, data)
    return cfg


def config_warning() -> str | None:
    """上次读取配置失败的原因（正常时 None）。"""
    return LAST_LOAD_ERROR


def save_error() -> str | None:
    """上次保存失败的原因（正常时 None）。

    与 LAST_LOAD_ERROR 同款模式：`save_config` 保持 bool 返回（`cli._cmd_set` 等处按
    真值判断，改成元组会破坏它们），失败原因另走这个访问器，好让 API 层能如实回给
    用户 —— 此前 `save_config` 把所有 OSError 吞成 `return False`，而几个调用点又不
    检查返回值，于是“导入成功”可能是假的（磁盘满 / 目录只读时），且下一次启用会读到
    老配置。
    """
    return LAST_SAVE_ERROR


def save_config(updates: dict[str, Any]) -> bool:
    """增量保存：只写入已知键，且不会用未提供的字段清空现有配置。

    写入走 `_replace_atomic`（同目录临时文件 + `os.replace`）。此前是 `write_text`：
    它先截断再写，而面板每 3 秒的 `/api/status` 会读这个文件 —— 截断窗口里的读解析
    失败，旧逻辑随即把 config.json 改名成 `.bad-<时间戳>`，用户的订阅/节点当场从活动
    配置里消失（回归见 `tests/test_config.py` 的并发用例）。
    """
    global LAST_SAVE_ERROR
    try:
        path = config_path()
        if path.exists():
            # 覆盖前留一份上一版：写入被中断也不会只剩半截（与上面的 .bad 各管一段）
            with suppress(OSError):
                _replace_atomic(path.with_name(path.name + ".bak"), path.read_bytes())
        current = load_config()
        for key, value in updates.items():
            if key in DEFAULT_CONFIG:
                current[key] = value
        _replace_atomic(
            path, json.dumps(current, indent=2, ensure_ascii=False).encode("utf-8")
        )
        LAST_SAVE_ERROR = None
        return True
    except OSError as exc:
        LAST_SAVE_ERROR = f"写入配置失败（{exc}）"
        return False


def channel_state(
    source: str | None = None, config: dict[str, Any] | None = None
) -> dict[str, Any]:
    """把配置映射为启用状态：{state, kwargs, candidates, message}。

    - state: ok（可启用）/ unconfigured（**没配**任何来源）/ empty（**配了**地址但候选为空）/
      unselected（池未选出站）/ pin_dead（指定节点已失效）；
    - kwargs: state==ok 时可直接传给 enable_channel；
    - candidates: 当前槽位的候选节点；
    - message: 非 ok 时给用户看的一句话（全仓库唯一定义点）。

    - 仅一个可用节点：直接用那一个（退化为直接使用，出口 IP 固定）；
    - 池来源（≥2）：未选择 → unselected；pinned → 只用指定那一个（失效即 pin_dead，不替用户挑）；
      auto → 该来源的候选集（并池只发生在来源内部，不跨来源）。
    """
    cfg = config if config is not None else load_config()
    src = source if source is not None else active_source_of(cfg)
    unconfigured = {
        "state": "unconfigured",
        "kwargs": {},
        "candidates": [],
        "message": "未配置节点：请先配置单节点、订阅，或导入 JSON",
    }
    if not src:
        return unconfigured
    candidates, why = _slot_candidates_with_errors(cfg, src)
    if not candidates:
        # “没配”与“配了却没认出来”必须分开说：后者往往是地址填错、订阅服务给了
        # 403 页面、或内容格式不认——此时告诉用户“未配置节点”是误导。
        if _slot_has_address(cfg, src):
            detail = "；".join(why) if why else "没有解析出可用节点"
            return {
                "state": "empty",
                "kwargs": {},
                "candidates": [],
                "message": (
                    f"已配置{SOURCE_LABEL.get(src, src)}来源，"
                    f"但没有可用的候选节点：{detail}"
                ),
            }
        return unconfigured
    if len(candidates) == 1:
        return {
            "state": "ok",
            "kwargs": {"nodes": [candidates[0]]},
            "candidates": candidates,
            "message": "",
        }
    sel = slot_selection(cfg, src)
    if sel["mode"] == "":
        return {
            "state": "unselected",
            "kwargs": {},
            "candidates": candidates,
            "message": (
                f"已配置 {len(candidates)} 个候选节点，但尚未选择出站："
                "请在面板「启用时的出站」选择"
            ),
        }
    if sel["mode"] == "pinned":
        node = _find_by_key(candidates, sel["node_key"])
        if node is None:
            return {
                "state": "pin_dead",
                "kwargs": {},
                "candidates": candidates,
                "message": (
                    f"所选节点已失效（当前候选 {len(candidates)} 个）：请重新选择出站"
                ),
            }
        return {
            "state": "ok",
            "kwargs": {"nodes": [node]},
            "candidates": candidates,
            "message": "",
        }
    return {
        "state": "ok",
        "kwargs": {"nodes": candidates},
        "candidates": candidates,
        "message": "",
    }


def channel_kwargs(config: dict[str, Any] | None = None) -> dict[str, Any]:
    """公开 API 薄壳：返回 channel_state 的 kwargs（非 ok 状态返回空 dict）。"""
    st = channel_state(config=config)
    return st["kwargs"] if st["state"] == "ok" else {}


def target_kwargs(target: str) -> dict[str, Any] | None:
    """把用户输入的代理目标映射为 enable_channel 参数。

    支持单节点链接（vless/trojan/ss）与订阅链接；本地地址（BYO）已移除，不作为目标。
    无法识别时返回 None。CLI 与 Agent 工具共用此函数，避免逻辑重复。
    """
    value = target.strip()
    if not value:
        return None
    if value.startswith(("vless://", "trojan://", "ss://")):
        return {"node_uri": value}
    if value.startswith(("http://", "https://")):
        if urlparse(value).hostname in ("127.0.0.1", "localhost", "::1"):
            return None
        return {"subscription_url": value}
    return None
