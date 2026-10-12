"""进程环境变量注入：让本进程的 HTTP 客户端无缝走通道代理。

只动本进程的环境变量（`os.environ`），不碰系统级设置 —— 系统代理是另一条路
（见 `sysproxy.py`），两者互不替代：Agent 走这条（零权限、进程内），
浏览器走系统代理那条（需要时由用户在面板显式打开）。
"""

import os
import platform

# 注入前的原值；`unpatch_environment` 按它还原。键存在但值为 None 表示"原本没有这个键"。
_ORIGINAL_ENV: dict[str, str | None] = {}


def patch_environment(proxy_url: str) -> None:
    """为当前进程注入网络代理环境变量，使 Python HTTP 客户端无缝走代理。

    在 Windows 下环境变量键名不区分大小写，只取标准大写键避免覆盖冲突。
    """
    if platform.system().lower() == "windows":
        keys = ["HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"]
    else:
        keys = [
            "HTTP_PROXY",
            "HTTPS_PROXY",
            "ALL_PROXY",
            "http_proxy",
            "https_proxy",
            "all_proxy",
        ]

    # 先完整快照原始值，再统一注入，防止 Windows 大小写重叠读取
    for k in keys:
        if k not in _ORIGINAL_ENV:
            _ORIGINAL_ENV[k] = os.environ.get(k)

    for k in keys:
        os.environ[k] = proxy_url


def unpatch_environment() -> None:
    """还原环境变量至注入前的状态（原本没有的键删掉，原本有值的写回原值）。"""
    for k, v in list(_ORIGINAL_ENV.items()):
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v
    _ORIGINAL_ENV.clear()
