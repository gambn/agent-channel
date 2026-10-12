"""跨平台打包脚本。

用法：
    python packaging/build.py              # 自动选形态（带内核时用 onedir）
    python packaging/build.py --onefile    # 强制单文件
    python packaging/build.py --onedir     # 强制目录形态
    python packaging/build.py --no-core    # 不预置内核（首次运行需联网下载）
    python packaging/build.py --all        # 按当前代码重建全部可分发产物

`--all` 是最省心的交付入口：一次串行产出目录形态（含内核）+ 压缩包 + 精简单文件 +
pip wheel，并对每份产物做凭据自检；其余形态开关会被忽略（由 --all 自行决定）。
压缩包与安装形态按平台分叉：Windows 是 zip + Inno Setup 安装向导，Linux/macOS 是
tar.gz（内含 install.sh），Linux 另有 .deb。

形态选择理由：
- onefile 形态每次启动都需把打包内容解包到临时目录，带内核时启动明显变慢；
  因此带内核默认产出 onedir 目录，启动无解包开销。

说明：
- 内核用官方预编译文件，全程零本地编译；
- 预置内核只在「本地尚无内核」时被播种，不会覆盖用户已更新的版本。
"""

import argparse
import hashlib
import io
import json
import lzma
import os
import re
import shutil
import subprocess
import sys
import tarfile
import zipfile
from collections.abc import Iterator
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SPEC = ROOT / "packaging" / "agent-channel.spec"
VENDOR_DIR = ROOT / "packaging" / "vendor"
DIST_DIR = ROOT / "dist"
APP_NAME = "agent-channel"

# POSIX 产物的 glibc 下限：由构建环境决定，不是拍出来的偏好值。
# 2.31 = Debian 11 / Ubuntu 20.04 的 glibc，也是 python:3.10-slim-bullseye 构建环境的
# 下限；换构建环境（例如改用 ubuntu:22.04 容器）时必须同步改这里，并重跑
# `readelf --version-info` 断言，避免 .deb 的依赖声明与产物实际要求脱节。
POSIX_GLIBC_FLOOR = "2.31"

# `python packaging/build.py` 时 sys.path[0] 是 `packaging/`，而 `agent_channel` 包在
# 仓库根下——文档里就是这么调的，所以显式补上根目录，别让"照着文档跑"变成
# ModuleNotFoundError（`_path_()`/`stage_core()` 在调用期才 import agent_channel）。
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# ---- 内置内核清单（B 强制 / A 便利） ----
#
# B（强制）：打包前逐字节比对暂存内核与清单，不匹配即中止。两层目的：
#   1. 分发产物里的内核必须是**官方原件**，不是来路不明或被替换的文件；
#   2. “内置内核是哪一版”从此可复现——换台机器打包，装的还是同一份官方文件。
# A（便利）：暂存缺件时按清单里的官方 URL 下载并校验（digest 取自官方 release 资产）。
#
# 随包形态按官方资产类型分叉（`_bundle_form`）：
#   - Windows 资产是 `.zip`：外层压缩器（Inno 的 LZMA2 / zip 的 deflate）已经把这
#     93 MB 压到 ~30 MB，再预压一层它压不动，setup.exe 反而会变大 → 原样随包；
#   - Linux/macOS 资产是 `.tar.gz`：外层是 zlib/gzip，实测预压成 xz 能省 30–40%
#     （linux-amd64 30.2→18.2 MB、darwin-arm64 27.8→18.6 MB）→ 重压成 `.tar.xz` 随包。
# 两条路径共用同一道纪律：**先按 archive_sha256 验官方归档，再按 files 逐文件验真，
# 通过之后才允许重压**；重压只是容器换了，解出来的文件仍须与官方逐字节一致。
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


def _onefile_name() -> str:
    """精简单文件的产物名（与 spec 里的 `ONEFILE_NAME` 必须一致）。

    Windows 靠 `.exe` 区分两个形态；POSIX 没有后缀，若都叫 `agent-channel`，
    PyInstaller 的 `--noconfirm` 会把先建好的目录形态整个删掉——实测踩过：
    `dist/agent-channel/` 被单文件覆盖，目录产物在构建末尾凭空消失。
    """
    return f"{APP_NAME}.exe" if os.name == "nt" else f"{APP_NAME}-slim"


def _target_arch() -> str | None:
    """交叉构建的目标架构（macOS 用）。

    宿主是 Intel、目标却是 Apple Silicon 时，PyInstaller 必须被告知往哪交叉
    （`target_arch`）；不给就按宿主架构出包。取值与 PyInstaller 一致：
    `x86_64` / `arm64`。**这也是清单取键的依据**——不设就会在 Intel 上给 arm64 包
    打进 darwin-amd64 的内核，产物名字与内容对不上。
    """
    value = os.environ.get("AGENT_CHANNEL_TARGET_ARCH", "").strip()
    return value or None


_TARGET_ARCH_CLASS = {"x86_64": "amd64", "arm64": "arm64"}


def _arch_class() -> str:
    """本次构建的架构类名（清单键、产物名都用它）。"""
    from agent_channel.binary import platform_arch

    target = _target_arch()
    if target is None:
        return platform_arch()
    mapped = _TARGET_ARCH_CLASS.get(target)
    if mapped is None:
        raise SystemExit(
            f"[build] 不认识的 AGENT_CHANNEL_TARGET_ARCH: {target}"
            "（只支持 x86_64 / arm64；universal2 需要包里同时放两份内核，尚未实现）"
        )
    return mapped


def _platform_key() -> str:
    """当前平台在清单里的键（复用 agent_channel 的分类器，不另造一套）。"""
    from agent_channel.binary import platform_os

    return f"{platform_os()}-{_arch_class()}"


def _bundle_form(entry: dict) -> str:
    """随包形态：`archive` = 官方归档原样，`xz` = 重压成 `.tar.xz`。

    依据是官方资产类型，不是平台名：`.zip`（Windows）的外层容器已经压过一遍，
    `.tar.gz`（Linux/macOS）只是 gzip，重压成 xz 有 30–40% 的实测收益。
    """
    return "archive" if _archive_name(entry).endswith(".zip") else "xz"


def _bundle_name(entry: dict) -> str:
    """随包文件名：官方归档名，或 POSIX 侧的 `.tar.xz` 名。"""
    if _bundle_form(entry) == "archive":
        return _archive_name(entry)
    return _archive_name(entry)[: -len(".tar.gz")] + ".tar.xz"


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _archive_name(entry: dict) -> str:
    """清单条目对应的官方归档文件名（取自 URL，不留第二份口径）。"""
    return entry["url"].split("?")[0].rsplit("/", 1)[-1]


def _bundle_ok(entry: dict) -> bool:
    """暂存里是否已经是本平台该有的那一份：文件名对、且成员逐一对上清单。"""
    name = _bundle_name(entry)
    if _staged_file_set() != {name}:
        return False
    try:
        return _member_hashes(VENDOR_DIR / name) == dict(entry["files"])
    except (OSError, tarfile.TarError, zipfile.BadZipFile, lzma.LZMAError):
        return False


def _prepare_bundle(entry: dict) -> None:
    """把暂存里的官方归档重压成 `.tar.xz`，并清掉官方归档（两个都留会一起进包）。

    顺序是刻意的：**先验真再重压**。先逐文件核对清单，再压——这样"包里那份内核"
    从源头到成品都在清单的覆盖下，重压只是换了容器。
    """
    official = VENDOR_DIR / _archive_name(entry)
    if not official.is_file():
        raise SystemExit(f"[build] 暂存里没有官方归档 {official.name}，无法重压。")
    members = _read_members(official)
    hashes = {name: hashlib.sha256(body).hexdigest() for name, body in members}
    problems = [
        f"  {name} 哈希不符"
        for name, expected in entry["files"].items()
        if hashes.get(name) != expected
    ]
    extra = sorted(set(hashes) - set(entry["files"]))
    if extra:
        problems.append(f"  官方归档里有清单未列出的文件: {extra}")
    if problems:
        raise SystemExit(
            f"[build] 官方归档 {official.name} 与清单不符，已中止重压：\n"
            + "\n".join(problems)
        )
    official_mb = official.stat().st_size / 1048576
    target = VENDOR_DIR / _bundle_name(entry)
    _write_xz_bundle(members, target)
    official.unlink()
    print(
        f"[build] 已重压为 {target.name}：{target.stat().st_size / 1048576:.1f} MB"
        f"（官方归档 {official_mb:.1f} MB，省 "
        f"{official_mb - target.stat().st_size / 1048576:.1f} MB）"
    )


def verify_vendor_core(entry: dict) -> None:
    """B（强制）：暂存内核必须与清单逐字节一致，否则中止打包。

    两种随包形态共用同一道"逐文件对清单"的校验，官方归档原样随包时再加一道
    `archive_sha256`（那一份的字节数就是官方资产的字节数，能直接对）。

    另外拒绝任何**运行态配置**混进暂存目录：`active_config.json` 之类含真实节点凭据，
    一旦随包发出去就是凭据泄露。官方资产里不含 `.json`，所以按后缀一律拒绝。
    """
    runtime_state = sorted(p.name for p in VENDOR_DIR.glob("*.json"))
    if runtime_state:
        raise SystemExit(
            f"[build] 暂存目录含运行态配置文件 {runtime_state}——它们可能带真实凭据，"
            "已中止打包。请清掉后重跑。"
        )

    expected_name = _bundle_name(entry)
    staged = sorted(_staged_file_set())
    if staged != [expected_name]:
        raise SystemExit(
            f"[build] {_platform_key()} 的暂存目录应恰好包含 {expected_name}，"
            f"实际: {staged}。请重跑以重新暂存。"
        )

    bundle = VENDOR_DIR / expected_name
    if _bundle_form(entry) == "archive":
        actual = _sha256_file(bundle)
        if actual != entry["archive_sha256"]:
            raise SystemExit(
                f"[build] 暂存归档 {bundle.name} 的 sha256 不符，已中止打包：\n"
                f"  实际: {actual}\n  期望: {entry['archive_sha256']}"
            )

    problems: list[str] = []
    try:
        members = _member_hashes(bundle)
    except (OSError, tarfile.TarError, zipfile.BadZipFile, lzma.LZMAError) as exc:
        raise SystemExit(f"[build] 读不出暂存内核 {bundle.name}: {exc}") from exc
    for name, expected in entry["files"].items():
        got = members.get(name)
        if got is None:
            problems.append(f"  缺文件: {name}")
        elif got != expected:
            problems.append(
                f"  {name} 哈希不符\n    实际: {got}\n    期望: {expected}"
            )
    extra = sorted(set(members) - set(entry["files"]))
    if extra:
        problems.append(f"  有清单未列出的文件: {extra}")
    if problems:
        raise SystemExit(
            f"[build] 内置内核 v{entry['version']} 校验未通过，已中止打包：\n"
            + "\n".join(problems)
            + "\n  若确实要升级内核，请同时更新 CORE_MANIFEST 的版本号、"
            "archive_sha256 与 files 里的各 sha256。"
        )
    print(
        f"[build] 内置内核校验通过：v{entry['version']}"
        f"（{bundle.name} + {len(entry['files'])} 个文件逐一对上）"
    )


def _read_members(path: Path) -> list[tuple[str, bytes]]:
    """读出归档里的成员（纯文件名 + 内容）；zip 与各种 tar 压缩都支持。"""
    members: list[tuple[str, bytes]] = []
    if path.name.endswith(".zip"):
        with zipfile.ZipFile(path) as archive:
            for info in archive.infolist():
                name = Path(info.filename).name
                if info.is_dir() or not name:
                    continue
                members.append((name, archive.read(info.filename)))
        return members
    with tarfile.open(path, mode="r:*") as archive:
        for member in archive.getmembers():
            if not member.isfile():
                continue
            name = Path(member.name).name
            if not name:
                continue
            source = archive.extractfile(member)
            if source is not None:
                members.append((name, source.read()))
    return members


def _member_hashes(path: Path) -> dict[str, str]:
    return {
        name: hashlib.sha256(body).hexdigest() for name, body in _read_members(path)
    }


def _write_xz_bundle(members: list[tuple[str, bytes]], target: Path) -> None:
    """把成员按**规范化元数据**写成 `tar.xz`。

    规范化是有意的：成员名排序、mtime 归零、uid/gid 归零、去掉 uname/gname，
    于是同一份官方归档在任何机器、任何用户名、任何时刻重压出来的内容都一致
    （可以复算比对），也不会把构建机的痕迹带进包里。
    """
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w", format=tarfile.USTAR_FORMAT) as bundle:
        for name, body in sorted(members):
            info = tarfile.TarInfo(name)
            info.size = len(body)
            info.mode = 0o644
            info.mtime = 0
            info.uid = 0
            info.gid = 0
            info.uname = ""
            info.gname = ""
            bundle.addfile(info, io.BytesIO(body))
    target.write_bytes(lzma.compress(buffer.getvalue(), preset=9))


def _extract_official_archive(blob: bytes, url: str, target_dir: Path) -> None:
    """把官方归档里的内核与伴生文件解到暂存目录。

    Windows 资产是 `.zip`，Linux/macOS 资产是 `.tar.gz`——只按 zip 解在 POSIX 上会直接
    抛 `BadZipFile`（`--all` 连构建那一步都到不了）。解包只取**纯文件名**、扁平落盘：
    归档里的目录层级与链接一律不进暂存，既避免路径穿越，也与 zip 那条路径口径一致。
    """
    lower = url.lower().split("?")[0]
    if lower.endswith(".zip"):
        with zipfile.ZipFile(io.BytesIO(blob)) as archive:
            for info in archive.infolist():
                name = Path(info.filename).name
                if not name or info.is_dir():
                    continue
                # 读进内存再落盘（最大单文件 82MB），避开 `open()` 的未处理抛错检查；
                # 真正的写失败会被上层捕获并以非零码终止打包。
                (target_dir / name).write_bytes(archive.read(info))
                print(f"[build] 已解出: {name}")
        return

    if lower.endswith((".tar.gz", ".tgz", ".tar.xz", ".tar.bz2", ".tar")):
        with tarfile.open(fileobj=io.BytesIO(blob), mode="r:*") as archive:
            for member in archive.getmembers():
                name = Path(member.name).name
                if not name or not member.isfile():
                    continue
                source = archive.extractfile(member)
                if source is None:
                    continue
                (target_dir / name).write_bytes(source.read())
                print(f"[build] 已解出: {name}")
        return

    raise SystemExit(f"[build] 认不出官方归档格式，已中止打包: {url}")


def fetch_official_core(entry: dict) -> None:
    """A（便利）：暂存缺件时按清单里的官方 URL 下载**归档原件**并校验 sha256。

    刻意不解包：包里带的就是官方那一份压缩原件，解包交给首次启动的
    `agent_channel.binary.ensure_local_core()`。这样"包内内核 == 官方资产"在任何平台上
    都能用 sha256 直接核验（解开的 Mach-O 在 macOS 上会被 codesign 重签，就没法这么查了）。
    """
    import urllib.request

    name = _archive_name(entry)
    print(f"[build] 按官方 URL 下载 v{entry['version']}: {entry['url']}")
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
    (VENDOR_DIR / name).write_bytes(blob)
    print(f"[build] 已暂存官方归档: {name} ({len(blob) / 1048576:.1f} MB)")


def _staged_file_set() -> set[str]:
    """暂存目录里的文件名集合（不存在时为空集）。"""
    if not VENDOR_DIR.is_dir():
        return set()
    return {p.name for p in VENDOR_DIR.iterdir() if p.is_file()}


def _archive_matches(entry: dict) -> bool:
    """暂存里**恰好**只有本平台清单那一份归档，且 sha256 相符。"""
    if _staged_file_set() != {_archive_name(entry)}:
        return False
    return _sha256_file(VENDOR_DIR / _archive_name(entry)) == entry["archive_sha256"]


def _clear_vendor() -> list[str]:
    """清空暂存目录里的文件，返回被清掉的名字。

    换平台或换架构构建时，上一轮的残留会挡在这里：归档文件名不同（`-linux-amd64` 与
    `-darwin-arm64`），不清理就会同时打进包里，或者让校验读错那一份。
    """
    removed = sorted(_staged_file_set())
    for name in removed:
        try:
            (VENDOR_DIR / name).unlink()
        except OSError as exc:
            print(f"[build] 清理暂存文件失败（忽略）: {name}: {exc}")
    return removed


def stage_core() -> Path | None:
    """准备打包用的内核，做 B 强制校验；返回随包文件路径（无条目时 None）。

    随包形态由 `_bundle_form` 决定：Windows 原样放官方 `.zip`，Linux/macOS 放重压后的
    `.tar.xz`（实测省 30–40%，Windows 侧外层压缩器已经压过一遍、预压无收益还会变大）。
    两条路都遵守同一道纪律——**先按 archive_sha256 验官方归档，再按 files 逐文件验真，
    通过之后才允许重压**；解包交给首次启动的 `ensure_local_core()`。

    暂存一律以清单为准（哈希比对），不看"文件在不在"：换平台或换架构构建时上一轮的
    残留还在，不清就会把别的平台/架构的内核打进包里。
    """
    entry = CORE_MANIFEST.get(_platform_key())
    if entry is None:
        stale = sorted(_staged_file_set())
        if stale:
            raise SystemExit(
                f"[build] {_platform_key()} 没有内核清单条目，但暂存目录里已有内核文件 "
                f"({stale})，无法校验——请补清单或清掉暂存。"
            )
        print(f"[build] {_platform_key()} 无内置内核清单，本次构建不带内核。")
        return None

    if _bundle_ok(entry):
        print(f"[build] 暂存内核与 {_platform_key()} 清单一致，直接复用")
    else:
        official = VENDOR_DIR / _archive_name(entry)
        reusable_official = (
            _bundle_form(entry) == "xz"
            and official.is_file()
            and _sha256_file(official) == entry["archive_sha256"]
        )
        if reusable_official:
            print(f"[build] 暂存里已有校验通过的官方归档 {official.name}，直接重压复用")
        else:
            stale = _clear_vendor()
            if stale:
                print(f"[build] 暂存目录与 {_platform_key()} 清单不符，已清理: {stale}")
            fetch_official_core(entry)

    if _bundle_form(entry) == "xz" and not (VENDOR_DIR / _bundle_name(entry)).is_file():
        _prepare_bundle(entry)

    verify_vendor_core(entry)
    bundle = VENDOR_DIR / _bundle_name(entry)
    return bundle if bundle.is_file() else None


def clean_previous(onedir: bool) -> None:
    """只清理本次将要产出的同形态旧产物，避免误删另一形态的产物。"""
    if onedir:
        folder = DIST_DIR / APP_NAME
        if folder.is_file():
            # 同名文件挡在目录形态的位置上会让 COLLECT 直接失败（POSIX 侧曾因两个形态
            # 撞名把目录换成单文件），这里一并清掉。
            try:
                folder.unlink()
            except OSError as exc:
                print(f"清理同名旧产物失败（忽略）: {exc}")
            return
        if not folder.is_dir():
            return
        try:
            shutil.rmtree(folder, ignore_errors=True)
        except OSError as exc:
            print(f"清理旧目录失败（忽略）: {exc}")
        return

    exe_file = DIST_DIR / _onefile_name()
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


def _iter_ar_members(path: Path) -> Iterator[tuple[str, bytes]]:
    """读 ar 容器的成员（`.deb` 就是 ar：`debian-binary` + `control.tar.*` + `data.tar.*`）。"""
    try:
        data = path.read_bytes()
    except OSError:
        return
    if not data.startswith(b"!<arch>\n"):
        return
    offset = 8
    while offset + 60 <= len(data):
        header = data[offset : offset + 60]
        name = header[0:16].decode("utf-8", "replace").strip()
        try:
            size = int(header[48:58].decode("utf-8", "replace").strip())
        except ValueError:
            return
        offset += 60
        yield name.rstrip("/"), data[offset : offset + size]
        offset += size + (size % 2)


def _iter_archive_entries(path: Path) -> Iterator[tuple[str, bytes]]:
    """逐个产出压缩包内的条目（名字, 内容）。

    凭据检查必须**进压缩包**：直接扫压缩字节会被压缩掩盖，等于没查。`.deb` 是 ar 容器
    再套一层 tar，所以先读 ar 成员、再按内层 tar 的压缩格式解开。
    """
    lower = path.name.lower()
    if lower.endswith(".zip"):
        with zipfile.ZipFile(path) as archive:
            for info in archive.infolist():
                if not info.is_dir():
                    yield info.filename, archive.read(info.filename)
        return
    if lower.endswith((".tar.gz", ".tgz", ".tar.xz", ".tar.bz2", ".tar")):
        with tarfile.open(path, mode="r:*") as archive:
            for member in archive.getmembers():
                if not member.isfile():
                    continue
                source = archive.extractfile(member)
                if source is not None:
                    yield member.name, source.read()
        return
    if lower.endswith(".deb"):
        for name, blob in _iter_ar_members(path):
            if not name.startswith(("data.tar", "control.tar")):
                continue
            with tarfile.open(fileobj=io.BytesIO(blob), mode="r:*") as archive:
                for member in archive.getmembers():
                    if not member.isfile():
                        continue
                    source = archive.extractfile(member)
                    if source is not None:
                        yield f"{name}:{member.name}", source.read()


def verify_no_secrets(target: Path) -> list[str]:
    """扫描分发产物，确认不含任何本地节点凭据；返回命中的敏感值。"""
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
        try:
            for _name, blob in _iter_archive_entries(path):
                _scan_bytes(blob, needles, hits)
        except (OSError, tarfile.TarError, zipfile.BadZipFile, EOFError) as exc:
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
    """产物平台标签（如 windows-x64 / linux-armv7 / darwin-arm64）。"""
    from agent_channel.binary import platform_os

    arch_class = _arch_class()
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


def package_tarball(payload: Path | None = None) -> Path | None:
    """把 onedir 产物打成 tar.gz（POSIX 侧的分发形态），并带上 install.sh。

    形态与 zip 对齐：包内顶层就是载荷目录本身（Linux 是 `agent-channel/`，macOS 是
    `agent-channel.app/`），`install.sh` 与它同级，用户解包后直接 `./install.sh` 即可，
    不需要系统 Python，也不需要 root。
    """
    if payload is not None:
        # macOS：载荷是 .app，tar.gz 里带的就是整个 bundle
        folder = payload
        top = folder.name
    else:
        folder = DIST_DIR / APP_NAME
        top = APP_NAME
    if not folder.is_dir():
        return None

    archive = DIST_DIR / f"{APP_NAME}-{_project_version()}-{_platform_tag()}.tar.gz"
    assets = ROOT / "packaging" / "assets"
    extras = [(ROOT / "packaging" / "install.sh", "install.sh", 0o755)]
    if sys.platform != "darwin":
        # 菜单项与图标只对 Linux 有意义（macOS 由 .app 自带 Info.plist 与图标）
        extras += [
            (assets / "agent-channel.desktop", f"{APP_NAME}.desktop", 0o644),
            (assets / "icon-512.png", f"{APP_NAME}.png", 0o644),
        ]
    if archive.exists():
        try:
            archive.unlink()
        except OSError as exc:
            print(f"覆盖旧压缩包失败: {exc}")
            return None

    raw_bytes = 0
    with tarfile.open(archive, "w:gz") as bundle:
        for path in sorted(folder.rglob("*")):
            if not path.is_file():
                continue
            bundle.add(path, arcname=f"{top}/{path.relative_to(folder)}")
            raw_bytes += path.stat().st_size
        for source, name, mode in extras:
            if not source.is_file():
                print(f"警告: 未找到 {source}，压缩包将不含 {name}")
                continue
            info = bundle.gettarinfo(str(source), arcname=name)
            info.mode = mode
            with source.open("rb") as handle:
                bundle.addfile(info, handle)
            raw_bytes += source.stat().st_size

    packed_mb = archive.stat().st_size / 1048576
    print(
        f"已打包: {archive.name} "
        f"({packed_mb:.1f} MB，原始 {raw_bytes / 1048576:.1f} MB)"
    )
    return archive


_DEB_ARCH = {
    "386": "i386",
    "amd64": "amd64",
    "arm64": "arm64",
    "armv6": "armel",
    "armv7": "armhf",
    "riscv64": "riscv64",
}


def _deb_control(version: str, arch: str, installed_kb: int) -> str:
    """生成 DEBIAN/control（Description 必须是最后一个字段，续行才不会被截断）。

    `Depends: libc6 (>= …)` 写的是构建环境的 glibc 下限（POSIX_GLIBC_FLOOR）：老发行版
    在 apt 阶段就被明确拒绝，而不是装完才报“找不到加载器”这种玄学错误。
    """
    return (
        f"Package: {APP_NAME}\n"
        f"Version: {version}\n"
        f"Architecture: {arch}\n"
        "Maintainer: gambn\n"
        "Section: net\n"
        "Priority: optional\n"
        f"Depends: libc6 (>= {POSIX_GLIBC_FLOOR})\n"
        f"Installed-Size: {installed_kb}\n"
        "Homepage: https://github.com/gambn/agent-channel\n"
        "Description: agent-channel 通道代理（本地 sing-box 通道 + 控制面板）\n"
        " 随包预置官方编译的 sing-box 内核，装完即用，不依赖系统 Python。\n"
    )


def _deb_stage(arch: str) -> tuple[Path, int]:
    """摆出 .deb 的文件树，返回（暂存根, 载荷 KB）。

    程序整体进 `/usr/lib/agent-channel`，`/usr/bin/agent-channel` 只放相对软链；
    面板与图标按 FHS 落位；用户数据仍然只写 `$HOME/.agent_channel`。
    """
    staging = ROOT / "build" / f"deb-{arch}"
    if staging.exists():
        shutil.rmtree(staging)
    lib_dir = staging / "usr" / "lib" / APP_NAME
    dirs = (
        lib_dir,
        staging / "usr" / "bin",
        staging / "usr" / "share" / "applications",
        staging / "usr" / "share" / "icons" / "hicolor" / "512x512" / "apps",
        staging / "DEBIAN",
    )
    for path in dirs:
        path.mkdir(parents=True, exist_ok=True)

    payload_bytes = 0
    source_root = DIST_DIR / APP_NAME
    # onedir 里有 `_internal/agent_channel/web/` 这类嵌套目录，直接 copy2 会在父目录
    # 不存在时抛 FileNotFoundError（实测踩过一次），所以整棵树一起拷。
    shutil.copytree(source_root, lib_dir, dirs_exist_ok=True)
    for path in source_root.rglob("*"):
        if path.is_file():
            payload_bytes += path.stat().st_size

    link = staging / "usr" / "bin" / APP_NAME
    link.unlink(missing_ok=True)
    link.symlink_to(Path("..") / "lib" / APP_NAME / APP_NAME)

    assets = ROOT / "packaging" / "assets"
    for asset, target in (
        (assets / "agent-channel.desktop", staging / "usr" / "share" / "applications"),
        (assets / "icon-512.png", staging / "usr" / "share" / "icons" / "hicolor" / "512x512" / "apps"),
    ):
        if not asset.is_file():
            print(f"警告: 未找到 {asset}，对应的菜单项/图标将不被安装")
            continue
        name = f"{APP_NAME}.png" if asset.suffix == ".png" else asset.name
        dest = target / name
        shutil.copy2(asset, dest)
        # 源文件在仓库里的权限不该泄漏到 /usr/share：实测桌面上出现过 0600 的
        # .desktop（只有 root 能读，等于没装），这里统一按可读收紧一次。
        dest.chmod(0o644)
    return staging, payload_bytes // 1024


def build_deb() -> int:
    """把 onedir 产物装成 .deb（apt/双击安装形态）。"""
    if not (DIST_DIR / APP_NAME).is_dir():
        print("未找到 onedir 产物，跳过 .deb（请先构建目录形态）")
        return 1
    dpkg_deb = shutil.which("dpkg-deb")
    if dpkg_deb is None:
        print("未找到 dpkg-deb，跳过 .deb 打包。")
        return 1

    from agent_channel.binary import platform_arch

    arch_class = platform_arch()
    arch = _DEB_ARCH.get(arch_class)
    if arch is None:
        print(f"[deb] 认不出 Debian 架构名: {arch_class}，跳过 .deb。")
        return 1

    version = _project_version()
    staging, installed_kb = _deb_stage(arch)
    (staging / "DEBIAN" / "control").write_text(
        _deb_control(version, arch, installed_kb), encoding="utf-8"
    )
    deb = DIST_DIR / f"{APP_NAME}_{version}_{arch}.deb"
    cmd = [dpkg_deb, "--build", "--root-owner-group", str(staging), str(deb)]
    print("运行:", " ".join(cmd))
    code = subprocess.call(cmd)
    if code != 0:
        return code
    print(f"已打包: {deb.name} ({deb.stat().st_size / 1048576:.1f} MB)")
    return 0


_ICNS_SIZES = ((16, 1), (16, 2), (32, 1), (32, 2), (128, 1), (128, 2), (256, 1), (256, 2), (512, 1), (512, 2))
_ICNS_MEMO: dict[str, Path | None] = {}


def build_icns() -> Path | None:
    """从 `icon-1024.png` 生成 `.icns`（macOS 自带的 sips + iconutil，不需要 Pillow）。

    生成物放 `build/` 下、不写进仓库；spec 通过 `AGENT_CHANNEL_ICNS` 取它。
    `iconutil` 要求一个规矩的 `.iconset` 目录（每档尺寸单独出图，不能只放一张大图）。
    """
    if sys.platform != "darwin":
        return None
    if "value" in _ICNS_MEMO:
        return _ICNS_MEMO["value"]

    source = ROOT / "packaging" / "assets" / "icon-1024.png"
    sips = shutil.which("sips")
    iconutil = shutil.which("iconutil")
    result: Path | None = None
    if source.is_file() and sips and iconutil:
        work = ROOT / "build" / "macos"
        iconset = work / "icon.iconset"
        if iconset.exists():
            shutil.rmtree(iconset, ignore_errors=True)
        iconset.mkdir(parents=True, exist_ok=True)
        for size, scale in _ICNS_SIZES:
            pixels = size * scale
            suffix = "@2x" if scale == 2 else ""
            name = f"icon_{size}x{size}{suffix}.png"
            subprocess.run(
                [sips, "-z", str(pixels), str(pixels), str(source), "--out", str(iconset / name)],
                capture_output=True,
                check=False,
            )
        target = work / "icon.icns"
        code = subprocess.run(
            [iconutil, "-c", "icns", str(iconset), "-o", str(target)],
            capture_output=True,
            check=False,
        ).returncode
        if code == 0 and target.is_file():
            result = target
            print(f"[icns] 已生成 {target.relative_to(ROOT)}")
        else:
            print("[icns] iconutil 生成失败，本次产物将不带应用图标")
    else:
        print("[icns] 缺少 icon-1024.png 或 sips/iconutil，本次产物将不带应用图标")

    _ICNS_MEMO["value"] = result
    return result


def build_dmg() -> int:
    """把 `.app` 装进 `.dmg`（macOS 侧的双击安装形态）。

    形态沿用惯例：卷里就是 `agent-channel.app` 加一个指向 `/Applications` 的软链，
    用户拖进去即可。压缩格式 UDZO（zlib），与 UDRW 相比体积小一个量级。
    """
    if sys.platform != "darwin":
        print("非 macOS，跳过 .dmg。")
        return 1
    app = DIST_DIR / f"{APP_NAME}.app"
    if not app.is_dir():
        print(f"未找到 {app.name}，跳过 .dmg（请先构建 .app）")
        return 1
    hdiutil = shutil.which("hdiutil")
    if hdiutil is None:
        print("未找到 hdiutil，跳过 .dmg。")
        return 1

    version = _project_version()
    staging = ROOT / "build" / "dmg"
    if staging.exists():
        shutil.rmtree(staging, ignore_errors=True)
    staging.mkdir(parents=True)
    shutil.copytree(app, staging / app.name, symlinks=True)
    (staging / "Applications").symlink_to("/Applications")

    dmg = DIST_DIR / f"{APP_NAME}-{version}-{_platform_tag()}.dmg"
    if dmg.exists():
        try:
            dmg.unlink()
        except OSError as exc:
            print(f"覆盖旧 .dmg 失败: {exc}")
            return 1
    cmd = [
        hdiutil,
        "create",
        "-volname",
        f"{APP_NAME} {version}",
        "-srcfolder",
        str(staging),
        "-ov",
        "-format",
        "UDZO",
        str(dmg),
    ]
    print("运行:", " ".join(cmd))
    code = subprocess.call(cmd)
    if code != 0:
        return code
    print(f"已打包: {dmg.name} ({dmg.stat().st_size / 1048576:.1f} MB)")

    # 容器也要验：以前只验了 .app 本身，dmg 只 create 不 verify。
    # 校验和是硬判据（不通过就中止）；挂载比对是软判据——`hdiutil attach` 在真机上
    # 偶发"无此文件或目录"（同一条命令重试即可），不能让它把一次正确的构建判死。
    verify = subprocess.run(
        [hdiutil, "verify", str(dmg)], capture_output=True, text=True, check=False
    )
    if verify.returncode != 0:
        print(f"!! dmg 校验和不通过，已中止：\n{verify.stdout.strip()}")
        return 1
    print("dmg 校验和有效")

    mounted = ROOT / "build" / "dmg-mnt"
    if mounted.exists():
        shutil.rmtree(mounted, ignore_errors=True)
    mounted.mkdir(parents=True)
    attach = subprocess.run(
        [hdiutil, "attach", "-nobrowse", "-readonly", "-mountpoint", str(mounted), str(dmg)],
        capture_output=True,
        text=True,
        check=False,
    )
    if attach.returncode != 0:
        print(f"（挂载比对跳过：{attach.stderr.strip() or 'hdiutil attach 失败'}）")
        return 0
    try:
        problems = _compare_trees(app, mounted / app.name)
        if problems:
            print("!! 卷内 .app 与构建产物不一致，已中止：")
            for item in problems[:10]:
                print(f"   - {item}")
            return 1
        print(f"卷内 .app 与 {app.name} 逐文件一致")
    finally:
        subprocess.run([hdiutil, "detach", str(mounted)], capture_output=True, check=False)
    return 0


def _compare_trees(left: Path, right: Path) -> list[str]:
    """两棵目录树是否逐文件一致（文件名 + sha256），返回差异描述。"""
    def snapshot(root: Path) -> dict[str, str]:
        out: dict[str, str] = {}
        for path in root.rglob("*"):
            if path.is_file() and not path.is_symlink():
                out[str(path.relative_to(root))] = _sha256_file(path)
        return out

    if not right.is_dir():
        return [f"卷内缺少 {right.name}"]
    a, b = snapshot(left), snapshot(right)
    problems = [f"只在产物里: {name}" for name in sorted(set(a) - set(b))]
    problems += [f"只在卷内: {name}" for name in sorted(set(b) - set(a))]
    problems += [
        f"{name} 内容不一致"
        for name in sorted(set(a) & set(b))
        if a[name] != b[name]
    ]
    return problems


def _window_toolkit_items(tree: Path) -> list[str]:
    """产物里与图形界面有关的文件（tkinter 扩展模块 + Tcl/Tk 库与脚本目录）。"""
    items: list[str] = []
    if not tree.exists():
        return items
    for path in tree.rglob("*"):
        # 命中任一"路径分量"即算：libtcl9.0.dylib、_tkinter....so、tcl9.0/ 目录及其下脚本
        parts = [part.lower() for part in path.relative_to(tree).parts]
        if any(
            part.startswith(("_tkinter", "libtcl", "libtk", "tcl", "tk"))
            for part in parts
        ):
            items.append(str(path.relative_to(tree)))
    return items


def assert_window_toolkit(app: Path) -> None:
    """macOS 承诺有原生窗口：产物里找不到 Tcl/Tk 就中止打包。

    这是防"静默退化"：构建机换了、tcl/tk 没收进包，产物照样能出来，只是窗口没了、
    用户只能看到浏览器面板——那种事必须让构建失败，而不是等用户发现。
    """
    items = _window_toolkit_items(app)
    if not items:
        raise SystemExit(
            "[build] macOS 产物里没有 Tcl/Tk（tkinter）——窗口是 macOS 的承诺，"
            "缺了会静默退化成只开面板，已中止打包。"
        )
    print(f"[build] 窗口组件已随包：{len(items)} 项（tkinter/Tcl/Tk）")


def report_window_toolkit(tree: Path) -> None:
    """非 macOS 侧如实报告：Linux 产物按设计不带原生窗口（CLI + 浏览器面板）。"""
    items = _window_toolkit_items(tree)
    if items:
        print(f"[build] 图形界面组件随包 {len(items)} 项（本机构建环境含 Tcl/Tk）")
    else:
        print(
            "[build] 图形界面组件未随包：Linux 产物按设计为 CLI + 浏览器面板，"
            "面板底部有「退出程序」按钮（见 README 的平台差异说明）"
        )


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


def _run_pyinstaller(
    onedir: bool, no_core: bool, core_archive: Path | None = None
) -> int:
    """按指定形态跑一次 PyInstaller，返回其退出码。"""
    env = dict(os.environ)
    env["AGENT_CHANNEL_ONEDIR"] = "1" if onedir else "0"
    if no_core:
        env["AGENT_CHANNEL_NO_CORE"] = "1"
    elif core_archive is not None:
        env["AGENT_CHANNEL_BUNDLED_CORE"] = str(core_archive)
    icns = build_icns()
    if icns is not None:
        env["AGENT_CHANNEL_ICNS"] = str(icns)
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
    if os.name == "nt":
        names = [
            f"{APP_NAME}-{version}-setup.exe",
            f"{APP_NAME}-{version}-{_platform_tag()}.zip",
            _onefile_name(),
        ]
    elif sys.platform == "darwin":
        names = [
            f"{APP_NAME}-{version}-{_platform_tag()}.dmg",
            f"{APP_NAME}-{version}-{_platform_tag()}.tar.gz",
            _onefile_name(),
        ]
    else:
        names = [
            f"{APP_NAME}-{version}-{_platform_tag()}.tar.gz",
            f"{APP_NAME}_{version}_{_DEB_ARCH.get(_arch_class(), 'noarch')}.deb",
            _onefile_name(),
        ]
    names.append(f"agent_channel-{version}-py3-none-any.whl")
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
    """一条命令按当前代码重建全部可分发产物（按平台选形态）。"""
    if os.name == "nt":
        return _build_all_windows()
    if sys.platform == "darwin":
        return _build_all_darwin()
    return _build_all_posix()


def _build_all_darwin() -> int:
    """macOS：`.app`（+ `.dmg` + tar.gz）+ 精简单文件 + wheel。

    macOS 侧的分发形态是 bundle：`.dmg` 给普通用户拖进「应用程序」，tar.gz 里的
    `.app` + `install.sh` 给不想走磁盘映像的人（脚本会把 bundle 复制到 ~/Applications
    并摘掉浏览器下载留下的 quarantine 标记）。arm64 与 x64 各出一份，靠
    `AGENT_CHANNEL_TARGET_ARCH` 交叉构建，产物名里带架构标签。
    """
    target = _target_arch() or "（宿主架构）"
    print(f"=== 1/3 目录形态（含内核）→ .app + .dmg + tar.gz  目标架构: {target} ===")
    core = stage_core()
    if core is None:
        print("警告：没有可用的官方内核归档，目录形态将不含内核（首次运行需联网下载）。")
    code = _run_pyinstaller(onedir=True, no_core=False, core_archive=core)
    if code != 0:
        return code
    app = DIST_DIR / f"{APP_NAME}.app"
    if not app.is_dir():
        print(f"!! 未产出 {app.name}：spec 的 BUNDLE 没生效？")
        return 1
    assert_window_toolkit(app)
    if _check_credentials(app) != 0:
        return 1
    tarball = package_tarball(app)
    if tarball is not None and _check_credentials(tarball) != 0:
        return 1
    if build_dmg() != 0:
        return 1

    print("=== 2/3 精简单文件（不含内核）===")
    code = _run_pyinstaller(onedir=False, no_core=True)
    if code != 0:
        return code
    if _check_credentials(DIST_DIR / _onefile_name()) != 0:
        return 1

    print("=== 3/3 pip wheel ===")
    if build_wheel() != 0:
        return 1

    _report_artifacts()
    return 0


def _build_all_windows() -> int:
    """Windows：目录形态（含内核）+ zip + 安装向导 + 精简单文件 + wheel。

    顺序有讲究：先出带内核的目录形态（安装向导与 zip 都以它为源），
    再出不带内核的精简单文件，最后构建 wheel；
    每一步都带凭据自检，任一步失败就停下来，不交出半套产物。
    """
    print("=== 1/3 目录形态（含内核）+ zip + 安装向导 ===")
    core = stage_core()
    if core is None:
        print("警告：没有可用的官方内核归档，目录形态将不含内核（首次运行需联网下载）。")
    code = _run_pyinstaller(onedir=True, no_core=False, core_archive=core)
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
    if _check_credentials(DIST_DIR / _onefile_name()) != 0:
        return 1

    print("=== 3/3 pip wheel ===")
    if build_wheel() != 0:
        return 1

    _report_artifacts()
    return 0


def _build_all_posix() -> int:
    """Linux/macOS：目录形态（含内核）+ tar.gz（+ Linux 的 .deb）+ 精简单文件 + wheel。

    POSIX 侧没有 Inno Setup 那一步：`.deb` 由系统 dpkg-deb 直接装（macOS 上不产物），
    macOS 侧只有 tar.gz + install.sh。凭据自检在**打包装壳之前**对目录跑一遍，
    打壳之后再对最终包跑一遍（压缩包会被逐个条目解出来查）。
    """
    print("=== 1/3 目录形态（含内核）+ tar.gz ===")
    core = stage_core()
    if core is None:
        print("警告：没有可用的官方内核归档，目录形态将不含内核（首次运行需联网下载）。")
    code = _run_pyinstaller(onedir=True, no_core=False, core_archive=core)
    if code != 0:
        return code
    report_window_toolkit(DIST_DIR / APP_NAME)
    if _check_credentials(DIST_DIR / APP_NAME) != 0:
        return 1
    tarball = package_tarball()
    if tarball is not None and _check_credentials(tarball) != 0:
        return 1
    if sys.platform.startswith("linux"):
        if build_deb() != 0:
            return 1

    print("=== 2/3 精简单文件（不含内核）===")
    code = _run_pyinstaller(onedir=False, no_core=True)
    if code != 0:
        return code
    if _check_credentials(DIST_DIR / _onefile_name()) != 0:
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
        help="按当前代码重建全部产物（Windows：目录形态 + zip + 安装向导；"
        "Linux/macOS：目录形态 + tar.gz + deb）",
    )
    parser.add_argument(
        "--no-core",
        action="store_true",
        help="不预置 sing-box 内核，产出精简包（首次运行需联网下载）",
    )
    parser.add_argument(
        "--no-zip",
        action="store_true",
        help="onedir 产物不打压缩包（Windows 是 zip，Linux/macOS 是 tar.gz）",
    )
    parser.add_argument(
        "--installer",
        action="store_true",
        help="额外用 Inno Setup 编译 Windows 安装向导（需 onedir 产物）",
    )
    parser.add_argument(
        "--deb",
        action="store_true",
        help="额外构建 .deb 安装包（Linux，需 onedir 产物）",
    )
    args = parser.parse_args()

    if args.all:
        return build_all()

    core: Path | None = None
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

    code = _run_pyinstaller(onedir=onedir, no_core=no_core, core_archive=core)
    if code != 0:
        return code

    target = (DIST_DIR / APP_NAME) if onedir else (DIST_DIR / _onefile_name())
    if _check_credentials(target) != 0:
        return 1

    if onedir and not args.no_zip:
        if os.name == "nt":
            package_zip()
        else:
            package_tarball()

    if args.installer:
        if not onedir:
            print(
                "安装向导基于 onedir 目录产物，已跳过（请去掉 --no-core / --onefile）"
            )
            return 0
        return build_installer()

    if args.deb:
        if not onedir:
            print(".deb 基于 onedir 目录产物，已跳过（请去掉 --no-core / --onefile）")
            return 0
        return build_deb()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
