"""本地控制面板的 HTTP 服务层。

只负责 HTTP 与静态资源，业务逻辑全部委托给 core / config / binary 等模块。
前端静态资源位于 agent_channel/web/，按包内相对路径读取，不依赖外部 CDN。
"""

import http.server
import json
import socket
import socketserver
import sys
import threading
import time
import webbrowser
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlparse

from .binary import (
    check_core_update,
    ensure_local_core,
    get_local_core_version,
    update_core,
)
from .config import (
    DEFAULT_LISTEN_PORT,
    DEFAULT_UI_PORT,
    SOURCE_LABEL,
    active_source_of,
    channel_state,
    config_warning,
    find_candidate,
    load_config,
    node_key,
    save_config,
    save_error,
    slot_candidates,
    slot_selection,
    updated_selections,
)
from .core import (
    ChannelBusyError,
    benchmark_nodes,
    channel_operation,
    disable_channel,
    enable_channel,
    get_channel_status,
    heal_stale_state,
    shutdown_app,
)
from .parsers import parse_singbox_json
from .probe import geolocate_via_proxy, test_single_endpoint
from .sysproxy import set_system_proxy, unset_system_proxy
from .utils import generate_uuid, get_free_port, warn_stderr


def _resolve_web_dir() -> Path:
    """定位前端静态资源目录。

    PyInstaller onefile 模式会把数据解包到 sys._MEIPASS，此时包的 __file__
    不再指向真实磁盘目录，必须优先用解包目录。

    统一 resolve()：Windows 上 _MEIPASS 常是 8.3 短路径（C:\\Users\\<user>~1\\...），
    不规范化会让下面的包含判断失效。
    """
    bundled = getattr(sys, "_MEIPASS", None)
    if bundled:
        candidate = Path(bundled) / "agent_channel" / "web"
        if candidate.is_dir():
            return candidate.resolve()
    return (Path(__file__).resolve().parent / "web").resolve()


WEB_DIR = _resolve_web_dir()


def _option_summary(key: str, source: str, node: dict[str, Any]) -> dict[str, Any]:
    """候选项摘要：只给可读字段，不带任何凭据。"""
    return {
        "key": key,
        "source": source,
        "tag": node.get("tag") or f"{source}节点",
        "type": node.get("type"),
        "server": node.get("server"),
        "server_port": node.get("server_port"),
    }


# 最近一次测速的节点延迟缓存（进程内，重启即失）：key=(type, server, server_port) -> tcp_ms。
# 供 outbound_options 给下拉每项带出 latency_ms；「自动优选」项用按 source 分桶的
# _LAST_FASTEST[source] 展示本批最快（消灭跨来源残留：json 测速不能写进 sub 槽的最快）。
_LATENCY_CACHE: dict[tuple[Any, Any, Any], float] = {}
_LAST_FASTEST: dict[str, tuple[float | None, str | None]] = {}
# 缓存条数上限：键是节点身份，长期反复换节点/订阅会缓慢堆积。超限直接清空——
# 它只是"下拉里显示延迟"的展示缓存，下一轮测速会重新填，清掉没有任何正确性代价。
_LATENCY_CACHE_MAX = 512


def _record_benchmark_latency(results: list[dict[str, Any]], source: str) -> None:
    """把最近一批测速的 tcp_ms 缓存进进程内，供 outbound_options 带出 latency_ms。

    同时按 source 分桶记下本批**转发验证通过**的节点里延迟最快的 tag
    （供 auto 项展示“最快是哪个节点”）。
    """
    global _LATENCY_CACHE, _LAST_FASTEST
    fastest: float | None = None
    fastest_tag: str | None = None
    for entry in results:
        tcp = entry.get("tcp_ms")
        if isinstance(tcp, (int, float)):
            # 转一次供下面复用：isinstance 已守卫，float() 不会抛；值同时用于缓存与比大小。
            latency = float(tcp)
            key = (entry.get("type"), entry.get("server"), entry.get("server_port"))
            _LATENCY_CACHE[key] = latency
            # 「本批最快」只认**转发验证通过**的节点：失败（False，如部分 argo/CDN）
            # 或未验证（None，如内核不在、没跑转发检查）都不算——否则面板会把一个
            # 没验过的节点标成「本批最快」，与 auto 项标签“按延迟 + 转发验证挑最优”不符。
            if entry.get("forwarding") and (fastest is None or latency < fastest):
                fastest = latency
                fastest_tag = entry.get("tag")
    _LAST_FASTEST[source] = (fastest, fastest_tag)
    if len(_LATENCY_CACHE) > _LATENCY_CACHE_MAX:
        _LATENCY_CACHE.clear()


def _outbound_options(source: str | None = None) -> dict[str, Any]:
    """「出站选择」指定来源（缺省 active_source）的候选摘要 + available_nodes 计数 + 当前选择（不含凭据）。"""
    cfg = load_config()
    src = source if source in ("node", "sub", "json") else active_source_of(cfg)
    candidates = slot_candidates(cfg, src)
    n = len(candidates)
    label = SOURCE_LABEL.get(src or "", "来源")

    options: list[dict[str, Any]] = []
    if n >= 2:
        fastest_ms, fastest_tag = _LAST_FASTEST.get(src, (None, None))
        options.append(
            {
                "key": "auto",
                "source": label,
                "tag": "自动优选（按延迟 + 转发验证挑最优）",
                "latency_ms": fastest_ms,
                "fastest_tag": fastest_tag,
            }
        )
    for node in candidates:
        opt = _option_summary(node_key(node), label, node)
        opt["latency_ms"] = _LATENCY_CACHE.get(
            (opt.get("type"), opt.get("server"), opt.get("server_port"))
        )
        options.append(opt)

    selected: dict[str, Any] | None = None
    sel = slot_selection(cfg, src)
    if n == 1:
        selected = {"mode": "pinned", "node_key": node_key(candidates[0])}
    elif sel["mode"] == "pinned":
        selected = {"mode": "pinned", "node_key": sel["node_key"]}
    elif sel["mode"] == "auto":
        selected = {"mode": "auto", "node_key": None}

    return {
        "source": src or None,
        "available_nodes": n,
        "selected": selected,
        "options": options,
    }


# 静态资源白名单：只暴露这几个文件，从根上杜绝路径穿越
# 本进程起的面板服务句柄，以及跑 serve_forever 的那个线程：退出路径要用它们把服务关掉
# （见 `stop_ui_server`，同一线程里关自己会死锁）。
_UI_SERVER: Any = None
_UI_SERVER_THREAD: Any = None


_STATIC_ROUTES: dict[str, str] = {
    "/": "index.html",
    "/index.html": "index.html",
    "/style.css": "style.css",
    "/app.js": "app.js",
    "/logo.svg": "logo.svg",
}

_CONTENT_TYPES: dict[str, str] = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".svg": "image/svg+xml",
}


class ChannelRequestHandler(http.server.BaseHTTPRequestHandler):
    def log_message(self, format: str, *args: Any) -> None:
        """静默 HTTP 访问日志，避免污染 stdout。"""

    def _send_json(self, data: dict[str, Any], status: int = 200) -> None:
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_asset(self, body: bytes, content_type: str) -> None:
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        # 本地工具的面板资源一律不缓存：升级后如果浏览器还留着旧 app.js，
        # 前端就会按旧逻辑发请求（例如测速不发 source），表现为“新后端 + 旧前端”的怪故障。
        self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
        self.send_header("Pragma", "no-cache")
        self.end_headers()
        self.wfile.write(body)

    def _serve_static(self, name: str) -> bool:
        """读取并返回 web/ 下的静态资源；名称只能来自白名单。"""
        path = (WEB_DIR / name).resolve()
        if not path.is_file() or not path.is_relative_to(WEB_DIR):
            return False
        content_type = _CONTENT_TYPES.get(path.suffix, "application/octet-stream")
        try:
            body = path.read_bytes()
        except OSError:
            return False
        self._send_asset(body, content_type)
        return True

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path
        query = dict(parse_qsl(parsed.query))
        static_name = _STATIC_ROUTES.get(path)
        if static_name is not None:
            if not self._serve_static(static_name):
                self.send_error(500, "Static asset unavailable")
        elif path == "/api/status":
            status = get_channel_status()
            # 配置读取失败过就如实带出来（面板显示与否另说，先让数据到得了）
            status["config_warning"] = config_warning()
            cfg = load_config()
            running_source = cfg.get("running_source")
            # source = 面板正在浏览的标签（?source= 传入，默认 active_source）。
            req_src = query.get("source")
            src = (
                req_src if req_src in ("node", "sub", "json") else active_source_of(cfg)
            )
            status["source"] = src or None
            status["running_source"] = running_source
            # 状态卡显示 running_source ?? active_source：启动后又切标签仍显示“跑的那条线”。
            display = running_source or active_source_of(cfg)
            status["source_label"] = SOURCE_LABEL.get(display or "", "未配置")
            self._send_json(status)
        elif path == "/api/config":
            # 红线：imported_nodes 含凭据，只落 config.json，不回前端；只回数量。
            cfg = load_config()
            imported_count = len(cfg.get("imported_nodes") or [])
            cfg.pop("imported_nodes", None)
            cfg["imported_count"] = imported_count
            self._send_json(cfg)
        elif path == "/api/uuid":
            self._send_json({"uuid": generate_uuid()})
        elif path == "/api/core_info":
            self._send_json({"local_version": get_local_core_version()})
        elif path == "/api/check_update":
            self._send_json(check_core_update())
        elif path == "/api/outbound_options":
            self._send_json(_outbound_options(query.get("source")))
        elif path == "/api/geolocation":
            self._handle_geolocation()
        else:
            self.send_error(404, "Not Found")

    # 写接口守卫参数
    _MAX_POST_BYTES = 8 * 1024 * 1024
    _LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "::1")

    def _reject_cross_site(self) -> bool:
        """跨站写请求一律拒；返回 True 表示已拒绝并已响应。

        为什么必须有这道：浏览器允许任意网页向 `127.0.0.1:<port>` 发**简单请求**
        （表单编码 / text-plain 的 fetch 都不触发 CORS 预检），而本面板的写接口能停
        通道、改订阅（等于把用户流量引到攻击者节点）、替换内核二进制。响应读不到
        无所谓 —— 副作用已经发生。

        放行规则（宁可保守在“拒”，但不能把正常路径一起拒了）：
        - **Origin 缺失 → 放行**：curl、CLI、托盘、守护进程、以及任何非浏览器的本机
          调用都不带 Origin；挡掉它们等于把正常路径一起挡掉。
        - 有 Origin 时：按 **Host 头归一化**比对 —— Origin 的 host:port 必须与请求的
          Host 完全一致，且 host 属于回环三写法。按 Host 归一化而不是写死
          `http://127.0.0.1:<port>`：用户手输 `http://localhost:18888` 打开面板时，
          同源 POST 的 Origin 就是 `http://localhost:18888`，写死比对会把他 403。
        - `Sec-Fetch-Site` 存在且为 `cross-site` → 直接拒（浏览器的跨站信号）。
        """
        fetch_site = (self.headers.get("Sec-Fetch-Site") or "").strip().lower()
        if fetch_site == "cross-site":
            self._send_json({"error": "拒绝跨站写请求"}, status=403)
            return True
        origin = (self.headers.get("Origin") or "").strip()
        if not origin:
            return False
        parsed = urlparse(origin)
        request_host = (self.headers.get("Host") or "").strip()
        if (
            parsed.hostname is None
            or parsed.netloc != request_host
            or parsed.hostname not in self._LOOPBACK_HOSTS
        ):
            self._send_json({"error": "拒绝非本机来源的写请求"}, status=403)
            return True
        return False

    def _read_json_body(self) -> dict[str, Any] | None:
        """读 JSON 请求体；不合法时已响应 4xx 并返回 None。

        - 强制 `application/json`（415）：表单编码的跨站请求也走这条路，挡掉它；
        - 上限 8 MiB（413）—— 与订阅读取的限长同款思路；
        - 解析失败**不再兜底成空 `{}`**（400）：旧行为会把一个坏请求当成“空操作”执行。
        """
        content_type = (
            (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        )
        if content_type != "application/json":
            self._send_json({"error": "写接口只接受 application/json"}, status=415)
            return None
        try:
            content_len = int(self.headers.get("Content-Length") or 0)
        except (TypeError, ValueError):
            self._send_json({"error": "Content-Length 非法"}, status=400)
            return None
        if content_len > self._MAX_POST_BYTES:
            # 不读完 body 就回错：为了报错先把 9 MiB 读进内存，等于自己做成 DoS
            self.close_connection = True
            self._send_json(
                {"error": f"请求体过大（上限 {self._MAX_POST_BYTES} 字节）"}, status=413
            )
            return None
        raw = self.rfile.read(content_len) if content_len > 0 else b""
        if not raw:
            return {}
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            self._send_json({"error": "请求体不是合法 JSON"}, status=400)
            return None
        if not isinstance(payload, dict):
            self._send_json({"error": "请求体必须是 JSON 对象"}, status=400)
            return None
        return payload

    def do_POST(self) -> None:
        # 守卫顺序：先来源（便宜、与 body 无关），再 Content-Type，再长度，最后解析
        if self._reject_cross_site():
            return
        payload = self._read_json_body()
        if payload is None:
            return  # 已响应 4xx

        if self.path == "/api/config":
            # 这一处**本来就有**返回值守护（面板也会弹“保存失败”）；这里只是把失败原因
            # 一并回给前端，口径与其他三处对齐
            saved = save_config(payload)
            self._send_json(
                {"success": saved, "error": None if saved else save_error()}
            )
        elif self.path == "/api/sysproxy":
            self._handle_sysproxy(payload)
        elif self.path == "/api/enable":
            self._handle_enable(payload)
        elif self.path == "/api/disable":
            try:
                disable_channel()
            except ChannelBusyError as exc:
                # 启停共用一把锁：忙就当场回 409，不让"停止"按钮干等一次启用跑完
                self._send_json({"error": str(exc)}, status=409)
                return
            self._send_json({"status": "stopped"})
        elif self.path == "/api/update_core":
            self._send_json(update_core())
        elif self.path == "/api/test_ai":
            self._handle_test_ai()
        elif self.path == "/api/benchmark":
            self._handle_benchmark(payload)
        elif self.path == "/api/import_nodes":
            self._handle_import_nodes(payload)
        elif self.path == "/api/set_outbound":
            self._handle_set_outbound(payload)
        elif self.path == "/api/quit":
            self._handle_quit()
        else:
            self.send_error(404, "Not Found")

    def _handle_sysproxy(self, payload: dict[str, Any]) -> None:
        if not payload.get("enabled", False):
            self._send_json({"success": unset_system_proxy()})
            return
        cur = get_channel_status()
        port = cur.get("port")
        if not port or cur.get("status") != "running":
            self._send_json(
                {"success": False, "error": "通道尚未处于运行状态，请先启动通道"},
                status=400,
            )
            return
        self._send_json({"success": set_system_proxy(port=port)})

    def _handle_enable(self, payload: dict[str, Any]) -> None:
        try:
            # 整段进启停互斥：忙就当场 409，且**忙时连配置都不动**（见 core.channel_operation）
            with channel_operation():
                # 启用以请求为准，禁止回落。payload 必须显式给出 source 与地址；
                # 说不出来源就明确拒绝，不得回落到 config.json 的既有来源。写盘是结果记录，
                # 不是判据来源（旧 `if any(given)` 一空就跳过写盘、随后读回老配置，
                # 就是 Linux 真机那次“老配置堵在 JSON”的机制）。
                src = payload.get("source")
                if src not in ("node", "sub", "json"):
                    self._send_json({"error": "请先在面板里选择来源并填入地址"}, status=400)
                    return

                given: dict[str, Any] = {}
                if src == "node":
                    node_uri = str(payload.get("node_uri") or "").strip()
                    if not node_uri:
                        self._send_json(
                            {"error": "请先在面板里选择来源并填入地址"}, status=400
                        )
                        return
                    given["node_uri"] = node_uri
                elif src == "sub":
                    subscription_url = str(payload.get("subscription_url") or "").strip()
                    if not subscription_url:
                        self._send_json(
                            {"error": "请先在面板里选择来源并填入地址"}, status=400
                        )
                        return
                    given["subscription_url"] = subscription_url
                # src == "json"：候选来自 imported_nodes（导入即写入），此处无地址可写。
                given["active_source"] = src

                # 先按「即将生效的配置」校验，**校验通过才落盘**：失败的动作不该改持久状态。
                # 此前是先把 active_source 写盘再校验 —— 在空的 json 标签点「启动」会先把
                # 生效来源改成 json，转头又报 400「未配置节点」，用户什么都没做成、配置却变了。
                merged = dict(load_config())
                merged.update(given)
                # 状态与文案统一走 channel_state：未配置 / 未选出站 / 所选节点失效三档只有
                # 一处定义点，面板启用被拒的话术与状态卡因此完全一致（不再各写一套）。
                st = channel_state(source=src, config=merged)
                if st["state"] != "ok":
                    self._send_json({"error": st["message"]}, status=400)
                    return
                if not save_config(merged):
                    # 写不进去就别启动：随后读回的是老配置，拿它启用等于“静默用错出口”
                    # （Linux 真机那次就是这个形态）
                    self._send_json(
                        {"error": f"配置写入失败，已中止启动：{save_error()}"},
                        status=400,
                    )
                    return

                node: dict[str, Any] | None = None
                cfg = merged

                candidates = st["candidates"]
                note: str | None = None
                sel = slot_selection(cfg, src)
                if len(candidates) == 1:
                    # 单节点来源（或仅一个可用节点）：直接用，退化为直接使用
                    node = candidates[0]
                    if sel["mode"] == "auto":
                        note = "自动优选（仅一个节点）"
                elif sel["mode"] == "pinned":
                    # state == "ok" 已保证能找到；直接取 channel_state 解出的那个，不再二次查表
                    node = st["kwargs"]["nodes"][0]
                # sel["mode"] == "auto"：node 保持 None（走池）

                if node is not None:
                    status = dict(
                        enable_channel(
                            node=node,
                            auto_patch=True,
                            set_sys_proxy=payload.get("set_sys_proxy", False),
                            listen_port=DEFAULT_LISTEN_PORT,
                        )
                    )
                else:
                    status = dict(
                        enable_channel(
                            nodes=candidates,
                            auto_patch=True,
                            set_sys_proxy=payload.get("set_sys_proxy", False),
                            listen_port=DEFAULT_LISTEN_PORT,
                        )
                    )
                # 启用成功才写 running_source 快照（与 active_source 同值）；回执带生效来源。
                if not save_config({"running_source": src}):
                    # 通道已经跑起来了，快照写失败只影响状态卡显示，不该让启用失败；
                    # 但要留下痕迹，别让"显示的还是上一条线"变成无解之谜。
                    warn_stderr(
                        f"运行来源快照写入失败（{save_error()}）：状态卡可能仍显示旧来源"
                    )
                if note:
                    status["outbound_note"] = note
                status["source"] = src
                self._send_json(status)
        except ChannelBusyError as exc:
            # 有另一次启停/切换在跑：409 让面板立刻收到明确答复（详见 core._begin_channel_operation）
            self._send_json({"error": str(exc)}, status=409)
        except Exception as e:
            self._send_json({"error": str(e)}, status=400)

    def _handle_test_ai(self) -> None:
        """一键测试：报告三大 AI 接口的**真机通达**（这是本工具的目的契约）。

        通道未运行时**不拒绝测试**（直连结果仍有诊断价值），但必须标注诚实：结果带
        `via`（channel/direct）与 `channel_running`，前端据此提示。不这么做的话，直连成功
        会被读成“通道没问题”—— 健康检查说谎是本清单里性质最差的一类。
        """
        status = get_channel_status()
        proxy = status.get("proxy_url")
        running = bool(proxy) and status.get("status") == "running"
        # 通道没跑就不用（可能是残留的）代理地址，保证 via=direct 与“真的没走代理”一致
        effective_proxy = proxy if running else None
        payload: dict[str, Any] = {
            "channel_running": running,
            "via": "channel" if running else "direct",
            "openai": test_single_endpoint(
                "https://api.openai.com/v1/models", effective_proxy
            ),
            "gemini": test_single_endpoint(
                "https://generativelanguage.googleapis.com", effective_proxy
            ),
            "anthropic": test_single_endpoint(
                "https://api.anthropic.com", effective_proxy
            ),
        }
        if not running:
            payload["note"] = "通道未运行，以下为直连结果，不代表通道可用"
        self._send_json(payload)

    def _handle_quit(self) -> None:
        """退出整套程序：先回响应，再在后台线程里走与托盘「退出」同一套清理。

        顺序不能反：`shutdown_app()` 会把这个 HTTP 服务也关掉，若在请求线程里先关服务，
        客户端就拿不到这次响应，面板会显示成一个没有反馈的按钮。
        """
        self._send_json({"status": "quitting", "message": "正在停止通道并退出…"})

        def _quit_soon() -> None:
            time.sleep(0.3)  # 留给响应发出去
            shutdown_app()

        threading.Thread(target=_quit_soon, daemon=True).start()

    def _handle_geolocation(self) -> None:
        """探测落地 IP 的地理位置与天气（走当前通道代理，反映节点出口的真实落地）。"""
        try:
            status = get_channel_status()
            proxy = status.get("proxy_url")
            if not proxy or status.get("status") != "running":
                self._send_json({"error": "通道未运行，无法探测落地 IP"}, status=400)
                return
            data = geolocate_via_proxy(proxy)
            if "error" in data:
                self._send_json(data, status=400)
                return
            self._send_json(data)
        except Exception as e:
            self._send_json({"error": str(e)}, status=400)

    def _handle_benchmark(self, payload: dict[str, Any]) -> None:
        try:
            # 按**面板正在浏览的标签**测（与出站下拉同一口径）：切到订阅标签就测订阅。
            # 不能用 active_source——否则“在单节点标签启用过、切到订阅再测速”会测回单节点，
            # 用户看到的“测试通畅”是旧来源的结果。缺省/非法值才回落 active_source。
            cfg = load_config()
            src = payload.get("source")
            if src not in ("node", "sub", "json"):
                src = active_source_of(cfg)
            nodes = slot_candidates(cfg, src)
            results = benchmark_nodes(nodes=nodes)
            _record_benchmark_latency(results, src)
            # 回带 source：面板据此显示“来源：JSON”，让用户一眼确认测的是哪个槽。
            self._send_json({"results": results, "source": src})
        except Exception as e:
            self._send_json({"error": str(e)}, status=400)

    def _handle_import_nodes(self, payload: dict[str, Any]) -> None:
        try:
            if payload.get("action") == "clear":
                if not save_config({"imported_nodes": []}):
                    self._send_json(
                        {"error": f"清空结果写入失败：{save_error()}"}, status=500
                    )
                    return
                self._send_json({"imported": 0, "cleared": True, "total": 0})
                return
            nodes, errors, renamed = parse_singbox_json(str(payload.get("text") or ""))
            # 导入≠启用：只写 imported_nodes，不写 active_source。
            # 追加 + 去重（按 type + server + server_port），不覆盖已有导入节点
            current = list(load_config().get("imported_nodes") or [])
            seen = {
                (n.get("type"), n.get("server"), n.get("server_port")) for n in current
            }
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
                self._send_json(
                    {"error": f"导入结果写入失败：{save_error()}"}, status=500
                )
                return
            self._send_json(
                {
                    "imported": added,
                    "errors": errors,
                    "duplicates": duplicates,
                    "renamed": renamed,
                    "total": len(current),
                }
            )
        except Exception as e:
            self._send_json({"error": str(e)}, status=400)

    def _handle_set_outbound(self, payload: dict[str, Any]) -> None:
        """设置「出站选择」：auto（并池优选）或 pinned（指定节点）。

        前端传 {mode, node_key?, source?}；source 指定槽位（面板正在浏览的标签），
        缺省回落 active_source；node_key 用 (type, server, server_port) 字符串，
        服务端校验它确实属于该槽位后才落 config。
        """
        mode = str(payload.get("mode") or "")
        cfg = load_config()
        src = payload.get("source") or active_source_of(cfg)
        if src not in ("node", "sub", "json"):
            self._send_json({"error": "未配置来源，无法选择出站"}, status=400)
            return
        if mode == "auto":
            selections = updated_selections(cfg, src, "auto")
            if not save_config({"outbound_selections": selections}):
                self._send_json({"error": f"写入失败：{save_error()}"}, status=500)
                return
            self._send_json({"success": True, "outbound_mode": "auto"})
            return
        if mode == "pinned":
            key = str(payload.get("node_key") or "")
            node = find_candidate(cfg, key, source=src)
            if node is None:
                self._send_json({"error": "节点不存在或无法解析"}, status=400)
                return
            selections = updated_selections(cfg, src, "pinned", key)
            if not save_config({"outbound_selections": selections}):
                self._send_json({"error": f"写入失败：{save_error()}"}, status=500)
                return
            self._send_json(
                {"success": True, "outbound_mode": "pinned", "tag": node.get("tag")}
            )
            return
        self._send_json({"error": "出站模式无效"}, status=400)


def _is_port_free(port: int) -> bool:
    """探测端口是否真的空闲。

    Windows 的 SO_REUSEADDR 允许重复绑定同一端口（不会报 OSError），
    所以不能只靠 bind 异常判断占用，必须显式试绑。
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        try:
            probe.bind(("127.0.0.1", port))
        except OSError:
            return False
    return True


class PanelHTTPServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    """面板的 HTTP 服务。

    `handle_error` 有意收窄：客户端提前断开（关标签页、`curl --max-time` 到点、
    3 秒轮询被打断）会让写入抛 `BrokenPipeError`/`ConnectionResetError`——这是常态，
    不是错误；原样走 `socketserver` 默认实现会把整段回溯刷进日志，把真正的故障淹掉。
    其它异常照旧按默认行为打印回溯。
    """

    daemon_threads = True

    def handle_error(self, request: Any, client_address: Any) -> None:
        exc = sys.exc_info()[1]
        if isinstance(exc, (BrokenPipeError, ConnectionResetError)):
            return
        super().handle_error(request, client_address)


def launch_ui(
    port: int = DEFAULT_UI_PORT, open_browser: bool = True, block: bool = False
) -> str:
    """启动本地控制面板；端口被占用时自动改用空闲端口。返回面板地址。

    返回地址是给外层外壳窗口用的（窗口要显示"在浏览器中打开"的真实端口，
    端口被占用时这个值会与传入的不同）。
    """

    # 首次启动时把随包预置的内核播种到本地目录，使面板/通道开箱可用
    ensure_local_core()

    # 启动期自愈：清掉上次异常退出留下的残留内核与悬挂系统代理。
    # 这里先要求「本进程没有通道在跑」——launch_ui 并不专属于启动期，托盘「打开
    # 面板」与打包 exe 入口都会调它；真正的判据在 heal_stale_kernel 内部（跳过本
    # 进程活内核与父进程仍在的内核），这里只是多一道前置。
    if get_channel_status().get("status") != "running":
        for note in heal_stale_state(DEFAULT_LISTEN_PORT):
            print(f"[agent-channel通道代理] {note}")

    if not _is_port_free(port):
        port = get_free_port()
    server = PanelHTTPServer(("127.0.0.1", port), ChannelRequestHandler)
    global _UI_SERVER
    _UI_SERVER = server

    url = f"http://127.0.0.1:{port}"
    print(f"[agent-channel通道代理] 控制与测试面板已启动: {url}", flush=True)

    if open_browser:
        webbrowser.open(url)

    global _UI_SERVER_THREAD
    if block:
        _UI_SERVER_THREAD = threading.current_thread()
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            server.shutdown()
    else:
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        _UI_SERVER_THREAD = worker
        worker.start()
    return url


def _close_ui_server(server: Any) -> None:
    try:
        server.shutdown()
        server.server_close()
    except Exception as exc:  # 关不掉也要继续退出流程
        warn_stderr(f"关闭面板服务失败（忽略）: {exc}")


def stop_ui_server() -> bool:
    """关掉本进程起的面板服务；返回是否确实关掉了一个。

    `shutdown()` 要等 serve_forever 的循环退出，所以**不能**由正在跑那个循环的线程来调：
    SIGTERM 处理器就正好在主线程上，而主线程此刻卡在 serve_forever 里，直接等会死锁
    （实测：面板端口释放不掉、进程也不退出）。这种情况交给后台线程收尾，本线程继续往上
    抛 SystemExit 结束进程。
    """
    global _UI_SERVER, _UI_SERVER_THREAD
    server, _UI_SERVER = _UI_SERVER, None
    serving_thread, _UI_SERVER_THREAD = _UI_SERVER_THREAD, None
    if server is None:
        return False
    if serving_thread is threading.current_thread():
        threading.Thread(target=_close_ui_server, args=(server,), daemon=True).start()
        return True
    _close_ui_server(server)
    return True
