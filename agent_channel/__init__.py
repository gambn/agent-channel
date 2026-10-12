"""
agent-channel通道代理: 轻量级 AI 通道代理模块
"""

__version__ = "0.1.0"

from .binary import check_core_update, update_core
from .config import (
    DEFAULT_CONFIG,
    DEFAULT_LISTEN_PORT,
    active_source_of,
    channel_kwargs,
    channel_state,
    config_path,
    load_config,
    save_config,
    slot_candidates,
    slot_selection,
)
from .core import (
    benchmark_nodes,
    disable_channel,
    enable_channel,
    get_channel_status,
)
from .server import launch_ui
from .sysproxy import get_system_proxy_status, set_system_proxy, unset_system_proxy
from .tools import AGENT_TOOLS, execute_natural_language
from .utils import generate_uuid, get_free_port

__all__ = [
    "AGENT_TOOLS",
    "DEFAULT_CONFIG",
    "DEFAULT_LISTEN_PORT",
    "active_source_of",
    "benchmark_nodes",
    "channel_kwargs",
    "channel_state",
    "check_core_update",
    "config_path",
    "disable_channel",
    "enable_channel",
    "execute_natural_language",
    "generate_uuid",
    "get_channel_status",
    "get_free_port",
    "get_system_proxy_status",
    "launch_ui",
    "load_config",
    "save_config",
    "set_system_proxy",
    "slot_candidates",
    "slot_selection",
    "unset_system_proxy",
    "update_core",
]
