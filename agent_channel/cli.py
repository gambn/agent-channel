"""Agent 外壳：无交互界面的命令行入口。

设计取舍：节点的配置只从 config.json 读取，用 `set-*` 子命令写入一次即可。
日常运维走 `status` / `test` 这类轻量只读命令，输出刻意保持紧凑，
避免把「配置节点」变成对话式 tool calling 的 token 开销。

用法示例：
    agent-channel status
    agent-channel set-sub https://example.com/sub
    agent-channel start
    agent-channel benchmark            # 紧凑输出
    agent-channel benchmark --json     # 供程序消费
"""

import argparse
import json
import sys
import urllib.error
import urllib.request
from typing import Any

from .binary import (
    get_local_binary_path,
    get_local_core_version,
    install_core,
    is_version_installable,
    list_core_releases,
)
from .config import (
    DEFAULT_LISTEN_PORT,
    DEFAULT_UI_PORT,
    channel_state,
    config_path,
    config_warning,
    load_config,
    save_config,
    target_kwargs,
)
from .core import benchmark_nodes, disable_channel, enable_channel, get_channel_status
from .utils import generate_uuid

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_UNCONFIGURED = 2

_SECRET_KEYS = ("node_uri", "subscription_url")

# 导入节点的凭据字段随协议而变（uuid / password / auth_str / private_key / username…），
# 逐个枚举要遮的键必然漏一个。默认脱敏改为**只保留非敏感字段**，将来新增协议也不会漏。
_IMPORTED_NODE_SAFE_FIELDS = ("type", "tag", "server", "server_port")


def _mask(value: str) -> str:
    """敏感目标只显示前 24 个字符，便于核对又不易整体泄露。"""
    if len(value) <= 24:
        return value
    return value[:24] + "..."


def _masked_imported_nodes(nodes: Any) -> list[dict[str, Any]]:
    """把导入节点压成只含非敏感字段的摘要。

    面板的出站下拉用的也是这一组字段（type/tag/server/server_port），所以摘要够用户
    核对"导入了哪些节点"；uuid / password 之类的凭据一个都不出去。
    """
    if not isinstance(nodes, list):
        return []
    return [
        {key: node.get(key) for key in _IMPORTED_NODE_SAFE_FIELDS if key in node}
        for node in nodes
        if isinstance(node, dict)
    ]


def _remote_status() -> dict[str, Any] | None:
    """向常驻守护进程的面板询问状态；不可达时返回 None。

    通道状态保存在进程内，独立的 CLI 调用看不到它，所以优先问守护进程。
    """
    url = f"http://127.0.0.1:{DEFAULT_UI_PORT}/api/status"
    try:
        with urllib.request.urlopen(url, timeout=1.5) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, ValueError):
        return None


def _remote_post(
    path: str, payload: dict[str, Any] | None = None
) -> dict[str, Any] | None:
    """向常驻守护进程的面板发起 POST；不可达时返回 None。

    服务端明确答复的错误（如 409「已有通道操作正在进行中」）要把它的 JSON 原样带回，
    不能和"守护进程不在"混成同一个 None —— 否则 `stop` 会把"忙、没停成"报成 stopped。
    """
    url = f"http://127.0.0.1:{DEFAULT_UI_PORT}{path}"
    req = urllib.request.Request(
        url,
        data=json.dumps(payload or {}).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        try:
            return json.loads(exc.read().decode("utf-8"))
        except (OSError, ValueError):
            return None
    except (urllib.error.URLError, OSError, ValueError):
        return None


def _cmd_status(_: argparse.Namespace) -> int:
    if config_warning():
        print(f"warning: {config_warning()}", file=sys.stderr)
    st = _remote_status() or get_channel_status()
    if st.get("status") != "running":
        print("stopped")
        return EXIT_OK
    sp = st.get("system_proxy") or {}
    print(
        f"running mode={st.get('mode')} node={st.get('node_tag') or '-'} "
        f"proxy={st.get('proxy_url')} sysproxy={'on' if sp.get('enabled') else 'off'}"
    )
    return EXIT_OK


def _resolve_target(
    args: argparse.Namespace, source: str | None = None
) -> dict[str, Any] | None:
    """优先使用命令行显式目标，否则回落到配置文件（可选指定来源槽位）。"""
    target = getattr(args, "target", None)
    if target:
        parsed = target_kwargs(target)
        if parsed is None:
            print(f"error: 无法识别的目标 '{target}'", file=sys.stderr)
            return None
        return parsed
    st = channel_state(source=source)
    if st["state"] != "ok":
        print(f"error: {st['message']}", file=sys.stderr)
        return None
    return st["kwargs"]


def _cmd_start(args: argparse.Namespace) -> int:
    # 启动期自愈：清掉上次异常退出留下的残留内核（占端口）与悬挂系统代理。
    # 只挂在启动入口，不在 enable_channel 里——那里同时承担「换节点」，
    # 清理会把正在服务的旧内核一并杀掉。
    from .core import heal_stale_state

    for note in heal_stale_state(args.port):
        print(f"[agent-channel] {note}")

    kwargs = _resolve_target(args, source=args.source)
    if kwargs is None:
        return EXIT_UNCONFIGURED
    try:
        status = enable_channel(
            **kwargs,
            auto_patch=True,
            set_sys_proxy=not args.no_sysproxy,
            listen_port=args.port,
        )
    except Exception as exc:
        print(f"error: 通道启动失败: {exc}", file=sys.stderr)
        return EXIT_ERROR
    print(
        f"running mode={status.get('mode')} node={status.get('node_tag') or '-'} "
        f"proxy={status.get('proxy_url')}"
    )
    return EXIT_OK


def _cmd_stop(_: argparse.Namespace) -> int:
    # 通道可能跑在守护进程里，先请它停；不可达再停本进程的
    remote = _remote_post("/api/disable")
    if remote is not None:
        if remote.get("error"):
            # 守护进程明确拒绝了（如 409 忙）：如实报错，别打印 stopped
            print(f"error: {remote['error']}", file=sys.stderr)
            return EXIT_ERROR
        print("stopped")
        return EXIT_OK
    disable_channel()
    print("stopped")
    return EXIT_OK


def _cmd_test(_: argparse.Namespace) -> int:
    from .tools import tool_test_ai

    st = _remote_status()
    running = bool(st and st.get("status") == "running" and st.get("proxy_url"))
    print(tool_test_ai(st.get("proxy_url") if st else None, running=running))
    return EXIT_OK


def _cmd_uuid(_: argparse.Namespace) -> int:
    print(generate_uuid())
    return EXIT_OK


def _cmd_benchmark(args: argparse.Namespace) -> int:
    try:
        results = benchmark_nodes(
            node_uri=args.node,
            subscription_url=args.sub,
        )
    except Exception as exc:
        print(f"error: 测速失败: {exc}", file=sys.stderr)
        return EXIT_ERROR

    if args.json:
        print(json.dumps({"results": results}, ensure_ascii=False))
        return EXIT_OK

    ok = sum(1 for r in results if r["forwarding"])
    print(f"{len(results)} nodes, {ok} can forward")
    for i, r in enumerate(results):
        fwd = {True: "ok", False: "fail", None: "?"}[r["forwarding"]]
        tcp = f"{r['tcp_ms']}ms" if r["tcp_ms"] is not None else "udp"
        print(f"[{i}] {fwd:<4} {tcp:<7} {r['type']:<10} {r['tag']}")
    return EXIT_OK


def _cmd_set(args: argparse.Namespace) -> int:
    value = (args.value or "").strip()
    if not value:
        print(f"error: set-{args.mode} 需要一个非空值", file=sys.stderr)
        return EXIT_ERROR
    if args.mode == "node":
        updates = {
            "active_source": "node",
            "node_uri": value,
            "subscription_url": "",
        }
    else:
        updates = {
            "active_source": "sub",
            "subscription_url": value,
            "node_uri": "",
        }
    if not save_config(updates):
        print("error: 写入配置失败", file=sys.stderr)
        return EXIT_ERROR
    print(f"saved source={args.mode} -> {config_path()}")
    return EXIT_OK


def _cmd_set_json(args: argparse.Namespace) -> int:
    """① CLI 补 set-json：从文件/stdin 导入 sing-box JSON，只写 imported_nodes。"""
    from pathlib import Path

    from .parsers import parse_singbox_json

    if args.path == "-":
        text = sys.stdin.read()
    else:
        path = Path(args.path)
        if not path.is_file():
            print(f"error: 文件不存在: {args.path}", file=sys.stderr)
            return EXIT_ERROR
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as exc:
            print(f"error: 读取文件失败: {exc}", file=sys.stderr)
            return EXIT_ERROR

    text = text.strip()
    if not text:
        print("error: 输入为空，未导入任何节点", file=sys.stderr)
        return EXIT_ERROR

    nodes, errors, renamed = parse_singbox_json(text)
    # 追加 + 去重（type+server+server_port），与面板导入同一口径；只写 imported_nodes，
    # 不写 active_source（导入≠启用）。
    current = list(load_config().get("imported_nodes") or [])
    seen = {(n.get("type"), n.get("server"), n.get("server_port")) for n in current}
    added = 0
    duplicates = 0
    for node in nodes:
        key = (node.get("type"), node.get("server"), node.get("server_port"))
        if key in seen:
            duplicates += 1
            continue
        current.append(node)
        seen.add(key)
        added += 1
    if not save_config({"imported_nodes": current}):
        print("error: 写入配置失败", file=sys.stderr)
        return EXIT_ERROR
    print(
        f"imported={added} skipped={len(errors)} duplicates={duplicates} "
        f"renamed={len(renamed)} total={len(current)}"
    )
    for err in errors:
        print(f"  skip: {err}")
    return EXIT_OK


def _cmd_config(args: argparse.Namespace) -> int:
    cfg = load_config()
    if args.reveal:
        print(json.dumps(cfg, ensure_ascii=False, indent=2))
    else:
        masked = {
            k: (_mask(str(cfg.get(k, ""))) if k in _SECRET_KEYS else cfg.get(k))
            for k in cfg
        }
        # 导入节点含 uuid / password 等凭据：这条命令的输出会被 Agent 读进对话，
        # 默认只给可读摘要（`--reveal` 才是全量）。
        masked["imported_nodes"] = _masked_imported_nodes(cfg.get("imported_nodes"))
        print(json.dumps(masked, ensure_ascii=False, indent=2))
    return EXIT_OK


def _cmd_core_list(_: argparse.Namespace) -> int:
    releases = list_core_releases(limit=15)
    if not releases:
        print("error: 查询官方版本列表失败（检查网络）", file=sys.stderr)
        return EXIT_ERROR
    print(f"local={get_local_core_version()} available={len(releases)}")
    for release in releases:
        # "+" 表示该版本有匹配当前平台的预编译资产，可直接安装
        mark = "+" if is_version_installable(release["assets"]) else "-"
        suffix = " pre" if release["prerelease"] else ""
        print(f"  {release['version']:<12} {mark}{suffix}")
    return EXIT_OK


def _cmd_core_install(args: argparse.Namespace) -> int:
    result = install_core(args.version)
    if not result.get("success"):
        print(f"error: {result.get('error')}", file=sys.stderr)
        return EXIT_ERROR
    print(f"installed {result.get('version')} -> {get_local_binary_path()}")
    return EXIT_OK


def _cmd_ui(args: argparse.Namespace) -> int:
    from .server import launch_ui

    launch_ui(port=args.port, open_browser=not args.no_browser, block=True)
    return EXIT_OK


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="agent-channel",
        description="agent-channel通道代理 命令行入口（Agent 壳，无交互界面）",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("status", help="查看通道运行状态（轻量只读）").set_defaults(
        func=_cmd_status
    )

    p_start = sub.add_parser("start", help="按配置启用通道")
    p_start.add_argument("target", nargs="?", help="可选：节点链接/订阅链接")
    p_start.add_argument(
        "--port", type=int, default=DEFAULT_LISTEN_PORT, help="本地监听端口"
    )
    p_start.add_argument(
        "--source", choices=("node", "sub", "json"), help="临时指定来源槽位"
    )
    p_start.add_argument("--no-sysproxy", action="store_true", help="不设置系统代理")
    p_start.set_defaults(func=_cmd_start)

    sub.add_parser("stop", help="停止通道并还原系统代理").set_defaults(func=_cmd_stop)
    sub.add_parser("test", help="测试海外 AI 接口连通性").set_defaults(func=_cmd_test)
    sub.add_parser("uuid", help="生成 v4 UUID").set_defaults(func=_cmd_uuid)

    p_bench = sub.add_parser("benchmark", help="对节点或订阅做连接测速")
    p_bench.add_argument("--sub", help="订阅链接")
    p_bench.add_argument("--node", help="单节点链接")
    p_bench.add_argument("--json", action="store_true", help="输出 JSON（供程序消费）")
    p_bench.set_defaults(func=_cmd_benchmark)

    for mode, help_text in (
        ("node", "配置为单个节点链接"),
        ("sub", "配置为订阅链接"),
    ):
        p_set = sub.add_parser(f"set-{mode}", help=help_text)
        p_set.add_argument("value", help="代理地址 / 节点链接 / 订阅链接")
        p_set.set_defaults(func=_cmd_set, mode=mode)

    p_json = sub.add_parser("set-json", help="从文件或 stdin 导入 sing-box JSON 节点")
    p_json.add_argument("path", help="JSON 文件路径；'-' 读 stdin")
    p_json.set_defaults(func=_cmd_set_json)

    p_cfg = sub.add_parser("config", help="查看当前配置（默认脱敏）")
    p_cfg.add_argument("--reveal", action="store_true", help="显示完整值")
    p_cfg.set_defaults(func=_cmd_config)

    p_core = sub.add_parser("core", help="内核版本管理")
    core_sub = p_core.add_subparsers(dest="core_command", required=True)
    core_sub.add_parser(
        "list", help="列出官方可用内核版本（+ 表示本平台可装）"
    ).set_defaults(func=_cmd_core_list)
    p_core_install = core_sub.add_parser("install", help="安装内核（缺省为最新版）")
    p_core_install.add_argument("version", nargs="?", help="如 v1.14.3；缺省安装最新版")
    p_core_install.set_defaults(func=_cmd_core_install)

    p_ui = sub.add_parser("ui", help="启动人类用的控制面板")
    p_ui.add_argument("--port", type=int, default=DEFAULT_UI_PORT)
    p_ui.add_argument("--no-browser", action="store_true", help="不自动打开浏览器")
    p_ui.set_defaults(func=_cmd_ui)

    return parser


def _force_utf8_io() -> None:
    """Windows 上 stdout 重定向时默认走 ANSI 代码页，会用乱码破坏中文节点名。

    Agent 读取 CLI 输出时无控制台上下文，因此统一为 UTF-8。
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8", errors="replace")


def main(argv: list[str] | None = None) -> int:
    _force_utf8_io()
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
