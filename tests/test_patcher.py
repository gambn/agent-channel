import os

from agent_channel.patcher import patch_environment, unpatch_environment

# patch_environment 会注入这些键（非 Windows 六写法，Windows 三写法）。
_KEYS = (
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "ALL_PROXY",
    "http_proxy",
    "https_proxy",
    "all_proxy",
)


def test_environment_patch():
    proxy_url = "http://127.0.0.1:12345"
    # 先记下注入前的原值，再断言“还原成注入前的样子”。
    # 不能断言“环境里没有 HTTP_PROXY”：开发机常挂着代理（本机就设着
    # HTTP_PROXY=127.0.0.1:12334），宿主本来有值时该键**应该**被还原回来，
    # 而不是被删掉 —— 那不是还原，是顺手把人家的代理抹了。
    before = {key: os.environ.get(key) for key in _KEYS}

    patch_environment(proxy_url)
    assert os.environ.get("HTTP_PROXY") == proxy_url
    assert os.environ.get("HTTPS_PROXY") == proxy_url
    assert os.environ.get("ALL_PROXY") == proxy_url

    unpatch_environment()
    for key, value in before.items():
        assert os.environ.get(key) == value, f"{key} 未还原成注入前的值"
