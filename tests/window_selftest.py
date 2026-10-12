"""外壳窗口的自检（需要图形会话，故不进 `run_tests.py` 的自动清单）。

为什么单独一条：这是唯一能验证"窗口起得来、关掉它真的走清理"的方式。
单元测试只能验纯逻辑（状态文字、单实例判据、清理函数），Tk 窗口本身要在有显示环境的
机器上跑——本机 macOS、CI 的 macOS runner 都属于这类。

做法：在隔离的 HOME 里起窗口，`close_after_ms` 到点自动关一次（走的正是红叉那条
`WM_DELETE_WINDOW` 路径），然后断言清理完成、面板端口已释放、随包内核被解到本地。

用法: python3 tests/window_selftest.py [超时秒数]
"""

import os
import signal
import socket
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO) if str(REPO) not in sys.path else str(REPO))

CLOSE_AFTER_MS = 3000


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _port_is_free(port: int) -> bool:
    with socket.socket() as sock:
        sock.settimeout(0.5)
        return sock.connect_ex(("127.0.0.1", port)) != 0


def main() -> int:
    deadline = int(sys.argv[1]) if len(sys.argv) > 1 else 90
    # 卡住就自己了结：CI 里不能靠 timeout 命令（macOS 默认没有）
    signal.alarm(deadline)

    sandbox = Path(tempfile.mkdtemp(prefix="ac-window-selftest-"))
    os.environ["HOME"] = str(sandbox)
    os.environ["LOCALAPPDATA"] = str(sandbox / "LocalAppData")
    os.environ.pop("AGENT_CHANNEL_NO_CORE", None)

    port = _free_port()
    print(f"[selftest] 隔离 HOME = {sandbox}")
    print(f"[selftest] 面板端口 = {port}")

    from agent_channel.window import run_window

    code = run_window(port=port, open_panel=False, close_after_ms=CLOSE_AFTER_MS)
    print(f"[selftest] run_window 返回 {code}")

    assert code == 0, f"退出码应为 0，实际 {code}"
    assert _port_is_free(port), "退出后面板端口必须已释放"

    from agent_channel.binary import get_bundled_core_archive

    binary = sandbox / ".agent_channel" / "bin"
    seeded = sorted(p.name for p in binary.iterdir()) if binary.is_dir() else []
    print(f"[selftest] 首次启动解出的内核文件: {seeded}")
    if get_bundled_core_archive() is None:
        # 从源码树跑时包里没有预置内核，播种这条断言只对"已打包产物"成立
        print("[selftest] 源码树运行：包里没有预置内核，跳过播种断言")
    else:
        assert seeded, "随包内核应被解到隔离 HOME 的 ~/.agent_channel/bin"

    print("SELFTEST OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
