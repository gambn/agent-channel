#!/bin/sh
# agent-channel 解包即用安装脚本（Linux / macOS）
#
# 在解压出来的目录里执行：
#   ./install.sh                        Linux 装到 ~/.local/opt；macOS 把 .app 装到 ~/Applications
#   ./install.sh --prefix /some/dir     Linux：换程序目录
#   ./install.sh --bin-dir /some/bin    换命令软链目录（默认 ~/.local/bin）
#   ./install.sh --app-dir /some/dir    macOS：换 .app 存放目录（默认 ~/Applications）
#   ./install.sh --uninstall            卸载（只删自己装的程序/菜单项，不动 ~/.agent_channel 数据）
#
# 为什么需要它：本包自带官方编译的 sing-box 内核，用户不需要装 Python、也不需要 root；
# 但 tar.gz 不会自己铺到 PATH、也不会自己出现在应用菜单/启动台里——这份脚本就干这些事。
set -eu

APP=agent-channel
BUNDLE=agent-channel.app
here=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
prefix="${HOME}/.local/opt/${APP}"
bindir="${HOME}/.local/bin"
appdir="${HOME}/Applications"
data_home="${XDG_DATA_HOME:-${HOME}/.local/share}"
action=install

usage() {
    cat <<'EOF'
用法: ./install.sh [--prefix DIR] [--bin-dir DIR] [--app-dir DIR] [--uninstall]

  --prefix DIR   Linux 程序安装目录（默认 ~/.local/opt/agent-channel）
  --bin-dir DIR  命令软链所在目录（默认 ~/.local/bin）
  --app-dir DIR  macOS 的 .app 安装目录（默认 ~/Applications）
  --uninstall    卸载程序与菜单项，保留用户数据 ~/.agent_channel
EOF
}

while [ "$#" -gt 0 ]; do
    case "$1" in
        --prefix)
            [ "$#" -ge 2 ] || { echo "错误: --prefix 缺少参数" >&2; exit 2; }
            prefix=$2
            shift 2
            ;;
        --bin-dir)
            [ "$#" -ge 2 ] || { echo "错误: --bin-dir 缺少参数" >&2; exit 2; }
            bindir=$2
            shift 2
            ;;
        --app-dir)
            [ "$#" -ge 2 ] || { echo "错误: --app-dir 缺少参数" >&2; exit 2; }
            appdir=$2
            shift 2
            ;;
        --uninstall)
            action=uninstall
            shift
            ;;
        -h | --help)
            usage
            exit 0
            ;;
        *)
            echo "错误: 不认识的参数 $1" >&2
            usage >&2
            exit 2
            ;;
    esac
done

if [ "$action" = uninstall ]; then
    # 只删自己铺出去的东西：程序目录/bundle、命令软链、菜单项与图标。
    if [ -x "${prefix}/${APP}/${APP}" ]; then
        rm -rf -- "${prefix:?}/${APP}"
    fi
    rmdir -- "$prefix" 2>/dev/null || true
    if [ -d "${appdir}/${BUNDLE}" ]; then
        rm -rf -- "${appdir:?}/${BUNDLE}"
    fi
    rm -f -- "${bindir}/${APP}"
    rm -f -- "${data_home}/applications/${APP}.desktop"
    rm -f -- "${data_home}/icons/hicolor/512x512/apps/${APP}.png"
    echo "已卸载 agent-channel。"
    echo "用户数据仍在 ~/.agent_channel（配置与内核），需要清除请自行删除。"
    exit 0
fi

if [ -d "${here}/${BUNDLE}" ]; then
    # ---- macOS：装 .app ----
    mkdir -p -- "$appdir" "$bindir"
    rm -rf -- "${appdir:?}/${BUNDLE}"
    cp -R -- "${here}/${BUNDLE}" "${appdir}/${BUNDLE}"
    ln -sfn -- "${appdir}/${BUNDLE}/Contents/MacOS/${APP}" "${bindir}/${APP}"

    # 浏览器下载的包会带 quarantine 标记，带标记的 bundle 会被 Gatekeeper 拦下。
    # 解包安装这条路把它摘掉，用户就不必再去「系统设置 → 隐私与安全性」手动放行。
    if command -v xattr >/dev/null 2>&1; then
        xattr -dr com.apple.quarantine "${appdir}/${BUNDLE}" 2>/dev/null || true
    fi

    echo "已安装: ${appdir}/${BUNDLE}"
    echo "命令:   ${bindir}/${APP}"
    case ":${PATH}:" in
        *":${bindir}:"*) ;;
        *) echo "提示: ${bindir} 不在 PATH 中，加进 shell 配置后可直接敲 ${APP}" ;;
    esac
    echo "启动:   在「启动台/应用程序」里双击 agent-channel，或敲 ${APP}"
    echo "卸载:   ${here}/install.sh --uninstall"
    exit 0
fi

if [ ! -x "${here}/${APP}/${APP}" ]; then
    echo "错误: 在 ${here} 下找不到 ${BUNDLE} 或 ${APP}/${APP}，请在解压出来的目录里运行本脚本。" >&2
    exit 1
fi

# ---- Linux：装目录形态 ----
mkdir -p -- "$prefix" "$bindir"
rm -rf -- "${prefix:?}/${APP}"
cp -R -- "${here}/${APP}" "${prefix}/${APP}"
chmod +x -- "${prefix}/${APP}/${APP}"
ln -sfn -- "${prefix}/${APP}/${APP}" "${bindir}/${APP}"

# 应用菜单与图标（仅 Linux）：Exec 指向装好的绝对路径，避免 ~/.local/bin 不在 PATH 时点不开。
if [ "$(uname -s)" = "Linux" ] && [ -f "${here}/${APP}.desktop" ]; then
    mkdir -p -- "${data_home}/applications" "${data_home}/icons/hicolor/512x512/apps"
    sed "s|^Exec=.*|Exec=${prefix}/${APP}/${APP} ui|" \
        "${here}/${APP}.desktop" >"${data_home}/applications/${APP}.desktop"
    if [ -f "${here}/${APP}.png" ]; then
        cp -- "${here}/${APP}.png" "${data_home}/icons/hicolor/512x512/apps/${APP}.png"
    fi
fi

echo "已安装: ${prefix}/${APP}/${APP}"
echo "命令:   ${bindir}/${APP}"
case ":${PATH}:" in
    *":${bindir}:"*) ;;
    *) echo "提示: ${bindir} 不在 PATH 中，加进 shell 配置后可直接敲 ${APP}" ;;
esac
echo "启动:   ${APP}           # 打开控制面板（首次启动会把随包内核播种到 ~/.agent_channel）"
echo "卸载:   ${here}/install.sh --uninstall"
