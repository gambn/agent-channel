"""退出路径与外壳窗口的纯逻辑（不依赖显示环境）。"""

import contextlib
import json
import signal
import threading
import urllib.error
from unittest.mock import patch

from agent_channel import core, server, window


def test_shutdown_app_cleans_once_and_exits_main_thread():
    """主线程退出：清理一次后抛 SystemExit，让调用方自然返回。

    幂等是有意的——面板请求、信号、atexit 三条路可能同时来，清理跑多遍会把
    "本进程是否拥有过通道"这类判据搅乱（`_disable_channel` 里靠它决定要不要动系统代理）。
    """
    calls: list[str] = []

    def fake_stop_server() -> bool:
        calls.append("server")
        return True

    with (
        patch.object(core, "_APP_SHUTDOWN_DONE", False),
        patch.object(core, "_shutdown_channel", lambda *a, **k: calls.append("channel")),
        patch.object(server, "stop_ui_server", fake_stop_server),
    ):
        for _ in range(2):
            try:
                core.shutdown_app()
            except SystemExit:
                continue
            raise AssertionError("主线程调用应抛 SystemExit")

    assert calls == ["channel", "server"], calls


def test_shutdown_app_exits_process_from_worker_thread():
    """工作线程里没有"返回"可言：清理完必须真的结束进程，否则 /api/quit 等于没退。"""
    with (
        patch.object(core, "_APP_SHUTDOWN_DONE", False),
        patch.object(core, "_shutdown_channel", lambda *a, **k: None),
        patch.object(server, "stop_ui_server", lambda: True),
        patch("agent_channel.core.os._exit") as fake_exit,
    ):
        worker = threading.Thread(target=core.shutdown_app)
        worker.start()
        worker.join(5)

    assert fake_exit.called, "工作线程里应调用 os._exit 结束进程"


def test_install_exit_handlers_registers_expected_signals():
    """SIGTERM（Dock 右键退出、kill）与 SIGINT（终端 Ctrl-C）都要接到清理上。"""
    recorded: dict[int, object] = {}

    with (
        patch("agent_channel.core.signal.signal", lambda sig, handler: recorded.update({sig: handler})),
        patch.object(core, "_exit_signal_names", lambda: ["SIGTERM", "SIGINT"]),
    ):
        names = core.install_exit_handlers()

    assert names == ["SIGTERM", "SIGINT"], names
    assert set(recorded) == {signal.SIGTERM, signal.SIGINT}
    # 处理器本身指向统一清理入口
    with patch.object(core, "shutdown_app") as fake_shutdown:
        handler = recorded[signal.SIGTERM]
        handler(signal.SIGTERM, None)  # type: ignore[operator]
    assert fake_shutdown.called


def test_install_exit_handlers_windows_only_sigint():
    """Windows 上只装 SIGINT：那边 SIGTERM 不可靠，托盘「退出」才是正式入口。"""
    recorded: dict[int, object] = {}
    with (
        patch("agent_channel.core.signal.signal", lambda sig, handler: recorded.update({sig: handler})),
        patch.object(core, "_exit_signal_names", lambda: ["SIGINT"]),
    ):
        names = core.install_exit_handlers()
    assert names == ["SIGINT"], names
    assert set(recorded) == {signal.SIGINT}


def test_install_exit_handlers_skips_when_not_main_thread():
    """非主线程里注册信号会 ValueError：如实返回空清单，不假装装上了。"""
    def boom(sig, handler):
        raise ValueError("signal only works in main thread")

    with (
        patch("agent_channel.core.signal.signal", boom),
        patch.object(core, "_exit_signal_names", lambda: ["SIGTERM", "SIGINT"]),
    ):
        assert core.install_exit_handlers() == []


def test_stop_ui_server_reports_whether_it_stopped_anything():
    """关面板服务要如实汇报：没起过就返回 False，别让调用方以为关了。"""
    with patch.object(server, "_UI_SERVER", None):
        assert server.stop_ui_server() is False

    class FakeServer:
        def __init__(self) -> None:
            self.shutdown_called = False

        def shutdown(self) -> None:
            self.shutdown_called = True

        def server_close(self) -> None:
            pass

    fake = FakeServer()
    with patch.object(server, "_UI_SERVER", fake):
        assert server.stop_ui_server() is True
        # 句柄要当场清掉：再调一次时如果还拿着同一个 server，shutdown() 会等一个
        # 早已退出的 serve_forever 循环，退出路径可能就此卡住
        assert server._UI_SERVER is None
    assert fake.shutdown_called


def test_status_text_reads_state_and_node():
    """窗口里那行状态：停止/运行两种状态都要说清楚，运行时要带节点标签。"""
    assert window.status_text({"status": "stopped"}, "v1.14.3") == "通道已停止 · 内核 v1.14.3"
    assert (
        window.status_text({"status": "running", "node_tag": "JP-01"}, "v1.14.3")
        == "通道运行中 · JP-01 · 内核 v1.14.3"
    )
    # 缺字段不许崩：状态卡读不到东西也要能显示
    assert window.status_text({}, "未安装") == "通道已停止 · 内核 未安装"


def _fake_response(payload: bytes, status: int = 200):
    class Resp:
        headers = {"Content-Type": "application/json"}

        def __init__(self) -> None:
            self.status = status

        def read(self) -> bytes:
            return payload

    return contextlib.nullcontext(Resp())


def test_existing_instance_url_only_accepts_our_panel():
    """单实例判据是"那个端口上有像我们的 /api/status"，不是"端口被占着"。"""
    with patch.object(
        window.urllib.request,
        "urlopen",
        lambda *a, **k: _fake_response(json.dumps({"status": "stopped"}).encode()),
    ):
        assert window.existing_instance_url(18888) == "http://127.0.0.1:18888"

    with patch.object(
        window.urllib.request,
        "urlopen",
        lambda *a, **k: _fake_response(json.dumps({"hello": "world"}).encode()),
    ):
        assert window.existing_instance_url(18888) is None

    def raise_error(*a, **k):
        raise urllib.error.URLError("connection refused")

    with patch.object(window.urllib.request, "urlopen", raise_error):
        assert window.existing_instance_url(18888) is None


def test_stop_ui_server_does_not_deadlock_in_serving_thread():
    """在 serve_forever 那个线程里关服务必须交给后台线程。

    回归背景：SIGTERM 处理器跑在主线程，而主线程此刻正卡在 `serve_forever()` 里；
    直接调 `shutdown()` 等于"等自己退出"，实测表现为面板端口释放不掉、进程也退不掉。
    """
    caller: list[str] = []

    class BlockingServer:
        def shutdown(self) -> None:
            caller.append(threading.current_thread().name)

        def server_close(self) -> None:
            caller.append("closed")

    server_impl = BlockingServer()
    with (
        patch.object(server, "_UI_SERVER", server_impl),
        patch.object(server, "_UI_SERVER_THREAD", threading.current_thread()),
    ):
        assert server.stop_ui_server() is True

    # 关服务的活儿必须由**别的**线程干（在服务线程里同步调 shutdown 就是等自己退出）
    import time

    for _ in range(40):
        if "closed" in caller:
            break
        time.sleep(0.05)
    assert caller, "应当有人去关这个服务"
    assert caller[0] != threading.current_thread().name, caller
    assert "closed" in caller, caller


def test_configure_stdio_sets_line_buffering():
    """冻结产物输出被重定向时是块缓冲：入口必须显式设成行缓冲。

    实测背景：`PYTHONUNBUFFERED=1` 压不住冻结产物，启动横幅要等进程退出才刷出来，
    用户看不到面板地址、验收脚本 grep 日志必然误判。
    """
    class FakeStream:
        def __init__(self) -> None:
            self.calls: list[dict] = []

        def reconfigure(self, **kwargs) -> None:
            self.calls.append(kwargs)

    from agent_channel import utils

    out, err = FakeStream(), FakeStream()
    with patch.object(utils.sys, "stdout", out), patch.object(utils.sys, "stderr", err):
        utils.configure_stdio()
    assert out.calls == [{"line_buffering": True}], out.calls
    assert err.calls == [{"line_buffering": True}], err.calls


def test_configure_stdio_tolerates_missing_or_broken_streams():
    """窗口模式下 stdio 可能是 None、或不支持 reconfigure：设不上也不能影响启动。"""
    from agent_channel import utils

    class Broken:
        def reconfigure(self, **kwargs) -> None:
            raise ValueError("detached")

    with patch.object(utils.sys, "stdout", None), patch.object(utils.sys, "stderr", Broken()):
        utils.configure_stdio()  # 不抛异常即通过


def test_panel_server_silences_client_disconnects():
    """客户端提前断开是常态，不该把 BrokenPipeError 回溯刷进日志；其它异常要照旧报。"""
    import io

    from agent_channel import server as server_mod

    panel = server_mod.PanelHTTPServer(("127.0.0.1", 0), server_mod.ChannelRequestHandler)
    try:
        cases = (
            (BrokenPipeError(32, "Broken pipe"), ""),
            (ConnectionResetError(104, "Connection reset"), ""),
            (ValueError("boom"), "boom"),
        )
        for exc, expect in cases:
            captured = io.StringIO()
            try:
                raise exc
            except Exception:
                with patch("sys.stderr", captured):
                    panel.handle_error(None, ("127.0.0.1", 1))
            got = captured.getvalue()
            if expect == "":
                assert got == "", f"{type(exc).__name__} 不该打印: {got!r}"
            else:
                assert expect in got, f"其它异常应照旧打印回溯: {got!r}"
    finally:
        panel.server_close()
