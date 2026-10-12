"""README 事实一致性哨兵。

存在的理由：README 曾把 `wireguard`、`socks`、`http` 列为"只走 JSON 的"支持协议，
而 `parsers._IMPORT_SUPPORTED_TYPES` 根本不收这三类（wireguard 出站自 sing-box
1.13.0 起已移除）。把这条断言放进套件，"README 列了工具不认的类型"当场就红。

若 README 的措辞被改写导致抓不到协议名，本测试会以「没抓到协议名」失败——
这是有意的：宁可响一声，也不要静默地什么都不检查。
"""

import re
from pathlib import Path

from agent_channel.parsers import _IMPORT_SUPPORTED_TYPES

# 两组协议清单所在行的识别标记（中/英各一套）
_URI_FORM_MARKERS = ("有通行 URI 格式的", "With a common URI form")
_JSON_ONLY_MARKERS = ("只走 JSON 的", "JSON only")


def _readme_protocol_names(text: str) -> set[str]:
    """从 README 的"有通行 URI 格式的 / 只走 JSON 的"两行里抠出反引号里的协议名。"""
    names: set[str] = set()
    for line in text.splitlines():
        if any(m in line for m in _URI_FORM_MARKERS + _JSON_ONLY_MARKERS):
            names.update(re.findall(r"`([a-z0-9]+)`", line))
    return names


def test_readme_protocol_lists_only_supported_types():
    """README 两处协议清单里的名字必须都在 _IMPORT_SUPPORTED_TYPES 里。"""
    root = Path(__file__).resolve().parent.parent
    for name in ("README.md", "README.en.md"):
        names = _readme_protocol_names((root / name).read_text(encoding="utf-8"))
        assert names, f"{name} 里没抓到协议名（清单措辞变了？请同步本测试）"
        unsupported = names - set(_IMPORT_SUPPORTED_TYPES)
        assert not unsupported, f"{name} 列了工具不认的类型: {sorted(unsupported)}"


def test_project_version_is_consistent():
    """版本号两处（`pyproject.toml` 与 `agent_channel.__version__`）必须一致。

    回归背景：版本号一直靠人记得两处同改，没有任何机器校验——发版时最容易漏的就是它。
    """
    import agent_channel

    root = Path(__file__).resolve().parent.parent
    pyproject = (root / "pyproject.toml").read_text(encoding="utf-8")
    match = re.search(r'^version\s*=\s*"([^"]+)"', pyproject, re.MULTILINE)
    assert match, "pyproject.toml 里找不到 version 字段"
    declared = match.group(1)
    assert declared == agent_channel.__version__, (
        f"版本号不一致：pyproject.toml={declared} vs "
        f"agent_channel.__version__={agent_channel.__version__}"
    )
