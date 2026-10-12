from pathlib import Path

from agent_channel.runner import build_minimal_config


def test_build_minimal_config():
    node = {
        "type": "vless",
        "tag": "proxy-out",
        "server": "example.com",
        "server_port": 443,
        "uuid": "test-uuid",
    }
    cfg = build_minimal_config(inbound_port=12345, outbound_node=node)
    assert cfg["inbounds"][0]["type"] == "mixed"
    assert cfg["inbounds"][0]["listen_port"] == 12345
    # sing-box 1.14 的 inbound 不支持 udp 字段（UDP 由 SOCKS5 UDP associate 默认提供）
    assert "udp" not in cfg["inbounds"][0]
    # dns 块应被省略：节点 server 为域名时 dns 走代理 detour 会形成循环依赖
    assert "dns" not in cfg
    assert cfg["outbounds"][0] is node
    assert cfg["outbounds"][1]["tag"] == "direct"
    assert cfg["outbounds"][2]["tag"] == "block"


def test_runner_fails_when_port_never_opens():
    """进程活着但监听端口始终没就绪 → 必须判失败（原先只在进程也退出时才报）。"""
    import io
    import socket
    import tempfile
    from unittest.mock import patch

    from agent_channel.runner import SingboxRunner

    class FakeProc:
        returncode = None
        stderr = io.BytesIO(b"")
        # start() 会把 pid 登记进 _LIVE_KERNEL_PIDS，替身也得有这个属性
        pid = 0

        def poll(self):
            return None

        def terminate(self):
            pass

        def wait(self, timeout=None):
            return 0

        def kill(self):
            pass

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]        # 绑定后立刻关掉，端口没人听

    runner = SingboxRunner(Path("sing-box.exe"))
    config = {"inbounds": [{"listen_port": port}], "outbounds": []}
    with tempfile.TemporaryDirectory() as tmp, patch(
        "subprocess.Popen", return_value=FakeProc()
    ):
        try:
            runner.start(config, Path(tmp) / "c.json")
        except RuntimeError as exc:
            assert "未就绪" in str(exc), exc
        else:
            raise AssertionError("端口没就绪时必须报错")


def test_heal_stale_kernel_only_reaps_orphans():
    """自愈只收真残留：本进程活内核与父进程仍在的内核都不能碰。

    回归背景：判据原先只有「端口被占 + 进程名含 sing-box」，既没排除本进程
    正在服务的内核（换节点误杀），也没排除另一个活实例的通道（重复启动应用
    误杀）。这条测试锁住扩展后的判据——以后换调用点也不会再犯同一类错。
    """
    from unittest.mock import patch

    from agent_channel import runner

    killed: list[int] = []

    # 1000 = 本进程活内核；2000 = 另一个活实例的通道（父进程 2001 仍在）；
    # 3000 = 父进程已死的孤儿残留
    parents = {1000: 999, 2000: 2001, 3000: 2999}

    with (
        patch.object(runner, "_pids_listening_on", lambda _port: [1000, 2000, 3000]),
        patch.object(runner, "_is_singbox_process", lambda _pid: True),
        patch.object(runner, "_parent_pid", lambda pid: parents.get(pid)),
        patch.object(runner, "_process_alive", lambda pid: pid == 2001),
        patch.object(runner, "_LIVE_KERNEL_PIDS", {1000}),
        patch.object(runner, "_kill_pid", lambda pid: killed.append(pid) or True),
    ):
        notes = runner.heal_stale_kernel(20849)

    assert killed == [3000], f"只应收掉孤儿残留 3000，实际: {killed}"
    # 跳过必须留提示：静默误判会退化成「用户看不懂的失败」
    assert any("未处理" in note for note in notes), f"跳过时应留提示，实际: {notes}"
