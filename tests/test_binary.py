import hashlib
import tempfile
import zipfile
from pathlib import Path

from agent_channel.binary import get_local_binary_path, verify_sha256


def test_get_local_binary_path():
    """本地内核路径的文件名必须是 sing-box 的可执行文件名。"""
    allowed = {"sing-box.exe", "sing-box"}
    assert get_local_binary_path().name.lower() in allowed


def test_ensure_local_core_seeds_companion_files():
    """随包播种内核时必须一并播伴生文件（libcronet.dll）：
    缺它时含 naive 出站的配置会让内核在初始化 outbound[0] 时 FATAL 退出。"""
    from unittest.mock import patch

    from agent_channel import binary

    with tempfile.TemporaryDirectory() as tmp:
        tmp_dir = Path(tmp)
        bundle = tmp_dir / "bundle"
        bundle.mkdir()
        (bundle / binary._core_filename()).write_bytes(b"KERNEL")
        (bundle / "libcronet.dll").write_bytes(b"DLL")
        (bundle / "LICENSE").write_bytes(b"MIT")
        # .bak 备份不应被播种（曾在真机播种实验里被带入本地 bin）
        (bundle / (binary._core_filename() + ".v1.14.2.bak")).write_bytes(b"OLD")
        local_bin = tmp_dir / "local_bin"
        target = local_bin / binary._core_filename()
        with (
            patch.object(binary, "get_bundled_core_dir", lambda: bundle),
            patch.object(binary, "get_local_binary_path", lambda: target),
        ):
            assert binary.ensure_local_core()
            assert target.read_bytes() == b"KERNEL"
            assert (local_bin / "libcronet.dll").read_bytes() == b"DLL"
            assert (local_bin / "LICENSE").read_bytes() == b"MIT"
            assert not (
                local_bin / (binary._core_filename() + ".v1.14.2.bak")
            ).exists(), "备份文件不该被播种"


def test_ensure_local_core_fills_missing_companion():
    """本地已有内核但缺伴生文件时补齐缺失件，且不覆盖已有内核（不降级用户版本）。"""
    from unittest.mock import patch

    from agent_channel import binary

    with tempfile.TemporaryDirectory() as tmp:
        tmp_dir = Path(tmp)
        bundle = tmp_dir / "bundle"
        bundle.mkdir()
        (bundle / binary._core_filename()).write_bytes(b"NEW-KERNEL")
        (bundle / "libcronet.dll").write_bytes(b"DLL")
        local_bin = tmp_dir / "local_bin"
        local_bin.mkdir()
        target = local_bin / binary._core_filename()
        target.write_bytes(b"USER-KERNEL")
        with (
            patch.object(binary, "get_bundled_core_dir", lambda: bundle),
            patch.object(binary, "get_local_binary_path", lambda: target),
        ):
            assert binary.ensure_local_core()
            # 已有内核不被覆盖（不降级用户手动更新的版本）
            assert target.read_bytes() == b"USER-KERNEL"
            # 缺失的伴生文件被补齐
            assert (local_bin / "libcronet.dll").read_bytes() == b"DLL"


def test_verify_sha256():
    """信任链依赖的函数：对的哈希放行、错的拒绝、文件不存在也拒绝。

    用 tempfile 而不是 pytest 的 tmp_path —— 本仓库的测试由 tests/run_tests.py
    直接调用（不经过 pytest），带 fixture 参数的用例根本跑不起来，所以它一直
    没被注册过。
    """
    with tempfile.TemporaryDirectory() as tmp:
        target = Path(tmp) / "test.bin"
        content = b"hello sing-box binary"
        target.write_bytes(content)
        digest = hashlib.sha256(content).hexdigest()
        assert verify_sha256(target, digest)
        assert not verify_sha256(target, "badhash")
        assert not verify_sha256(Path(tmp) / "missing.bin", digest)


def test_check_core_update_missing_tag_name():
    """GitHub 限流时返回合法 JSON 但无 tag_name，必须显式报失败，不能拼出空版本。"""
    from unittest.mock import patch

    from agent_channel import binary

    class FakeResp:
        def read(self):
            return b'{"message": "API rate limit exceeded"}'

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    with patch.object(binary, "_open_direct", lambda req, timeout: FakeResp()):
        result = binary.check_core_update()

    assert result["success"] is False
    assert "查询官方版本失败" in result["status_message"]
    assert "检测到官方新版本" not in result["status_message"]


def test_install_core_passes_official_digest():
    """官方 digest 必须真的传到下载函数上（原先没传，校验分支永不执行）。"""
    from unittest.mock import patch

    from agent_channel import binary

    digest = "a" * 64
    assets = [
        {
            "name": f"sing-box-1.14.2-{binary._platform_asset_suffix()}",
            "browser_download_url": "https://example.invalid/core.zip",
            "size": 1234,
            "digest": "sha256:" + digest,
        }
    ]
    seen = {}

    def fake_download(url, expected_hash=None):
        seen["url"] = url
        seen["hash"] = expected_hash
        return Path("installed")

    with (
        patch.object(
            binary,
            "check_core_update",
            lambda: {
                "success": True,
                "has_update": True,
                "latest_version": "v1.14.2",
                "assets": assets,
            },
        ),
        patch.object(binary, "download_and_extract_binary", fake_download),
    ):
        result = binary.install_core()

    assert result["success"], result
    assert seen.get("hash") == digest, f"expected_hash 没传下去: {seen}"
    assert result["digest_verified"] is True, result


def test_failed_extract_keeps_old_kernel():
    """解压失败不许把现有内核弄成半截（先写临时文件、验证后再替换）。"""
    import http.server
    import socketserver
    import tempfile
    import threading
    from unittest.mock import patch

    from agent_channel import binary

    with tempfile.TemporaryDirectory() as tmp:
        tmp_dir = Path(tmp)
        server_dir = tmp_dir / "served"
        server_dir.mkdir()
        with zipfile.ZipFile(server_dir / "core.zip", "w") as zf:
            zf.writestr("SFA.apk", b"not-a-kernel")  # 包里没有内核

        class Handler(http.server.SimpleHTTPRequestHandler):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, directory=str(server_dir), **kwargs)

            def log_message(self, *args):
                pass

        class Server(socketserver.TCPServer):
            allow_reuse_address = True

        target = tmp_dir / "sing-box.exe"
        target.write_bytes(b"OLD-GOOD-KERNEL")
        with Server(("127.0.0.1", 0), Handler) as httpd:
            threading.Thread(target=httpd.serve_forever, daemon=True).start()
            url = f"http://127.0.0.1:{httpd.server_address[1]}/core.zip"
            with (
                patch.object(binary, "get_bin_dir", lambda: tmp_dir),
                patch.object(binary, "get_local_binary_path", lambda: target),
            ):
                try:
                    binary.download_and_extract_binary(url)
                except Exception:
                    pass  # 失败是对的
        assert target.read_bytes() == b"OLD-GOOD-KERNEL", "旧内核被写坏了"
        assert not (tmp_dir / "sing-box.exe.new").exists(), "临时文件没清掉"


def test_platform_suffix_rejects_unknown_machine():
    """认不出的架构必须报错，不许静默当 amd64。"""
    from unittest.mock import patch

    from agent_channel import binary

    with patch("platform.machine", return_value="sparc64"):
        try:
            binary._platform_asset_suffix()
        except ValueError as exc:
            assert "手动" in str(exc)
        else:
            raise AssertionError("未知架构必须报错")

    with patch("platform.system", return_value="freebsd"):
        try:
            binary._platform_asset_suffix()
        except ValueError as exc:
            assert "手动" in str(exc)
        else:
            raise AssertionError("未知系统必须报错")


def test_update_refuses_when_kernel_in_use():
    """目标内核写不进去时，不能先去下那几十兆再报错。"""
    import tempfile
    from unittest.mock import patch

    from agent_channel import binary

    with tempfile.TemporaryDirectory() as tmp:
        tmp_dir = Path(tmp)
        blocked = tmp_dir / "sing-box.exe"
        blocked.mkdir()  # 用目录当目标：写不进去
        calls = {"n": 0}

        def must_not_download(url, dest):
            calls["n"] += 1
            raise AssertionError("目标写不进去时不该发起下载")

        with (
            patch.object(binary, "get_bin_dir", lambda: tmp_dir),
            patch.object(binary, "get_local_binary_path", lambda: blocked),
            patch.object(binary, "_download_once", must_not_download),
        ):
            try:
                binary.download_and_extract_binary("https://example.invalid/x.zip")
            except OSError:
                pass
            else:
                raise AssertionError("目标写不进去时必须报错")
        assert calls["n"] == 0, calls


def test_truncated_download_is_retried():
    """声明长度比实收多 → 判定截断并重下；第二次完整就成功。"""
    import contextlib
    import http.server
    import socketserver
    import tempfile
    import threading
    import zipfile
    from unittest.mock import patch

    from agent_channel import binary

    with tempfile.TemporaryDirectory() as tmp:
        tmp_dir = Path(tmp)
        payload = b"fake-sing-box-payload"
        archive = tmp_dir / "core.zip"
        with zipfile.ZipFile(archive, "w") as zf:
            zf.writestr("sing-box", payload)
        blob = archive.read_bytes()
        state = {"served": 0}

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                state["served"] += 1
                first = state["served"] == 1
                self.send_response(200)
                self.send_header("Content-Length", str(len(blob)))
                self.end_headers()
                self.wfile.write(blob[: len(blob) // 2] if first else blob)

        class Server(socketserver.TCPServer):
            allow_reuse_address = True

        install_dir = tmp_dir / "install"
        install_dir.mkdir()
        with Server(("127.0.0.1", 0), Handler) as httpd:
            with contextlib.suppress(OSError):
                threading.Thread(target=httpd.serve_forever, daemon=True).start()
            url = f"http://127.0.0.1:{httpd.server_address[1]}/core.zip"
            with (
                patch.object(binary, "get_bin_dir", lambda: install_dir),
                patch.object(
                    binary, "get_local_binary_path", lambda: install_dir / "sing-box"
                ),
            ):
                target = binary.download_and_extract_binary(url)

        assert state["served"] == 2, state
        assert target.read_bytes() == payload
        assert not (install_dir / "temp_core.zip").exists(), "临时文件没清掉"


def test_pick_asset_ignores_longer_names_containing_the_base():
    """子串匹配会把 `…-windows-amd64.zip.sha256` 当基础包；精确匹配不会。"""
    from agent_channel import binary

    base = f"sing-box-1.14.2-{binary._platform_asset_suffix()}"
    assets = [
        {
            "name": base + ".sha256",
            "browser_download_url": "https://example.invalid/sha",
        },
        {"name": base, "browser_download_url": "https://example.invalid/zip"},
    ]
    assert binary._pick_asset(assets)["name"] == base
    assert binary._pick_asset_url(assets).endswith("/zip")


def test_extract_all_reports_write_failure_instead_of_missing_member():
    """写不进去（目录不可写 / 磁盘满）不能报成“包里没有这个成员”。"""
    import tempfile
    import zipfile

    from agent_channel import binary

    with tempfile.TemporaryDirectory() as tmp:
        tmp_dir = Path(tmp)
        archive = tmp_dir / "core.zip"
        with zipfile.ZipFile(archive, "w") as zf:
            zf.writestr("sing-box.exe", b"kernel-bytes")
        blocked = tmp_dir / "sing-box.exe"
        blocked.mkdir()  # 目标是个目录：写不进去
        try:
            binary._extract_all(archive, tmp_dir)
        except OSError as exc:
            assert "写入内核文件失败" in str(exc), exc
        else:
            raise AssertionError("写不进去必须抛 OSError，不能返回 False")


def test_extract_all_includes_companion_library():
    """伴生库（libcronet.dll）必须随内核一起解出，不能只解 sing-box.exe。"""
    import tempfile
    import zipfile

    from agent_channel import binary

    with tempfile.TemporaryDirectory() as tmp:
        tmp_dir = Path(tmp)
        archive = tmp_dir / "core.zip"
        with zipfile.ZipFile(archive, "w") as zf:
            zf.writestr("sing-box-1.14.2-windows-amd64/sing-box.exe", b"kernel")
            zf.writestr("sing-box-1.14.2-windows-amd64/libcronet.dll", b"dll")
        names = binary._extract_all(archive, tmp_dir)
        assert set(names) == {"sing-box.exe", "libcronet.dll"}
        assert (tmp_dir / "libcronet.dll").read_bytes() == b"dll"
        assert (tmp_dir / "sing-box.exe").read_bytes() == b"kernel"


def test_platform_tag_reuses_the_shared_arch_mapping():
    """产物标签必须复用 binary 的映射：armv7l 不能标成 arm64，未知架构不能静默接受。"""
    import importlib.util
    from unittest.mock import patch

    build_py = Path(__file__).resolve().parent.parent / "packaging" / "build.py"
    spec = importlib.util.spec_from_file_location("agent_channel_build", build_py)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    with patch("platform.machine", return_value="armv7l"):
        assert module._platform_tag().endswith("-armv7"), (
            module._platform_tag()
        )  # 旧实现给 arm64
    with patch("platform.machine", return_value="sparc64"):
        try:
            module._platform_tag()
        except ValueError as exc:
            assert "手动" in str(exc)
        else:
            raise AssertionError("未知架构必须报错，不能静默接受")


def test_open_direct_bypasses_system_proxy():
    """检查更新/下载内核必须直连 GitHub：opener 用空代理构造（绕过系统代理）。"""
    import urllib.request
    from unittest.mock import patch

    from agent_channel import binary

    captured = {}

    class FakeOpener:
        def open(self, req, timeout):
            return None

    def fake_proxy_handler(proxies=None):
        captured["proxies"] = proxies
        return object()

    def fake_build_opener(handler):
        return FakeOpener()

    with (
        patch("urllib.request.build_opener", fake_build_opener),
        patch("urllib.request.ProxyHandler", fake_proxy_handler),
    ):
        binary._open_direct(urllib.request.Request("https://example.invalid"), 1)

    assert captured["proxies"] == {}
