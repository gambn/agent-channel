import re
from typing import Any

from .config import DEFAULT_LISTEN_PORT, target_kwargs
from .core import ChannelBusyError, disable_channel, enable_channel, get_channel_status
from .probe import test_single_endpoint
from .utils import generate_uuid

# ==========================================
# 1. 兼容 OpenAI / Anthropic / MCP 的标准工具规范
# ==========================================

AGENT_TOOLS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "channel_enable",
            "description": "启用或切换网络通道代理。支持传入单个节点链接(vless://, trojan://, ss://)或订阅链接。",
            "parameters": {
                "type": "object",
                "properties": {
                    "target": {
                        "type": "string",
                        "description": "代理目标：vless/trojan/ss 节点链接或订阅链接",
                    }
                },
                "required": ["target"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "channel_disable",
            "description": "停止并关闭网络通道代理，还原系统环境变量。",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "channel_status",
            "description": "查询当前网络通道的运行状态、本地监听端口和活跃节点信息。",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "channel_test_ai",
            "description": "测试当前网络通道对主流海外 AI 接口 (OpenAI, Gemini, Anthropic) 的连通性与握手延迟。",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "channel_generate_uuid",
            "description": "生成一个标准 RFC 4122 v4 UUID，供节点配置或调试使用。",
            "parameters": {"type": "object", "properties": {}},
        },
    },
]

# ==========================================
# 2. 原生 Tool 执行函数（供 Agent 框架直接回调）
# ==========================================


def tool_enable(target: str) -> str:
    kwargs = target_kwargs(target)
    if kwargs is None:
        return f"未能识别的代理目标: {target.strip()}。支持节点链接或订阅地址。"
    try:
        # 与其余四个入口一致：通道恒在 127.0.0.1:20849（README §九 的承诺）。
        # 此前不传 listen_port → 走 get_free_port() 随机口，于是出现“面板侧固定、Agent 侧随机”
        # 的口径不一；这是 **Agent 侧 API 契约变化**，README 已同步声明。
        res = enable_channel(**kwargs, listen_port=DEFAULT_LISTEN_PORT)
        return (
            f"网络通道已成功启用！独立内核 (节点: {res.get('node_tag', '默认')})，"
            f"本地端口: {res.get('port')}。"
        )
    except Exception as e:
        return f"启用通道失败: {e!s}"


def tool_disable() -> str:
    """停止通道；忙（另一次启停还在跑）时如实回报。

    工具函数的契约是"返回一句人机可读的话"，所以这里要自己接住 `ChannelBusyError`，
    不能把异常抛给调用方。
    """
    try:
        disable_channel()
    except ChannelBusyError as exc:
        return f"未能停止通道：{exc}"
    return "网络通道已成功停止并还原系统环境。"


def tool_status() -> str:
    cur = get_channel_status()
    if cur.get("status") == "running":
        return (
            f"当前通道运行中 (模式: {cur.get('mode')})，本地监听: {cur.get('proxy_url')}，"
            f"活跃节点: {cur.get('node_tag', '本地直连')}。"
        )
    return "网络通道当前处于停止状态。"


def tool_test_ai(proxy_url: str | None = None, running: bool | None = None) -> str:
    cur = get_channel_status()
    if running is None:
        # 与 server._handle_test_ai 同一判据：bool(proxy) and status == "running"。
        # 只看 proxy 真值的话，未来若出现“非 running 却带 proxy_url”的状态会静默走死代理。
        running = cur.get("status") == "running" and bool(cur.get("proxy_url"))
    proxy = proxy_url if proxy_url is not None else cur.get("proxy_url")
    effective_proxy = proxy if running else None
    openai_res = test_single_endpoint(
        "https://api.openai.com/v1/models", effective_proxy
    )
    gemini_res = test_single_endpoint(
        "https://generativelanguage.googleapis.com", effective_proxy
    )
    anthropic_res = test_single_endpoint("https://api.anthropic.com", effective_proxy)

    def fmt(name: str, r: dict[str, Any]) -> str:
        if r.get("reachable"):
            return f"{name}: 连通正常 ({r['latency_ms']}ms)"
        return f"{name}: 失败 ({r.get('error', '超时')})"

    lines = ["AI 接口连通性测试结果:"]
    if not running:
        # 诚实优先于好看：通道没跑时这是**直连**结果，不能让它看起来像“通道没问题”
        lines.append("（通道未运行，以下为直连结果，不代表通道可用）")
    lines += [
        f"- {fmt('OpenAI API', openai_res)}",
        f"- {fmt('Google Gemini', gemini_res)}",
        f"- {fmt('Anthropic Claude', anthropic_res)}",
    ]
    return "\n".join(lines)


def tool_generate_uuid() -> str:
    return f"已生成新 UUID: {generate_uuid()}"


# ==========================================
# 3. 自然语言直接解析与执行器
# ==========================================


def execute_natural_language(instruction: str) -> str:
    """解析自然语言指令并执行对应通道操作，返回人机可读结果。"""
    text = instruction.strip()
    lower = text.lower()

    # 1. 节点 URI
    uri_match = re.search(r"(vless://[^\s]+|trojan://[^\s]+|ss://[^\s]+)", text)
    if uri_match:
        return tool_enable(uri_match.group(1))

    # 2. 订阅 URL
    if ("订阅" in text or "airport" in lower or re.search(r"\bsub\b", lower)) and (
        "http://" in lower or "https://" in lower
    ):
        url_match = re.search(r"(https?://[^\s]+)", text)
        if url_match:
            return tool_enable(url_match.group(1))

    # 3. 连通性测试
    if any(k in text for k in ["测试", "测一下", "能连", "通不通", "连通性", "延迟"]):
        return tool_test_ai()

    # 4. 关闭通道
    if any(k in text for k in ["关闭", "停止", "停用", "关掉", "断开", "退出通道"]):
        return tool_disable()

    # 5. 状态查询
    if any(k in text for k in ["状态", "情况", "开了吗", "运行吗", "查一下通道"]):
        return tool_status()

    # 6. 生成 UUID
    if "uuid" in lower and any(k in text for k in ["生成", "创建", "给一个", "做一个"]):
        return tool_generate_uuid()

    # 7. 开启通道（未指定目标时提示）
    if any(
        k in text for k in ["启动通道", "开启通道", "打开通道", "连代理", "开启代理"]
    ):
        return "请提供节点链接（vless://...）或订阅地址以启用通道。"

    return (
        "抱歉，未能识别该通道指令。你可以尝试说：“连接 vless://...”"
        "“测一下 OpenAI 连通性”、“关闭代理”或“生成一个 UUID”。"
    )
