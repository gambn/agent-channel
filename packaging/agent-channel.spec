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

CORE_NAME = "sing-box.exe" if sys.platform == "win32" else "sing-box"
VENDOR_DIR = ROOT / "packaging" / "vendor"
VENDOR_CORE = VENDOR_DIR / CORE_NAME
SKIP_CORE = os.environ.get("AGENT_CHANNEL_NO_CORE") == "1"
ONEDIR = os.environ.get("AGENT_CHANNEL_ONEDIR") == "1"

datas = [(str(ROOT / "agent_channel" / "web"), "agent_channel/web")]

# 图标：icon.ico 兼任 EXE 自身图标，tray.ico 是托盘专用（透明底、小尺寸单独绘制），
# 两者都随包分发供运行时加载
APP_ICON = ROOT / "packaging" / "assets" / "icon.ico"
TRAY_ICON = ROOT / "packaging" / "assets" / "tray.ico"
for icon_path, label in ((APP_ICON, "应用图标"), (TRAY_ICON, "托盘图标")):
    if icon_path.is_file():
        datas.append((str(icon_path), "assets"))
        print(f"[spec] {label}: {icon_path.name}")
    else:
        print(f"[spec] 未找到 {icon_path.name}")

if SKIP_CORE:
    print("[spec] 已按 AGENT_CHANNEL_NO_CORE 跳过内核，构建精简包")
elif VENDOR_CORE.is_file():
    # 内核 + 全部伴生文件一并打进 bin/：naive 出站依赖 libcronet.dll，
    # 只打 exe 会让内核在初始化 outbound[0] 时 FATAL 退出（表现为“所有节点都不可用”）。
    # 排除 .bak 备份（不入包）。
    packed = [
        p for p in sorted(VENDOR_DIR.iterdir()) if p.is_file() and p.suffix != ".bak"
    ]
    for path in packed:
        datas.append((str(path), "bin"))
        print(f"[spec] 随包预置: {path.name} ({path.stat().st_size / 1048576:.1f} MB)")
else:
    print("[spec] 未找到打包暂存内核，本次产物首次运行需联网下载内核")

print(f"[spec] 构建形态: {'onedir（目录）' if ONEDIR else 'onefile（单文件）'}")

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
        console=True,
        disable_windowed_traceback=False,
        argv_emulation=False,
        target_arch=None,
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
else:
    exe = EXE(
        pyz,
        a.scripts,
        a.binaries,
        a.datas,
        [],
        name="agent-channel",
        debug=False,
        bootloader_ignore_signals=False,
        strip=False,
        upx=False,
        runtime_tmpdir=None,
        console=True,
        disable_windowed_traceback=False,
        argv_emulation=False,
        target_arch=None,
        codesign_identity=None,
        entitlements_file=None,
        icon=str(APP_ICON) if APP_ICON.is_file() else None,
    )
