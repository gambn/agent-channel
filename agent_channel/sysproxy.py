import atexit
import platform
import subprocess
import time
from contextlib import suppress
from typing import Any

from .utils import warn_stderr

_SYSTEM_PROXY_ENABLED_BY_US = False
# 我们动手之前用户原有的代理设置。只在第一次开启时记，关闭时按它还原 ——
# 否则“用一次就把别人配好的代理和绕过列表冲掉”。
_ORIGINAL_PROXY: dict[str, Any] | None = None
# 我们接管时写进系统的那一个端口。归属校验要用它回答“当前值还是不是我们设的”：
# `_SYSTEM_PROXY_ENABLED_BY_US` 只说明“我们设过”，不等于“现在仍是我们的”。
_MANAGED_PORT: int | None = None

# 状态读数的进程内短缓存。面板每 3 秒轮询一次 /api/status，而每次读系统代理都要起
# 子进程（Linux 三个 gsettings、macOS networksetup）—— 纯属浪费。2 秒 TTL 让连续
# 轮询复用同一次读数；自己的开/关操作会主动失效缓存，所以面板点完开关立刻能看到新
# 状态，不会读到过期值。
_STATUS_CACHE_TTL = 2.0
_status_cache: tuple[float, dict[str, Any]] | None = None


def _invalidate_status_cache() -> None:
    """让下次读取重新问系统（自己的写操作之后必须调用）。"""
    global _status_cache
    _status_cache = None


def get_os_type() -> str:
    return platform.system().lower()


# ==========================================
# Windows 平台实现 (通过 HKCU 注册表 + WinINet 广播)
# ==========================================


def _refresh_wininet() -> None:
    try:
        import ctypes

        wininet = ctypes.windll.wininet
        INTERNET_OPTION_SETTINGS_CHANGED = 39
        INTERNET_OPTION_REFRESH = 37
        wininet.InternetSetOptionW(0, INTERNET_OPTION_SETTINGS_CHANGED, 0, 0)
        wininet.InternetSetOptionW(0, INTERNET_OPTION_REFRESH, 0, 0)
    except Exception:
        pass


def _get_windows_status() -> dict[str, Any]:
    try:
        import winreg

        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Internet Settings",
        ) as key:
            try:
                enabled, _ = winreg.QueryValueEx(key, "ProxyEnable")
            except FileNotFoundError:
                enabled = 0
            try:
                server, _ = winreg.QueryValueEx(key, "ProxyServer")
            except FileNotFoundError:
                server = ""
            return {"enabled": bool(enabled), "server": str(server)}
    except Exception:
        return {"enabled": False, "server": ""}


def _set_windows_proxy(port: int, host: str = "127.0.0.1") -> bool:
    try:
        import winreg

        proxy_str = f"{host}:{port}"
        override = "<local>;localhost;127.*;10.*;172.16.*;172.17.*;172.18.*;172.19.*;172.20.*;172.21.*;172.22.*;172.23.*;172.24.*;172.25.*;172.26.*;172.27.*;172.28.*;172.29.*;172.30.*;172.31.*;192.168.*"
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Internet Settings",
            0,
            winreg.KEY_WRITE,
        ) as key:
            winreg.SetValueEx(key, "ProxyEnable", 0, winreg.REG_DWORD, 1)
            winreg.SetValueEx(key, "ProxyServer", 0, winreg.REG_SZ, proxy_str)
            winreg.SetValueEx(key, "ProxyOverride", 0, winreg.REG_SZ, override)
            # PAC（AutoConfigURL）优先于手动代理：系统里若残留它，刚写的手动设置会被
            # 旁路。只在原值已快照时清除——快照失败还清就等于直接丢掉用户的配置。
            if _ORIGINAL_PROXY is not None:
                with suppress(FileNotFoundError):
                    winreg.DeleteValue(key, "AutoConfigURL")
        _refresh_wininet()
        return True
    except Exception:
        return False


def _unset_windows_proxy() -> bool:
    try:
        import winreg

        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Internet Settings",
            0,
            winreg.KEY_WRITE,
        ) as key:
            winreg.SetValueEx(key, "ProxyEnable", 0, winreg.REG_DWORD, 0)
        _refresh_wininet()
        return True
    except Exception:
        return False


def _write_windows_raw(values: dict[str, Any]) -> bool:
    """把注册表里那几项按原样写回（含 ProxyOverride）。值为 None 表示原本没有这项，删掉。"""
    try:
        import winreg

        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Internet Settings",
            0,
            winreg.KEY_WRITE,
        ) as key:
            for name, value in values.items():
                if value is None:
                    with suppress(FileNotFoundError):
                        winreg.DeleteValue(key, name)
                else:
                    kind = winreg.REG_DWORD if name == "ProxyEnable" else winreg.REG_SZ
                    winreg.SetValueEx(key, name, 0, kind, value)
        _refresh_wininet()
        return True
    except Exception:
        return False


# ==========================================
# macOS (Darwin) 平台实现 (通过 networksetup 命令)
# ==========================================


def _get_macos_network_service() -> str:
    try:
        out = subprocess.check_output(
            ["networksetup", "-listnetworkserviceorder"],
            text=True,
            stderr=subprocess.DEVNULL,
        )
        for svc in ["Wi-Fi", "Ethernet", "Thunderbolt Ethernet"]:
            if svc in out:
                return svc
    except Exception:
        pass
    return "Wi-Fi"


def _get_macos_status() -> dict[str, Any]:
    svc = _get_macos_network_service()
    try:
        out = subprocess.check_output(
            ["networksetup", "-getwebproxy", svc], text=True, stderr=subprocess.DEVNULL
        )
        enabled = "Enabled: Yes" in out
        server = ""
        for line in out.splitlines():
            if line.startswith("Server:"):
                server = line.split(":", 1)[1].strip()
        return {"enabled": enabled, "server": server}
    except Exception:
        return {"enabled": False, "server": ""}


def _set_macos_proxy(port: int, host: str = "127.0.0.1") -> bool:
    svc = _get_macos_network_service()
    try:
        subprocess.run(
            ["networksetup", "-setwebproxy", svc, host, str(port)],
            check=True,
            stderr=subprocess.DEVNULL,
        )
        subprocess.run(
            ["networksetup", "-setsecurewebproxy", svc, host, str(port)],
            check=True,
            stderr=subprocess.DEVNULL,
        )
        subprocess.run(
            ["networksetup", "-setwebproxystate", svc, "on"],
            check=True,
            stderr=subprocess.DEVNULL,
        )
        subprocess.run(
            ["networksetup", "-setsecurewebproxystate", svc, "on"],
            check=True,
            stderr=subprocess.DEVNULL,
        )
        return True
    except Exception:
        return False


def _unset_macos_proxy() -> bool:
    svc = _get_macos_network_service()
    try:
        subprocess.run(
            ["networksetup", "-setwebproxystate", svc, "off"], stderr=subprocess.DEVNULL
        )
        subprocess.run(
            ["networksetup", "-setsecurewebproxystate", svc, "off"],
            stderr=subprocess.DEVNULL,
        )
        return True
    except Exception:
        return False


# ==========================================
# Linux 平台实现 (基于 gsettings 桌面代理规范)
# ==========================================


def _get_linux_status() -> dict[str, Any]:
    try:
        mode = (
            subprocess.check_output(
                ["gsettings", "get", "org.gnome.system.proxy", "mode"],
                text=True,
                stderr=subprocess.DEVNULL,
            )
            .strip()
            .replace("'", "")
        )
        host = (
            subprocess.check_output(
                ["gsettings", "get", "org.gnome.system.proxy.http", "host"],
                text=True,
                stderr=subprocess.DEVNULL,
            )
            .strip()
            .replace("'", "")
        )
        port = subprocess.check_output(
            ["gsettings", "get", "org.gnome.system.proxy.http", "port"],
            text=True,
            stderr=subprocess.DEVNULL,
        ).strip()
        return {"enabled": mode == "manual", "server": f"{host}:{port}" if host else ""}
    except Exception:
        return {"enabled": False, "server": ""}


def _set_linux_proxy(port: int, host: str = "127.0.0.1") -> bool:
    try:
        subprocess.run(
            ["gsettings", "set", "org.gnome.system.proxy.http", "host", host],
            check=True,
            stderr=subprocess.DEVNULL,
        )
        subprocess.run(
            ["gsettings", "set", "org.gnome.system.proxy.http", "port", str(port)],
            check=True,
            stderr=subprocess.DEVNULL,
        )
        subprocess.run(
            ["gsettings", "set", "org.gnome.system.proxy.https", "host", host],
            check=True,
            stderr=subprocess.DEVNULL,
        )
        subprocess.run(
            ["gsettings", "set", "org.gnome.system.proxy.https", "port", str(port)],
            check=True,
            stderr=subprocess.DEVNULL,
        )
        subprocess.run(
            ["gsettings", "set", "org.gnome.system.proxy", "mode", "manual"],
            check=True,
            stderr=subprocess.DEVNULL,
        )
        return True
    except Exception:
        return False


def _unset_linux_proxy() -> bool:
    try:
        subprocess.run(
            ["gsettings", "set", "org.gnome.system.proxy", "mode", "none"],
            stderr=subprocess.DEVNULL,
        )
        return True
    except Exception:
        return False


# ==========================================
# 跨平台公共接口
# ==========================================


def get_system_proxy_status() -> dict[str, Any]:
    """读取当前操作系统的系统代理状态（跨 Windows, macOS, Linux）。

    带 2 秒进程内缓存：面板的 3 秒轮询会密集调用它，而每次都要起子进程。返回副本，
    调用方改返回值不会污染缓存。
    """
    global _status_cache
    now = time.monotonic()
    if _status_cache is not None and now - _status_cache[0] < _STATUS_CACHE_TTL:
        return dict(_status_cache[1])
    sys_type = get_os_type()
    if sys_type == "windows":
        status = _get_windows_status()
    elif sys_type == "darwin":
        status = _get_macos_status()
    else:
        status = _get_linux_status()
    _status_cache = (now, dict(status))
    return dict(status)


# ==========================================
# 原值快照 / 还原（跨平台）
# ==========================================


def _snapshot_proxy() -> dict[str, Any]:
    """记下当前的系统代理设置。Windows 连注册表原值一起记，其余平台记状态。"""
    if get_os_type() == "windows":
        import winreg

        out: dict[str, Any] = {}
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Internet Settings",
        ) as key:
            for name in (
                "ProxyEnable",
                "ProxyServer",
                "ProxyOverride",
                "AutoConfigURL",
            ):
                try:
                    out[name] = winreg.QueryValueEx(key, name)[0]
                except FileNotFoundError:
                    out[name] = None
        return out
    return _get_macos_status() if get_os_type() == "darwin" else _get_linux_status()


def _restore_proxy(snapshot: dict[str, Any]) -> bool:
    """按快照还原。"""
    try:
        if get_os_type() == "windows":
            return _write_windows_raw(snapshot)
        # macOS / Linux：按记下的服务器与开关状态尽量还原
        if snapshot.get("enabled") and snapshot.get("server"):
            host, _, port = str(snapshot["server"]).rpartition(":")
            if host and port.isdigit():
                return (
                    _set_macos_proxy(int(port), host)
                    if get_os_type() == "darwin"
                    else _set_linux_proxy(int(port), host)
                )
        return (
            _unset_macos_proxy() if get_os_type() == "darwin" else _unset_linux_proxy()
        )
    except Exception:
        return False


def _proxy_targets_port(server: str, port: int) -> bool:
    """`server` 是否**精确**指向回环地址上的 `port`。

    Windows 的 `ProxyServer` 可能是多协议串（`http=1.2.3.4:8080;https=...`），所以先按
    `;` 分段、逐段剥掉 `协议=` 前缀，再按 host:port 精确比对**端口**。
    此前用的是 `str(listen_port) in server` 子串判据 —— 查 888 会把指向 8888 的用户代理误关。
    解析不出来的段一律视为不匹配（**宁可不自愈，也不误关用户自己的代理**）。
    """
    for part in str(server).split(";"):
        piece = part.strip()
        if "=" in piece:
            piece = piece.split("=", 1)[1].strip()
        host, _, tail = piece.rpartition(":")
        if host.strip() not in ("127.0.0.1", "localhost", "::1", "[::1]"):
            continue
        if tail.strip() == str(port):
            return True
    return False


def _gsettings_value(schema: str, key: str) -> str:
    """读一个 gsettings 值；读不到返回空串（调用方据此判为“不是我们的”）。"""
    try:
        out = subprocess.check_output(
            ["gsettings", "get", schema, key], text=True, stderr=subprocess.DEVNULL
        )
        return out.strip().strip("'\"")
    except Exception:
        return ""


def _macos_proxy_segment(svc: str, flag: str) -> tuple[str, str] | None:
    """解析 `networksetup -get(secure)webproxy`：返回 (host, port)；未启用/读不到返回 None。"""
    try:
        out = subprocess.check_output(
            ["networksetup", flag, svc], text=True, stderr=subprocess.DEVNULL
        )
    except Exception:
        return None
    if "Enabled: Yes" not in out:
        return None
    host = ""
    port = ""
    for line in out.splitlines():
        if line.startswith("Server:"):
            host = line.split(":", 1)[1].strip()
        elif line.startswith("Port:"):
            port = line.split(":", 1)[1].strip()
    return (host, port)


def _macos_auto_enabled(svc: str) -> bool:
    """PAC（-getautoproxyurl）是否启用。读不到按“已启用”算，宁可不动。"""
    try:
        out = subprocess.check_output(
            ["networksetup", "-getautoproxyurl", svc],
            text=True,
            stderr=subprocess.DEVNULL,
        )
    except Exception as exc:
        # 读不到时我们既不会还原也不会提示用户，“怎么没还原”必须留一道痕迹
        warn_stderr(f"无法读取 macOS 自动代理状态（按已启用处理，不改动系统）: {exc}")
        return True
    return "Enabled: Yes" in out


def _segment_is_ours(host: str, port: str, expected: int) -> bool:
    return str(host).strip() in ("127.0.0.1", "localhost", "::1", "[::1]") and str(
        port
    ).strip() == str(expected)


def _read_current_segments() -> dict[str, Any] | None:
    """**不走缓存**地读当前系统代理的逐段原值，供归属校验使用。

    刻意不复用 `_get_*_status()`：那几个只读 http / web 一段，判不出“用户只改了另一段”。
    读不到就返回 None，调用方按“不是我们的”处理 —— 宁可不碰，也不误改用户配置。
    """
    system = get_os_type()
    try:
        if system == "windows":
            import winreg

            raw: dict[str, Any] = {}
            with winreg.OpenKey(
                winreg.HKEY_CURRENT_USER,
                r"Software\Microsoft\Windows\CurrentVersion\Internet Settings",
            ) as key:
                for name in ("ProxyEnable", "ProxyServer", "AutoConfigURL"):
                    try:
                        raw[name] = winreg.QueryValueEx(key, name)[0]
                    except FileNotFoundError:
                        raw[name] = None
            return {
                "kind": "windows",
                "enable": raw["ProxyEnable"],
                "server": raw["ProxyServer"],
                "auto": raw["AutoConfigURL"],
            }
        if system == "darwin":
            svc = _get_macos_network_service()
            return {
                "kind": "macos",
                "web": _macos_proxy_segment(svc, "-getwebproxy"),
                "secure": _macos_proxy_segment(svc, "-getsecurewebproxy"),
                "auto": _macos_auto_enabled(svc),
            }
        return {
            "kind": "linux",
            "mode": _gsettings_value("org.gnome.system.proxy", "mode"),
            "http": (
                _gsettings_value("org.gnome.system.proxy.http", "host"),
                _gsettings_value("org.gnome.system.proxy.http", "port"),
            ),
            "https": (
                _gsettings_value("org.gnome.system.proxy.https", "host"),
                _gsettings_value("org.gnome.system.proxy.https", "port"),
            ),
        }
    except Exception:
        return None


def _windows_server_is_ours(server: Any, expected: int) -> bool:
    """Windows 的 `ProxyServer` 是否**整串**就是我们写的 `host:port`。

    不能用 `_proxy_targets_port()`：“任一段命中”在多协议串上太宽。我们从来只写裸
    `host:port`，一旦当前值变成 `http=…;https=…`，说明是别人（clash-verge 等）写的；
    此时若按“任一段命中”放行，还原会把用户改过的那一段覆盖回快照值。
    """
    text = str(server or "").strip()
    if not text or ";" in text or "=" in text:
        return False
    host, _, port = text.rpartition(":")
    return _segment_is_ours(host, port, expected)


def _current_proxy_is_ours() -> bool:
    """当前系统代理是否仍**逐段精确**等于我们设的那一份。

    任何一段不是我们写的（被别的客户端改写、用户中途换成 PAC、只改了其中一段），
    或者读不出来，一律 False —— 调用方据此只清自己的状态、不写系统。

    残留风险（可接受）：判定与写入之间仍有毫秒级 TOCTOU 窗口，另一个客户端刚好在这个
    窗口里改写仍会被覆盖。gsettings / 注册表都没有互斥机制，要彻底消除需另做系统级锁。
    """
    expected = _MANAGED_PORT
    if expected is None:
        return False
    seg = _read_current_segments()
    if seg is None:
        return False
    kind = seg.get("kind")
    if kind == "windows":
        try:
            enabled = int(seg.get("enable") or 0)
        except (TypeError, ValueError):
            return False
        if enabled != 1:
            return False
        if seg.get("auto"):
            # 用户中途开了 PAC：Windows 上 PAC 优先于手动代理，这份已经不是我们的了
            return False
        return _windows_server_is_ours(seg.get("server"), expected)
    if kind == "macos":
        if seg.get("auto"):
            return False
        for name in ("web", "secure"):
            item = seg.get(name)
            if not item or not _segment_is_ours(item[0], item[1], expected):
                return False
        return True
    if str(seg.get("mode") or "") != "manual":
        return False
    for name in ("http", "https"):
        item = seg.get(name) or ("", "")
        if not _segment_is_ours(item[0], item[1], expected):
            return False
    return True


def heal_stale_system_proxy(listen_port: int) -> bool:
    """启动期自愈：系统代理指向我们自己的端口、而本进程并无通道在跑时，关掉它。

    进程被强杀（taskkill /F、kill -9、断电）时 atexit 不触发，系统代理会悬挂在
    127.0.0.1:<listen_port>，用户重启后上不了网却看不出原因。

    边界：只能「关闭悬挂代理」，**不能还原用户原有代理**——_ORIGINAL_PROXY 是
    内存变量，进程崩溃即丢失；要真正还原得把快照落盘（未做）。
    """
    system = get_os_type()
    if system == "windows":
        status = _get_windows_status()
        unset = _unset_windows_proxy
    elif system == "darwin":
        status = _get_macos_status()
        unset = _unset_macos_proxy
    else:
        status = _get_linux_status()
        unset = _unset_linux_proxy
    if not status.get("enabled"):
        return False
    if not _proxy_targets_port(str(status.get("server", "")), listen_port):
        return False
    changed = bool(unset())
    # 自愈直接调私有的 unset，绕过了公开入口，缓存要在这里一并失效
    _invalidate_status_cache()
    return changed


def set_system_proxy(port: int, host: str = "127.0.0.1") -> bool:
    """跨平台开启系统代理（支持 Windows, macOS, Linux）"""
    global _SYSTEM_PROXY_ENABLED_BY_US, _ORIGINAL_PROXY, _MANAGED_PORT
    if _ORIGINAL_PROXY is None:
        # 只在第一次开启时记原值，重复开启不会拿我们自己的值覆盖这份记录
        _ORIGINAL_PROXY = _snapshot_proxy()
    sys_type = get_os_type()
    ok = False
    if sys_type == "windows":
        ok = _set_windows_proxy(port, host)
    elif sys_type == "darwin":
        ok = _set_macos_proxy(port, host)
    else:
        ok = _set_linux_proxy(port, host)
    if ok:
        _SYSTEM_PROXY_ENABLED_BY_US = True
        _MANAGED_PORT = port
    elif _ORIGINAL_PROXY is not None:
        # 写失败：平台实现可能已经改了一半（例如只写了 http 段）。这种“半截是我们的”
        # 混合态，归属校验（要求**逐段**都是我们的）判不出来，只能当场收尾：
        # best-effort 按快照还原，再把状态清干净，不留旧快照给后面猜。
        # 三个变量一起清：若此前成功接管过一次，只清快照会留下“标志为真、端口还在、
        # 却已不再对应任何快照”的脏语义。
        _restore_proxy(_ORIGINAL_PROXY)
        _ORIGINAL_PROXY = None
        _SYSTEM_PROXY_ENABLED_BY_US = False
        _MANAGED_PORT = None
    # 无论成败都失效缓存：失败也可能已改了一半（如只写了 http 段），
    # 让下一次读数如实反映现状，而不是继续发缓存的旧状态。
    _invalidate_status_cache()
    return ok


_UNSET_OK = "ok"
_UNSET_NOT_MANAGED = "not-managed"
_UNSET_SUPERSEDED = "superseded"
_UNSET_FAILED = "failed"


def _unset_system_proxy_impl() -> tuple[bool, str]:
    """关闭系统代理的内部实现，返回 (是否真的改动系统, 原因码)。

    两道守卫：
    1. 本进程没接管过 → 一律不写（别把别人的代理关掉）；
    2. 接管过，但当前值已被改写（别的客户端、用户中途换 PAC、只改了其中一段）
       → 只清自己的状态，也不写（别把别人正在用的值覆盖掉）。
    """
    global _SYSTEM_PROXY_ENABLED_BY_US, _ORIGINAL_PROXY, _MANAGED_PORT
    managed = _SYSTEM_PROXY_ENABLED_BY_US or _ORIGINAL_PROXY is not None
    # 先失效缓存：即使这一步不写系统，也要让下一次读数如实反映现状
    # （tests/test_sysproxy.py 的缓存用例依赖这一点：开关之后必须重读）
    _invalidate_status_cache()
    if not managed:
        return False, _UNSET_NOT_MANAGED
    if not _current_proxy_is_ours():
        _SYSTEM_PROXY_ENABLED_BY_US = False
        _ORIGINAL_PROXY = None
        _MANAGED_PORT = None
        return False, _UNSET_SUPERSEDED
    sys_type = get_os_type()
    ok = False
    if _ORIGINAL_PROXY is not None:
        # 有原值就按原值还原，而不是一律关掉
        ok = _restore_proxy(_ORIGINAL_PROXY)
        _ORIGINAL_PROXY = None
    elif sys_type == "windows":
        ok = _unset_windows_proxy()
    elif sys_type == "darwin":
        ok = _unset_macos_proxy()
    else:
        ok = _unset_linux_proxy()
    # 快照已被消费、端口已不再受管：两处都要清，否则下次会拿着旧端口去比。
    # 即使本次还原失败也清 —— 重试由调用方决定，而守卫会保证重试不会误写别人的值。
    _SYSTEM_PROXY_ENABLED_BY_US = False
    _MANAGED_PORT = None
    return bool(ok), _UNSET_OK if ok else _UNSET_FAILED


def unset_system_proxy() -> bool:
    """跨平台关闭系统代理。返回“是否真的改动了系统设置”。

    未接管过、或当前值已被别人改写时返回 False，且**不做任何写入**。
    需要区分“未改动”与“失败”时用 `unset_system_proxy_result()`。
    """
    return _unset_system_proxy_impl()[0]


def unset_system_proxy_result() -> dict[str, Any]:
    """关闭系统代理，并返回可区分的结果。

    reason: `ok`（已还原/已关闭）、`not-managed`（本进程没接管过）、
            `superseded`（当前值已被别人改写，故未动）、`failed`（写失败）
    """
    changed, reason = _unset_system_proxy_impl()
    return {
        "success": reason != _UNSET_FAILED,
        "changed": changed,
        "reason": reason,
        "noop": reason in (_UNSET_NOT_MANAGED, _UNSET_SUPERSEDED),
    }


def cleanup_on_exit() -> None:
    """进程退出时自动将系统代理还原关闭"""
    if _SYSTEM_PROXY_ENABLED_BY_US:
        unset_system_proxy()


atexit.register(cleanup_on_exit)
