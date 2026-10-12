import contextlib
import io
import re
from unittest.mock import patch

from agent_channel import utils
from agent_channel.utils import generate_uuid, get_free_port, warn_stderr


def test_generate_uuid_default():
    val = generate_uuid()
    assert re.match(r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$", val)


def test_generate_uuid_without_dash():
    val = generate_uuid(dash=False)
    assert len(val) == 32
    assert "-" not in val


def test_get_free_port():
    port = get_free_port()
    assert isinstance(port, int)
    assert 1024 <= port <= 65535


def test_warn_stderr_does_not_fall_back_to_stdout():
    """stderr 不可用时宁可不写，也不能把告警甩到 stdout。

    回归背景：`print(msg, file=None)` 会回落到 `sys.stdout` —— 不抛异常，但 stdout 是
    CLI 与 Agent 的机器可读输出（面板启动横幅也在那里），告警混进去就是污染。
    """
    leaked = io.StringIO()
    with (
        patch.object(utils.sys, "stderr", None),
        contextlib.redirect_stdout(leaked),
    ):
        warn_stderr("stderr 没了也不能落到 stdout")
    assert leaked.getvalue() == "", f"告警落到了 stdout：{leaked.getvalue()!r}"

    captured = io.StringIO()
    with contextlib.redirect_stderr(captured):
        warn_stderr("正常时要把话说出来")
    assert "正常时要把话说出来" in captured.getvalue(), captured.getvalue()


def test_warn_stderr_survives_unwritable_stderr():
    """stderr 不可写（已关闭）时记日志不得抛异常。

    回归背景：`print(..., file=已关闭的流)` 会抛 `ValueError: I/O operation on closed
    file`；而调用点在 `finally` 与 HTTP 处理里，抛出会把"日志没写出去"升级成真故障。
    """
    closed = io.StringIO()
    closed.close()
    with patch.object(utils.sys, "stderr", closed):
        warn_stderr("写不进去也不能抛")  # 不抛即通过
