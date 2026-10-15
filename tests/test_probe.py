"""probe.py 的守卫测试：一键测试必须自带"这次走的是哪条路"的标注。

背景：`test_single_endpoint(url, proxy_url=None)` 原先用不带 handler 的
`build_opener()`，会挂上 urllib 默认的 ProxyHandler —— 于是**读宿主环境的
HTTP_PROXY/HTTPS_PROXY**。结果由宿主环境决定：宿主设着代理就拿宿主代理去测（绿），
宿主直连被墙就超时（红）。"绿不代表通道通，红不代表通道不通"，两头都不可信。
"""

import os
import socket
import threading
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

from agent_channel import probe
from agent_channel.probe import test_single_endpoint


def _local_http_server() -> tuple[str, ThreadingHTTPServer]:
    """起一个只回 200 的本机 HTTP 服务，返回 (url, server)。"""

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            body = b"ok"
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):  # 静音，别把测试日志刷满
            pass

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return f"http://127.0.0.1:{port}/", server


def test_direct_probe_ignores_host_proxy_env():
    """`proxy_url=None` 时必须是**真直连** —— 不得读宿主的 HTTP_PROXY。

    故障注入：把 HTTP_PROXY / HTTPS_PROXY 指向一个死端口。若实现退回"不带 handler 的
    build_opener"，urllib 会挂上默认 ProxyHandler 去用它，于是连本机地址都连不上 ——
    这条测试就会红。这就是"故障存在时会红"的证据形态：不需要改代码去证明。
    """
    url, server = _local_http_server()
    try:
        with patch.dict(
            os.environ,
            {
                "HTTP_PROXY": "http://127.0.0.1:1",
                "HTTPS_PROXY": "http://127.0.0.1:1",
            },
        ):
            result = test_single_endpoint(url, proxy_url=None, timeout=3.0)
    finally:
        server.shutdown()

    assert result.get("via") == "direct", f"直连结果必须标注 via=direct：{result}"
    assert result.get("reachable"), (
        f"直连不应受宿主 HTTP_PROXY 影响（指向死端口也不该失败）：{result}"
    )


def test_probe_with_proxy_marks_via_channel():
    """传了代理就必须标注 `via=channel`（与"直连"区分开）。"""
    url, server = _local_http_server()
    try:
        result = test_single_endpoint(url, proxy_url="http://127.0.0.1:1", timeout=0.5)
    finally:
        server.shutdown()
    assert result.get("via") == "channel", result


def test_probe_never_lets_no_proxy_bypass_the_channel():
    """`no_proxy`/`NO_PROXY` 不得让探测绕过给定代理静默直连（否则是假阳）。

    故障注入：宿主环境带 `NO_PROXY=*`，代理指向死端口。urllib 的
    `ProxyHandler.proxy_open` 里有一步 `proxy_bypass(host)`，命中就**静默直连**——
    直连 example.com 一通，探测就报 `reachable=True`，于是"通道死了却报可达"，
    转发验证的四个信号（google/cloudflare/youtube + 落地兜底）会集体失效。
    回归背景（2026-10-13）：老大质疑落地兜底会被这种环境骗过，实测属实
    （`NO_PROXY=*` 甚至 `NO_PROXY=example.com` 下，死代理探测都报成功），
    故探测期间必须让 `proxy_bypass` 失效——0.1.4 起改成**按线程收窄**
    （`probe._ProbeScope`，见 muse 审计 M1），本测试钉的就是"收窄之后仍然不绕过"。

    代理指向 127.0.0.1:1（必然拒绝），本测试不需要任何外部网络。
    """
    with patch.dict(os.environ, {"NO_PROXY": "*", "no_proxy": "*"}):
        result = test_single_endpoint(
            "https://example.com/", "http://127.0.0.1:1", timeout=3.0
        )
    assert result.get("reachable") is not True, (
        f"no_proxy 把探测绕过了给定代理（假阳，通道死了却报可达）：{result}"
    )
    assert result.get("via") == "channel", result


def test_proxy_bypass_keeps_native_behaviour_outside_probes():
    """收窄作用域（muse 审计 M1）：探针之外，`proxy_bypass` 必须还是原生语义。

    早先是导入期把 `urllib.request.proxy_bypass` 整个换成恒 False——那会让**同进程所有**
    urllib 调用失去 no_proxy 语义：测试用 `urlopen` 打本机面板会被塞进宿主的 HTTP_PROXY
    （实测：设了 HTTP_PROXY 的机器上 27 个用例假失败），作为库被别人 import 时更是静默
    改掉对方的网络行为。现在只按线程、按调用生效。
    """
    with patch.dict(os.environ, {"NO_PROXY": "example.com", "no_proxy": "example.com"}):
        # 探针之外：命中 no_proxy 就该照常返回 True（原生行为）
        assert urllib.request.proxy_bypass("example.com") is True
        # 探针作用域内：恒 False，探测不会被静默改道
        with probe._ProbeScope():
            assert urllib.request.proxy_bypass("example.com") is False
        # 出了作用域立刻还原
        assert urllib.request.proxy_bypass("example.com") is True
    # 模块留着原函数并做委托，而不是把它替换成恒 False 的 lambda
    assert probe._proxy_bypass_original is not urllib.request.proxy_bypass


def test_probe_scope_is_thread_local():
    """作用域是按线程的：并发探测互不干扰，别的线程拿不到被改过的语义。

    这正是不能用"临时打补丁再还原"的原因——四个探测并发时会在彼此之间提前还原。
    """
    seen: dict[str, bool] = {}

    def _other_thread() -> None:
        seen["other"] = urllib.request.proxy_bypass("example.com")

    with patch.dict(os.environ, {"NO_PROXY": "example.com", "no_proxy": "example.com"}):
        with probe._ProbeScope():
            worker = threading.Thread(target=_other_thread)
            worker.start()
            worker.join(timeout=5)
            seen["self"] = urllib.request.proxy_bypass("example.com")

    assert seen.get("self") is False, "探针线程内必须不绕过"
    assert seen.get("other") is True, "别的线程不该被探针的作用域影响"


def test_probe_error_keeps_the_whole_message():
    """探测失败要把原始错误整句带出来：截断会丢掉最该看的状态码。

    回归背景：此前用 `str(e).split(":")[-1].strip()` 收尾，把
    "HTTP Error 404: Not Found" 截成 "Not Found" —— 排查时最想看的 404 反而没了。
    """
    with patch(
        "urllib.request.OpenerDirector.open",
        side_effect=OSError("HTTP Error 404: Not Found"),
    ):
        result = test_single_endpoint("http://example.invalid/", None, timeout=0.1)

    assert result.get("reachable") is False, result
    assert "404" in result.get("error", ""), result
    assert "Not Found" in result.get("error", ""), result
