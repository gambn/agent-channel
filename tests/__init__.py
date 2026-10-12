"""测试包：测试进程的隔离边界。

本文件负责测试隔离：解决三件"测试会写到真实环境"的事：

1. **配置与内核目录**。`config.config_path()` 与 `binary.get_bin_dir()`
   都是**先读 `LOCALAPPDATA`**、读不到才回退 `Path.home()`。所以在导入本包时把
   `LOCALAPPDATA` 指向进程内临时目录，**一个变量即覆盖三平台**（POSIX 上那条
   `Path.home()` 分支也被顶掉）。`HOME` 一并设上，兜住任何走 `Path.home()` 的路径。

   历史事实：`test_server.py` 里成片的 `save_config(...)` 曾直写真实用户配置，而
   `save_config` 每次写入都轮转 `.bak` —— 一旦落盘，连"从 `.bak` 复原"都会复原出
   fixture。三平台跑同一份测试就三平台一起中。

2. **系统代理的写操作**。Windows 三个 + macOS/Linux 四个写函数全部打桩成 no-op；
   只读的 `get_system_proxy_status` **保持真实**（它只读，且测试要用它的真实行为）。

3. **进程查杀**。`runner.heal_stale_kernel` 会真的 `taskkill` / `kill -9`。这里在
   **叶子**（`_pids_listening_on` / `_kill_pid`）打桩，于是**任何测试路径**都不可能
   杀到真实进程 —— 不必依赖每个调用点各自记得打桩（`test_server._launch_panel`
   那处打桩仍保留，属双保险）。个别测试要验判据时用 `patch.object` 覆盖本桩即可。

注意：以上都在**导入时**完成，而 `config_path()` / `get_bin_dir()` 都是**调用时**读环境
变量，所以不依赖导入顺序。
"""

import atexit
import os
import shutil
import tempfile
from pathlib import Path

import agent_channel.runner as _runner
import agent_channel.sysproxy as _sysproxy

# 隔离前的真实位置：下面马上会把 LOCALAPPDATA/HOME 指到临时目录，所以先抓原值，
# 留给「真实内核回归」测试把本机已装好的内核播种进隔离目录（见 seed_real_kernel）。
_REAL_LOCALAPPDATA = os.environ.get("LOCALAPPDATA")
_REAL_HOME = os.environ.get("HOME")

# 进程内临时目录：测试期间 config.json 与 bin/ 都落在这里，进程退出即删。
# 导入放上面、环境变量设在这里，是安全的：config_path() / get_bin_dir() 都是
# **调用时**读 os.environ，不依赖导入顺序。
_TMP_HOME = tempfile.mkdtemp(prefix="agent-channel-tests-")
atexit.register(shutil.rmtree, _TMP_HOME, ignore_errors=True)

os.environ["LOCALAPPDATA"] = _TMP_HOME  # config_path()/get_bin_dir() 的首选变量
os.environ["HOME"] = _TMP_HOME  # 兜住 Path.home() 分支


def real_core_dir() -> Path | None:
    """隔离前真实环境里的内核目录（`…/agent_channel/bin`）；没有则 None。"""
    if _REAL_LOCALAPPDATA:
        candidate = Path(_REAL_LOCALAPPDATA) / "agent_channel" / "bin"
    elif _REAL_HOME:
        candidate = Path(_REAL_HOME) / ".agent_channel" / "bin"
    else:
        return None
    return candidate if candidate.is_dir() else None


def seed_real_kernel() -> bool:
    """把真实环境里已装好的内核（连同 libcronet 等伴生文件）放进隔离 bin 目录。

    为什么需要这一步：隔离把 `LOCALAPPDATA` 指到临时目录后，`get_local_binary_path()`
    永远指向那个空目录 —— 于是「真实内核回归」测试在**任何机器上都跳过**，包括本机
    明明装好内核的情况，等于把最初“所有 runner 测试都 mock、从不真实 bind”的盲区
    又原样盖了回去。这是测试专用入口：只从真实目录**读**，只往隔离目录**写**。

    用软链优先（不复制几十兆），平台不支持时退回复制。返回隔离目录里的内核是否就绪。

    **只播种内核本体与伴生库，不播种运行态文件**：软链会穿透写入，若把
    `active_config.json`（测试跑起来就会被 `SingboxRunner.start` 重写）也链进来，
    测试写临时路径时改的是**用户的真实文件** —— 这正是本项目最不能犯的错。
    """
    source = real_core_dir()
    if source is None:
        return False
    target = Path(os.environ["LOCALAPPDATA"]) / "agent_channel" / "bin"
    target.mkdir(parents=True, exist_ok=True)
    for item in source.iterdir():
        if not item.is_file():
            continue
        # 该目录里的 .json 是运行态（active_config.json / benchmark_*.json），
        # 内核发行包本身不含 .json；跳过它们，等于把"只读的内核"与"可写的状态"分开。
        if item.suffix.lower() == ".json":
            continue
        dst = target / item.name
        if dst.exists():
            continue
        try:
            os.symlink(item, dst)
        except OSError:
            try:
                shutil.copy2(item, dst)
            except OSError:
                return False
    return True


def _noop(*_args, **_kwargs) -> bool:
    """写操作的统一替身：报告成功，但不碰真实系统。"""
    return True


# 系统代理的**写**函数（三平台）。只打桩写；读（_get_*_status）保持真实。
_PROXY_WRITERS = (
    "_set_windows_proxy",
    "_unset_windows_proxy",
    "_write_windows_raw",
    "_set_macos_proxy",
    "_unset_macos_proxy",
    "_set_linux_proxy",
    "_unset_linux_proxy",
)
for _name in _PROXY_WRITERS:
    if hasattr(_sysproxy, _name):
        setattr(_sysproxy, _name, _noop)

# 进程查杀的叶子：测试永不触达真实 taskkill / kill
_runner._pids_listening_on = lambda port: []
_runner._kill_pid = lambda pid: False
