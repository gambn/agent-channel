import platform
from unittest.mock import Mock, patch

from agent_channel.sysproxy import (
    get_system_proxy_status,
    set_system_proxy,
    unset_system_proxy,
)


def test_sysproxy_basic():
    # 只读状态查询应返回 dict，无副作用
    status = get_system_proxy_status()
    assert "enabled" in status
    assert "server" in status

    if platform.system().lower() == "windows":
        original = {
            "ProxyEnable": 0,
            "ProxyServer": "127.0.0.1:12334",
            "ProxyOverride": "<local>;我的自定义绕过列表",
        }
        restored: dict = {}
        with (
            patch(
                "agent_channel.sysproxy._set_windows_proxy", return_value=True
            ) as mock_set,
            patch(
                "agent_channel.sysproxy._snapshot_proxy", return_value=dict(original)
            ),
            patch(
                "agent_channel.sysproxy._write_windows_raw",
                side_effect=lambda values: bool(restored.update(values)) or True,
            ),
            patch("agent_channel.sysproxy._ORIGINAL_PROXY", None),
            # 本用例验的是“还原原值”，归属校验不在它的问题域内，直接放行。
            patch("agent_channel.sysproxy._current_proxy_is_ours", lambda: True),
        ):
            assert set_system_proxy(port=54321)
            mock_set.assert_called_once_with(54321, "127.0.0.1")

            assert unset_system_proxy()

        # 关闭时必须把用户原有的值还原回去，而不是只把开关关掉
        assert restored == original, restored


def test_system_proxy_status_is_cached_and_self_invalidated():
    """3 秒轮询不得每次去拉子进程；自己开关过代理之后必须立刻重读。

    面板每 3 秒打一次 /api/status，而读系统代理在 Linux 要起三个 gsettings、macOS 要
    起 networksetup —— 不缓存就是白烧。反过来，缓存若不在自己的写操作后失效，用户点完
    开关会看到旧状态。
    """
    from agent_channel import sysproxy

    calls = {"n": 0}

    def counter():
        calls["n"] += 1
        return {"enabled": False, "server": ""}

    sysproxy._invalidate_status_cache()
    try:
        with (
            patch("agent_channel.sysproxy.get_os_type", lambda: "linux"),
            patch("agent_channel.sysproxy._get_linux_status", counter),
            patch("agent_channel.sysproxy._ORIGINAL_PROXY", None),
        ):
            sysproxy.get_system_proxy_status()
            sysproxy.get_system_proxy_status()
            assert calls["n"] == 1, "TTL 内的重复读取应命中缓存"

            # 自己的写操作必须让缓存失效（_unset_linux_proxy 在测试里是 no-op，
            # 这里只验“缓存被清掉、下次会重新问系统”）
            sysproxy.unset_system_proxy()
            sysproxy.get_system_proxy_status()
            assert calls["n"] == 2, "开关系统代理后应重新读取，不得发旧值"
    finally:
        sysproxy._invalidate_status_cache()


def test_heal_stale_system_proxy_matches_port_exactly():
    """自愈的端口判据必须**精确**匹配，并认得 Windows 的多协议串。

    旧实现是 `str(listen_port) in server` 子串判据 —— 查 888 会命中 8888，把用户自己
    指向 8888 的代理误关。这里覆盖：子串不得命中、精确命中、多协议串形态、非回环不认。
    """
    from agent_channel import sysproxy

    cases = (
        ("http=127.0.0.1:8888;https=127.0.0.1:8888", 888, False),
        ("http=127.0.0.1:8888;https=127.0.0.1:8888", 8888, True),
        ("127.0.0.1:20849", 20849, True),
        ("localhost:20849", 20849, True),
        ("1.2.3.4:888", 888, False),
        ("", 20849, False),
    )
    for server, port, expected in cases:
        with (
            patch("agent_channel.sysproxy.get_os_type", lambda: "windows"),
            patch(
                "agent_channel.sysproxy._get_windows_status",
                lambda s=server: {"enabled": True, "server": s},
            ),
        ):
            got = sysproxy.heal_stale_system_proxy(port)
        assert bool(got) == expected, f"server={server!r} port={port} → {got}"


# ==========================================
# 归属校验（当前值还是不是我们设的那一份）
# ==========================================


def _reset_managed_state() -> None:
    """测试之间隔离：这三个模块级变量会跨用例泄漏。"""
    from agent_channel import sysproxy

    sysproxy._SYSTEM_PROXY_ENABLED_BY_US = False
    sysproxy._ORIGINAL_PROXY = None
    sysproxy._MANAGED_PORT = None
    sysproxy._invalidate_status_cache()


def _arm_managed(port: int = 20849) -> None:
    """模拟“我们刚接管过系统代理”：标志置位、快照记着用户原值 7897、端口记着。"""
    from agent_channel import sysproxy

    sysproxy._SYSTEM_PROXY_ENABLED_BY_US = True
    sysproxy._ORIGINAL_PROXY = {"enabled": True, "server": "127.0.0.1:7897"}
    sysproxy._MANAGED_PORT = port


def test_unset_skips_when_current_proxy_rewritten_by_others():
    """接管后别的客户端把系统代理改成了自己的：一个 set 都不能发。

    `_SYSTEM_PROXY_ENABLED_BY_US` 只说明“我们设过”，不等于“现在仍是我们的”；
    不判这一道就会把别人正在用的代理按旧快照覆盖掉。
    """
    from agent_channel import sysproxy

    restore = Mock(return_value=True)
    seg = {
        "kind": "linux",
        "mode": "manual",
        "http": ("127.0.0.1", "1080"),
        "https": ("127.0.0.1", "1080"),
    }
    try:
        with (
            patch("agent_channel.sysproxy.get_os_type", lambda: "linux"),
            patch("agent_channel.sysproxy._read_current_segments", lambda: seg),
            patch("agent_channel.sysproxy._restore_proxy", restore),
        ):
            _arm_managed()
            assert sysproxy.unset_system_proxy() is False
        restore.assert_not_called()
        assert sysproxy._MANAGED_PORT is None
        assert sysproxy._ORIGINAL_PROXY is None
        assert sysproxy._SYSTEM_PROXY_ENABLED_BY_US is False
    finally:
        _reset_managed_state()


def test_unset_reports_noop_when_user_switched_to_pac():
    """用户切到 PAC：`http` 残值仍指向我们，也不能当成“还是我们的”。"""
    from agent_channel import sysproxy

    restore = Mock(return_value=True)
    seg = {
        "kind": "linux",
        "mode": "auto",
        "http": ("127.0.0.1", "20849"),
        "https": ("127.0.0.1", "20849"),
    }
    try:
        with (
            patch("agent_channel.sysproxy.get_os_type", lambda: "linux"),
            patch("agent_channel.sysproxy._read_current_segments", lambda: seg),
            patch("agent_channel.sysproxy._restore_proxy", restore),
        ):
            _arm_managed()
            res = sysproxy.unset_system_proxy_result()
        restore.assert_not_called()
        assert res == {
            "success": True,
            "changed": False,
            "reason": "superseded",
            "noop": True,
        }, res
    finally:
        _reset_managed_state()


def test_unset_skips_when_only_https_segment_changed():
    """用户只改了 https 段：只读 http 的判据会误判为“还是我们的”，从而抹掉 9999。"""
    from agent_channel import sysproxy

    restore = Mock(return_value=True)
    seg = {
        "kind": "linux",
        "mode": "manual",
        "http": ("127.0.0.1", "20849"),
        "https": ("127.0.0.1", "9999"),
    }
    try:
        with (
            patch("agent_channel.sysproxy.get_os_type", lambda: "linux"),
            patch("agent_channel.sysproxy._read_current_segments", lambda: seg),
            patch("agent_channel.sysproxy._restore_proxy", restore),
        ):
            _arm_managed()
            assert sysproxy.unset_system_proxy() is False
        restore.assert_not_called()
    finally:
        _reset_managed_state()


def test_unset_skips_when_windows_pac_enabled_after_takeover():
    """Windows：接管期间用户启用了 PAC。还原会把快照里的 None 写成 DeleteValue，
    等于删掉用户的 PAC，必须拦住。"""
    from agent_channel import sysproxy

    restore = Mock(return_value=True)
    seg = {
        "kind": "windows",
        "enable": 1,
        "server": "127.0.0.1:20849",
        "auto": "http://example.com/proxy.pac",
    }
    try:
        with (
            patch("agent_channel.sysproxy.get_os_type", lambda: "windows"),
            patch("agent_channel.sysproxy._read_current_segments", lambda: seg),
            patch("agent_channel.sysproxy._restore_proxy", restore),
        ):
            _arm_managed()
            assert sysproxy.unset_system_proxy() is False
        restore.assert_not_called()
    finally:
        _reset_managed_state()


def test_unset_restores_when_all_segments_still_ours():
    """反向对照：逐段都还是我们的，就必须正常还原 —— 防止守卫变成“什么都不做”。"""
    from agent_channel import sysproxy

    restore = Mock(return_value=True)
    seg = {
        "kind": "linux",
        "mode": "manual",
        "http": ("127.0.0.1", "20849"),
        "https": ("127.0.0.1", "20849"),
    }
    try:
        with (
            patch("agent_channel.sysproxy.get_os_type", lambda: "linux"),
            patch("agent_channel.sysproxy._read_current_segments", lambda: seg),
            patch("agent_channel.sysproxy._restore_proxy", restore),
        ):
            _arm_managed()
            res = sysproxy.unset_system_proxy_result()
        restore.assert_called_once_with({"enabled": True, "server": "127.0.0.1:7897"})
        assert res == {
            "success": True,
            "changed": True,
            "reason": "ok",
            "noop": False,
        }, res
    finally:
        _reset_managed_state()


def test_set_failure_restores_snapshot_and_clears_state():
    """开代理失败时平台可能只写了一半：“半截是我们的”这种混合态归属校验判不出来，
    必须当场按快照收尾，不能把旧快照留给下一次调用去猜。"""
    from agent_channel import sysproxy

    original = {"enabled": True, "server": "127.0.0.1:7897"}
    restore = Mock(return_value=True)
    try:
        with (
            patch("agent_channel.sysproxy.get_os_type", lambda: "linux"),
            patch("agent_channel.sysproxy._set_linux_proxy", lambda *a, **k: False),
            patch("agent_channel.sysproxy._snapshot_proxy", lambda: dict(original)),
            patch("agent_channel.sysproxy._restore_proxy", restore),
            patch("agent_channel.sysproxy._ORIGINAL_PROXY", None),
            patch("agent_channel.sysproxy._MANAGED_PORT", None),
        ):
            assert sysproxy.set_system_proxy(port=20849) is False
        restore.assert_called_once_with(original)
        assert sysproxy._ORIGINAL_PROXY is None
        assert sysproxy._MANAGED_PORT is None
        assert sysproxy._SYSTEM_PROXY_ENABLED_BY_US is False
    finally:
        _reset_managed_state()


# ==========================================
# 审计 4.1：解析层本身的单元测试（此前只被 mock 掉）
# ==========================================


def test_read_current_segments_linux_parses_all_segments():
    """Linux 分支必须真的读 mode + http 两段 + https 两段，并把引号/换行剥干净。"""
    from agent_channel import sysproxy

    values = {
        ("org.gnome.system.proxy", "mode"): "'manual'\n",
        ("org.gnome.system.proxy.http", "host"): "'127.0.0.1'\n",
        ("org.gnome.system.proxy.http", "port"): "20849\n",
        ("org.gnome.system.proxy.https", "host"): "'127.0.0.1'\n",
        ("org.gnome.system.proxy.https", "port"): "9999\n",
    }

    def fake_check_output(args, **_kwargs):
        return values[(args[2], args[3])]

    with (
        patch("agent_channel.sysproxy.get_os_type", lambda: "linux"),
        patch("agent_channel.sysproxy.subprocess.check_output", fake_check_output),
    ):
        seg = sysproxy._read_current_segments()
    assert seg == {
        "kind": "linux",
        "mode": "manual",
        "http": ("127.0.0.1", "20849"),
        "https": ("127.0.0.1", "9999"),
    }, seg


def test_current_proxy_is_ours_windows_truth_table():
    """Windows 分支：必须 enabled + 无 PAC + 整串等于 host:port。

    多协议串（`http=…;https=…`）我们从不写，出现即视为已被别人改写 —— 用“任一段命中”
    会把 `http=<我们>;https=<用户改的>` 判成“仍是我们的”，进而覆盖用户那一段。
    """
    from agent_channel import sysproxy

    base = {
        "kind": "windows",
        "enable": 1,
        "server": "127.0.0.1:20849",
        "auto": None,
    }
    cases = (
        (base, True),
        ({**base, "server": "localhost:20849"}, True),
        ({**base, "enable": 0}, False),
        ({**base, "auto": "http://example.com/proxy.pac"}, False),
        ({**base, "server": "127.0.0.1:9999"}, False),
        ({**base, "server": "http=127.0.0.1:20849;https=127.0.0.1:9999"}, False),
        ({**base, "server": "http=127.0.0.1:20849;https=127.0.0.1:20849"}, False),
        ({**base, "server": ""}, False),
    )
    try:
        for seg, expected in cases:
            with (
                patch("agent_channel.sysproxy.get_os_type", lambda: "windows"),
                patch("agent_channel.sysproxy._read_current_segments", lambda s=seg: s),
            ):
                sysproxy._MANAGED_PORT = 20849
                got = sysproxy._current_proxy_is_ours()
            assert got is expected, f"{seg} → {got}（期望 {expected}）"
    finally:
        _reset_managed_state()


def test_current_proxy_is_ours_macos_requires_both_segments_and_no_auto():
    """macOS 分支：web 与 secure 两段都要命中，且自动代理必须是关的。"""
    from agent_channel import sysproxy

    ok = {
        "kind": "macos",
        "web": ("127.0.0.1", "20849"),
        "secure": ("127.0.0.1", "20849"),
        "auto": False,
    }
    cases = (
        (ok, True),
        ({**ok, "secure": ("127.0.0.1", "9999")}, False),
        ({**ok, "web": None}, False),
        ({**ok, "auto": True}, False),
    )
    try:
        for seg, expected in cases:
            with (
                patch("agent_channel.sysproxy.get_os_type", lambda: "darwin"),
                patch("agent_channel.sysproxy._read_current_segments", lambda s=seg: s),
            ):
                sysproxy._MANAGED_PORT = 20849
                got = sysproxy._current_proxy_is_ours()
            assert got is expected, f"{seg} → {got}（期望 {expected}）"
    finally:
        _reset_managed_state()


def test_unset_result_payload_for_not_managed():
    """`not-managed` 的 payload 是对前端的契约：success 为真、noop 为真、changed 为假。"""
    from agent_channel import sysproxy

    try:
        _reset_managed_state()
        with patch("agent_channel.sysproxy.get_os_type", lambda: "linux"):
            res = sysproxy.unset_system_proxy_result()
        assert res == {
            "success": True,
            "changed": False,
            "reason": "not-managed",
            "noop": True,
        }, res
    finally:
        _reset_managed_state()


def test_set_failure_clears_flags_even_after_previous_success():
    """写失败分支要把三个状态变量一起清：只清快照会留下“标志为真、端口还在”的脏语义。"""
    from agent_channel import sysproxy

    restore = Mock(return_value=True)
    try:
        with (
            patch("agent_channel.sysproxy.get_os_type", lambda: "linux"),
            patch("agent_channel.sysproxy._set_linux_proxy", lambda *a, **k: False),
            patch("agent_channel.sysproxy._restore_proxy", restore),
        ):
            _arm_managed()  # 模拟此前成功接管过
            assert sysproxy.set_system_proxy(port=20849) is False
        restore.assert_called_once()
        assert sysproxy._SYSTEM_PROXY_ENABLED_BY_US is False
        assert sysproxy._MANAGED_PORT is None
        assert sysproxy._ORIGINAL_PROXY is None
    finally:
        _reset_managed_state()


# ==========================================
# 审计复第二轮：让 Win/macOS 的读取分支也进套件
# ==========================================


def test_read_current_segments_windows_via_injected_winreg():
    """Windows 读取分支（函数内 `import winreg`）——注入假模块在 Linux 上覆盖。

    此前只有 mock 掉 `_read_current_segments` 整体的用例，这两个分支后续改动无人守。
    """
    import sys
    import types

    from agent_channel import sysproxy

    class _Key:
        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            return False

    def _fake_registry(values):
        mod = types.ModuleType("winreg")
        # 用 setattr 而不是直接赋值：pyright 的 reportAttributeAccessIssue 不放过
        # 给 ModuleType 动态挂属性（项目门禁要求 0 errors）。
        setattr(mod, "HKEY_CURRENT_USER", object())
        setattr(mod, "KEY_WRITE", 2)
        setattr(mod, "REG_DWORD", 4)
        setattr(mod, "REG_SZ", 1)
        setattr(mod, "OpenKey", lambda *a, **k: _Key())

        def query_value_ex(_key, name):
            if name in values:
                return (values[name], 1)
            raise FileNotFoundError(name)

        setattr(mod, "QueryValueEx", query_value_ex)
        return mod

    with (
        patch("agent_channel.sysproxy.get_os_type", lambda: "windows"),
        patch.dict(
            sys.modules,
            {"winreg": _fake_registry({"ProxyEnable": 1, "ProxyServer": "127.0.0.1:20849"})},
        ),
    ):
        without_pac = sysproxy._read_current_segments()
    assert without_pac == {
        "kind": "windows",
        "enable": 1,
        "server": "127.0.0.1:20849",
        "auto": None,
    }, without_pac

    with (
        patch("agent_channel.sysproxy.get_os_type", lambda: "windows"),
        patch.dict(
            sys.modules,
            {
                "winreg": _fake_registry(
                    {
                        "ProxyEnable": 1,
                        "ProxyServer": "127.0.0.1:20849",
                        "AutoConfigURL": "http://example.com/proxy.pac",
                    }
                )
            },
        ),
    ):
        with_pac = sysproxy._read_current_segments()
    assert with_pac is not None
    assert with_pac["auto"] == "http://example.com/proxy.pac", with_pac


def test_read_current_segments_macos_parses_web_secure_and_auto():
    """macOS 解析分支：PATH 里放假 `networksetup`，验 web/secure/auto 三段都读对。

    `_get_macos_status()` 只读 webproxy，所以“用户只改了 secure 段”只有这个分支看得见。
    """
    if platform.system().lower() == "windows":
        # Windows 上没有 networksetup：`_macos_proxy_segment` 会返回 None、
        # `_macos_auto_enabled` 会返回 True，断言必然不符。按既有风格跳过。
        print("    [skip] Windows 上没有 networksetup，跳过 macOS 解析用例")
        return

    import os
    import stat
    import tempfile

    from agent_channel import sysproxy

    script = (
        "#!/bin/sh\n"
        'case "$1" in\n'
        '  -listnetworkserviceorder) echo "(1) Wi-Fi";;\n'
        '  -getwebproxy)       echo "Enabled: Yes"; echo "Server: 127.0.0.1";'
        ' echo "Port: 20849";;\n'
        '  -getsecurewebproxy) echo "Enabled: Yes"; echo "Server: 127.0.0.1";'
        ' echo "Port: 9999";;\n'
        '  -getautoproxyurl)   echo "URL: (null)"; echo "Enabled: No";;\n'
        "esac\n"
    )
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "networksetup")
        with open(path, "w") as fh:
            fh.write(script)
        os.chmod(path, os.stat(path).st_mode | stat.S_IXUSR)
        with (
            patch("agent_channel.sysproxy.get_os_type", lambda: "darwin"),
            patch.dict(os.environ, {"PATH": tmp + os.pathsep + os.environ.get("PATH", "")}),
        ):
            seg = sysproxy._read_current_segments()
    assert seg == {
        "kind": "macos",
        "web": ("127.0.0.1", "20849"),
        "secure": ("127.0.0.1", "9999"),
        "auto": False,
    }, seg
