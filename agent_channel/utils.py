import socket
import sys
import uuid
from contextlib import suppress
from typing import Any

# 不可信出口（第三方节点）的响应上限：这段流量经过不受信的节点出口，节点比订阅源
# 更不可信，标准不应低于订阅读取。
MAX_PROBE_BYTES = 1 * 1024 * 1024


def warn_stderr(message: str) -> None:
    """尽力把一句警告写到 stderr；写不出去也绝不让调用方失败。

    为什么不直接用 `print(..., file=sys.stderr)`（两处真实差异，已实测）：

    - `sys.stderr` 为 `None`（PyInstaller `console=False`）时，`print` 的 `file=None`
      会**回落到 stdout** —— 不抛异常，但会把告警混进本该干净的标准输出（CLI 输出由
      Agent 解析，面板启动横幅也在 stdout）。这里宁可不写；
    - stderr **不可写**（已关闭 / 句柄失效）时 `print` 会抛 `ValueError`。调用点常在
      `finally` 或 HTTP 处理里，那里再抛异常会把"一句日志没写出去"升级成真故障
      （从清理路径逃出、或让一次成功的启用回 400）。

    当前 spec 是 `console=True`，但这条底线不该由打包参数决定。
    """
    stream = sys.stderr
    if stream is None:
        return
    with suppress(Exception):
        print(message, file=stream)


def read_limited(resp: Any, limit: int) -> bytes:
    """分块读响应体，超限立刻抛 ValueError。

    第一道是 Content-Length 预检（服务器可能不报或报错），真正的防线是累计计数——
    下游常用 `errors="ignore"` 解码，会吞掉异常，不能靠解码失败发现问题。
    """
    declared = resp.headers.get("Content-Length") if resp.headers else None
    if declared:
        try:
            declared_n = int(declared)
        except (TypeError, ValueError):
            declared_n = 0
        if declared_n > limit:
            raise ValueError(f"响应过大（{declared_n} 字节，上限 {limit}）")
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = resp.read(64 * 1024)
        if not chunk:
            break
        total += len(chunk)
        if total > limit:
            raise ValueError(f"响应超过上限 {limit} 字节，已中止")
        chunks.append(chunk)
    return b"".join(chunks)


def generate_uuid(dash: bool = True) -> str:
    """生成标准 RFC 4122 v4 UUID。

    :param dash: 是否包含连字符，默认为 True
    """
    u = uuid.uuid4()
    return str(u) if dash else u.hex


def get_free_port() -> int:
    """动态绑定 127.0.0.1:0 获取系统随机分配的空闲端口。"""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def configure_stdio() -> None:
    """把标准输出/错误设成行缓冲。

    为什么需要：冻结产物（PyInstaller）的 stdout 被重定向到文件或管道时是**块缓冲**，
    实测 `PYTHONUNBUFFERED=1` 也压不住——启动横幅要等进程退出才刷出来，用户看不到面板
    地址，任何"grep 日志找端口"的脚本都会误判。这里显式设成行缓冲，入口处调一次。

    窗口模式（Windows 托盘 / macOS 外壳）下 stdio 可能是 None 或不支持 reconfigure，
    所以一律容错：设不上就算了，绝不让它影响启动。
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        with suppress(Exception):
            reconfigure(line_buffering=True)
