"""跨平台打包脚本。

用法：
    python packaging/build.py              # 自动选形态（带内核时用 onedir）
    python packaging/build.py --onefile    # 强制单文件
    python packaging/build.py --onedir     # 强制目录形态
    python packaging/build.py --no-core    # 不预置内核（首次运行需联网下载）
    python packaging/build.py --all        # 按当前代码重建全部可分发产物

`--all` 是最省心的交付入口：一次串行产出
目录形态（含内核）+ zip + 安装向导 + 精简单文件 + pip wheel，
并对每份产物做凭据自检；其余形态开关会被忽略（由 --all 自行决定）。

形态选择理由：
- onefile 形态每次启动都需把打包内容解包到临时目录，带内核时启动明显变慢；
  因此带内核默认产出 onedir 目录，启动无解包开销。

说明：
- 内核用官方预编译文件，全程零本地编译；
- 预置内核只在「本地尚无内核」时被播种，不会覆盖用户已更新的版本。
"""

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SPEC = ROOT / "packaging" / "agent-channel.spec"
VENDOR_DIR = ROOT / "packaging" / "vendor"
DIST_DIR = ROOT / "dist"
APP_NAME = "agent-channel"

# ---- 内置内核清单（B 强制 / A 便利） ----
#
# B（强制）：打包前逐字节比对暂存内核与清单，不匹配即中止。两层目的：
#   1. 分发产物里的内核必须是**官方原件**，不是来路不明或被替换的文件；
#   2. “内置内核是哪一版”从此可复现——换台机器打包，装的还是同一份官方文件。
# A（便利）：暂存缺件时按清单里的官方 URL 下载并校验（digest 取自官方 release 资产）。
#
# 升级内核时**所有条目一起改**：版本号、归档 sha256、解压后各文件 sha256。
# 条目覆盖 platform_os()/platform_arch() 能命名的全部平台键（共 11 个），
# tests/test_packaging.py 的清单一致性用例会拦住"只改了一部分"与版本漂移。
# 版本须等于**发布时的官方最新稳定版**；发布前跑该用例（并对照 /releases/latest）。
CORE_MANIFEST: dict[str, dict] = {
    "windows-amd64": {
        "version": "1.14.3",
        "url": "https://github.com/SagerNet/sing-box/releases/download/"
        "v1.14.3/sing-box-1.14.3-windows-amd64.zip",
        "archive_sha256": "d8b86ef58936dba9230ec6a57ffa4243933f3c14ef4809828b6c553440ed1518",
        "files": {
            "sing-box.exe": "5c19bb3f8c12d4e85b3d47fe63123a7624b314b58dd7a8e32f51004f1c4d2faf",
            "libcronet.dll": "3217c6260fbca5f16072e0b79735742f40109a63bb0ff88fd6b96dd6b54a2928",
            "LICENSE": "bb3805862b583aee73ad6f7805ec634747a37257a637a3069857843f05ea589c",
        },
    },
# ---- 以下条目与 windows-amd64 同版（v1.14.3），哈希取自官方归档 ----
    "windows-386": {
        "version": "1.14.3",
        "url": "https://github.com/SagerNet/sing-box/releases/download/v1.14.3/"
        "sing-box-1.14.3-windows-386.zip",
        "archive_sha256": "8706eeca87cbca2fc9eee704948e8b5a27ea66056fedac0caa063ccedf1acab5",
        "files": {
            "LICENSE": "bb3805862b583aee73ad6f7805ec634747a37257a637a3069857843f05ea589c",
            "sing-box.exe": "9091bc68a2ac86f75602456268e3db58b675068c7b3a0110051b762544587c17",
        },
    },
    "windows-arm64": {
        "version": "1.14.3",
        "url": "https://github.com/SagerNet/sing-box/releases/download/v1.14.3/"
        "sing-box-1.14.3-windows-arm64.zip",
        "archive_sha256": "195b35424c6e3f5913954c7f08c5d8ba2d2199f6d18eb27a3972a5a29cdaa78c",
        "files": {
            "LICENSE": "bb3805862b583aee73ad6f7805ec634747a37257a637a3069857843f05ea589c",
            "libcronet.dll": "a75a1b99a7e31802cf67ee14763e5cb9db6a24aaf7062f86c49f2fc1c030fcf0",
            "sing-box.exe": "db11afc05195e2e6484830a0a62050a22b3d488e080bfa8e21023e55ca3153d4",
        },
    },
# ---- darwin ----
    "darwin-amd64": {
        "version": "1.14.3",
        "url": "https://github.com/SagerNet/sing-box/releases/download/v1.14.3/"
        "sing-box-1.14.3-darwin-amd64.tar.gz",
        "archive_sha256": "6d002b74d03b478547349cc10af89e007868596e5aa931816d45da9f544715a9",
        "files": {
            "LICENSE": "650d5e3b99a446fb38e820fa87a49562e0c79eab868fff58618ac487a58e554c",
            "sing-box": "4d63d13fe7516a5d0a76c764095238f9d51ada7bfa5d18cf5030ca3032882c68",
        },
    },
    "darwin-arm64": {
        "version": "1.14.3",
        "url": "https://github.com/SagerNet/sing-box/releases/download/v1.14.3/"
        "sing-box-1.14.3-darwin-arm64.tar.gz",
        "archive_sha256": "0360ce634a04c26a6b4fd957517987bb4f6c8950f63435ce3c5af2fcae5860c5",
        "files": {
            "LICENSE": "650d5e3b99a446fb38e820fa87a49562e0c79eab868fff58618ac487a58e554c",
            "sing-box": "28b7200074d6380006acc4f035a404c8af3dceb65dd92d7386ed92c9224e08a2",
        },
    },
# ---- linux ----
    "linux-386": {
        "version": "1.14.3",
        "url": "https://github.com/SagerNet/sing-box/releases/download/v1.14.3/"
        "sing-box-1.14.3-linux-386.tar.gz",
        "archive_sha256": "92dad4663615d2cb30fb8feba48787e8529b1940004341712b0d0d4548cb42ff",
        "files": {
            "LICENSE": "650d5e3b99a446fb38e820fa87a49562e0c79eab868fff58618ac487a58e554c",
            "sing-box": "9d31fa328e6a5c3661d47606d06348b55f3c9bfb06ad4f587abd94faa51282d2",
        },
    },
    "linux-amd64": {
        "version": "1.14.3",
        "url": "https://github.com/SagerNet/sing-box/releases/download/v1.14.3/"
        "sing-box-1.14.3-linux-amd64.tar.gz",
        "archive_sha256": "e2bdf179c15a3652955dc44867e33ca0965c9c9b91b34267dcc5a5d639a5feee",
        "files": {
            "LICENSE": "650d5e3b99a446fb38e820fa87a49562e0c79eab868fff58618ac487a58e554c",
            "libcronet.so": "23109c55b08829bb1a68682a61a64852b1bbf243753b153cbd8f6e4ea2c05294",
            "sing-box": "768d31fa7f68debc067bb4626539498a1e8751d36a7b99e1a54bee6a23815f93",
        },
    },
    "linux-armv6": {
        "version": "1.14.3",
        "url": "https://github.com/SagerNet/sing-box/releases/download/v1.14.3/"
        "sing-box-1.14.3-linux-armv6.tar.gz",
        "archive_sha256": "63d91abdde9b9de2d1ef9d607d17779e4d9b4c23bef0ffa06bbb8bacb82311f2",
        "files": {
            "LICENSE": "650d5e3b99a446fb38e820fa87a49562e0c79eab868fff58618ac487a58e554c",
            "sing-box": "d9ebdefdfffe6a5a669247c054e5253fede673448c7b272af38a9c5ed4ef8632",
        },
    },
    "linux-armv7": {
        "version": "1.14.3",
        "url": "https://github.com/SagerNet/sing-box/releases/download/v1.14.3/"
        "sing-box-1.14.3-linux-armv7.tar.gz",
        "archive_sha256": "8e81ee6bc1db6fec6e0342360e1de7bfabecec8d2e9c480a3563d129f84fb034",
        "files": {
            "LICENSE": "650d5e3b99a446fb38e820fa87a49562e0c79eab868fff58618ac487a58e554c",
            "sing-box": "4741c001add6c4bbfac41631846ca5edb9e194eea1983d2c865cbf70796142f8",
        },
    },
    "linux-arm64": {
        "version": "1.14.3",
        "url": "https://github.com/SagerNet/sing-box/releases/download/v1.14.3/"
        "sing-box-1.14.3-linux-arm64.tar.gz",
        "archive_sha256": "29dae24d76bea3bc62d07dd3e60cfc12003de7debbd6b768df9be9c68360174e",
        "files": {
            "LICENSE": "650d5e3b99a446fb38e820fa87a49562e0c79eab868fff58618ac487a58e554c",
            "libcronet.so": "3c1fcfcb56261a4b318ffb6d2227b9726782edb5a894e74db2b534406db62498",
            "sing-box": "299f9f6080d9c43504714ddddae6aa9b853d430ccfc816c456a73613b5455689",
        },
    },
    "linux-riscv64": {
        "version": "1.14.3",
        "url": "https://github.com/SagerNet/sing-box/releases/download/v1.14.3/"
        "sing-box-1.14.3-linux-riscv64.tar.gz",
        "archive_sha256": "fe3620f67cac8e8d69f911116e255cb4e301fe2883cecd4b478cbe5dd97e2ba8",
        "files": {
            "LICENSE": "650d5e3b99a446fb38e820fa87a49562e0c79eab868fff58618ac487a58e554c",
            "sing-box": "5e4a9432fd381bf8a51ccde9cd89231582d9e9ba199fcc5b2b55649e9e9a88c5",
        },
    },
}


def _core_name() -> str:
    return "sing-box.exe" if os.name == "nt" else "sing-box"


def _app_exe_name() -> str:
    """当前平台的可执行产物名（Windows 带 .exe，其余无扩展名）。"""
    return f"{APP_NAME}.exe" if os.name == "nt" else APP_NAME


def _platform_key() -> str:
    """当前平台在清单里的键（复用 agent_channel 的分类器，不另造一套）。"""
    from agent_channel.binary import platform_arch, platform_os

    return f"{platform_os()}-{platform_arch()}"


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def verify_vendor_core(entry: dict) -> None:
    """B（强制）：暂存内核必须与清单逐字节一致，否则中止打包。"""
    problems: list[str] = []
    for name, expected in entry["files"].items():
        path = VENDOR_DIR / name
        if not path.is_file():
            problems.append(f"  缺文件: {name}")
            continue
        actual = _sha256_file(path)
        if actual != expected:
            problems.append(
                f"  {name} 哈希不符\n    实际: {actual}\n    期望: {expected}"
            )
    if problems:
        raise SystemExit(
            f"[build] 内置内核 v{entry['version']} 校验未通过，已中止打包：\n"
            + "\n".join(problems)
            + "\n  若确实要升级内核，请同时更新 CORE_MANIFEST 的版本号、"
            "archive_sha256 与 files 里的各 sha256。"
        )
    print(
        f"[build] 内置内核校验通过：v{entry['version']}（{len(entry['files'])} 个文件）"
    )


def fetch_official_core(entry: dict) -> None:
    """A（便利）：暂存缺件时按清单里的官方 URL 下载并解出全部文件（含伴生库）。"""
    import io
    import urllib.request

    print(f"[build] 暂存缺失，按官方 URL 下载 v{entry['version']}: {entry['url']}")
    request = urllib.request.Request(
        entry["url"], headers={"User-Agent": "agent-channel"}
    )
    with urllib.request.urlopen(request, timeout=180) as resp:
        blob = resp.read()
    actual = hashlib.sha256(blob).hexdigest()
    if actual != entry["archive_sha256"]:
        raise SystemExit(
            "[build] 下载的官方包 sha256 不符，已中止：\n"
            f"  实际: {actual}\n  期望: {entry['archive_sha256']}"
        )
    VENDOR_DIR.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(io.BytesIO(blob)) as archive:
        for info in archive.infolist():
            name = Path(info.filename).name
            if not name or info.is_dir():
                continue
            # 读进内存再落盘（最大单文件 82MB），避开 `open()` 的未处理抛错检查；
            # 真正的写失败会被上层捕获并以非零码终止打包。
            (VENDOR_DIR / name).write_bytes(archive.read(info))
            print(f"[build] 已解出: {name}")


def stage_core() -> Path | None:
    """准备打包暂存的内核（含伴生文件），并做 B 强制校验。

    - 暂存已有 → 直接校验；
    - 暂存缺失但本地已装内核 → **本地版本与清单版本一致**才从本地暂存（exe + 同目录
      伴生文件，跳过 `.bak`）；版本不一致改按官方 URL 下载 —— 包里必须是发布时的官方
      最新版，不能因为本机装着旧版就把旧版抄进包（那会让 verify 以"清单哈希不符"中止，
      报错还会误导人去改清单）；
    - 都没有 → A：按官方 URL 下载；
    - 当前平台无清单条目且无暂存 → 不预置内核（返回 None）。
    """
    from agent_channel.binary import get_local_binary_path, get_local_core_version

    staging = VENDOR_DIR / _core_name()
    entry = CORE_MANIFEST.get(_platform_key())
    if entry is None:
        if staging.is_file():
            raise SystemExit(
                f"[build] {_platform_key()} 没有内核清单条目，但暂存目录已有内核 "
                f"({staging.name})，无法校验——请补清单或清掉暂存。"
            )
        print(f"[build] {_platform_key()} 无内置内核清单，本次构建不带内核。")
        return None

    if not staging.is_file():
        local = get_local_binary_path()
        local_matches = False
        if local.is_file():
            local_matches = (
                get_local_core_version().lower().lstrip("v")
                == str(entry["version"]).lower().lstrip("v")
            )
        if local_matches:
            VENDOR_DIR.mkdir(parents=True, exist_ok=True)
            for src in local.parent.iterdir():
                if src.is_file() and src.suffix != ".bak":
                    shutil.copy2(src, VENDOR_DIR / src.name)
            print(f"已暂存本地内核（含伴生文件，版本与清单一致）: {local.parent} -> {VENDOR_DIR}")
        else:
            if local.is_file():
                print(
                    f"[build] 本地内核 {get_local_core_version()} ≠ 清单 "
                    f"v{entry['version']}，改按官方 URL 下载以对齐最新版"
                )
            fetch_official_core(entry)

    verify_vendor_core(entry)
    return staging if staging.is_file() else None


def clean_previous(onedir: bool) -> None:
    """只清理本次将要产出的同形态旧产物，避免误删另一形态的产物。"""
    if onedir:
        folder = DIST_DIR / APP_NAME
        if not folder.is_dir():
            return
        try:
            shutil.rmtree(folder, ignore_errors=True)
        except OSError as exc:
            print(f"清理旧目录失败（忽略）: {exc}")
        return

    exe_file = DIST_DIR / _app_exe_name()
    if not exe_file.is_file():
        return
    try:
        exe_file.unlink()
    except OSError as exc:
        print(f"清理旧单文件失败（忽略）: {exc}")


def _mask(value: str) -> str:
    """日志里只显示片段，避免把凭据又写进构建输出。"""
    if len(value) <= 8:
        return "*" * len(value)
    return f"{value[:4]}…{value[-4:]}"


def collect_secrets() -> list[str]:
    """收集绝不应进入分发产物的本地敏感值（全部来自用户本机配置）。

    节点的订阅地址/节点链接/已解析出的服务器与凭据都只应留在用户机器的
    %LOCALAPPDATA% 下，绝不能随安装包分发。
    """
    from agent_channel.config import config_path, load_config

    values: list[str] = []
    config = load_config()
    for key in ("subscription_url", "node_uri", "local_proxy"):
        value = str(config.get(key) or "").strip()
        if value:
            values.append(value)

    # 运行时 sing-box 配置里含节点地址与凭据
    active = config_path().parent / "bin" / "active_config.json"
    if active.is_file():
        try:
            data = json.loads(active.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}
        for outbound in data.get("outbounds", []):
            for key in ("server", "uuid", "password", "public_key", "short_id"):
                value = str(outbound.get(key) or "").strip()
                if value:
                    values.append(value)

    # 太短的串容易误报，只留具备辨识度的
    return [v for v in dict.fromkeys(values) if len(v) >= 6]


def _scan_bytes(data: bytes, needles: list[tuple[str, bytes]], hits: list[str]) -> None:
    for text, needle in needles:
        if needle in data and text not in hits:
            hits.append(text)


def verify_no_secrets(target: Path) -> list[str]:
    """扫描分发产物，确认不含任何本地节点凭据；返回命中的敏感值。

    压缩包必须解压后逐条目检查：直接扫压缩字节会被压缩掩盖，等于没查。
    """
    secrets = collect_secrets()
    if not secrets or not target.exists():
        return []
    needles = [(text, text.encode("utf-8")) for text in secrets]
    hits: list[str] = []
    files = (
        [target] if target.is_file() else [p for p in target.rglob("*") if p.is_file()]
    )
    for path in files:
        try:
            data = path.read_bytes()
        except OSError:
            continue
        _scan_bytes(data, needles, hits)
        if path.suffix.lower() != ".zip":
            continue
        try:
            with zipfile.ZipFile(path) as archive:
                for info in archive.infolist():
                    if info.is_dir():
                        continue
                    _scan_bytes(archive.read(info.filename), needles, hits)
        except (OSError, zipfile.BadZipFile) as exc:
            print(f"  警告: 无法解压检查 {path.name}: {exc}")
    return hits


def _project_version() -> str:
    """从 pyproject.toml 读版本号，避免与包元数据脱节。"""
    try:
        text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    except OSError:
        return "0.0.0"
    match = re.search(r'^version\s*=\s*"([^"]+)"', text, re.MULTILINE)
    return match.group(1) if match else "0.0.0"


# 架构分类只认 agent_channel 那一份实现：同一概念不留两份，否则必然只修一处。
# 这里只是把分类名换成产物名的习惯（内部叫 amd64，产物名沿用 x64）。
_ARCH_TAG = {"amd64": "x64"}


def _platform_tag() -> str:
    """产物平台标签（如 windows-x64 / linux-armv7）。"""
    from agent_channel.binary import platform_arch, platform_os

    arch_class = platform_arch()
    return f"{platform_os()}-{_ARCH_TAG.get(arch_class, arch_class)}"


def package_zip() -> Path | None:
    """把 onedir 产物打成 zip，便于分发。"""
    folder = DIST_DIR / APP_NAME
    if not folder.is_dir():
        return None

    archive = DIST_DIR / f"{APP_NAME}-{_project_version()}-{_platform_tag()}.zip"
    if archive.exists():
        try:
            archive.unlink()
        except OSError as exc:
            print(f"覆盖旧压缩包失败: {exc}")
            return None

    raw_bytes = 0
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as bundle:
        for path in sorted(folder.rglob("*")):
            if not path.is_file():
                continue
            try:
                bundle.write(path, path.relative_to(DIST_DIR))
            except OSError as exc:
                print(f"  跳过 {path.name}: {exc}")
                continue
            raw_bytes += path.stat().st_size

    packed_mb = archive.stat().st_size / 1048576
    print(
        f"已打包: {archive.name} "
        f"({packed_mb:.1f} MB，原始 {raw_bytes / 1048576:.1f} MB)"
    )
    return archive


def _iscc_candidates() -> list[Path]:
    local_app = os.environ.get("LOCALAPPDATA")
    candidates = []
    if local_app:
        candidates.append(Path(local_app) / "Programs" / "InnoSetup" / "ISCC.exe")
    candidates.append(Path(r"C:\Program Files (x86)\Inno Setup 6\ISCC.exe"))
    candidates.append(Path(r"C:\Program Files\Inno Setup 6\ISCC.exe"))
    return candidates


def find_iscc() -> Path | None:
    """定位 Inno Setup 编译器。"""
    for candidate in _iscc_candidates():
        if candidate.is_file():
            return candidate
    return None


def build_installer() -> int:
    """调用 Inno Setup 把 onedir 产物编成双击安装向导。"""
    iscc = find_iscc()
    if iscc is None:
        print("未找到 Inno Setup 编译器 (ISCC.exe)，跳过安装向导。")
        print("可从 https://jrsoftware.org/isdl.php 安装 Inno Setup 6 后重试。")
        return 1
    cmd = [
        str(iscc),
        f"/DAppVersion={_project_version()}",
        str(ROOT / "packaging" / "installer.iss"),
    ]
    print("运行:", " ".join(cmd))
    return subprocess.call(cmd, cwd=str(ROOT))


def _run_pyinstaller(onedir: bool, no_core: bool) -> int:
    """按指定形态跑一次 PyInstaller，返回其退出码。"""
    env = dict(os.environ)
    env["AGENT_CHANNEL_ONEDIR"] = "1" if onedir else "0"
    if no_core:
        env["AGENT_CHANNEL_NO_CORE"] = "1"
    clean_previous(onedir)
    cmd = [
        sys.executable,
        "-m",
        "PyInstaller",
        "--clean",
        "--noconfirm",
        str(SPEC),
    ]
    print("运行:", " ".join(cmd))
    return subprocess.call(cmd, cwd=str(ROOT), env=env)


def _check_credentials(target: Path) -> int:
    """硬约束：分发产物里绝不能出现本机节点信息。"""
    leaked = verify_no_secrets(target)
    if not leaked:
        print(f"凭据自检（{target.name}）：未包含本地节点信息")
        return 0
    print(f"!! {target.name} 中发现本地节点凭据，已中止：")
    for item in leaked:
        print(f"   - {_mask(item)}")
    return 1


def build_wheel() -> int:
    """构建 pip wheel（供 Agent 拉取调用）。"""
    cmd = [
        sys.executable,
        "-m",
        "pip",
        "wheel",
        ".",
        "-w",
        str(DIST_DIR),
        "--no-deps",
    ]
    print("运行:", " ".join(cmd))
    return subprocess.call(cmd, cwd=str(ROOT))


def _report_artifacts() -> None:
    """列一遍本次产出，避免交付时拿错或拿了旧文件。"""
    version = _project_version()
    print("\n本次产出：")
    names = [
        f"{APP_NAME}-{version}-setup.exe",
        f"{APP_NAME}-{version}-{_platform_tag()}.zip",
        _app_exe_name(),
        f"agent_channel-{version}-py3-none-any.whl",
    ]
    for name in names:
        path = DIST_DIR / name
        if path.is_file():
            print(f"  [文件] {path}  ({path.stat().st_size / 1048576:.1f} MB)")
        else:
            print(f"  [缺失] {path}")
    folder = DIST_DIR / APP_NAME
    if folder.is_dir():
        size = sum(f.stat().st_size for f in folder.rglob("*") if f.is_file())
        print(f"  [目录] {folder}  ({size / 1048576:.1f} MB)")


def build_all() -> int:
    """一条命令按当前代码重建全部可分发产物。

    顺序有讲究：先出带内核的目录形态（安装向导与 zip 都以它为源），
    再出不带内核的精简单文件，最后构建 wheel；
    每一步都带凭据自检，任一步失败就停下来，不交出半套产物。
    """
    print("=== 1/3 目录形态（含内核）+ zip + 安装向导 ===")
    if stage_core() is None:
        print("警告：本地没有可用内核，目录形态将不含内核（首次运行需联网下载）。")
    code = _run_pyinstaller(onedir=True, no_core=False)
    if code != 0:
        return code
    if _check_credentials(DIST_DIR / APP_NAME) != 0:
        return 1
    package_zip()
    if build_installer() != 0:
        return 1

    print("=== 2/3 精简单文件（不含内核）===")
    code = _run_pyinstaller(onedir=False, no_core=True)
    if code != 0:
        return code
    if _check_credentials(DIST_DIR / _app_exe_name()) != 0:
        return 1

    print("=== 3/3 pip wheel ===")
    if build_wheel() != 0:
        return 1

    _report_artifacts()
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="打包 agent-channel 可执行程序")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--onefile", action="store_true", help="强制单文件形态")
    mode.add_argument("--onedir", action="store_true", help="强制目录形态")
    mode.add_argument(
        "--all",
        action="store_true",
        help="按当前代码重建全部产物（目录形态 + zip + 安装向导 + 精简单文件 + wheel）",
    )
    parser.add_argument(
        "--no-core",
        action="store_true",
        help="不预置 sing-box 内核，产出精简包（首次运行需联网下载）",
    )
    parser.add_argument(
        "--no-zip",
        action="store_true",
        help="onedir 产物不打 zip",
    )
    parser.add_argument(
        "--installer",
        action="store_true",
        help="额外用 Inno Setup 编译 Windows 安装向导（需 onedir 产物）",
    )
    args = parser.parse_args()

    if args.all:
        return build_all()

    core = None
    no_core = args.no_core
    if no_core:
        print("已指定 --no-core：构建不带内核的精简包")
    else:
        core = stage_core()

    if args.onefile:
        onedir = False
    elif args.onedir:
        onedir = True
    else:
        # 带内核时单文件启动代价过高，自动选目录形态
        onedir = core is not None
        if onedir:
            print(
                "检测到将预置内核，自动选择 onedir 形态（单文件会每次解包内核，启动约 8 秒）"
            )

    code = _run_pyinstaller(onedir=onedir, no_core=no_core)
    if code != 0:
        return code

    target = (DIST_DIR / APP_NAME) if onedir else (DIST_DIR / _app_exe_name())
    if _check_credentials(target) != 0:
        return 1

    if onedir and not args.no_zip:
        package_zip()

    if args.installer:
        if not onedir:
            print(
                "安装向导基于 onedir 目录产物，已跳过（请去掉 --no-core / --onefile）"
            )
            return 0
        return build_installer()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
