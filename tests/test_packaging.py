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


def _synthetic_archive(
    tmp_dir: Path, members: dict[str, bytes], name: str = "sing-box-9.9.9-test-amd64.tar.gz"
) -> tuple[Path, dict]:
    """造一份"官方归档"与配套的清单条目（哈希由内容算出来，别无第二份口径）。"""
    import io
    import tarfile
    import zipfile

    archive = tmp_dir / name
    if name.endswith(".zip"):
        with zipfile.ZipFile(archive, "w") as handle:
            for member, body in members.items():
                handle.writestr(f"sing-box-9.9.9-test/{member}", body)
    else:
        with tarfile.open(archive, "w:gz") as handle:
            for member, body in members.items():
                info = tarfile.TarInfo(f"sing-box-9.9.9-test/{member}")
                info.size = len(body)
                info.mode = 0o755
                handle.addfile(info, io.BytesIO(body))
    blob = archive.read_bytes()
    entry = {
        "version": "9.9.9",
        "url": f"https://example.invalid/{name}",
        "archive_sha256": hashlib.sha256(blob).hexdigest(),
        "files": {
            member: hashlib.sha256(body).hexdigest() for member, body in members.items()
        },
    }
    return archive, entry


def test_verify_vendor_core_accepts_matching_and_rejects_mismatch():
    """B 强制：暂存内核与清单逐字节一致才放行；归档不符、内容不符、缺件、多件都要中止。

    POSIX 侧的随包形态是重压后的 `.tar.xz`（官方归档先验真再重压），Windows 侧是官方
    `.zip` 原样，两条路共用"逐文件对清单"这道校验，归档形态再加一道整包 sha256。
    """
    build = _load_build_module()
    members = {"sing-box": b"KERNEL", "libcronet.so": b"SO"}

    # ── POSIX 形态：官方 tar.gz → 重压 → 校验通过 ──
    with tempfile.TemporaryDirectory() as tmp:
        vendor = Path(tmp)
        official, entry = _synthetic_archive(vendor, members)
        with patch.object(build, "VENDOR_DIR", vendor):
            build._prepare_bundle(entry)
            assert sorted(p.name for p in vendor.iterdir()) == [
                "sing-box-9.9.9-test-amd64.tar.xz"
            ], "重压后官方归档不该留在暂存里"
            build.verify_vendor_core(entry)  # 一致 → 放行

    # ── zip 源（Windows 官方资产）：先验真、再重压，校验同样按逐文件比对 ──
    with tempfile.TemporaryDirectory() as tmp:
        vendor = Path(tmp)
        official, entry = _synthetic_archive(
            vendor, members, name="sing-box-9.9.9-test-windows-amd64.zip"
        )
        with patch.object(build, "VENDOR_DIR", vendor):
            build._prepare_bundle(entry)
            assert sorted(p.name for p in vendor.iterdir()) == [
                "sing-box-9.9.9-test-windows-amd64.tar.xz"
            ]
            build.verify_vendor_core(entry)  # 重压后逐文件一致 → 放行

    # ── 各种不通过：都必须中止打包 ──
    cases: dict[str, tuple[dict, list[tuple[str, bytes]]]] = {
        "归档里某个文件被换过": (
            entry,
            [("sing-box", b"TAMPERED"), ("libcronet.so", b"SO")],
        ),
        "归档里缺文件": (entry, [("sing-box", b"KERNEL")]),
        "归档里多出清单没有的文件": (
            entry,
            [("sing-box", b"KERNEL"), ("libcronet.so", b"SO"), ("evil", b"X")],
        ),
    }
    for label, (bad_entry, bad_members) in cases.items():
        with tempfile.TemporaryDirectory() as tmp:
            vendor = Path(tmp)
            build._write_xz_bundle(
                bad_members, vendor / build._bundle_name(entry)
            )
            with patch.object(build, "VENDOR_DIR", vendor):
                try:
                    build.verify_vendor_core(bad_entry)
                except SystemExit:
                    continue
            raise AssertionError(f"应中止打包: {label}")

    # 暂存里没有该有的东西
    with tempfile.TemporaryDirectory() as tmp:
        with patch.object(build, "VENDOR_DIR", Path(tmp)):
            try:
                build.verify_vendor_core(entry)
            except SystemExit as exc:
                assert "应恰好包含" in str(exc), exc
            else:
                raise AssertionError("暂存里没有随包内核时必须中止打包")


def test_bundle_name_is_xz_for_every_platform():
    """随包形态三平台一致：官方归档（.tar.gz 或 .zip）都换成 `.tar.xz`。

    为什么 Windows 也重压：实测官方 zip 是 deflate 压的（31.3 MB），外层 Inno 面对一个
    已压缩的 blob 只能原样塞进去，setup.exe 因此从 31.3 MB 涨到 41.9 MB；同一批文件用
    xz 只要 21.5 MB。
    """
    build = _load_build_module()
    assert (
        build._bundle_name({"url": "https://x/sing-box-1.14.3-linux-amd64.tar.gz"})
        == "sing-box-1.14.3-linux-amd64.tar.xz"
    )
    assert (
        build._bundle_name({"url": "https://x/sing-box-1.14.3-darwin-arm64.tar.gz"})
        == "sing-box-1.14.3-darwin-arm64.tar.xz"
    )
    assert (
        build._bundle_name({"url": "https://x/sing-box-1.14.3-windows-amd64.zip"})
        == "sing-box-1.14.3-windows-amd64.tar.xz"
    )


def test_xz_bundle_is_deterministic_and_free_of_build_traces():
    """同一份内容重压两次必须字节相同，且元数据里不带构建机痕迹。

    这是"可复算比对"的前提：不规范化（mtime/uid/uname）的话，换台机器压出来就不一样，
    包里还会留下构建机的用户名与时间。
    """
    import io
    import lzma
    import tarfile

    build = _load_build_module()
    members = [("sing-box", b"KERNEL"), ("LICENSE", b"MIT")]
    with tempfile.TemporaryDirectory() as tmp:
        first = Path(tmp) / "a.tar.xz"
        second = Path(tmp) / "b.tar.xz"
        build._write_xz_bundle(members, first)
        build._write_xz_bundle(list(reversed(members)), second)
        assert first.read_bytes() == second.read_bytes(), "同一份内容重压必须字节一致"

        raw = lzma.decompress(first.read_bytes())
        with tarfile.open(fileobj=io.BytesIO(raw)) as bundle:
            for member in bundle.getmembers():
                assert member.mtime == 0, member.name
                assert (member.uid, member.gid) == (0, 0), member.name
                assert member.uname == "" and member.gname == "", member.name
                assert member.mode == 0o644, member.name


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


def _stage_case(build, vendor: Path, entry: dict, local_kernel: Path | None = None):
    """跑一次 stage_core，返回被"下载"的次数记录（fetch 打成写归档的桩）。"""
    fetched: list[str] = []

    def fake_fetch(target_entry: dict) -> None:
        fetched.append(target_entry["version"])
        vendor.mkdir(parents=True, exist_ok=True)
        name = target_entry["url"].rsplit("/", 1)[-1]
        blob = (_OFFICIAL_BLOBS.setdefault(name, b""))
        (vendor / name).write_bytes(blob)

    with (
        patch.object(build, "VENDOR_DIR", vendor),
        patch.object(build, "CORE_MANIFEST", {build._platform_key(): entry}),
        patch.object(build, "fetch_official_core", fake_fetch),
    ):
        build.stage_core()
    return fetched


_OFFICIAL_BLOBS: dict[str, bytes] = {}


def test_stage_core_reuses_only_bundle_matching_the_manifest():
    """暂存复用与清理一律按清单哈希判定（重压后是 `.tar.xz`）。

    回归背景：同一台 Mac 上先出 x64 再出 arm64，两轮文件名与内容都不同；旧逻辑
    "见文件存在就复用"会把上一个架构的内核打进这一轮的包，校验时报的却是清单不符。
    """
    build = _load_build_module()
    bundle_name = "sing-box-9.9.9-test-amd64.tar.xz"
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        seed_dir = base / "seed"
        seed_dir.mkdir()
        official, entry = _synthetic_archive(seed_dir, {"sing-box": b"ARM64"})
        _OFFICIAL_BLOBS[official.name] = official.read_bytes()

        # 1) 暂存为空 → 下载官方归档并重压
        vendor = base / "vendor-empty"
        assert _stage_case(build, vendor, entry) == ["9.9.9"]
        assert sorted(p.name for p in vendor.iterdir()) == [bundle_name]
        assert build._member_hashes(vendor / bundle_name) == entry["files"]

        # 2) 暂存里正是本平台那一份 → 复用，不再下载
        assert _stage_case(build, vendor, entry) == []

        # 3) 同名但内容不同（另一个架构）→ 重下重压
        (vendor / bundle_name).write_bytes(b"X86_64-BUNDLE")
        assert _stage_case(build, vendor, entry) == ["9.9.9"]
        assert build._member_hashes(vendor / bundle_name) == entry["files"]

        # 4) 官方归档已在且校验通过 → 直接重压，不必再下载
        vendor = base / "vendor-official"
        vendor.mkdir()
        (vendor / official.name).write_bytes(official.read_bytes())
        assert _stage_case(build, vendor, entry) == []
        assert sorted(p.name for p in vendor.iterdir()) == [bundle_name]

        # 5) 另一个平台的残留 + 运行态 .json → 都清掉，只留本平台那一份
        vendor = base / "vendor-foreign"
        vendor.mkdir()
        (vendor / "sing-box-9.9.9-other-platform.tar.xz").write_bytes(b"OTHER")
        (vendor / "active_config.json").write_text('{"outbounds":[{"password":"REAL"}]}')
        assert _stage_case(build, vendor, entry) == ["9.9.9"]
        assert sorted(p.name for p in vendor.iterdir()) == [bundle_name]


def test_windows_style_zip_entry_is_also_repacked():
    """`.zip` 源（Windows 官方资产）现在同样重压成 `.tar.xz`，官方 zip 不留存。

    回归背景：Windows 曾"原样放官方 zip"，实测 setup.exe 因此涨了 10.6 MB。
    """
    build = _load_build_module()
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        seed_dir = base / "seed"
        seed_dir.mkdir()
        official, entry = _synthetic_archive(
            seed_dir, {"sing-box.exe": b"PE"}, name="sing-box-9.9.9-test-win.zip"
        )
        _OFFICIAL_BLOBS[official.name] = official.read_bytes()
        vendor = base / "vendor"
        assert _stage_case(build, vendor, entry) == ["9.9.9"]
        assert sorted(p.name for p in vendor.iterdir()) == [
            "sing-box-9.9.9-test-win.tar.xz"
        ], "zip 源应被重压，官方 zip 不留存"
        assert build._member_hashes(vendor / "sing-box-9.9.9-test-win.tar.xz") == entry["files"]


def test_stage_core_clears_runtime_state_before_packaging():
    """暂存里混入运行态 `.json` 时，暂存阶段就要清掉——它是凭据防线，不是洁癖。

    回归背景：构建机实测发生过一次，整个 bin 被抄进暂存，连带实盘配置与真节点凭据，
    靠 `_check_credentials` 才拦住。
    """
    build = _load_build_module()
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        seed_dir = base / "seed"
        seed_dir.mkdir()
        official, entry = _synthetic_archive(seed_dir, {"sing-box": b"KERNEL"})
        _OFFICIAL_BLOBS[official.name] = official.read_bytes()

        vendor = base / "vendor"
        vendor.mkdir()
        (vendor / "active_config.json").write_text('{"outbounds":[]}')
        (vendor / "sing-box.v1.14.2.bak").write_bytes(b"OLD")

        assert _stage_case(build, vendor, entry) == ["9.9.9"]
        assert sorted(p.name for p in vendor.iterdir()) == [
            "sing-box-9.9.9-test-amd64.tar.xz"
        ]


def test_target_arch_drives_manifest_key_and_artifact_tag():
    """交叉构建时，清单取键与产物标签都必须跟着**目标**架构走。

    回归背景：在 Intel Mac 上给 arm64 打包，若取键仍读宿主架构，就会从清单里拿到
    darwin-amd64 的条目——x86_64 的官方内核被塞进 arm64 包，产物名字与内容对不上，
    装到 Apple 芯片上直接起不来。
    """
    import os

    build = _load_build_module()
    with patch.dict(os.environ, {}, clear=False):
        os.environ.pop("AGENT_CHANNEL_TARGET_ARCH", None)
        host_arch = build._arch_class()
        assert build._platform_key() == f"{build._platform_key().split('-')[0]}-{host_arch}"

    with patch.dict(os.environ, {"AGENT_CHANNEL_TARGET_ARCH": "arm64"}):
        assert build._arch_class() == "arm64"
        assert build._platform_key().endswith("-arm64")
        assert build._platform_tag().endswith("-arm64")

    with patch.dict(os.environ, {"AGENT_CHANNEL_TARGET_ARCH": "x86_64"}):
        assert build._arch_class() == "amd64"
        assert build._platform_key().endswith("-amd64")
        assert build._platform_tag().endswith("-x64"), "产物标签沿用 x64 这个习惯写法"

    with patch.dict(os.environ, {"AGENT_CHANNEL_TARGET_ARCH": "universal2"}):
        try:
            build._arch_class()
        except SystemExit:
            pass
        else:
            raise AssertionError("universal2 需要包里同时放两份内核，必须明确拒绝")


def test_onefile_artifact_name_does_not_collide_with_onedir_dir():
    """单文件产物名不许与目录形态同名，且必须由 spec 的 `ONEFILE_NAME` 决定。

    回归背景：POSIX 没有 `.exe` 后缀，两个形态都叫 `agent-channel`，PyInstaller 的
    `--noconfirm` 于是在构建末尾把先建好的 `dist/agent-channel/` 整个删掉换成单文件——
    目录产物凭空消失，而日志上一切正常。
    """
    import os

    build = _load_build_module()
    name = build._onefile_name()
    if os.name == "nt":
        assert name == "agent-channel.exe", name
    else:
        assert name != build.APP_NAME, "单文件与目录形态同名会在 dist/ 里互相覆盖"

    spec_text = Path("packaging/agent-channel.spec").read_text(encoding="utf-8")
    assert "ONEFILE_NAME = " in spec_text, "spec 必须定义 ONEFILE_NAME"
    assert "name=ONEFILE_NAME" in spec_text, "单文件形态必须用 ONEFILE_NAME 命名"


def test_package_tarball_layout_is_the_documented_shape():
    """tar.gz 的形态是用户直接看到的：顶层 `agent-channel/`，同级 install.sh 与菜单文件。"""
    import tarfile

    build = _load_build_module()
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        dist = base / "dist"
        (dist / "agent-channel").mkdir(parents=True)
        (dist / "agent-channel" / "agent-channel").write_bytes(b"BIN")
        root = base / "root"
        (root / "packaging" / "assets").mkdir(parents=True)
        (root / "packaging" / "install.sh").write_text("#!/bin/sh\n", encoding="utf-8")
        (root / "packaging" / "assets" / "agent-channel.desktop").write_text(
            "[Desktop Entry]\n", encoding="utf-8"
        )
        (root / "packaging" / "assets" / "icon-512.png").write_bytes(b"PNG")

        with patch.object(build, "DIST_DIR", dist), patch.object(build, "ROOT", root):
            archive = build.package_tarball()

        assert archive is not None, "应产出 tar.gz"
        with tarfile.open(archive) as tar:
            names = sorted(tar.getnames())
            modes = {member.name: member.mode for member in tar.getmembers()}
        # 菜单项与图标只对 Linux 有意义（macOS 由 .app 自带），期望值必须跟着平台走：
        # 写死 Linux 那一套会在 macOS runner 上必红（CI 上实测踩到过）。
        import sys

        expected = ["agent-channel/agent-channel", "install.sh"]
        if sys.platform != "darwin":
            expected += ["agent-channel.desktop", "agent-channel.png"]
        assert names == sorted(expected), names
        assert modes["install.sh"] == 0o755, "安装脚本必须带可执行位"


def test_verify_vendor_core_rejects_runtime_state():
    """暂存目录混入运行态 `.json` 时必须中止——这是凭据防线，不是洁癖。"""
    build = _load_build_module()
    with tempfile.TemporaryDirectory() as tmp:
        vendor = Path(tmp)
        (vendor / "sing-box.exe").write_bytes(b"KERNEL")
        (vendor / "active_config.json").write_text('{"outbounds":[]}')
        entry = {
            "version": "t",
            "files": {"sing-box.exe": hashlib.sha256(b"KERNEL").hexdigest()},
        }
        with patch.object(build, "VENDOR_DIR", vendor):
            try:
                build.verify_vendor_core(entry)
            except SystemExit as exc:
                assert "运行态" in str(exc), exc
                return
        raise AssertionError("暂存目录含 active_config.json 时必须中止打包")


def _ar(members: list[tuple[str, bytes]]) -> bytes:
    """拼一个 ar 容器（`.deb` 就是 ar），供凭据扫描用例构造最小 `.deb`。"""
    out = bytearray(b"!<arch>\n")
    for name, blob in members:
        header = (
            f"{name + '/':<16}{0:<12}{0:<6}{0:<6}{'100644':<8}{len(blob):<10}`\n"
        )
        out += header.encode("ascii")
        out += blob
        if len(blob) % 2:
            out += b"\n"
    return bytes(out)


def test_extract_official_archive_handles_tar_gz_and_zip():
    """官方归档在 POSIX 上是 `.tar.gz`：只按 zip 解，Linux/macOS 打包连构建那步都到不了。

    回归背景：官方 windows 资产是 zip、linux/darwin 资产是 tar.gz，而 `fetch_official_core`
    原先写死 `zipfile`。解包口径：只取**纯文件名**扁平落盘，归档里的目录层级与链接都不进暂存。
    """
    import io
    import tarfile
    import zipfile

    build = _load_build_module()

    with tempfile.TemporaryDirectory() as tmp:
        target = Path(tmp)
        buffer = io.BytesIO()
        with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
            payload = b"KERNEL"
            info = tarfile.TarInfo("sing-box-1.14.3-linux-amd64/sing-box")
            info.size = len(payload)
            info.mode = 0o755
            tar.addfile(info, io.BytesIO(payload))
            link = tarfile.TarInfo("sing-box-1.14.3-linux-amd64/evil-link")
            link.type = tarfile.SYMTYPE
            link.linkname = "/etc/passwd"
            tar.addfile(link)
        build._extract_official_archive(
            buffer.getvalue(),
            "https://example.invalid/sing-box-1.14.3-linux-amd64.tar.gz",
            target,
        )
        assert sorted(p.name for p in target.iterdir()) == ["sing-box"], "只该解出内核本体"
        assert (target / "sing-box").read_bytes() == b"KERNEL"

    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        target = base / "vendor"
        target.mkdir()
        zip_path = base / "core.zip"
        with zipfile.ZipFile(zip_path, "w") as archive:
            archive.writestr("sing-box-1.14.3-windows-amd64/sing-box.exe", b"PE")
            archive.writestr("sing-box-1.14.3-windows-amd64/libcronet.dll", b"DLL")
        build._extract_official_archive(zip_path.read_bytes(), str(zip_path), target)
        assert sorted(p.name for p in target.iterdir()) == [
            "libcronet.dll",
            "sing-box.exe",
        ], "zip 那条路径不能被改坏"

    try:
        build._extract_official_archive(b"x", "https://example.invalid/core.7z", target)
    except SystemExit:
        pass
    else:
        raise AssertionError("认不出的归档格式必须中止打包")


def test_verify_no_secrets_scans_inside_tar_gz_and_deb():
    """凭据扫描要**进压缩包**：漏了 tar.gz/.deb，等于对 POSIX 产物根本没查。

    回归背景：`verify_no_secrets` 原先只解 `.zip`，而 POSIX 的分发形态是 tar.gz 与 .deb。
    """
    import io
    import tarfile

    build = _load_build_module()
    secret = "SUBSCRIPTION-SECRET-abcdef"

    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        tarball = base / "agent-channel-0.1.0-linux-x64.tar.gz"
        with tarfile.open(tarball, "w:gz") as tar:
            payload = f"node_uri={secret}".encode()
            info = tarfile.TarInfo("agent-channel/state.txt")
            info.size = len(payload)
            tar.addfile(info, io.BytesIO(payload))

        data = io.BytesIO()
        with tarfile.open(fileobj=data, mode="w:gz") as tar:
            payload = f"subscription_url={secret}".encode()
            info = tarfile.TarInfo("./usr/lib/agent-channel/state.txt")
            info.size = len(payload)
            tar.addfile(info, io.BytesIO(payload))
        deb = base / "agent-channel_0.1.0_amd64.deb"
        deb.write_bytes(
            _ar([("debian-binary", b"2.0\n"), ("data.tar.gz", data.getvalue())])
        )

        with patch.object(build, "collect_secrets", lambda: [secret]):
            for path in (tarball, deb):
                hits = build.verify_no_secrets(path)
                assert hits == [secret], f"{path.name} 里的凭据没被查到: {hits}"
        with patch.object(build, "collect_secrets", lambda: ["NOT-IN-PACKAGE-0123456"]):
            assert build.verify_no_secrets(tarball) == [], "不该误报不在包里的凭据"


def test_deb_control_declares_glibc_floor_and_field_order():
    """`.deb` 的依赖要带 glibc 下限，且 Description 必须是最后一个字段。

    下限写进依赖，老发行版在 apt 阶段就被明确拒绝，而不是装完才报加载器错误；
    而 dpkg 把 Description 之后的续行都算它的正文，`Installed-Size` 放到后面会被吞掉。
    """
    build = _load_build_module()
    text = build._deb_control("0.1.0", "amd64", 12345)
    assert f"Depends: libc6 (>= {build.POSIX_GLIBC_FLOOR})" in text
    assert "Installed-Size: 12345" in text
    assert text.index("Installed-Size") < text.index("Description:")
    assert text.endswith("\n")


def test_window_toolkit_detection_and_macos_assertion():
    """macOS 承诺有原生窗口：产物里缺 Tcl/Tk 必须让构建失败，而不是静默退化。

    现实教训：Linux 产物就是"构建环境没装 Tcl/Tk → 窗口没了、只剩浏览器面板"的样子；
    那是 Linux 的选择，但同样的事发生在 macOS 上就是缺陷——所以这里断言。
    """
    build = _load_build_module()
    with tempfile.TemporaryDirectory() as tmp:
        tree = Path(tmp)
        (tree / "Contents" / "Frameworks").mkdir(parents=True)
        try:
            build.assert_window_toolkit(tree)
        except SystemExit as exc:
            assert "没有 Tcl/Tk" in str(exc), exc
        else:
            raise AssertionError("缺 Tcl/Tk 时必须中止 macOS 打包")

        (tree / "Contents" / "Frameworks" / "libtcl9.0.dylib").write_bytes(b"TCL")
        (tree / "Contents" / "Frameworks" / "_tkinter.cpython-310-darwin.so").write_bytes(b"TK")
        build.assert_window_toolkit(tree)  # 有了就放行
        assert len(build._window_toolkit_items(tree)) == 2

        build.report_window_toolkit(tree)  # 非 macOS 侧只报告，不抛


def test_quit_hint_is_printed_when_there_is_no_window(tmp_path=None):
    """没有窗口的平台要明说"关掉浏览器标签不等于退出"，否则用户以为已经关了。"""
    import io
    from contextlib import redirect_stdout

    from agent_channel import window

    buf = io.StringIO()
    with redirect_stdout(buf):
        window.print_quit_hint()
    text = buf.getvalue()
    assert "退出程序" in text and "Ctrl-C" in text, text
    assert "关掉浏览器标签不会退出进程" in text, text


def test_write_build_manifest_binds_commit_to_artifact_hashes():
    """② 的落点：把"提交号 ↔ 各产物 sha256"写成清单，指纹才成为追溯凭据。

    审核指出过两个问题：产物来自未提交的工作树、且没有任何一处把提交号与指纹绑起来。
    这里验四件事：清单哈希与产物一致；只收**本版本**的产物（dist/ 里的旧版本不混进来）；
    同一提交的多次构建（先 x64 再 arm64）累积；工作树脏要标出来。
    """
    import os

    build = _load_build_module()
    version = build._project_version()

    with tempfile.TemporaryDirectory() as tmp:
        dist = Path(tmp)
        x64 = dist / f"agent-channel-{version}-darwin-x64.tar.gz"
        x64.write_bytes(b"X64")
        stale = dist / "agent-channel-0.0.9-linux-x64.tar.gz"
        stale.write_bytes(b"OLD")
        (dist / build._onefile_name()).write_bytes(b"SLIM")

        def fake_git(args: list[str]) -> str:
            return {
                ("log", "-1", "--pretty=%s"): "测试提交",
                ("status", "--porcelain"): "",
            }.get(tuple(args), "")

        with (
            patch.object(build, "DIST_DIR", dist),
            patch.object(build, "_git_field", fake_git),
            patch.dict(os.environ, {"AGENT_CHANNEL_BUILD_COMMIT": "c" * 40}),
        ):
            sums = build.write_build_manifest()

            # 第二次构建（另一架构）累积进同一份清单
            arm64 = dist / f"agent-channel-{version}-darwin-arm64.tar.gz"
            arm64.write_bytes(b"ARM64")
            build.write_build_manifest()

        assert sums is not None and sums.is_file()
        recorded = dict(
            (line.split("  ", 1)[1], line.split("  ", 1)[0])
            for line in sums.read_text(encoding="utf-8").splitlines()
        )
        assert set(recorded) == {x64.name, arm64.name, build._onefile_name()}
        assert stale.name not in recorded, "旧版本产物不许混进本次清单"
        for name, digest in recorded.items():
            assert digest == hashlib.sha256((dist / name).read_bytes()).hexdigest(), name

        info = (dist / "BUILD-INFO.txt").read_text(encoding="utf-8")
        assert "commit: " + "c" * 40 in info
        assert f"version: {version}" in info
        assert "worktree_dirty: false" in info

        # 工作树脏 → 必须如实标脏（这份产物对不上任何提交）
        with (
            patch.object(build, "DIST_DIR", dist),
            patch.object(build, "_git_field", fake_git),
            patch.dict(
                os.environ,
                {"AGENT_CHANNEL_BUILD_COMMIT": "c" * 40, "AGENT_CHANNEL_BUILD_DIRTY": "1"},
            ),
        ):
            build.write_build_manifest()
        assert "worktree_dirty: true" in (dist / "BUILD-INFO.txt").read_text(encoding="utf-8")


def test_build_commit_prefers_explicit_env_then_git_then_unknown():
    """构建环境没有 git 时（源码树被拷进容器/另一台机器）由调用方显式给提交号。

    实测背景：Linux 产物是在一个没有 git 的 rootfs 里打的，清单里写成了 `commit: unknown`。
    这里允许显式传入，但**不编造**：都不给就是 unknown。
    """
    import os

    build = _load_build_module()
    with patch.dict(os.environ, {"AGENT_CHANNEL_BUILD_COMMIT": "a" * 40}):
        assert build._build_commit() == "a" * 40
    with (
        patch.dict(os.environ, {}, clear=False),
        patch.object(build, "_git_field", lambda args: "b" * 40),
    ):
        os.environ.pop("AGENT_CHANNEL_BUILD_COMMIT", None)
        assert build._build_commit() == "b" * 40
    with (
        patch.dict(os.environ, {}, clear=False),
        patch.object(build, "_git_field", lambda args: ""),
    ):
        os.environ.pop("AGENT_CHANNEL_BUILD_COMMIT", None)
        assert build._build_commit() == "unknown"


def test_git_field_returns_empty_when_git_unavailable():
    """取不到提交号就返回空串（由上层写成 unknown），绝不编造出处。"""
    build = _load_build_module()
    with patch.object(build.subprocess, "run", side_effect=OSError("no git")):
        assert build._git_field(["rev-parse", "HEAD"]) == ""


def test_manifest_includes_windows_main_artifacts():
    """F1：Windows 的主交付物是 zip 与 setup.exe，漏收它们清单就绑不住那两个文件。

    PI 在构建机上实测：0.1.1 的 Windows 四件产物，清单只写进了 exe 与 whl。
    """
    build = _load_build_module()
    version = build._project_version()
    with tempfile.TemporaryDirectory() as tmp:
        dist = Path(tmp)
        names = [
            f"agent-channel-{version}-setup.exe",
            f"agent-channel-{version}-windows-x64.zip",
            build._onefile_name(),
            f"agent_channel-{version}-py3-none-any.whl",
        ]
        for name in names:
            (dist / name).write_bytes(name.encode())
        with patch.object(build, "DIST_DIR", dist):
            files, skipped = build._artifact_files()
        got = sorted(path.name for path in files)
        assert got == sorted(names), got
        assert skipped == []


def test_git_field_decodes_as_utf8():
    """F2：git 输出必须显式按 UTF-8 解码。

    构建机（Windows）默认 cp936，遇到中文提交标题会 UnicodeDecodeError，表现成
    out.stdout 为 None、清单步骤 AttributeError 崩掉——三件产物都在、清单没落盘。
    """
    import subprocess as real_subprocess

    build = _load_build_module()
    captured: dict = {}

    def fake_run(cmd, **kwargs):
        captured.update(kwargs)
        return real_subprocess.CompletedProcess(cmd, 0, stdout="标题", stderr="")

    with patch.object(build.subprocess, "run", fake_run):
        assert build._git_field(["log", "-1", "--pretty=%s"]) == "标题"
    assert captured.get("encoding") == "utf-8", captured
    assert captured.get("errors") == "replace", captured


def test_dirty_check_ignores_filemode_noise():
    """F2 附带：Windows 克隆常见的"执行位/换行"假脏不该把整批产物标成对不上提交。"""
    build = _load_build_module()
    seen: list[list[str]] = []

    def fake_git(args):
        seen.append(list(args))
        return ""

    with patch.object(build, "_git_field", fake_git), patch.dict("os.environ", {}, clear=False):
        import os

        os.environ.pop("AGENT_CHANNEL_BUILD_DIRTY", None)
        build._build_dirty()
    assert seen == [["-c", "core.fileMode=false", "status", "--porcelain"]], seen
