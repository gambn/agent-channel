"""打包脚本的内核清单校验（B 强制）。"""

import hashlib
import importlib.util
import tempfile
from pathlib import Path
from unittest.mock import patch


def _load_build_module():
    spec = importlib.util.spec_from_file_location(
        "ac_build", Path("packaging/build.py").resolve()
    )
    assert spec is not None, "无法为 packaging/build.py 构造模块 spec"
    assert spec.loader is not None, "该 spec 没有 loader"
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_verify_vendor_core_accepts_matching_and_rejects_mismatch():
    """B 强制：暂存内核与清单逐字节一致才放行；哈希不符或缺文件都必须中止打包。"""
    build = _load_build_module()
    with tempfile.TemporaryDirectory() as tmp:
        vendor = Path(tmp)
        (vendor / "sing-box.exe").write_bytes(b"KERNEL")
        ok = {
            "version": "test",
            "files": {"sing-box.exe": hashlib.sha256(b"KERNEL").hexdigest()},
        }
        with patch.object(build, "VENDOR_DIR", vendor):
            build.verify_vendor_core(ok)  # 一致 → 放行
            for bad in (
                {"version": "test", "files": {"sing-box.exe": "0" * 64}},
                {"version": "test", "files": {"nope.dll": "0" * 64}},
            ):
                try:
                    build.verify_vendor_core(bad)
                except SystemExit:
                    continue
                raise AssertionError(f"应中止打包: {bad}")


def test_core_manifest_covers_all_shipped_platforms_at_one_version():
    """清单必须覆盖全部可随包发布的平台键，且**所有条目同版**——"包内内核对齐发布时
    最新版"在仓库侧的两道保证：

    - 覆盖：`platform_arch()` 能命名的平台若缺条目，在那台机器上构建会静默打出
      **不含内核**的包（`stage_core` 只打印一句提示就继续），与"包内自带最新版"相反；
    - 同版：升级时只改一部分会在这里红（此前清单只有 windows-amd64 一条）。

    版本再与两份 README 对上，防"文档写 1.14.3、清单还留 1.14.2"。
    注：清单键 = 平台命名能力 **与官方资产** 的交集；`platform_arch()` 若新增架构
    （如 loong64），本用例的 expected 必须同步补键。
    """
    import re

    build = _load_build_module()
    manifest = build.CORE_MANIFEST

    nameable = {
        f"{os_}-{arch}"
        for os_ in ("windows", "darwin", "linux")
        for arch in ("amd64", "386", "arm64", "armv7", "armv6", "riscv64")
    }
    shipped = {
        "windows-amd64", "windows-386", "windows-arm64",
        "darwin-amd64", "darwin-arm64",
        "linux-386", "linux-amd64", "linux-arm64", "linux-armv6", "linux-armv7",
        "linux-riscv64",
    }
    expected = nameable & shipped
    missing = expected - set(manifest)
    assert not missing, f"清单缺平台条目（该平台会打出不含内核的包）: {sorted(missing)}"
    extra = set(manifest) - expected
    assert not extra, f"清单里有填不了的键（官方无对应资产）: {sorted(extra)}"

    versions = {e["version"] for e in manifest.values()}
    assert len(versions) == 1, f"条目版本不一致（升级只改了一部分？）: {sorted(versions)}"
    version = versions.pop()

    for key, entry in manifest.items():
        url = entry["url"]
        assert f"/v{version}/" in url and f"-{version}-" in url, f"{key}: url 与版本不符: {url}"
        assert re.fullmatch(r"[0-9a-f]{64}", entry["archive_sha256"]), f"{key}: 归档哈希非法"
        assert entry["files"], f"{key}: files 为空"
        for name, digest in entry["files"].items():
            assert re.fullmatch(r"[0-9a-f]{64}", digest), f"{key}/{name}: 哈希非法: {digest}"

    for readme in ("README.md", "README.en.md"):
        text = Path(readme).read_text(encoding="utf-8")
        assert version in text, f"{readme} 未提到内核版本 {version}（文档与清单不同步）"


def test_stage_core_uses_official_when_local_version_differs():
    """本地内核版本 ≠ 清单版本时，不许把本地旧版抄进包。

    抄进去的话 `verify_vendor_core` 会以"清单哈希不符"中止，报错还会让人去改清单——
    真正该改的是暂存来源：版本不符就按官方 URL 下载，版本一致才直接用本地（免下载）。
    """
    build = _load_build_module()
    entry_version = build.CORE_MANIFEST[build._platform_key()]["version"]
    core_name = build._core_name()

    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        vendor = base / "vendor"
        local_dir = base / "local"
        local_dir.mkdir()
        (local_dir / core_name).write_bytes(b"KERNEL")
        fetched: list[str] = []

        def run(local_version: str) -> list[str]:
            fetched.clear()
            if vendor.exists():
                import shutil

                shutil.rmtree(vendor)
            with (
                patch.object(build, "VENDOR_DIR", vendor),
                patch(
                    "agent_channel.binary.get_local_binary_path",
                    lambda: local_dir / core_name,
                ),
                patch(
                    "agent_channel.binary.get_local_core_version",
                    lambda: local_version,
                ),
                patch.object(
                    build, "fetch_official_core", lambda entry: fetched.append(entry["version"])
                ),
                patch.object(build, "verify_vendor_core", lambda entry: None),
            ):
                build.stage_core()
            return list(fetched)

        # 版本不一致 → 走官方下载，本地旧版一步都不许进 vendor
        got = run("v0.0.0")
        assert got == [entry_version], f"应按官方 URL 下载 v{entry_version}，实际: {got}"
        assert not (vendor / core_name).exists(), "本地旧版内核不该被抄进暂存目录"

        # 版本一致 → 直接用本地（免下载）
        got = run(f"v{entry_version}")
        assert got == [], f"版本一致时不该重复下载: {got}"
        assert (vendor / core_name).read_bytes() == b"KERNEL", "同版本地内核应被暂存"
