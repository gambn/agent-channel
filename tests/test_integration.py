from contextlib import suppress
from pathlib import Path
from unittest.mock import patch

from agent_channel import (
    generate_uuid,
    get_free_port,
)


def test_uuid_generation():
    uid = generate_uuid()
    assert len(uid) == 36
    assert uid.count("-") == 4


def test_get_free_port():
    p = get_free_port()
    assert isinstance(p, int)
    assert 1024 <= p <= 65535


def test_enable_channel_never_reaps_kernel():
    """换节点路径不得调用自愈——否则会先杀掉正在服务的旧内核。

    回归背景：自愈曾挂在 enable_channel 入口且不区分死活（只看“端口被占 +
    进程叫 sing-box”），换节点时把活着的旧内核当残留收掉，破坏了
    「先验证后拆旧」的平滑切换保证；新节点若最终不可用，用户直接断网。
    自愈自此只挂在启动入口（cli._cmd_start / server.launch_ui）。
    """
    from agent_channel import core

    reaped: list[int] = []
    healed_proxy: list[int] = []

    with (
        patch.object(
            core, "heal_stale_kernel", lambda port: reaped.append(port) or False
        ),
        patch.object(
            core,
            "heal_stale_system_proxy",
            lambda port: healed_proxy.append(port) or False,
        ),
        # 未配置时 enable_channel 会快速报错，不会起内核
        suppress(ValueError),
    ):
        core.enable_channel()

    assert reaped == [], f"enable_channel 不得调用内核自愈（实际端口: {reaped}）"
    assert healed_proxy == [], (
        f"enable_channel 不得调用代理自愈（实际端口: {healed_proxy}）"
    )


def test_subscription_response_size_limit():
    """订阅响应超过上限必须中止（防无限流 / 超大响应打爆内存）。"""
    from agent_channel.core import _read_limited

    class FakeResp:
        def __init__(self, total, declared=None):
            self.remaining = total
            self.headers = {"Content-Length": declared} if declared else {}

        def read(self, size):
            if self.remaining <= 0:
                return b""
            take = min(size, self.remaining)
            self.remaining -= take
            return b"x" * take

    # 上限之内应正常读完
    assert len(_read_limited(FakeResp(1000), limit=2048)) == 1000

    # 两条防线都要拦得住：分块累计超限、Content-Length 预检
    for resp, why in (
        (FakeResp(5000), "分块累计超限"),
        (FakeResp(10, declared="999999"), "Content-Length 预检"),
    ):
        try:
            _read_limited(resp, limit=2048)
        except ValueError:
            continue
        raise AssertionError(f"未拦住：{why}")


def test_fixed_port_switch_verifies_on_shadow_port_first():
    """固定端口切换必须先**影子验证**，再拆旧、再把候选绑到固定端口。

    断言三件事：① 第一次 start 用的是**临时端口**（≠ 20849）且此刻旧通道还没被停；
    ② 旧通道的 stop 排在新内核绑 20849 之前；③ 最终 status.port == 20849。

    这三条正是“先停旧再逐个试”那版做不到的：候选不通过时旧通道根本不会被碰。
    """
    from agent_channel import core

    events: list[str] = []

    class FakeRunner:
        def __init__(self, _bin=None):
            self.port = None

        def start(self, config_dict, _config_file):
            self.port = config_dict["inbounds"][0]["listen_port"]
            events.append(f"start:{self.port}")

        def stop(self):
            events.append(f"stop:{self.port}")

    old = FakeRunner()
    old.port = 20849
    core._ACTIVE_RUNNER = old
    core._CURRENT_STATUS = {"status": "running", "mode": "standalone", "port": 20849}
    node = {"type": "vless", "tag": "n1", "server": "a.invalid", "server_port": 443}
    try:
        with (
            patch.object(core, "SingboxRunner", FakeRunner),
            patch.object(core, "ensure_local_core"),
            patch.object(core, "get_local_binary_path", lambda: Path(__file__)),
            patch.object(core, "rank_nodes", lambda nodes, timeout=None: list(nodes)),
            patch.object(
                core, "_verify_proxy_forwarding", lambda _url, timeout=12.0: True
            ),
            patch.object(core, "patch_environment"),
            patch.object(core, "set_system_proxy", lambda **_kw: True),
            patch.object(core, "_wait_port_free"),
        ):
            status = core.enable_channel(
                node=node, auto_patch=False, set_sys_proxy=False, listen_port=20849
            )
    finally:
        core._ACTIVE_RUNNER = None
        core._CURRENT_STATUS = {"status": "stopped"}

    assert status.get("port") == 20849, status
    shadow = events[0]
    assert shadow.startswith("start:"), events
    assert shadow != "start:20849", f"第一次 start 必须是临时端口：{events}"
    assert "stop:20849" in events, f"旧通道必须被停：{events}"
    assert events.index(shadow) < events.index("stop:20849"), (
        f"影子验证必须先于拆旧：{events}"
    )
    assert events.index("stop:20849") < events.index("start:20849"), (
        f"回绑固定端口必须晚于拆旧：{events}"
    )


def test_rebind_failure_reports_the_real_cause():
    """残留守卫：拆旧后回绑固定端口失败，必须如实报真因。

    不得回落成“所有候选节点均无法建立有效转发通道” —— 那句会把“端口被占”藏起来，
    让人去查节点（Linux 真机那次就是被这类文案带偏的）。
    """
    from agent_channel import core

    class FakeRunner:
        def __init__(self, _bin=None):
            self.port = None

        def start(self, config_dict, _config_file):
            self.port = config_dict["inbounds"][0]["listen_port"]
            if self.port == 20849:
                raise OSError("address already in use")

        def stop(self):
            pass

    old = FakeRunner()
    old.port = 20849
    core._ACTIVE_RUNNER = old
    core._CURRENT_STATUS = {"status": "running", "port": 20849}
    node = {"type": "vless", "tag": "n1", "server": "a.invalid", "server_port": 443}
    message = ""
    try:
        with (
            patch.object(core, "SingboxRunner", FakeRunner),
            patch.object(core, "ensure_local_core"),
            patch.object(core, "get_local_binary_path", lambda: Path(__file__)),
            patch.object(core, "rank_nodes", lambda nodes, timeout=None: list(nodes)),
            patch.object(
                core, "_verify_proxy_forwarding", lambda _url, timeout=12.0: True
            ),
            patch.object(core, "patch_environment"),
            patch.object(core, "set_system_proxy", lambda **_kw: True),
            patch.object(core, "_wait_port_free"),
            patch.object(core, "_restore_previous", lambda *_a, **_k: False),
        ):
            try:
                core.enable_channel(
                    node=node, auto_patch=False, set_sys_proxy=False, listen_port=20849
                )
                raise AssertionError("回绑失败时必须报错")
            except RuntimeError as exc:
                message = str(exc)
    finally:
        core._ACTIVE_RUNNER = None
        core._CURRENT_STATUS = {"status": "stopped"}

    assert "旧通道已按切换流程停止" in message, message
    assert "20849" in message, message
    assert "无法建立有效转发" not in message, f"不得把真因藏起来：{message}"


def test_concurrent_enable_is_serialized():
    """并发启用必须串行化。

    不串行化时两个并发 `/api/enable` 会各起一个内核，后写的 `_ACTIVE_RUNNER` 把前一个
    指掉 —— 泄漏一个占着端口与内核文件锁的进程。这里量的是“同时处于启用中的峰值”。
    """
    import threading
    import time

    from agent_channel import core

    concurrent = 0
    peak = 0
    gate = threading.Lock()

    class FakeRunner:
        def __init__(self, _bin=None):
            pass

        def start(self, _config_dict, _config_file):
            nonlocal concurrent, peak
            with gate:
                concurrent += 1
                peak = max(peak, concurrent)
            time.sleep(0.05)
            with gate:
                concurrent -= 1

        def stop(self):
            pass

    node = {"type": "vless", "tag": "n1", "server": "a.invalid", "server_port": 443}

    def run_one() -> None:
        # 并发结果本身不重要，这里只量“同时处于启用中”的峰值
        with suppress(Exception):
            core.enable_channel(node=node, auto_patch=False, set_sys_proxy=False)

    try:
        with (
            patch.object(core, "SingboxRunner", FakeRunner),
            patch.object(core, "ensure_local_core"),
            patch.object(core, "get_local_binary_path", lambda: Path(__file__)),
            patch.object(core, "rank_nodes", lambda nodes, timeout=None: list(nodes)),
            patch.object(
                core, "_verify_proxy_forwarding", lambda _url, timeout=12.0: True
            ),
            patch.object(core, "patch_environment"),
            patch.object(core, "set_system_proxy", lambda **_kw: True),
            patch.object(core, "_wait_port_free"),
        ):
            threads = [threading.Thread(target=run_one) for _ in range(4)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
    finally:
        core._ACTIVE_RUNNER = None
        core._CURRENT_STATUS = {"status": "stopped"}

    assert peak == 1, f"并发启用应串行化，实测同时启用峰值 = {peak}"


def test_channel_operation_refuses_immediately_when_busy():
    """启停冲突改成"当场报忙"，不是排队干等（409 的底层语义）。

    面板的"启动/停止"是同步请求，弱网下一次启用要跑订阅拉取 + 逐候选转发验证，可以
    长达一分多钟。若让后来者排队，用户点"停止"会几十秒没有任何反馈；正确行为是立刻
    抛 `ChannelBusyError`，由 HTTP 层回 409。
    """
    import threading

    from agent_channel import core

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
        # 打桩实际启停：万一"忙"没被拦住（回归），也要立刻红在这里，而不是真起内核
        with (
            patch.object(
                core,
                "_enable_channel",
                side_effect=AssertionError("忙时不得进入实际启用"),
            ),
            patch.object(
                core,
                "_disable_channel",
                side_effect=AssertionError("忙时不得进入实际停止"),
            ),
        ):
            for name, call in (
                (
                    "enable_channel",
                    lambda: core.enable_channel(node={"type": "direct", "tag": "d"}),
                ),
                ("disable_channel", core.disable_channel),
            ):
                try:
                    call()
                except core.ChannelBusyError:
                    continue
                raise AssertionError(f"{name} 在忙时必须立刻抛 ChannelBusyError")
    finally:
        release.set()
        thread.join(timeout=5)

    # 忙态结束（锁被释放）后要能正常再操作：确认拒绝路径没有把锁留在残缺状态
    assert core._CHANNEL_LOCK.acquire(blocking=False), "锁没有被正确释放"
    core._CHANNEL_LOCK.release()


def test_real_kernel_switch_rebinds_fixed_port():
    """真实内核回归（**不 mock bind**）：固定端口上换节点，内核真的换了一个。

    为什么必须有这条：正确性若只有 FakeRunner 层的证据，就与最初漏网的原因
    一模一样 —— 所有 `SingboxRunner` 相关的测试都 mock，从不真实 bind。这里用
    `direct` 出站（不需要真节点）、把转发验证打成 True（不需要外网），只验
    **端口归属与 pid 更替**。

    无内核时 skip 并明示（与既有 binary 测试风格一致）。
    """
    import socket
    import time

    from agent_channel import core
    from tests import seed_real_kernel

    # 隔离把内核目录指到了临时目录，这里先把本机真实装好的内核播种进去 ——
    # 否则本机有内核也永远走 skip，这条回归测试等于空转（见 tests/__init__ 说明）。
    seed_real_kernel()
    core.ensure_local_core()
    binary = core.get_local_binary_path()
    if not binary.exists():
        print("    [skip] 本机无 sing-box 内核，跳过真实 bind 回归")
        return

    def listening(port: int) -> bool:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.settimeout(0.5)
            return probe.connect_ex(("127.0.0.1", port)) == 0

    def wait_released(port: int, timeout: float = 3.0) -> bool:
        deadline = time.perf_counter() + timeout
        while time.perf_counter() < deadline:
            if not listening(port):
                return True
            time.sleep(0.1)
        return not listening(port)

    def active_pid() -> int:
        """启用后必定持有真实内核；这里显式收窄类型，顺带把“没有 runner”变成断言。"""
        runner = core._ACTIVE_RUNNER
        assert runner is not None and runner.process is not None, "启用后应持有真实内核"
        return runner.process.pid

    port = get_free_port()
    try:
        with (
            patch.object(core, "rank_nodes", lambda nodes, timeout=None: list(nodes)),
            patch.object(
                core, "_verify_proxy_forwarding", lambda _url, timeout=12.0: True
            ),
        ):
            core.enable_channel(
                node={"type": "direct", "tag": "direct-a"},
                auto_patch=False,
                set_sys_proxy=False,
                listen_port=port,
            )
            first_pid = active_pid()
            assert listening(port), "第一次启用后固定端口应在听"

            core.enable_channel(
                node={"type": "direct", "tag": "direct-b"},
                auto_patch=False,
                set_sys_proxy=False,
                listen_port=port,
            )
            second_pid = active_pid()
            assert listening(port), "切换后固定端口仍应在听"
            assert second_pid != first_pid, (
                f"切换后内核应被换掉（{first_pid} → {second_pid}）"
            )
    finally:
        core.disable_channel()

    assert wait_released(port), "停止后固定端口应释放"


def test_failed_enable_keeps_running_channel():
    """启用失败不该把正在跑的通道停掉（否则连系统代理一起关，等于断网）。"""
    from agent_channel import core

    class FakeRunner:
        def __init__(self):
            self.stopped = False

        def stop(self):
            self.stopped = True

    fake = FakeRunner()
    core._ACTIVE_RUNNER = fake
    core._CURRENT_STATUS = {"status": "running", "mode": "standalone", "port": 20849}
    try:
        with patch.object(core, "parse_single_node", lambda _t: None):
            try:
                core.enable_channel(node_uri="写错的链接", auto_patch=False)
                raise AssertionError("没有可用目标时必须报错")
            except ValueError:
                pass
        assert not fake.stopped, "启用失败时不该停掉现有通道"
        assert core.get_channel_status()["status"] == "running"
    finally:
        core._ACTIVE_RUNNER = None
        core._CURRENT_STATUS = {"status": "stopped"}


def test_dead_node_keeps_running_channel():
    """预检过了、但候选节点在切换时全部转发失败 —— 旧通道也必须原样还在。"""
    from agent_channel import core

    class FakeRunner:
        def __init__(self, *_args, **_kwargs):
            self.stopped = False

        def start(self, *_args, **_kwargs):
            pass

        def stop(self):
            self.stopped = True

    old = FakeRunner()
    core._ACTIVE_RUNNER = old
    core._CURRENT_STATUS = {"status": "running", "mode": "standalone", "port": 20849}
    created = []

    def make_runner(*_args, **_kwargs):
        runner = FakeRunner()
        created.append(runner)
        return runner

    try:
        with (
            patch.object(
                core,
                "parse_single_node",
                lambda _t: {
                    "type": "vless",
                    "tag": "dead",
                    "server": "x",
                    "server_port": 443,
                },
            ),
            patch.object(core, "rank_nodes", lambda nodes, timeout=0: nodes),
            patch.object(core, "ensure_local_core", lambda: True),
            patch.object(
                core, "get_local_binary_path", lambda: Path(__file__).resolve()
            ),
            patch.object(core, "SingboxRunner", make_runner),
            patch.object(
                core, "_verify_proxy_forwarding", lambda url, timeout=0: False
            ),
        ):
            try:
                core.enable_channel(node_uri="vless://dead", auto_patch=False)
                raise AssertionError("全部候选失败时必须报错")
            except ValueError:
                pass
        assert not old.stopped, "旧通道不该被拆掉"
        assert core.get_channel_status()["status"] == "running"
        assert created and all(r.stopped for r in created), "试过的临时内核都要收掉"
    finally:
        core._ACTIVE_RUNNER = None
        core._CURRENT_STATUS = {"status": "stopped"}


def test_midway_error_stops_new_kernel_and_reports_truthfully():
    """新内核起来后中途抛错（环境注入 / 设系统代理失败）：内核必须被收掉，状态不许撒谎。"""
    from agent_channel import core

    class FakeRunner:
        def __init__(self, *_args, **_kwargs):
            self.stopped = False

        def start(self, *_args, **_kwargs):
            pass

        def stop(self):
            self.stopped = True

    created = []

    def make_runner(*_args, **_kwargs):
        runner = FakeRunner()
        created.append(runner)
        return runner

    core._ACTIVE_RUNNER = FakeRunner()  # 假装旧通道在跑
    core._CURRENT_STATUS = {"status": "running", "mode": "standalone", "port": 20849}
    try:
        with (
            patch.object(
                core,
                "parse_single_node",
                lambda _t: {
                    "type": "vless",
                    "tag": "n",
                    "server": "x",
                    "server_port": 443,
                },
            ),
            patch.object(core, "rank_nodes", lambda nodes, timeout=0: nodes),
            patch.object(core, "ensure_local_core", lambda: True),
            patch.object(
                core, "get_local_binary_path", lambda: Path(__file__).resolve()
            ),
            patch.object(core, "SingboxRunner", make_runner),
            patch.object(core, "_verify_proxy_forwarding", lambda url, timeout=0: True),
            patch.object(
                core,
                "patch_environment",
                side_effect=RuntimeError("伪造的环境注入失败"),
            ),
        ):
            try:
                core.enable_channel(node_uri="vless://x", auto_patch=True)
                raise AssertionError("中途失败必须抛错")
            except RuntimeError:
                pass

        assert created, "应有一个新内核被启动过"
        assert all(r.stopped for r in created), (
            "中途失败时新内核必须被收掉（否则谁也指不到它）"
        )
        assert core._ACTIVE_RUNNER is None, "_ACTIVE_RUNNER 必须清空"
        assert core.get_channel_status()["status"] != "running", (
            "状态不能还写着 running"
        )
    finally:
        core._ACTIVE_RUNNER = None
        core._CURRENT_STATUS = {"status": "stopped"}


def test_disable_survives_a_failing_stop():
    """停止内核抛错时，环境变量与系统代理的还原必须照样执行、状态也要落定。"""
    from agent_channel import core

    class BoomRunner:
        def __init__(self):
            self.asked = False

        def stop(self):
            self.asked = True
            raise RuntimeError("伪造的停止失败")

    runner = BoomRunner()
    core._ACTIVE_RUNNER = runner
    core._CURRENT_STATUS = {"status": "running", "mode": "standalone", "port": 20849}
    called = []
    try:
        with (
            patch.object(core, "unpatch_environment", lambda: called.append("unpatch")),
            patch.object(core, "unset_system_proxy", lambda: called.append("unset")),
        ):
            core.disable_channel()
        assert runner.asked, "应当尝试过停止内核"
        assert called == ["unpatch", "unset"], f"清理被中断了: {called}"
        assert core.get_channel_status()["status"] == "stopped"
    finally:
        core._ACTIVE_RUNNER = None
        core._CURRENT_STATUS = {"status": "stopped"}


def test_system_proxy_failure_is_visible_in_status():
    """面板要求同步系统代理、但设置失败时必须看得出来，不能静默返回 running。"""
    from agent_channel import core

    class FakeRunner:
        def __init__(self, *_args, **_kwargs):
            self.stopped = False

        def start(self, *_args, **_kwargs):
            pass

        def stop(self):
            self.stopped = True

    created = []

    def make_runner(*_args, **_kwargs):
        runner = FakeRunner()
        created.append(runner)
        return runner

    core._ACTIVE_RUNNER = None
    core._CURRENT_STATUS = {"status": "stopped"}
    try:
        with (
            patch.object(
                core,
                "parse_single_node",
                lambda _t: {
                    "type": "vless",
                    "tag": "n",
                    "server": "x",
                    "server_port": 443,
                },
            ),
            patch.object(core, "rank_nodes", lambda nodes, timeout=0: nodes),
            patch.object(core, "ensure_local_core", lambda: True),
            patch.object(
                core, "get_local_binary_path", lambda: Path(__file__).resolve()
            ),
            patch.object(core, "SingboxRunner", make_runner),
            patch.object(core, "_verify_proxy_forwarding", lambda url, timeout=0: True),
            patch.object(core, "set_system_proxy", lambda **kw: False),
        ):
            status = core.enable_channel(
                node_uri="vless://x", auto_patch=False, set_sys_proxy=True
            )
        assert status.get("status") == "running"
        assert status.get("system_proxy_set") is False, status
    finally:
        core._ACTIVE_RUNNER = None
        core._CURRENT_STATUS = {"status": "stopped"}


def test_benchmark_nodes_accepts_node_list():
    """benchmark 直接接受已解析节点列表（“测速导入节点”），摘要不回传凭据。"""
    from agent_channel.core import benchmark_nodes

    nodes = [
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
    ]
    results = benchmark_nodes(nodes=nodes, forwarding_check=False)
    assert len(results) == 2
    assert {r["tag"] for r in results} == {"n1", "n2"}
    for r in results:
        assert "uuid" not in r
        assert "password" not in r
        assert "node" not in r


def test_benchmark_nodes_falls_through_when_node_uri_invalid():
    """node_uri 非法时不拦截订阅：跳过非法单节点，仍测订阅节点。"""
    import json

    from agent_channel.core import benchmark_nodes

    sub_json = json.dumps(
        {
            "outbounds": [
                {
                    "type": "vless",
                    "tag": "s1",
                    "server": "s1.com",
                    "server_port": 443,
                    "uuid": "u",
                }
            ]
        }
    )
    with patch("agent_channel.core._fetch_subscription", return_value=sub_json):
        results = benchmark_nodes(
            node_uri="这不是节点链接",
            subscription_url="http://example.invalid/sub",
            forwarding_check=False,
        )
        assert len(results) == 1
        assert results[0]["tag"] == "s1"


def test_benchmark_nodes_reports_subscription_parse_reason():
    """订阅解析不出节点时，benchmark 报错要带具体原因，不再只有通用文案。"""
    from agent_channel.core import benchmark_nodes

    with patch("agent_channel.core._fetch_subscription", return_value=""):
        try:
            benchmark_nodes(subscription_url="http://example.invalid/sub")
        except ValueError as exc:
            assert "订阅内容为空" in str(exc)
        else:
            raise AssertionError("订阅为空时必须报 ValueError")


def test_enable_channel_reports_subscription_parse_reason():
    """启用订阅解析不出节点时，报错要带具体原因，不再只有“未找到可用节点”。"""
    from agent_channel import core

    with patch("agent_channel.core._fetch_subscription", return_value=""):
        try:
            core.enable_channel(subscription_url="http://example.invalid/sub")
        except ValueError as exc:
            assert "订阅内容为空" in str(exc)
        else:
            raise AssertionError("订阅为空时必须报 ValueError")


def test_benchmark_nodes_reports_subscription_fetch_failure():
    """订阅拉取失败（网络错误）统一报「订阅拉取失败」，不再原样抛 urlopen 异常。"""
    from agent_channel.core import benchmark_nodes

    def fail_fetch(_url):
        raise OSError("连接被拒")

    with patch("agent_channel.core._fetch_subscription", side_effect=fail_fetch):
        try:
            benchmark_nodes(subscription_url="http://example.invalid/sub")
        except ValueError as exc:
            assert "订阅拉取失败" in str(exc)
        else:
            raise AssertionError("拉取失败时必须报 ValueError")


def test_enable_channel_reports_subscription_fetch_failure():
    """启用时订阅拉取失败统一报「订阅拉取失败」。"""
    from agent_channel import core

    def fail_fetch(_url):
        raise OSError("连接被拒")

    with patch("agent_channel.core._fetch_subscription", side_effect=fail_fetch):
        try:
            core.enable_channel(subscription_url="http://example.invalid/sub")
        except ValueError as exc:
            assert "订阅拉取失败" in str(exc)
        else:
            raise AssertionError("拉取失败时必须报 ValueError")


def test_enable_channel_accepts_node_list():
    """enable_channel 接受节点列表（导入节点进候选池），并入 candidates 排序+验证。"""
    from pathlib import Path

    from agent_channel import core

    ranked_seen = {}

    def fake_rank(nodes, timeout=0):
        ranked_seen["nodes"] = nodes
        return nodes

    class FakeRunner:
        def __init__(self, *_a, **_k):
            self.stopped = False

        def start(self, *_a, **_k):
            pass

        def stop(self):
            self.stopped = True

    nodes_in = [
        {"type": "vless", "tag": "n1", "server": "x", "server_port": 443, "uuid": "u"}
    ]
    core._ACTIVE_RUNNER = None
    core._CURRENT_STATUS = {"status": "stopped"}
    try:
        with (
            patch.object(core, "rank_nodes", fake_rank),
            patch.object(core, "ensure_local_core", lambda: True),
            patch.object(
                core, "get_local_binary_path", lambda: Path(__file__).resolve()
            ),
            patch.object(core, "SingboxRunner", FakeRunner),
            patch.object(core, "_verify_proxy_forwarding", lambda url, timeout=0: True),
            patch.object(core, "set_system_proxy", lambda **kw: False),
        ):
            status = core.enable_channel(
                nodes=nodes_in, auto_patch=False, set_sys_proxy=True
            )
        assert status.get("status") == "running"
        assert ranked_seen["nodes"] == nodes_in
    finally:
        core._ACTIVE_RUNNER = None
        core._CURRENT_STATUS = {"status": "stopped"}


def test_enable_channel_accepts_json_single_node():
    """单节点输入可以是 JSON（{ 开头）：enable_channel 走 parse_single_node 解析。"""
    import json
    from pathlib import Path

    from agent_channel import core

    ranked_seen = {}

    def fake_rank(nodes, timeout=0):
        ranked_seen["nodes"] = nodes
        return nodes

    class FakeRunner:
        def __init__(self, *_a, **_k):
            pass

        def start(self, *_a, **_k):
            pass

        def stop(self):
            pass

    json_node = json.dumps(
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
    core._ACTIVE_RUNNER = None
    core._CURRENT_STATUS = {"status": "stopped"}
    try:
        with (
            patch.object(core, "rank_nodes", fake_rank),
            patch.object(core, "ensure_local_core", lambda: True),
            patch.object(
                core, "get_local_binary_path", lambda: Path(__file__).resolve()
            ),
            patch.object(core, "SingboxRunner", FakeRunner),
            patch.object(core, "_verify_proxy_forwarding", lambda url, timeout=0: True),
            patch.object(core, "set_system_proxy", lambda **kw: False),
        ):
            status = core.enable_channel(
                node_uri=json_node, auto_patch=False, set_sys_proxy=True
            )
        assert status.get("status") == "running"
        assert ranked_seen["nodes"][0]["type"] == "trojan"
    finally:
        core._ACTIVE_RUNNER = None
        core._CURRENT_STATUS = {"status": "stopped"}


def test_forwarding_check_timeout_tolerates_ipv6_fallback():
    """转发验证的超时必须能覆盖 IPv6 回落（实测 ~7s），否则“能通但首连慢”的节点被误判失败。"""
    import inspect

    from agent_channel import core

    verify = (
        inspect.signature(core._verify_proxy_forwarding).parameters["timeout"].default
    )
    check = inspect.signature(core._check_node_forwarding).parameters["timeout"].default
    assert verify >= 10, verify
    assert check >= 10, check


def test_is_relay_detects_landing_mismatch():
    """落地 IP 与 server 解析 IP 不一致 → 中转；一致 → 直连；无法判断 → None。"""
    from agent_channel import core

    relay = core._is_relay({"server": "203.0.113.10"}, "203.0.113.10")
    assert relay is not None and not relay
    assert core._is_relay({"server": "203.0.113.10"}, "198.51.100.7")
    assert core._is_relay({"server": "a.com"}, None) is None
    with patch("socket.gethostbyname", return_value="203.0.113.10"):
        assert core._is_relay({"server": "a.com"}, "198.51.100.7")


def test_benchmark_nodes_reports_landing_ip():
    """转发验证通过时，benchmark 结果带出真实落地 IP 与中转标记。"""
    from pathlib import Path

    from agent_channel import core

    node = {"type": "vless", "tag": "n", "server": "a.com", "server_port": 443}
    with (
        patch.object(core, "ensure_local_core"),
        patch.object(core, "get_local_binary_path", lambda: Path(__file__).resolve()),
        patch.object(core, "ping_node", return_value=50.0),
        patch.object(
            core, "_check_node_forwarding", return_value=(True, "198.51.100.7")
        ),
        patch.object(core, "_is_relay", return_value=True),
    ):
        results = core.benchmark_nodes(nodes=[node], forwarding_check=True)
    assert results[0]["forwarding"]
    assert results[0]["landing_ip"] == "198.51.100.7"
    assert results[0]["is_relay"]
