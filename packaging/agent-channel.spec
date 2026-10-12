# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包配置，支持 onefile 与 onedir 两种形态。

构建形态由环境变量决定（通常由 packaging/build.py 设置）：
- AGENT_CHANNEL_ONEDIR=1：产出目录形态，内核不解包、启动快，适合随包预置内核；
- 否则：产出单文件形态，适合不带内核的精简包。

内核与静态资源：
- agent_channel/web 始终随包分发；
- 若 packaging/vendor/ 下存在官方预编译内核，则一并打进包，安装后离线可用，
  首次运行时被播种到 %LOCALAPPDATA%\\agent_channel\\bin，之后不再联网下载；
- AGENT_CHANNEL_NO_CORE=1 可强制跳过内核。

单文件形态下无参数启动打开面板，带参数走 CLI，兼顾双击与命令行两种用法。
"""

import os
import sys
from pathlib import Path

ROOT = Path(SPECPATH).parent  # noqa: F821  SPECPATH 由 PyInstaller 注入

# 随包预置的是**官方归档原件**（路径由 build.py 的 stage_core() 通过环境变量传来），
# 不是解开的 `sing-box`：macOS 的 .app 会被 codesign --deep 重签嵌套的 Mach-O，解开的
# 二进制进包后就不再与官方字节一致；压缩归档不是 Mach-O，签名工具不碰它。
BUNDLED_CORE = os.environ.get("AGENT_CHANNEL_BUNDLED_CORE")
SKIP_CORE = os.environ.get("AGENT_CHANNEL_NO_CORE") == "1"
ONEDIR = os.environ.get("AGENT_CHANNEL_ONEDIR") == "1"

# 精简单文件的产物名。Windows 靠 `.exe` 后缀与目录形态天然区分；POSIX 没有后缀，
# 两个形态若都叫 `agent-channel`，PyInstaller 的 `--noconfirm` 会把先建好的目录形态
# 整个删掉（实测：`dist/agent-channel/` 被单文件覆盖，目录产物凭空消失）。
ONEFILE_NAME = "agent-channel" if sys.platform == "win32" else "agent-channel-slim"

# 交叉构建目标架构（PyInstaller 的 target_arch；None = 跟宿主的解释器走）。
# 在 Intel 上用 universal2 解释器出 arm64 包时就靠它。
TARGET_ARCH = os.environ.get("AGENT_CHANNEL_TARGET_ARCH") or None

# `.icns` 由 build.py 用 sips + iconutil 现生成（不写进仓库），路径经环境变量传进来。
ICNS_PATH = os.environ.get("AGENT_CHANNEL_ICNS")


def _project_version() -> str:
    """从 pyproject.toml 取版本，避免 Info.plist 里的版本与包元数据脱节。"""
    import re

    try:
        text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    except OSError:
        return "0.0.0"
    match = re.search(r'^version\s*=\s*"([^"]+)"', text, re.MULTILINE)
    return match.group(1) if match else "0.0.0"

datas = [(str(ROOT / "agent_channel" / "web"), "agent_channel/web")]

# 图标：icon.ico 兼任 EXE 自身图标，tray.ico 是托盘专用（透明底、小尺寸单独绘制），
# 两者都随包分发供运行时加载。
# macOS 上 PyInstaller 只接受 .icns（把 .ico 转过去要 Pillow，构建机不保证有），
# 所以按平台取名字：darwin 侧没有 .icns 时就不带图标，而不是让构建直接失败。
APP_ICON = (
    Path(ICNS_PATH)
    if sys.platform == "darwin" and ICNS_PATH
    else ROOT / "packaging" / "assets" / ("icon.ico" if sys.platform == "win32" else "icon.icns")
)
TRAY_ICON = ROOT / "packaging" / "assets" / "tray.ico"
for icon_path, label in ((APP_ICON, "应用图标"), (TRAY_ICON, "托盘图标")):
    if icon_path.is_file():
        datas.append((str(icon_path), "assets"))
        print(f"[spec] {label}: {icon_path.name}")
    else:
        print(f"[spec] 未找到 {icon_path.name}")

if SKIP_CORE:
    print("[spec] 已按 AGENT_CHANNEL_NO_CORE 跳过内核，构建精简包")
elif BUNDLED_CORE and Path(BUNDLED_CORE).is_file():
    bundled = Path(BUNDLED_CORE)
    datas.append((str(bundled), "bin"))
    print(
        f"[spec] 随包预置官方归档: {bundled.name} "
        f"({bundled.stat().st_size / 1048576:.1f} MB，首次启动解到用户目录)"
    )
else:
    print("[spec] 未找到打包暂存内核，本次产物首次运行需联网下载内核")

print(
    f"[spec] 构建形态: {'onedir（目录）' if ONEDIR else 'onefile（单文件）'}"
    f"{f'，目标架构 {TARGET_ARCH}' if TARGET_ARCH else ''}"
)

# macOS 的 .app 必须是窗口（GUI）形态：console=True 的 bundle 双击会挂着一个终端窗口。
# Linux 的目录形态仍是命令行程序，不改。
CONSOLE = not (ONEDIR and sys.platform == "darwin")

a = Analysis(
    [str(ROOT / "packaging" / "agent_channel_app.py")],
    pathex=[str(ROOT)],
    binaries=[],
    datas=datas,
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["pytest", "PyInstaller", "setuptools", "pip"],
    noarchive=False,
    optimize=0,
)

pyz = PYZ(a.pure)

if ONEDIR:
    exe = EXE(
        pyz,
        a.scripts,
        [],
        exclude_binaries=True,
        name="agent-channel",
        debug=False,
        bootloader_ignore_signals=False,
        strip=False,
        upx=False,
        console=CONSOLE,
        disable_windowed_traceback=False,
        argv_emulation=False,
        target_arch=TARGET_ARCH,
        codesign_identity=None,
        entitlements_file=None,
        icon=str(APP_ICON) if APP_ICON.is_file() else None,
    )
    coll = COLLECT(
        exe,
        a.binaries,
        a.datas,
        strip=False,
        upx=False,
        name="agent-channel",
    )

    if sys.platform == "darwin":
        # .app 由 BUNDLE 组装：COLLECT 的产物进 Contents/，内核等数据在
        # Contents/Frameworks/bin 下——而 `sys._MEIPASS` 在 bundle 里就指向 Frameworks，
        # 所以 `binary.get_bundled_core_dir()` 的 `sys._MEIPASS/bin` 那条分支照样成立。
        # codesign_identity=None → PyInstaller 自动做 ad-hoc 签名（arm64 必需）。
        version = _project_version()
        app = BUNDLE(
            coll,
            name="agent-channel.app",
            icon=str(APP_ICON) if APP_ICON.is_file() else None,
            bundle_identifier="io.github.gambn.agent-channel",
            info_plist={
                "CFBundleName": "agent-channel",
                "CFBundleDisplayName": "agent-channel 通道代理",
                "CFBundleShortVersionString": version,
                "CFBundleVersion": version,
                "LSMinimumSystemVersion": "11.0",
                "NSHighResolutionCapable": True,
                # 不隐藏 Dock 图标：没有菜单栏托盘时，Dock 的「退出」就是用户的一条退出路径
                "LSUIElement": False,
            },
        )
else:
    exe = EXE(
        pyz,
        a.scripts,
        a.binaries,
        a.datas,
        [],
        name=ONEFILE_NAME,
        debug=False,
        bootloader_ignore_signals=False,
        strip=False,
        upx=False,
        runtime_tmpdir=None,
        console=CONSOLE,
        disable_windowed_traceback=False,
        argv_emulation=False,
        target_arch=TARGET_ARCH,
        codesign_identity=None,
        entitlements_file=None,
        icon=str(APP_ICON) if APP_ICON.is_file() else None,
    )
