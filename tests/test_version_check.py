"""应用自身版本提示的守卫测试：只提示、不说谎、不打扰。

背景：版本提示是唯一能把“修复已经在源码里”自动变成“用户装到手里”的通道，但它
自己一旦出错就变成骚扰或假消息，所以三条边界都要有机器判据——只报**严格更新**的
版本、**查不到就沉默**（绝不等于“已是最新”）、跳过只压住**那一个**版本。
"""

import importlib
import time
from unittest.mock import patch

from agent_channel import updates


def _fake(version, url="https://example.invalid/release"):
    """替身：不联网，直接给一个 Release 响应。"""
    return lambda **kwargs: {"version": version, "url": url}


def _prime_cache(latest, at, ttl=None):
    """把模块缓存设成“刚刚查过”的状态，让用例不依赖真实时钟与网络。"""
    updates._cache.update(
        {
            "at": at,
            "latest": latest,
            "ttl": updates.CACHE_TTL_SECONDS if ttl is None else ttl,
        }
    )


def test_parse_version_accepts_prefix_and_suffix():
    assert updates.parse_version("v0.1.3") == (0, 1, 3)
    assert updates.parse_version("0.1.3-rc1") == (0, 1, 3)
    assert updates.parse_version("") is None
    assert updates.parse_version(None) is None
    assert updates.parse_version("latest") is None


def test_is_newer_compares_numeric_parts_not_strings():
    """字符串比会把 0.1.10 判成小于 0.1.9 —— 这条就是这个坑的判据。"""
    assert updates.is_newer("v0.1.10", "0.1.9")
    assert not updates.is_newer("0.1.9", "0.1.10")
    assert updates.is_newer("0.2", "0.1.9")
    assert not updates.is_newer("0.1.3", "0.1.3")
    # 段数不等时短的补 0：0.2 与 0.2.0 是同一版
    assert not updates.is_newer("0.2.0", "0.2")
    # 解析不出来一律不提示：宁可沉默，也不报一个假的新版本
    assert not updates.is_newer("latest", "0.1.3")
    assert not updates.is_newer("v0.1.4", "")


def test_check_for_update_only_reports_a_strictly_newer_version():
    assert updates.check_for_update("0.1.3", force=True, fetch=_fake("v0.1.3")) is None
    assert updates.check_for_update("0.1.3", force=True, fetch=_fake("v0.1.2")) is None
    got = updates.check_for_update("0.1.3", force=True, fetch=_fake("v0.1.4"))
    assert got == {
        "version": "v0.1.4",
        "url": "https://example.invalid/release",
        "current": "0.1.3",
        # 替身没给资产清单：认不出来就退到发布页，而不是猜一个包
        "asset": None,
    }


# 产物命名照仓库实际（见 BUILD-INFO.txt）：注意 deb 的名字里没有 `linux-x64`，
# Windows 安装包也没有 `windows-x64`，所以匹配规则必须分开写。
_RELEASE_ASSETS = [
    {"name": "agent-channel-9.9.9-linux-x64.tar.gz", "url": "u-linux-tar"},
    {"name": "agent-channel_9.9.9_amd64.deb", "url": "u-linux-deb"},
    {"name": "agent-channel-9.9.9-darwin-x64.dmg", "url": "u-mac-x64-dmg"},
    {"name": "agent-channel-9.9.9-darwin-x64.tar.gz", "url": "u-mac-x64-tar"},
    {"name": "agent-channel-9.9.9-darwin-arm64.dmg", "url": "u-mac-arm-dmg"},
    {"name": "agent-channel-9.9.9-darwin-arm64.tar.gz", "url": "u-mac-arm-tar"},
    {"name": "agent-channel-9.9.9-setup.exe", "url": "u-win-exe"},
    {"name": "agent-channel-9.9.9-windows-x64.zip", "url": "u-win-zip"},
    {"name": "agent_channel-9.9.9-py3-none-any.whl", "url": "u-whl"},
]


def _picked(assets, os_name, arch, system_install=False) -> str:
    """挑出来的资产直链；挑不出来直接判失败（用例要的是"必须挑中"）。"""
    got = updates.pick_asset(
        assets, os_name, arch, system_install=system_install
    )
    assert got is not None, f"{os_name}/{arch} 没挑出对应资产"
    return str(got["url"])


def test_pick_asset_picks_the_installer_for_each_platform():
    assert _picked(_RELEASE_ASSETS, "darwin", "amd64") == "u-mac-x64-dmg"
    assert _picked(_RELEASE_ASSETS, "darwin", "arm64") == "u-mac-arm-dmg"
    assert _picked(_RELEASE_ASSETS, "windows", "amd64") == "u-win-exe"
    # Linux 按当前这份的装法给：用户目录（install.sh 到 ~/.local/opt）给 tar.gz，
    # 系统目录（deb）给 deb —— 免得让用户去拿一个与自己现在这份不同装法的包。
    assert _picked(_RELEASE_ASSETS, "linux", "amd64") == "u-linux-tar"
    assert (
        _picked(_RELEASE_ASSETS, "linux", "amd64", system_install=True) == "u-linux-deb"
    )


def test_pick_asset_is_silent_when_nothing_matches():
    """认不出平台或没有对应资产时返回 None：调用方退到发布页，绝不猜一个包给用户。"""
    assert updates.pick_asset(_RELEASE_ASSETS, "", "amd64") is None
    assert updates.pick_asset(_RELEASE_ASSETS, "freebsd", "amd64") is None
    assert updates.pick_asset([], "linux", "amd64") is None
    assert updates.pick_asset(None, "linux", "amd64") is None
    # 只有 wheel 时也不该把它当成安装包推给用户
    only_wheel = [{"name": "agent_channel-9.9.9-py3-none-any.whl", "url": "u"}]
    assert updates.pick_asset(only_wheel, "linux", "amd64") is None


def test_check_for_update_carries_the_platform_asset_when_present():
    """平台上下文钉住再测：测试环境把 HOME 指到了临时目录，不钉住就测不出真实取舍。"""
    with patch.object(updates, "_platform_context", lambda: ("linux", "amd64", False)):
        got = updates.check_for_update(
            "0.1.3",
            force=True,
            fetch=lambda **kw: {
                "version": "v9.9.9",
                "url": "https://example.invalid/release",
                "assets": _RELEASE_ASSETS,
            },
        )
    assert got and got["asset"], "有对应资产时必须把直链带上"
    assert got["asset"]["url"] == "u-linux-tar"
    assert got["asset"]["name"].endswith(".tar.gz")


def test_check_for_update_is_silent_when_the_check_fails():
    """查不到就是查不到：返回 None，绝不等于“已是最新”。"""

    def boom(**kwargs):
        raise RuntimeError("网络不通")

    assert updates.check_for_update("0.1.3", force=True, fetch=boom) is None
    assert updates.check_for_update("0.1.3", force=True, fetch=lambda **kw: None) is None
    # 限流时 GitHub 回的是合法 JSON 但没有版本号：同样什么都不说
    assert (
        updates.check_for_update("0.1.3", force=True, fetch=lambda **kw: {"url": "u"})
        is None
    )


def test_check_for_update_respects_dismissed_version_only_for_that_version():
    dismissed = "v0.1.4"
    assert (
        updates.check_for_update(
            "0.1.3", dismissed=dismissed, force=True, fetch=_fake(dismissed)
        )
        is None
    )
    # 跳过的是 0.1.4：将来 0.1.5 仍要提示，不然“跳过”就变成了永久关闭提示
    got = updates.check_for_update(
        "0.1.3", dismissed=dismissed, force=True, fetch=_fake("v0.1.5")
    )
    assert got is not None and got["version"] == "v0.1.5"


def test_first_check_in_a_fresh_process_actually_fetches_even_on_small_uptime():
    """回归：**模块自己的初值**不能长得像"刚查过"，而不是"某个哨兵值长什么样"。

    `time.monotonic()` 是开机以来的秒数。初值若写成 0.0，开机不满 TTL（24 小时）的机器
    上"距上次检查"会小于 TTL，首查被判为 fresh 而跳过；跳过又不更新缓存，于是只要开机
    不满一天就永远不查、提示永不出现（实测踩过一次：演示实例上横条不来）。

    判据刻意**不注入哨兵**：`importlib.reload` 回到模块自己的初值再问一次。这样它钉的是
    行为（首查必须真去查），换一种同样正确的实现（比如用 -1.0 表示"从未查过"）也不会被
    误杀 —— 审计指出过：直接塞 `_cache["at"] = None` 的写法在旧代码上之所以"红"，是因为
    旧代码不认这个哨兵而抛 TypeError，并没有抓住初值这件事。
    """
    reloaded = importlib.reload(updates)
    calls: list[int] = []

    def counting(**kwargs):
        calls.append(1)
        return {"version": "v9.9.9", "url": ""}

    got = reloaded.check_for_update("0.1.3", now=600.0, fetch=counting)  # 开机 10 分钟
    assert len(calls) == 1, "首次检查必须真的去查，不能被「刚查过」的初值挡住"
    assert got is not None and got["version"] == "v9.9.9"


def test_check_for_update_caches_and_retries_sooner_after_a_failure():
    calls: list[int] = []

    def counting(**kwargs):
        calls.append(1)
        return {"version": "v0.1.9", "url": ""}

    _prime_cache(None, 0.0)
    updates.check_for_update("0.1.3", now=1000000.0, fetch=counting)
    updates.check_for_update("0.1.3", now=1000000.0 + 60, fetch=counting)
    assert len(calls) == 1, "查到结果后 24 小时内不该重复查"
    updates.check_for_update(
        "0.1.3", now=1000000.0 + updates.CACHE_TTL_SECONDS + 1, fetch=counting
    )
    assert len(calls) == 2, "过了静默期要能再查"

    fails: list[int] = []

    def failing(**kwargs):
        fails.append(1)
        return None

    _prime_cache(None, 0.0)
    updates.check_for_update("0.1.3", now=2000000.0, fetch=failing)
    updates.check_for_update("0.1.3", now=2000000.0 + 60, fetch=failing)
    assert len(fails) == 1, "失败也要有静默期，否则每次开面板都打一次网络"
    updates.check_for_update(
        "0.1.3",
        now=2000000.0 + updates.FAILURE_RETRY_SECONDS + 1,
        fetch=failing,
    )
    assert len(fails) == 2, "过了失败静默期要能再试（通道起来后仍查得到）"


def test_check_for_update_cached_failure_is_not_reported_as_up_to_date():
    """缓存下来的失败同样什么都不说 —— 面板不该出现在“已是最新”的假结论。"""
    _prime_cache(None, time.monotonic(), ttl=updates.FAILURE_RETRY_SECONDS)

    def must_not_call(**kwargs):
        raise AssertionError("缓存仍在静默期内，不该再打一次网络")

    assert updates.check_for_update("0.1.3", fetch=must_not_call) is None
