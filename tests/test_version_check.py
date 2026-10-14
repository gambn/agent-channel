"""应用自身版本提示的守卫测试：只提示、不说谎、不打扰。

背景：版本提示是唯一能把“修复已经在源码里”自动变成“用户装到手里”的通道，但它
自己一旦出错就变成骚扰或假消息，所以三条边界都要有机器判据——只报**严格更新**的
版本、**查不到就沉默**（绝不等于“已是最新”）、跳过只压住**那一个**版本。
"""

import time

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
    }


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
