import atexit
import os
import signal
import sys
import threading
import time
from collections.abc import Iterator
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from contextlib import contextmanager, suppress
from pathlib import Path
from typing import Any

from .binary import ensure_local_core, get_local_binary_path
from .parsers import extract_nodes_from_subscription, parse_single_node
from .patcher import patch_environment, unpatch_environment
from .pinger import UDP_ONLY_TYPES, ping_node, rank_nodes
from .probe import geolocate_via_proxy, test_single_endpoint
from .runner import SingboxRunner, build_minimal_config, heal_stale_kernel
from .sysproxy import (
    get_system_proxy_status,
    heal_stale_system_proxy,
    set_system_proxy,
    unset_system_proxy,
)
from .utils import MAX_PROBE_BYTES, get_free_port, read_limited, warn_stderr

_CURRENT_STATUS: dict[str, Any] = {"status": "stopped"}
_ACTIVE_RUNNER: SingboxRunner | None = None

# 启停串行化：面板是 ThreadingHTTPServer，两个并发 /api/enable 会各起一个内核、
# 后写的 _ACTIVE_RUNNER 把前一个指掉，泄漏一个占着端口与内核文件锁的进程。
# 用 RLock 而不是 Lock —— `_enable_channel` 内部会调 `disable_channel`，普通锁会自死锁。
_CHANNEL_LOCK = threading.RLock()


class ChannelBusyError(RuntimeError):
    """已有通道启停/切换操作在进行中（见 `_CHANNEL_LOCK`）。"""


def _begin_channel_operation() -> None:
    """非阻塞取启停锁；已有操作在跑就当场报忙，而不是排队干等。

    面板的"启动 / 停止"是同步 HTTP 请求，弱网下一次启用要跑订阅拉取 + 逐候选转发验证，
    可以长达一分多钟。排队意味着用户点了"停止"却几十秒没有任何反馈（看起来像卡死）；
    这里改成当场拒绝，由 HTTP 层回 409 并说清"有操作正在进行中"。

    RLock 对**持锁线程自己**可重入：`_enable_channel` 内部要调 `disable_channel()` 拆旧，
    那条路径不会被自己误判成"忙"。
    """
    if not _CHANNEL_LOCK.acquire(blocking=False):
        raise ChannelBusyError("已有通道操作正在进行中，请等它结束后再试")


@contextmanager
def channel_operation() -> Iterator[None]:
    """把一段操作纳入启停互斥；忙则立刻抛 `ChannelBusyError`（不排队）。

    公开入口 `enable_channel` / `disable_channel` 自己就用它；HTTP 层另用它把
    "校验 → 落盘 → 启用"整段包住 —— 这样忙时连配置都不动（失败的动作不改持久状态），
    两条并发写接口也不会交错改配置。
    """
    _begin_channel_operation()
    try:
        yield
    finally:
        _CHANNEL_LOCK.release()


# 转发验证的探针目标：**多目标取其一**。只用单个 Google 探针时，"目标不可达"会被
# 判成"节点不可用"——实测过一次：某出口把 google 探针挡住，youtube 照跑，测速却是 0/7
# 全灭。三个目标都取小响应体（204 / 追踪文本 / robots.txt），不多花带宽。
_FORWARDING_PROBES: tuple[tuple[str, str], ...] = (
    ("google", "https://www.google.com/generate_204"),
    ("cloudflare", "https://www.cloudflare.com/cdn-cgi/trace"),
    ("youtube", "https://www.youtube.com/robots.txt"),
)


def _verify_proxy_forwarding(proxy_url: str, timeout: float = 12.0) -> tuple[bool, str]:
    """通过本地代理访问海外探测地址，验证节点是否能真实转发流量。

    三个目标**并发**探测、任一应答即判可用，返回 `(是否可用, 说明)`：

    - 可用 → 说明是哪个目标答的（如 ``youtube``）；
    - 三目标全挂 → 再用落地探测（城市+天气那套）做**最后兜底**：它答了就判可用，
      说明写明是兜底应的（如 ``落地探测兜底应答（首尔，South Korea；前三目标仍不可达）``）；
    - 兜底也没答 → 说明里逐个列出目标与错误（如 ``3 个探针目标全部不可达
      （google: <错误>；cloudflare: <错误>；youtube: <错误>）；落地探测失败: <错误>``）。
      这句陈述的是**探针观测**，不替调用方断言"节点死了"——实不实用于调用方结合上下文
      判断（启动自检仍会拒绝，但报错里带着原因）。

    探测的可靠性前提——**必走给定代理、不被 no_proxy 静默绕过**——由 `probe.py` 导入期
    对 `urllib.request.proxy_bypass` 的进程级禁用保证；否则带 `NO_PROXY=*` 的宿主机上
    会给死代理报出"可达"（假阳），这四个信号会集体失效。

    为什么并发而非顺序：失败路径的耗时不能因多探针翻倍（顺序最坏 3×12s，
    这里最坏仍约等于单目标的 12s，且任一目标先答就立刻收工）。任何 HTTP 状态码
    都算链路可达（复用 ``test_single_endpoint`` 的既有语义：服务端已应答 = 链路通），
    所以"目标返回 403"不再被记成转发失败。

    仅 TCP 握手可达不代表节点可用（例如 Reality 握手失败的节点），
    必须实际请求一次海外站点确认数据能往返。

    超时给得宽（12s）：节点域名若同时有 AAAA（IPv6）而该 IPv6 不可达，
    内核会先拨 IPv6 直到超时（实测约 7s）才回落 IPv4，首连合计可达 8s+；
    给 3~4s 会把这种"能通但首连慢"的节点误判为转发失败。
    """
    results: dict[str, dict[str, Any]] = {}
    deadline = time.monotonic() + timeout + 3.0
    with ThreadPoolExecutor(max_workers=len(_FORWARDING_PROBES)) as pool:
        futures = {
            pool.submit(test_single_endpoint, url, proxy_url, timeout): name
            for name, url in _FORWARDING_PROBES
        }
        pending = set(futures)
        while pending:
            left = deadline - time.monotonic()
            if left <= 0:
                break
            done, pending = wait(pending, timeout=min(left, 0.1), return_when=FIRST_COMPLETED)
            for future in done:
                name = futures[future]
                try:
                    result = future.result()
                except Exception as exc:  # 线程内异常如实记成该目标的观测，不炸整个验证
                    result = {"reachable": False, "error": str(exc).strip() or repr(exc)}
                results[name] = result
                if result.get("reachable"):
                    return True, name  # 任一目标答了就收工，不等慢的那个
        for future in pending:  # 预算内没等到的按未应答如实记下
            results[futures[future]] = {"reachable": False, "error": "预算内未应答"}

    detail = "；".join(
        f"{name}: {result.get('error') or 'HTTP ' + str(result.get('status'))}"
        for name, result in results.items()
    )

    # ---- 三目标全挂时的最后兜底：落地探测（城市+天气）当第四个外部信号 ----
    # 它走的也是同一个出口：能答就说明链路其实通（那三个目标属特异性不可达），不判
    # 节点不可用；答不上来就把它的观测并进下面的说明。
    geo = geolocate_via_proxy(proxy_url, timeout=timeout)
    if not geo.get("error"):
        place = "，".join(
            part for part in (geo.get("city"), geo.get("country")) if part
        ) or str(geo.get("ip") or "")
        return True, f"落地探测兜底应答（{place}；前三目标仍不可达）"
    return False, (
        f"{len(results)} 个探针目标全部不可达（{detail}）；{geo.get('error')}"
    )


# 订阅地址只接受 http/https，避免 file:// 等方案被用来读取本地文件
_ALLOWED_URL_SCHEMES = ("http://", "https://")

# 订阅响应上限：订阅源不受信，无上限的 read() 会被无限流/超大响应打爆内存。
# 8s 超时只挡得住慢速攻击，挡不住「快而大」。
_MAX_SUBSCRIPTION_BYTES = 5 * 1024 * 1024


def _read_limited(resp: Any, limit: int = _MAX_SUBSCRIPTION_BYTES) -> bytes:
    """订阅响应的限长读。

    实现见 `utils.read_limited`（与落地探测共用一份）；保留这个薄封装是因为调用点
    与测试都按这个名字用法，且它带订阅专用默认上限。
    """
    return read_limited(resp, limit)


def _fetch_subscription(url: str) -> str:
    """下载订阅内容并解码为文本。

    显式绕过系统代理：订阅是启动通道的输入。若系统代理仍指向已停止的本机端口，
    走代理会直接连接被拒，导致“通道停了就再也起不来”的自举失败。
    """
    target = url.strip()
    if not target.startswith(_ALLOWED_URL_SCHEMES):
        raise ValueError("订阅地址必须以 http:// 或 https:// 开头")
    import urllib.request

    req = urllib.request.Request(target, headers={"User-Agent": "v2rayN"})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(req, timeout=8) as resp:
        return _read_limited(resp).decode("utf-8", errors="ignore")


def heal_stale_state(listen_port: int) -> list[str]:
    """启动期自愈：清掉上次异常退出留在系统里的残留内核与悬挂系统代理。

    返回给用户看的一句话列表（没有残留时为空）。幂等，可重复调用。
    """
    notes: list[str] = []
    if heal_stale_system_proxy(listen_port):
        notes.append(f"已关闭指向已停止通道的系统代理（127.0.0.1:{listen_port}）")
    # heal_stale_kernel 自己生成提示（含「跳过」的原因），直接并入
    notes.extend(heal_stale_kernel(listen_port))
    return notes


def enable_channel(
    node_uri: str | None = None,
    subscription_url: str | None = None,
    auto_patch: bool = True,
    set_sys_proxy: bool = False,
    candidate_limit: int = 8,
    connect_timeout: float = 1.5,
    listen_port: int | None = None,
    node: dict[str, Any] | None = None,
    nodes: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """启用通道的公开入口：整段持锁，使并发启用串行化（见 `_CHANNEL_LOCK`）。

    锁被别的线程持有时抛 `ChannelBusyError`（不排队等待）——HTTP 层据此回 409。
    """
    with channel_operation():
        return _enable_channel(
            node_uri=node_uri,
            subscription_url=subscription_url,
            auto_patch=auto_patch,
            set_sys_proxy=set_sys_proxy,
            candidate_limit=candidate_limit,
            connect_timeout=connect_timeout,
            listen_port=listen_port,
            node=node,
            nodes=nodes,
        )


def _enable_channel(
    node_uri: str | None = None,
    subscription_url: str | None = None,
    auto_patch: bool = True,
    set_sys_proxy: bool = False,
    candidate_limit: int = 8,
    connect_timeout: float = 1.5,
    listen_port: int | None = None,
    node: dict[str, Any] | None = None,
    nodes: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """
    启用网络通道（独立内核转发）。
    - 目标来源：node（单节点字典）、node_uri（单节点链接）、subscription_url（订阅）、
      nodes（导入节点列表），合并进候选池自动排序 + 转发验证。
    - listen_port 可选，指定独立内核的本地监听端口；默认 None 时自动分配空闲端口。
    """
    global _CURRENT_STATUS, _ACTIVE_RUNNER

    # 自愈（残留内核 / 悬挂系统代理）不在这里做：本函数同时承担「换节点」，
    # 此时旧通道正活着并占着端口，无条件清理会先杀掉活内核、破坏下方
    # 「先验证后拆旧」的平滑切换保证。调用点见 cli._cmd_start 与 server.launch_ui。

    # 先把“要换成什么”定下来，再动现有通道：解析节点、拉订阅、探本地端口、检查内核
    # 是否就绪都可能失败，而一次写错的订阅或一个坏节点不该把正在跑的通道（连系统代理）
    # 一起拆掉 —— 那是把“启用失败”放大成“断网”。
    # 1. 独立内核模式（单节点、订阅或导入节点列表）
    # 当前三个入口（面板 /api/enable、CLI start、守护 launch_service）都走 nodes=/node=，
    # node_uri / subscription_url 两个分支只服务公开 Python API enable_channel(...) 的
    # 直接调用与测试，属公开签名的一部分，保留（legacy 入口）。
    candidates: list[dict[str, Any]] = []
    sub_errors: list[str] = []
    if nodes:
        candidates.extend(nodes)
    if node and isinstance(node, dict):
        candidates.append(node)
    elif node_uri and node_uri.strip():
        parsed_node = parse_single_node(node_uri.strip())
        if parsed_node:
            candidates.append(parsed_node)
    elif subscription_url and subscription_url.strip():
        try:
            raw = _fetch_subscription(subscription_url)
        except Exception as exc:
            sub_nodes, sub_errors = [], [f"订阅拉取失败: {exc}"]
        else:
            sub_nodes, sub_errors = extract_nodes_from_subscription(raw)
        candidates.extend(sub_nodes)
    if candidates:
        candidates = candidates[:candidate_limit]

    if candidates:
        ordered_nodes = rank_nodes(candidates, timeout=connect_timeout)

        ensure_local_core()
        bin_path = get_local_binary_path()
        if not bin_path.exists():
            raise FileNotFoundError(
                f"sing-box 二进制未就绪: {bin_path}。请先在面板点击'检查官方更新'下载安装内核。"
            )

        config_path = bin_path.parent / "active_config.json"
        last_error: Exception | None = None
        # 影子验证期间旧通道仍在服务，所以先把它现在用的配置存下来：万一“拆旧后回绑
        # 固定端口”失败，用这份把旧通道绑回去（见下面的残留守卫）。
        previous_config = config_path.read_bytes() if config_path.exists() else None

        for candidate in ordered_nodes:
            # ---- 第一段：影子验证 ----
            # 在临时空闲端口起内核并验证真实转发，**此时旧通道照旧服务**：候选不通过
            # 也不会把它带走。这是相对“先停旧再逐个试”的核心收益（那种做法在候选全挂时
            # 会真的没通道，而现状的失败是零损失）。
            probe_port = get_free_port()
            runner = SingboxRunner(bin_path)
            try:
                runner.start(
                    build_minimal_config(
                        inbound_port=probe_port, outbound_node=candidate
                    ),
                    config_path,
                )
            except Exception as exc:
                last_error = exc
                runner.stop()
                continue

            forwarding_ok, probe_note = _verify_proxy_forwarding(
                f"http://127.0.0.1:{probe_port}"
            )
            if not forwarding_ok:
                runner.stop()
                # 报错里带上探针观测：区分"目标不可达"与"节点不可用"的唯一依据就是它——
                # 全灭时那句是"3 个探针目标全部不可达（…）"，不是断言节点死了。
                last_error = ValueError(
                    f"节点 {candidate.get('tag')} 无法建立有效转发：{probe_note}"
                )
                continue

            # ---- 第二段：候选已证明可用，现在才拆旧 ----
            # 顺序仍然要紧：拆旧必须排在新通道 patch_environment / set_system_proxy 之前
            # —— disable_channel() 会还原环境变量与系统代理，反了就把新通道的设置抹掉。
            port = listen_port if listen_port is not None else probe_port
            if port == probe_port:
                disable_channel()  # 拆旧；影子内核跑在别的端口，不受影响
                active_runner = runner
            else:
                # 固定端口（如 20849）被旧内核占着，而候选必须先证明可用：
                # 影子内核让位 → 拆旧释放端口 → 再把候选绑到固定端口。
                runner.stop()
                disable_channel()
                _wait_port_free(port)
                active_runner = SingboxRunner(bin_path)
                try:
                    active_runner.start(
                        build_minimal_config(
                            inbound_port=port, outbound_node=candidate
                        ),
                        config_path,
                    )
                except Exception as exc:
                    # 残留守卫：旧通道已按切换流程停止，而新内核没能在固定端口起来。
                    # 如实报出真因（不是“节点无法建立转发”），并尝试用上一份配置把旧通道
                    # 绑回去；回绑也失败就明说“当前无通道在跑”。
                    restored = _restore_previous(previous_config, config_path, bin_path)
                    if not restored:
                        _CURRENT_STATUS = {"status": "stopped"}
                    raise RuntimeError(
                        f"旧通道已按切换流程停止，新内核未能在端口 {port} 起（{exc}）；"
                        + (
                            # 回绑成功也不代表“一切照旧”：系统代理没重新挂上（见
                            # _restore_previous 的 system_proxy_set=False），不说清会被误读
                            "已回绑上一配置（注意：系统代理未重新开启，需要时在面板点一下同步开关）"
                            if restored
                            else "回绑上一配置也未成功，当前无通道在跑"
                        )
                    ) from exc

            proxy_url = f"http://127.0.0.1:{port}"
            system_proxy_set = True
            try:
                _ACTIVE_RUNNER = active_runner
                if auto_patch:
                    patch_environment(proxy_url)
                if set_sys_proxy:
                    system_proxy_set = bool(
                        set_system_proxy(port=port, host="127.0.0.1")
                    )
            except Exception:
                # 中途失败：刚起来的内核不能就这么留着 —— 那会是一个谁都指不到、却占着
                # 端口与内核文件锁的进程；状态也要如实（旧通道已拆、新通道没接上 = 什么都没跑）。
                active_runner.stop()
                _ACTIVE_RUNNER = None
                _CURRENT_STATUS = {"status": "stopped"}
                raise

            _CURRENT_STATUS = {
                "status": "running",
                "mode": "standalone",
                "proxy_url": proxy_url,
                "port": port,
                "node_tag": candidate.get("tag"),
                "system_proxy_set": system_proxy_set,
            }
            return _CURRENT_STATUS

        raise ValueError(f"所有候选节点均无法建立有效转发通道: {last_error}")

    # 预检不过就原样报错，一个字节都不动现有通道
    if sub_errors:
        raise ValueError("未找到可用节点或有效订阅：" + "；".join(sub_errors))
    raise ValueError("未找到可用节点或有效订阅")


def _wait_port_free(port: int, timeout: float = 2.0) -> None:
    """等端口真正释放（Windows 上句柄回收有延迟，刚 terminate 的内核可能还占着一瞬）。

    等不到也照常往下走 —— 由随后的 start 失败来如实报错，不在这里静默换端口。
    """
    import socket
    import time

    deadline = time.perf_counter() + timeout
    while time.perf_counter() < deadline:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            try:
                probe.bind(("127.0.0.1", port))
                return
            except OSError:
                time.sleep(0.1)


def _restore_previous(
    previous_config: bytes | None, config_path: Path, bin_path: Path
) -> bool:
    """回绑上一配置：把 active_config.json 还原并重新起内核；失败返回 False。

    只在“候选已在影子端口验证通过、拆旧后却没能绑上固定端口”时用（残留守卫）。
    地址与端口从还原出来的那份配置里读，**不编造**。
    """
    global _ACTIVE_RUNNER
    if not previous_config:
        return False
    import json

    try:
        parsed = json.loads(previous_config.decode("utf-8"))
        config_path.write_bytes(previous_config)
        runner = SingboxRunner(bin_path)
        runner.start(parsed, config_path)
    except Exception:
        return False
    port = None
    for inbound in parsed.get("inbounds", []):
        if inbound.get("listen_port"):
            port = inbound.get("listen_port")
            break
    _ACTIVE_RUNNER = runner
    _CURRENT_STATUS = {
        "status": "running",
        "mode": "standalone",
        "proxy_url": f"http://127.0.0.1:{port}",
        "port": port,
        "node_tag": "（回绑的上一配置）",
        "system_proxy_set": False,
    }
    return True


def disable_channel() -> None:
    """关闭通道的公开入口：与 enable 共用同一把锁（见 `_CHANNEL_LOCK`）。

    忙时抛 `ChannelBusyError`（HTTP 层回 409）；进程退出清理走 `_shutdown_channel`，
    那里是"等待"而不是"报忙"——退出没有"稍后再试"的机会。
    """
    with channel_operation():
        _disable_channel()


def _shutdown_channel(timeout: float | None = None) -> None:
    """退出时的清理入口：等在跑的启停操作结束后再收尾（见 `disable_channel`）。

    `timeout` 只给"用户按了关闭/退出"这类交互路径用：启停一次最长可能要一分钟（拉订阅 +
    逐个候选实测转发），窗口不该为此冻住一分钟。等不到就如实说一声并继续退出，
    残留由下次启动的 `heal_stale_state` 自愈；atexit 那条路仍然无限等（进程已经没别的事做）。
    """
    if timeout is None:
        with _CHANNEL_LOCK:
            _disable_channel()
        return
    if _CHANNEL_LOCK.acquire(timeout=timeout):
        try:
            _disable_channel()
        finally:
            _CHANNEL_LOCK.release()
    else:
        warn_stderr(
            f"有通道操作仍在进行，已等待 {timeout:.0f} 秒；先退出，残留由下次启动自愈"
        )


# 用户主动退出时最多等多久（见 `_shutdown_channel`）
SHUTDOWN_WAIT_SECONDS = 10.0

# 清理只做一次：关闭入口可能同时被面板请求、信号、atexit 触发
_APP_SHUTDOWN_DONE = False


def shutdown_app(exit_code: int = 0) -> None:
    """退出整套程序：停通道、还原系统代理、关掉面板服务，然后结束本进程。

    托盘「退出」、面板「退出」、SIGTERM/SIGINT 三条入口共用这一份实现——各写一遍必然
    漏掉某一步，最典型的是忘了还原系统代理，把用户留在"系统代理指向死端口"的状态里。

    幂等：重复调用只清理一次。在主线程里调用会抛 `SystemExit` 让调用方自然返回
    （窗口关掉 → mainloop 返回 → 进程正常退出）；在工作线程（HTTP 请求线程）里没有
    "返回"可言，清理完直接 `os._exit`。
    """
    global _APP_SHUTDOWN_DONE
    first = not _APP_SHUTDOWN_DONE
    _APP_SHUTDOWN_DONE = True
    if first:
        _shutdown_channel(timeout=SHUTDOWN_WAIT_SECONDS)
        from .server import stop_ui_server

        stop_ui_server()
        with suppress(Exception):
            sys.stdout.flush()
            sys.stderr.flush()
    if threading.current_thread() is threading.main_thread():
        raise SystemExit(exit_code)
    os._exit(exit_code)


def _exit_signal_names() -> list[str]:
    """要接管的退出信号名。Windows 上只有 SIGINT（见 `install_exit_handlers`）。"""
    return ["SIGINT"] if os.name == "nt" else ["SIGTERM", "SIGINT"]


def install_exit_handlers() -> list[str]:
    """把"被外部要求退出"接到同一套清理上：SIGTERM（Dock 右键退出、kill）与 SIGINT。

    返回实际装上的信号名，便于调用方与测试断言。Windows 上只装 SIGINT：那边 SIGTERM
    既不可靠也不是用户会走的路径，托盘「退出」才是正式入口。

    注意 Python 只在主线程的字节码边界跑信号处理器：主线程若长时间阻塞在 C 代码里，
    处理器要等到下一次回到 Python 才执行（Tk 窗口循环因此配了每秒一次的定时回调）。
    """
    installed: list[str] = []
    for name in _exit_signal_names():
        sig = getattr(signal, name, None)
        if sig is None:
            continue
        try:
            signal.signal(sig, lambda *_: shutdown_app())
        except (ValueError, OSError):
            # 非主线程里注册信号会 ValueError：装不上就如实跳过，不假装成功
            continue
        installed.append(name)
    return installed


def _disable_channel() -> None:
    """关闭通道并还原环境变量和系统代理。

    只在本进程确实持有通道时才动系统代理。否则一个短命的 CLI 调用
    （如 `agent-channel status`）退出时会顺手关掉守护进程已开启的系统代理。
    """
    global _CURRENT_STATUS, _ACTIVE_RUNNER
    owned = _ACTIVE_RUNNER is not None or _CURRENT_STATUS.get("status") == "running"
    runner, _ACTIVE_RUNNER = _ACTIVE_RUNNER, None
    try:
        if runner:
            runner.stop()
    except Exception as exc:
        # 停止失败不能挡住后面的还原：环境变量与系统代理必须照样清干净，
        # 否则用户被留在一个“系统代理指向死端口”的状态里（atexit 走的也是这条路）
        warn_stderr(f"停止内核失败（继续还原环境与系统代理）: {exc}")
    finally:
        unpatch_environment()
        if owned:
            unset_system_proxy()
            # running_source 快照清零：**本进程确实拥有过通道、现在停了**才写盘。
            # 必须放在 owned 分支内：否则任何导入 core 的短命进程（`agent-channel status`、
            # 测试脚本等）退出时 atexit 都会把共享 config 的 running_source 清成 None，
            # 把面板正在跑的来源记录抹掉。函数内 import 避免 core↔config 的顶层循环依赖。
            from .config import save_config, save_error

            if not save_config({"running_source": None}):
                # 快照清不掉不影响通道本身，但面板状态卡会继续显示"跑的那条线"，
                # 属看得见的错，如实说一声（与上面"停止内核失败"同一个口径）。
                warn_stderr(
                    f"清除运行来源快照失败（{save_error()}）：状态卡可能仍显示旧来源"
                )
        _CURRENT_STATUS = {"status": "stopped"}


def get_channel_status() -> dict[str, Any]:
    """获取当前通道运行状态，附带系统代理状态"""
    st = dict(_CURRENT_STATUS)
    st["system_proxy"] = get_system_proxy_status()
    return st


def benchmark_nodes(
    node_uri: str | None = None,
    subscription_url: str | None = None,
    forwarding_check: bool = True,
    include_node: bool = False,
    nodes: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """对单个节点、订阅或已解析节点列表做连接测速。

    TCP 型节点先测握手延迟，UDP 型节点（hysteria2/tuic）跳过 TCP 测速
    （anytls 实测是 TCP+TLS，不在此列）；
    随后可选地启动临时 sing-box 验证真实转发，返回按转发结果与延迟排序的结果。
    测速使用独立临时配置，不影响正在运行的通道。

    nodes 可选，直接传入已解析的节点列表（面板“测速导入节点”场景），与 node_uri /
    subscription_url 解析出的节点合并测速；非法 node_uri 会被跳过，不会拦截订阅。
    默认只返回可读摘要，不回传含 uuid/password 的完整节点配置；
    需要连同节点配置一起返回时显式传 include_node=True。
    """
    node_list: list[dict[str, Any]] = []
    sub_errors: list[str] = []
    if nodes:
        node_list.extend(nodes)
    if node_uri and node_uri.strip():
        parsed = parse_single_node(node_uri.strip())
        if parsed:
            node_list.append(parsed)
    if subscription_url and subscription_url.strip():
        try:
            raw = _fetch_subscription(subscription_url)
        except Exception as exc:
            sub_nodes, sub_errors = [], [f"订阅拉取失败: {exc}"]
        else:
            sub_nodes, sub_errors = extract_nodes_from_subscription(raw)
        node_list.extend(sub_nodes)

    if not node_list:
        if sub_errors:
            raise ValueError("未解析到可用节点：" + "；".join(sub_errors))
        raise ValueError("未解析到可用节点，请检查节点链接或订阅地址")

    # UDP 型节点（hysteria2/tuic）无 TCP 监听，跳过 TCP 握手测速（anytls 除外：它是 TCP+TLS）
    scored: list[tuple[dict[str, Any], float | None]] = []
    for node in node_list:
        if node.get("type") in UDP_ONLY_TYPES:
            scored.append((node, None))
        else:
            scored.append((node, ping_node(node, timeout=2.0)))

    ensure_local_core()
    bin_path = get_local_binary_path()
    ranked: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for node, rtt in scored:
        node_type = node.get("type", "")
        entry: dict[str, Any] = {
            "tag": node.get("tag", ""),
            "type": node_type,
            "server": node.get("server", ""),
            "server_port": node.get("server_port"),
            "tcp_ms": round(rtt, 1) if rtt is not None else None,
            "tcp_reachable": rtt is not None,
            "forwarding": None,
            "forwarding_note": "",
        }
        can_forward = rtt is not None or node_type in UDP_ONLY_TYPES
        if forwarding_check and can_forward and bin_path.exists():
            entry["forwarding"], landing_ip, entry["forwarding_note"] = (
                _check_node_forwarding(node, bin_path)
            )
            if entry["forwarding"]:
                entry["landing_ip"] = landing_ip
                entry["is_relay"] = _is_relay(node, landing_ip)
        ranked.append((entry, node))

    def _rank(pair: tuple[dict[str, Any], dict[str, Any]]) -> tuple[int, float]:
        entry = pair[0]
        fwd = entry["forwarding"]
        if fwd:
            group = 0
        elif fwd is None:
            group = 1
        else:
            group = 2
        return (group, entry["tcp_ms"] if entry["tcp_ms"] is not None else 1e9)

    ranked.sort(key=_rank)

    results = [entry for entry, _ in ranked]
    if include_node:
        for entry, node in ranked:
            entry["node"] = node
    return results


def _probe_landing_ip(proxy_url: str, timeout: float = 8.0) -> str | None:
    """通过临时内核代理访问 ipwho.is，返回真实落地 IP；失败返回 None。"""
    import json
    import urllib.request

    handler = urllib.request.ProxyHandler({"http": proxy_url, "https": proxy_url})
    opener = urllib.request.build_opener(handler)
    req = urllib.request.Request(
        "https://ipwho.is/", headers={"User-Agent": "agent-channel"}
    )
    try:
        with opener.open(req, timeout=timeout) as resp:
            # 限长：这段流量经**不受信节点出口**，标准不应低于订阅读取
            raw = read_limited(resp, MAX_PROBE_BYTES)
            return json.loads(raw.decode("utf-8", errors="replace")).get("ip")
    except Exception:
        return None


def _is_relay(node: dict[str, Any], landing_ip: str | None) -> bool | None:
    """判断节点是否中转/隧道：落地 IP 与 server 解析 IP 不一致返回 True。

    一致（直连）返回 False；落地未知或 server 无法解析时返回 None（无法判断）。
    """
    if not landing_ip:
        return None
    server = str(node.get("server") or "").strip()
    if not server:
        return None
    try:
        import socket

        server_ip = socket.gethostbyname(server)
    except Exception:
        return None
    return landing_ip != server_ip


def _check_node_forwarding(
    node: dict[str, Any], bin_path: Path, timeout: float = 12.0
) -> tuple[bool, str | None, str]:
    """启动临时 sing-box 验证节点能否真实转发流量，并顺带探测真实落地 IP。

    返回 (forwarding, landing_ip, 探针说明)；探针说明在转发失败时是
    `_verify_proxy_forwarding` 给的观测（哪些目标、什么错误），成功时是应答目标的名字。
    完成后清理进程与临时配置。
    """
    port = get_free_port()
    config = build_minimal_config(inbound_port=port, outbound_node=node)
    tmp_config = bin_path.parent / f"benchmark_{port}.json"
    runner = SingboxRunner(bin_path)
    try:
        runner.start(config, tmp_config)
        proxy_url = f"http://127.0.0.1:{port}"
        forwarding, probe_note = _verify_proxy_forwarding(proxy_url, timeout=timeout)
        landing_ip = None
        if forwarding:
            landing_ip = _probe_landing_ip(proxy_url)
        return forwarding, landing_ip, probe_note
    except Exception as exc:
        # 内核没起来是节点侧问题（配置解析失败等），如实带出真因，
        # 不与"探针目标不可达"混为一谈。
        return False, None, f"临时内核启动失败: {str(exc).strip() or repr(exc)}"
    finally:
        runner.stop()
        with suppress(OSError):
            tmp_config.unlink(missing_ok=True)


atexit.register(_shutdown_channel)
