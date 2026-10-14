"""应用自身的新版本提示：只告知、不自动更新、查不到就什么都不说。

为什么要有这一块：用户装到手里的是发布产物，修复躺在源码里对他们等于不存在。
实例就是 0.1.2 —— 它的转发验证只问一个 Google 目标，目标被挡就把可用节点判死、
通道起不来；修复在 0.1.3 里，而用户机器上的 0.1.2 不会自己知道这件事。

三条纪律：

- **只告知**：三平台安装形态不同（deb / dmg / exe），静默自更新一旦出错就是把
  用户的通道弄没，风险远大于收益。这里只给“有新版本 + 去哪下”。
- **不阻塞、不打扰**：默认 24 小时内最多查一次（失败则 10 分钟后可再试），失败
  静默；没有新版本时上游拿到 None，前端什么都不显示。
- **不说谎**：查不到（被墙 / 超时 / 限流 / 响应异常）一律返回 None，绝不把
  “没查到”说成“已是最新版本”——与探针那条原则同源。
"""

from __future__ import annotations

import json
import re
import threading
import time
import urllib.request
from typing import Any, Callable

# Release API：只取最新一条。不带任何 token —— 本工具不持有也不需要凭据。
APP_RELEASE_API = "https://api.github.com/repos/gambn/agent-channel/releases/latest"

# 查到结果后的静默期；查失败时只歇这么久，免得一次网络抖动把提示压一整天。
CACHE_TTL_SECONDS = 24 * 3600
FAILURE_RETRY_SECONDS = 10 * 60

_FETCH_TIMEOUT = 5.0

_lock = threading.Lock()
_cache: dict[str, Any] = {"at": 0.0, "latest": None, "ttl": CACHE_TTL_SECONDS}


def parse_version(text: object) -> tuple[int, ...] | None:
    """把 `v0.1.3` / `0.1.3` / `0.1.3-rc1` 解析成可比较的元组；解析不出来返回 None。"""
    raw = str(text or "").strip().lstrip("vV")
    if not raw:
        return None
    parts: list[int] = []
    for chunk in raw.split("."):
        match = re.match(r"^(\d+)", chunk)
        if match is None:
            return None
        parts.append(int(match.group(1)))
    return tuple(parts)


def is_newer(latest: object, current: object) -> bool:
    """latest 是否**严格**新于 current。

    逐段按整数比，不按字符串比：字符串比会把 `0.1.10` 判成小于 `0.1.9`。段数不等时
    短的补 0（`0.2` 与 `0.2.0` 视为同一版）；解析不出来一律 False —— 宁可什么都不
    提示，也不能报一个假的新版本。
    """
    left, right = parse_version(latest), parse_version(current)
    if left is None or right is None:
        return False
    width = max(len(left), len(right))
    left += (0,) * (width - len(left))
    right += (0,) * (width - len(right))
    return left > right


def fetch_latest(
    timeout: float = _FETCH_TIMEOUT, proxy_url: str | None = None
) -> dict[str, Any] | None:
    """查最新 Release，返回 `{"version", "url"}`；任何异常都返回 None（不抛）。

    `proxy_url` 给了就走它，不给就直连并**绕过系统代理**：系统代理可能指向本工具
    已停止的端口，走它必然失败（与 `binary._open_direct` 同一个理由）。给代理的情形
    是通道正在跑 —— 被墙的网络里，那是唯一查得到的一条路。
    """
    req = urllib.request.Request(
        APP_RELEASE_API,
        headers={
            "User-Agent": "agent-channel",
            "Accept": "application/vnd.github+json",
        },
    )
    handlers = (
        [urllib.request.ProxyHandler({"http": proxy_url, "https": proxy_url})]
        if proxy_url
        else [urllib.request.ProxyHandler({})]
    )
    try:
        with urllib.request.build_opener(*handlers).open(req, timeout=timeout) as resp:
            data = json.loads(resp.read(1 << 16).decode("utf-8", errors="ignore"))
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    tag = str(data.get("tag_name") or "").strip()
    if not tag:
        # 限流时 GitHub 回的也是合法 JSON（`{"message": ...}`），此时没有版本可言
        return None
    assets = [
        {"name": str(a.get("name") or ""), "url": str(a.get("browser_download_url") or "")}
        for a in (data.get("assets") or [])
        if isinstance(a, dict)
    ]
    return {"version": tag, "url": str(data.get("html_url") or ""), "assets": assets}


def _asset_preferences(os_name: str, arch: str, system_install: bool) -> list[str]:
    """本机该下的资产名的匹配次序（第一个命中的就是它）。

    名字按仓库产物的实际命名来（见 BUILD-INFO.txt）：macOS 是 `-darwin-x64.dmg`、
    Windows 是 `-setup.exe` 与 `-windows-x64.zip`、Linux 是 `-linux-x64.tar.gz` 与
    `_amd64.deb`（deb 的命名里没有平台词，所以单列一条）。
    """
    arm = arch == "arm64"
    if os_name == "darwin":
        tag = "darwin-arm64" if arm else "darwin-x64"
        # 装过就用 dmg（拖进应用程序更规矩），免安装的 tar.gz 排后面
        return [f"{tag}.dmg", f"{tag}.tar.gz"]
    if os_name == "windows":
        return ["-setup.exe", "windows-x64.zip"]
    if os_name == "linux":
        tag = "linux-arm64" if arm else "linux-x64"
        deb = "_arm64.deb" if arm else "_amd64.deb"
        # 装在系统目录的（deb）给 deb，装在用户目录的（install.sh 到 ~/.local/opt）给 tar.gz：
        # 让用户拿到的那个包跟他现在这份是同一种装法，免得要他去卸一个不该卸的东西。
        return [deb, f"{tag}.tar.gz"] if system_install else [f"{tag}.tar.gz", deb]
    return []


def _platform_context() -> tuple[str, str, bool]:
    """(系统名, 架构, 是否系统目录安装)；认不出来返回空串组合（于是不给资产直链）。

    认不出来绝不抛：版本提示是附赠信息，不能因为识别平台失败就让面板出错。
    """
    try:
        from .binary import platform_arch, platform_os

        os_name, arch = platform_os(), platform_arch()
    except Exception:
        return "", "", False
    try:
        import sys
        from pathlib import Path

        # 打包后的程序看自身可执行文件；从wheel/源码跑就看这个模块所在位置
        here = Path(sys.executable) if getattr(sys, "frozen", False) else Path(__file__)
        system_install = not str(here.resolve()).startswith(str(Path.home()))
    except Exception:
        system_install = False
    return os_name, arch, system_install


def pick_asset(
    assets: object, os_name: str, arch: str, *, system_install: bool = False
) -> dict[str, Any] | None:
    """从 Release 资产里挑出本机该下的那一个；挑不出来返回 None（调用方退到发布页）。"""
    if not isinstance(assets, list) or not os_name:
        return None
    for want in _asset_preferences(os_name, arch, system_install):
        for asset in assets:
            if not isinstance(asset, dict):
                continue
            name = str(asset.get("name") or "")
            url = str(asset.get("url") or "")
            if want in name and url:
                return {"name": name, "url": url}
    return None


def check_for_update(
    current: str | None = None,
    *,
    dismissed: str = "",
    force: bool = False,
    proxy_url: str | None = None,
    now: float | None = None,
    fetch: Callable[..., dict[str, Any] | None] | None = None,
) -> dict[str, Any] | None:
    """有严格更新的版本就返回 `{"version", "url", "current"}`，否则返回 None。

    None 同时表示三件事：没有新版本、这次没查到、这个版本已被用户跳过。调用方据此
    不做任何显示 —— **“查不到”与“已是最新”在界面上必须一样安静**，因为面板本来
    就没有“已是最新”这个结论可下。
    """
    if current is None:
        from . import __version__

        current = __version__
    moment = time.monotonic() if now is None else now
    do_fetch = fetch or fetch_latest

    with _lock:
        fresh = (moment - float(_cache["at"])) < float(_cache["ttl"])
        latest: dict[str, Any] | None = _cache["latest"] if fresh else None
    # 只看"缓存还新不新"，不看缓存里是不是 None：缓存下来的失败也算一次结果，
    # 否则失败静默期形同虚设（每次调用都会立刻再打一次网络）。
    if not fresh or force:
        try:
            latest = do_fetch(proxy_url=proxy_url)
        except Exception:
            # 这一层是给调用方的硬保证：它被 HTTP 处理器直接调用，抛出去就是面板 500
            # 加一段回溯。"查不到"必须是安静的（fetch_latest 自己已不抛，这里是兜底）。
            latest = None
        with _lock:
            _cache["at"] = moment
            _cache["latest"] = latest
            _cache["ttl"] = CACHE_TTL_SECONDS if latest else FAILURE_RETRY_SECONDS
    if not latest:
        return None

    version = str(latest.get("version") or "")
    dismissed_version = parse_version(dismissed)
    if dismissed_version is not None and dismissed_version == parse_version(version):
        return None
    if not is_newer(version, current):
        return None
    os_name, arch, system_install = _platform_context()
    return {
        "version": version,
        "url": str(latest.get("url") or ""),
        "current": current,
        # 有没有直链都不影响提示：没有就退回发布页，让用户自己挑
        "asset": pick_asset(
            latest.get("assets"),
            os_name,
            arch,
            system_install=system_install,
        ),
    }
