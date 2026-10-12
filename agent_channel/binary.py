"""sing-box 内核管理：定位、随包预置、校验与官方更新。"""

import errno
import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tarfile
import urllib.request
import zipfile
from pathlib import Path
from typing import Any

GITHUB_RELEASE_API = "https://api.github.com/repos/SagerNet/sing-box/releases/latest"
GITHUB_RELEASES_API = "https://api.github.com/repos/SagerNet/sing-box/releases"


def _open_direct(req: urllib.request.Request, timeout: float):
    """直连（绕过系统代理）打开请求。

    系统代理可能指向本工具自己已停止的端口（通道停了但代理未还原），或用户
    自己已关闭的代理端口，走代理会“连接被拒绝”。检查更新/下载内核都是直连
    GitHub 的输入，必须绕过系统代理，否则代理一死就再也查不了更新。
    """
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    return opener.open(req, timeout=timeout)


def _core_filename() -> str:
    """当前平台的 sing-box 可执行文件名。"""
    ext = ".exe" if platform.system().lower() == "windows" else ""
    return f"sing-box{ext}"


def get_bin_dir() -> Path:
    """获取本地二进制缓存目录。"""
    local_app_data = os.environ.get("LOCALAPPDATA")
    base = (
        Path(local_app_data) / "agent_channel" / "bin"
        if local_app_data
        else Path.home() / ".agent_channel" / "bin"
    )
    base.mkdir(parents=True, exist_ok=True)
    return base


def get_local_binary_path() -> Path:
    """获取 sing-box 本地可执行文件路径。"""
    return get_bin_dir() / _core_filename()


def get_bundled_core_dir() -> Path | None:
    """返回随安装包预置内核所在目录（含伴生文件如 libcronet.dll）；未预置时返回 None。

    覆盖两种分发形态：
    - PyInstaller onefile：解包到 sys._MEIPASS/bin/
    - PyInstaller onedir / 免安装绿色包：可执行文件同级的 bin/

    返回**目录**而不是单个 exe：naive 出站依赖同目录的 libcronet.dll，只播种 exe 会让
    内核在初始化 outbound[0] 时 FATAL 退出（表现为“所有节点都不可用”）。
    """
    name = _core_filename()
    candidates: list[Path] = []
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        candidates.append(Path(meipass) / "bin")
    candidates.append(Path(sys.executable).resolve().parent / "bin")
    for candidate in candidates:
        if (candidate / name).is_file():
            return candidate
    return None


def ensure_local_core() -> bool:
    """确保本地内核就绪；缺失时用随包副本播种，避免首次运行还要联网下载。

    逐文件补齐：本地已有的**不覆盖**（不降级用户手动更新的内核），但缺失的伴生文件
    （如 libcronet.dll）要补上——naive 出站缺它会让内核在初始化 outbound[0] 时
    FATAL 退出，表现成“所有节点都不可用”。`core install` 路径解出全部文件，本函数
    只负责随包预置这条路径。
    """
    target = get_local_binary_path()
    bundle_dir = get_bundled_core_dir()
    if bundle_dir is None:
        return target.exists()
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        for src in bundle_dir.iterdir():
            # 跳过 .bak 备份（如 `sing-box.exe.v1.14.2.bak`）：只播内核与伴生文件。
            if not src.is_file() or src.suffix == ".bak":
                continue
            dest = target.parent / src.name
            if not dest.exists():
                shutil.copy2(src, dest)
    except OSError:
        return False
    return target.exists()


def get_local_core_version() -> str:
    """获取本地已安装的 sing-box 内核版本号，未安装则返回“未安装”。"""
    bin_path = get_local_binary_path()
    if not bin_path.exists():
        return "未安装"
    try:
        out = subprocess.check_output(
            [str(bin_path), "version"],
            text=True,
            stderr=subprocess.DEVNULL,
            timeout=2.0,
        )
    except (OSError, subprocess.SubprocessError):
        return "未知版本"
    # 例如: sing-box version 1.10.0
    match = re.search(r"sing-box version\s+([0-9a-zA-Z.\-]+)", out)
    if match:
        version = match.group(1).strip()
        return version if version.startswith("v") else f"v{version}"
    return "未知版本"


def verify_sha256(file_path: Path, expected_hash: str) -> bool:
    """计算并校验文件的 SHA256 散列值。"""
    if not file_path.exists():
        return False
    digest = hashlib.sha256()
    try:
        with open(file_path, "rb") as handle:
            while chunk := handle.read(65536):
                digest.update(chunk)
    except OSError:
        return False
    return digest.hexdigest().lower() == expected_hash.strip().lower()


def check_core_update() -> dict[str, Any]:
    """查询官方 GitHub Release，并与本地内核版本进行精准比对。"""
    local_ver = get_local_core_version()
    req = urllib.request.Request(
        GITHUB_RELEASE_API, headers={"User-Agent": "agent-channel"}
    )
    try:
        with _open_direct(req, timeout=5) as resp:
            data = json.loads(resp.read().decode())
    except Exception as exc:
        return {
            "success": False,
            "local_version": local_ver,
            "latest_version": "",
            "is_latest": False,
            "has_update": False,
            "status_message": f"检查更新失败: {exc}",
            "error": str(exc),
        }

    latest_tag = str(data.get("tag_name", "")).strip()
    if not latest_tag:
        # GitHub 限流时返回的是合法 JSON 但非 Release 对象（如 {"message": ...}），
        # 此时 tag_name 缺失，不能拼出"检测到官方新版本: 空"的矛盾消息。
        return {
            "success": False,
            "local_version": local_ver,
            "latest_version": "",
            "is_latest": False,
            "has_update": False,
            "status_message": "查询官方版本失败（限流或响应异常）",
            "error": "tag_name 缺失",
        }
    if latest_tag and not latest_tag.startswith("v"):
        latest_tag = f"v{latest_tag}"

    is_latest = (
        local_ver.lower().lstrip("v") == latest_tag.lower().lstrip("v")
    ) and local_ver != "未安装"
    has_update = (not is_latest) and bool(latest_tag)

    if is_latest:
        message = f"已是最新版本 ({latest_tag})"
    elif local_ver == "未安装":
        message = f"本地尚未安装内核 (官方最新: {latest_tag})"
    else:
        message = f"检测到官方新版本: {latest_tag} (当前本地: {local_ver})"

    return {
        "success": True,
        "local_version": local_ver,
        "latest_version": latest_tag,
        "is_latest": is_latest,
        "has_update": has_update,
        "status_message": message,
        "assets": data.get("assets", []),
    }


def _extract_all(archive_path: Path, dest_dir: Path) -> list[str]:
    """解出资产目录内全部文件（可执行文件 + 伴生库如 libcronet.dll）。

    返回解出的文件名（basename）列表。写不进去报 OSError；归档本身有问题
    返回空列表（哈希已经校验过，属异常情况）。
    """
    names: list[str] = []
    try:
        if archive_path.name.endswith((".tar.gz", ".tgz")):
            with tarfile.open(archive_path, "r:gz") as archive:
                for member in archive.getmembers():
                    if not member.isfile():
                        continue
                    name = member.name.split("/")[-1]
                    if not name:
                        continue
                    source = archive.extractfile(member)
                    if source is None:
                        continue
                    with source, open(dest_dir / name, "wb") as dst:
                        shutil.copyfileobj(source, dst)
                    names.append(name)
            return names
        with zipfile.ZipFile(archive_path, "r") as archive:
            for item in archive.namelist():
                if item.endswith("/"):
                    continue
                name = item.split("/")[-1]
                if not name:
                    continue
                with archive.open(item) as src, open(dest_dir / name, "wb") as dst:
                    shutil.copyfileobj(src, dst)
                names.append(name)
        return names
    except (tarfile.TarError, zipfile.BadZipFile):
        return []
    except OSError as exc:
        # 写不进去（目录不可写 / 磁盘满）不是“包里没有这个成员”：如实上报，
        # 让上层说清楚原因，也不要白跑那几次重试
        raise OSError(f"写入内核文件失败: {exc}") from exc


_DOWNLOAD_ATTEMPTS = 3


def _ensure_target_writable(target_file: Path) -> None:
    """目标内核文件能不能被替换；不能就当场说清楚，别去下那几十兆。

    通道正在运行时 Windows 会锁住那个 exe，替换必然失败。原先要先下完整个包
    才会发现，白跑一趟流量。
    """
    if not target_file.exists():
        return
    try:
        fd = os.open(target_file, os.O_RDWR)
    except OSError as exc:
        busy = isinstance(exc, PermissionError) or getattr(exc, "errno", None) in (
            errno.EACCES,
            errno.EBUSY,
            errno.ETXTBSY,
        )
        if busy:
            raise OSError(
                f"内核文件被占用（通道可能正在运行），请先停止通道再更新。原始错误: {exc}"
            ) from exc
        raise
    else:
        # 探测成功也要立刻关掉句柄，否则这个句柄自己会把文件锁住
        os.close(fd)


def _download_once(url: str, dest: Path) -> tuple[int | None, int]:
    """下载一次，返回 (声明长度 或 None, 实收字节数)。"""
    req = urllib.request.Request(url, headers={"User-Agent": "agent-channel"})
    received = 0
    declared: int | None = None
    with _open_direct(req, timeout=60) as resp:
        raw_length = resp.headers.get("Content-Length")
        try:
            declared = int(raw_length) if raw_length is not None else None
        except (TypeError, ValueError):
            declared = None
        with open(dest, "wb") as f:
            while True:
                chunk = resp.read(1 << 20)
                if not chunk:
                    break
                f.write(chunk)
                received += len(chunk)
    return declared, received


def download_and_extract_binary(url: str, expected_hash: str | None = None) -> Path:
    """下载并解压官方预编译包到 bin 目录，全程零本地编译。"""
    bin_dir = get_bin_dir()
    is_tar = url.split("?")[0].endswith((".tar.gz", ".tgz"))
    tmp_file = bin_dir / ("temp_core.tar.gz" if is_tar else "temp_core.zip")
    target_file = get_local_binary_path()

    # 目标被占用就别下这几十兆了：通道在跑时替换必然失败，先下完再报错纯属白跑
    _ensure_target_writable(target_file)

    last_error = "下载未成功"
    try:
        for attempt in range(1, _DOWNLOAD_ATTEMPTS + 1):
            try:
                declared, received = _download_once(url, tmp_file)
            except Exception as exc:
                last_error = f"第 {attempt} 次下载失败: {exc}"
                continue

            # 响应被截断是最常见的失败：声明长度和实收对不上就先重下
            if declared is not None and declared != received:
                last_error = f"第 {attempt} 次响应被截断：收到 {received} 字节，声明 {declared} 字节"
                continue

            if expected_hash and not verify_sha256(tmp_file, expected_hash):
                last_error = f"第 {attempt} 次 SHA256 校验失败"
                continue

            # 先解到旁边的临时目录，验证通过再逐个原子替换：解压/替换失败时，
            # 原来那个能用的内核原样留着，不会被截成半截。
            staging_dir = bin_dir / ".staging"
            shutil.rmtree(staging_dir, ignore_errors=True)
            staging_dir.mkdir(parents=True, exist_ok=True)
            try:
                extracted = _extract_all(tmp_file, staging_dir)
                if target_file.name not in extracted:
                    last_error = "发布包里没有 sing-box 可执行文件"
                    continue
                for name in extracted:
                    try:
                        os.replace(staging_dir / name, bin_dir / name)
                    except OSError as exc:
                        if (
                            name == target_file.name
                            and platform.system().lower() == "windows"
                        ):
                            raise OSError(
                                f"无法替换内核文件（{exc}）。通道正在运行时内核被系统占用，"
                                f"请先停止通道再更新。"
                            ) from exc
                        raise
            finally:
                shutil.rmtree(staging_dir, ignore_errors=True)
            if platform.system().lower() != "windows":
                os.chmod(target_file, 0o755)
            return target_file

        raise ValueError(
            f"下载重试 {_DOWNLOAD_ATTEMPTS} 次仍未取得完整内核：{last_error}"
        )
    finally:
        if tmp_file.exists():
            try:
                tmp_file.unlink()
            except OSError:
                pass


def platform_arch() -> str:
    """当前机器的官方架构类名；认不出来即报错，不猜。

    猜错会装上一个跑不起来的内核，比报错难查得多。
    """
    machine = platform.machine().lower()
    if machine in ("amd64", "x86_64"):
        return "amd64"
    if machine in ("i386", "i486", "i586", "i686", "x86", "386"):
        return "386"
    if machine in ("aarch64", "arm64"):
        return "arm64"
    if machine in ("armv7l", "armv7", "armhf"):
        return "armv7"
    if machine in ("armv6l", "armv6"):
        return "armv6"
    if machine == "riscv64":
        return "riscv64"
    raise ValueError(
        f"无法识别 CPU 架构: {machine or '未知'}。请手动下载官方 sing-box "
        f"可执行文件放到 {get_bin_dir()}，工具会直接使用它。"
    )


def platform_os() -> str:
    """当前机器的官方系统名（windows/darwin/linux）；认不出来即报错。"""
    sys_name = platform.system().lower()
    if sys_name in ("windows", "darwin", "linux"):
        return sys_name
    raise ValueError(
        f"无法识别操作系统: {sys_name or '未知'}。请手动下载官方 sing-box "
        f"可执行文件放到 {get_bin_dir()}，工具会直接使用它。"
    )


def _platform_asset_suffix() -> str:
    """当前平台在官方 Release 资产名里的匹配后缀。"""
    os_name = platform_os()
    extension = ".zip" if os_name == "windows" else ".tar.gz"
    return f"{os_name}-{platform_arch()}{extension}"


def list_core_releases(limit: int = 15) -> list[dict[str, Any]]:
    """列出官方可用内核版本（新的在前），供用户自选安装。

    返回 [{version, prerelease, assets}]；查询失败返回空列表。
    """
    req = urllib.request.Request(
        f"{GITHUB_RELEASES_API}?per_page={limit}",
        headers={"User-Agent": "agent-channel"},
    )
    try:
        with _open_direct(req, timeout=8) as resp:
            data = json.loads(resp.read().decode())
    except Exception:
        return []
    if not isinstance(data, list):
        return []

    releases: list[dict[str, Any]] = []
    for item in data:
        tag = str(item.get("tag_name", "")).strip()
        if not tag or item.get("draft"):
            continue
        releases.append(
            {
                "version": tag if tag.startswith("v") else f"v{tag}",
                "prerelease": bool(item.get("prerelease")),
                "assets": item.get("assets", []),
            }
        )
    return releases


def _assets_version(assets: list[dict[str, Any]]) -> str:
    """从资产名里推出版本号（`sing-box-1.14.3-windows-amd64.zip` → `1.14.3`）。"""
    for asset in assets:
        matched = re.match(r"^sing-box-(v?[0-9][^-]*)-", str(asset.get("name", "")))
        if matched:
            return matched.group(1)
    return ""


def _pick_asset(assets: list[dict[str, Any]]) -> dict[str, Any] | None:
    """挑出匹配当前平台的那个资产：basename 必须**恰好等于**官方命名。

    不用子串包含：`-legacy` / `-glibc` 这些变体因为不含完整后缀所以本来也匹配不上，
    但将来若出现 `…-windows-amd64.zip.sha256` 这类"把基础名含在更长名字里"的资产，
    子串匹配会静默挑错，而且结果取决于资产在列表里的先后顺序。
    """
    version = _assets_version(assets)
    if not version:
        return None
    wanted = f"sing-box-{version}-{_platform_asset_suffix()}"
    for asset in assets:
        if str(asset.get("name", "")) == wanted:
            return asset
    return None


def _asset_digest(asset: dict[str, Any] | None) -> str | None:
    """官方资产的 sha256（形如 `sha256:abc…`）；老版本没有这个字段则返回 None。"""
    digest = str((asset or {}).get("digest") or "").strip().lower()
    if digest.startswith("sha256:"):
        return digest.split(":", 1)[1].strip() or None
    return None


def _pick_asset_url(assets: list[dict[str, Any]]) -> str | None:
    """在资产列表里挑出匹配当前平台的那一个下载地址（复用同一份匹配逻辑）。"""
    asset = _pick_asset(assets)
    return str(asset.get("browser_download_url") or "") if asset else None


def is_version_installable(assets: list[dict[str, Any]]) -> bool:
    """该版本是否存在匹配当前平台的预编译资产。"""
    return _pick_asset_url(assets) is not None


def install_core(version: str | None = None) -> dict[str, Any]:
    """安装内核：version 为空取官方最新版，否则安装指定版本（如 v1.14.3）。"""
    if version:
        wanted = version.strip().lower().lstrip("v")
        match = next(
            (
                release
                for release in list_core_releases(limit=50)
                if release["version"].lower().lstrip("v") == wanted
            ),
            None,
        )
        if match is None:
            return {"success": False, "error": f"未找到内核版本 {version}"}
        assets, tag = match["assets"], match["version"]
    else:
        info = check_core_update()
        if not info.get("success"):
            return {
                "success": False,
                "error": info.get("error") or "查询官方最新版失败",
            }
        assets, tag = info.get("assets", []), info.get("latest_version")

    asset = _pick_asset(assets)
    if asset is None:
        return {
            "success": False,
            "error": f"版本 {tag} 没有匹配当前平台的预编译资产 ({_platform_asset_suffix()})",
        }
    download_url = str(asset.get("browser_download_url") or "")
    if not download_url:
        return {"success": False, "error": f"资产 {asset.get('name')} 没有下载地址"}

    # 官方 Release 从 v1.12 起提供 digest。这是“装上的到底是不是官方那个文件”的
    # 唯一凭据，必须传下去 —— 原先这里没传，校验分支永远不会执行。
    digest = _asset_digest(asset)
    try:
        download_and_extract_binary(download_url, expected_hash=digest)
    except Exception as exc:
        return {"success": False, "error": str(exc)}
    return {
        "success": True,
        "version": tag,
        "digest_verified": bool(digest),
        "asset": str(asset.get("name") or ""),
    }


def update_core() -> dict[str, Any]:
    """一键更新内核到官方最新版（面板按钮使用）。"""
    info = check_core_update()
    if not info.get("has_update"):
        return {"success": False, "error": info.get("error") or "当前无需更新"}
    return install_core()
