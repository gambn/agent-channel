import json
import platform
import subprocess
import time
from contextlib import suppress
from pathlib import Path
from typing import Any


def build_minimal_config(
    inbound_port: int, outbound_node: dict[str, Any]
) -> dict[str, Any]:
    """生成单出站与本地 mixed 入站的极简 sing-box JSON 配置。

    不配置 dns 块：若节点 server 为域名，dns 走代理 detour 会形成
    “解析节点域名 → 建立代理隧道 → 需要解析节点域名” 的循环依赖，
    导致握手超时。交由 sing-box 使用系统 DNS 解析。
    """
    return {
        "log": {"level": "warn"},
        "inbounds": [
            {
                "type": "mixed",
                "tag": "mixed-in",
                "listen": "127.0.0.1",
                "listen_port": inbound_port,
            }
        ],
        "outbounds": [
            outbound_node,
            {"type": "direct", "tag": "direct"},
            {"type": "block", "tag": "block"},
        ],
    }


def _core_log_path() -> Path:
    """内核日志：与 config.json 同目录下的 logs/core.log。"""
    from .config import config_path

    return config_path().parent / "logs" / "core.log"


def _read_log_tail(path: Path, max_bytes: int = 4096) -> str:
    """读日志尾部若干字节，用于启动失败时把 sing-box 的报错带进异常。"""
    try:
        with open(path, "rb") as handle:
            handle.seek(0, 2)
            size = handle.tell()
            handle.seek(max(0, size - max_bytes))
            return handle.read().decode(errors="ignore")
    except Exception:
        return ""


# 本进程正在服务的内核 pid。自愈判据必须跳过它们，否则会把「在用的」
# 当成「残留」收掉。
_LIVE_KERNEL_PIDS: set[int] = set()


def _pids_listening_on(port: int) -> list[int]:
    """列出正在监听 port 的 PID；查询失败一律返回空（宁可什么都不做）。"""
    system = platform.system().lower()
    try:
        if system == "windows":
            out = subprocess.run(
                ["netstat", "-ano", "-p", "TCP"],
                capture_output=True,
                text=True,
                timeout=5,
                creationflags=subprocess.CREATE_NO_WINDOW,
            ).stdout
            pids: list[int] = []
            for line in out.splitlines():
                parts = line.split()
                if (
                    len(parts) >= 5
                    and parts[1].endswith(f":{port}")
                    and "LISTENING" in parts[3]
                ):
                    with suppress(ValueError):
                        pids.append(int(parts[4]))
            return pids
        out = subprocess.run(
            ["lsof", "-ti", f"tcp:{port}", "-sTCP:LISTEN"],
            capture_output=True,
            text=True,
            timeout=5,
        ).stdout
        return [int(x) for x in out.split() if x.isdigit()]
    except (OSError, subprocess.SubprocessError):
        return []


def _is_singbox_process(pid: int) -> bool:
    """判断 pid 是不是 sing-box：只认进程名，避免误杀别人的服务。"""
    system = platform.system().lower()
    try:
        if system == "windows":
            out = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
                capture_output=True,
                text=True,
                timeout=5,
                creationflags=subprocess.CREATE_NO_WINDOW,
            ).stdout
        else:
            out = subprocess.run(
                ["ps", "-p", str(pid), "-o", "comm="],
                capture_output=True,
                text=True,
                timeout=5,
            ).stdout
    except (OSError, subprocess.SubprocessError):
        return False
    return "sing-box" in out.lower() or "sing_box" in out.lower()


def _process_alive(pid: int) -> bool:
    """pid 对应进程是否还活着（用于区分「残留孤儿」与「有主的活内核」）。"""
    if pid <= 0:
        return False
    if platform.system().lower() == "windows":
        import ctypes

        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return False
        kernel32.CloseHandle(handle)
        return True
    import os

    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _windows_parent_pid(pid: int) -> int | None:
    """用 Toolhelp32 快照取父进程 pid（不依赖已弃用的 wmic / 慢速 PowerShell）。"""
    import ctypes
    from ctypes import wintypes

    TH32CS_SNAPPROCESS = 0x00000002

    class PROCESSENTRY32W(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD),
            ("cntUsage", wintypes.DWORD),
            ("th32ProcessID", wintypes.DWORD),
            ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_ulong)),
            ("th32ModuleID", wintypes.DWORD),
            ("cntThreads", wintypes.DWORD),
            ("th32ParentProcessID", wintypes.DWORD),
            ("pcPriClassBase", ctypes.c_long),
            ("dwFlags", wintypes.DWORD),
            ("szExeFile", ctypes.c_wchar * 260),
        ]

    kernel32 = ctypes.windll.kernel32
    snapshot = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if not snapshot or snapshot == -1:
        return None
    try:
        entry = PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(entry)
        if not kernel32.Process32FirstW(snapshot, ctypes.byref(entry)):
            return None
        while True:
            if entry.th32ProcessID == pid:
                return int(entry.th32ParentProcessID)
            if not kernel32.Process32NextW(snapshot, ctypes.byref(entry)):
                return None
    finally:
        kernel32.CloseHandle(snapshot)


def _parent_pid(pid: int) -> int | None:
    """取父进程 pid；取不到返回 None。"""
    try:
        if platform.system().lower() == "windows":
            return _windows_parent_pid(pid)
        out = subprocess.run(
            ["ps", "-o", "ppid=", "-p", str(pid)],
            capture_output=True,
            text=True,
            timeout=5,
        ).stdout.strip()
        return int(out) if out.isdigit() else None
    except (OSError, subprocess.SubprocessError, ValueError):
        return None


def heal_stale_kernel(port: int) -> list[str]:
    """启动期自愈：收掉上次残留的 sing-box 内核，返回给用户看的提示。

    Windows 有 Job Object 保证「父死子必死」；POSIX 侧没有等价内核原语（macOS
    尤其），父进程被 kill -9 后残留内核会一直占着端口，让下次启动失败在一个
    用户看不懂的地方。只在占用者确实是 sing-box 时才动手，且**按实际结果返回**
    ——杀不掉（如权限不足）不留「已收掉」的假提示。

    「跳过」也各记一条提示：静默跳过一旦判据失误，用户就又落回「看不懂的失败」，
    与自愈的初衷相悖。
    """
    notes: list[str] = []
    for pid in _pids_listening_on(port):
        if pid in _LIVE_KERNEL_PIDS:
            notes.append(f"端口 {port} 由本进程的内核 pid {pid} 占用，未处理")
            continue
        if not _is_singbox_process(pid):
            continue
        parent = _parent_pid(pid)
        if parent is not None and _process_alive(parent):
            notes.append(
                f"端口 {port} 被 pid {pid} 占用，其父进程 {parent} 仍存活，"
                "判定为其它实例的活内核，未处理"
            )
            continue
        if _kill_pid(pid):
            notes.append(f"已收掉占用端口 {port} 的残留 sing-box 内核（pid {pid}）")
    return notes


def _kill_pid(pid: int) -> bool:
    """强杀 pid，返回是否真的成功。

    提取成独立函数有两个好处：一是「失败不谎报」（权限不足等返回 False），
    二是判据可被测试注入替身，不必真的去杀进程。
    """
    if platform.system().lower() == "windows":
        try:
            result = subprocess.run(
                ["taskkill", "/F", "/PID", str(pid)],
                capture_output=True,
                timeout=5,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
        except (OSError, subprocess.SubprocessError):
            return False
        return result.returncode == 0
    import os
    import signal

    try:
        # SIGKILL 是 POSIX 专有，用 getattr 取以避开 Windows 侧类型检查
        os.kill(pid, getattr(signal, "SIGKILL", signal.SIGTERM))
    except OSError:
        return False
    return True


class SingboxRunner:
    def __init__(self, binary_path: Path):
        self.binary_path = binary_path
        self.process: subprocess.Popen | None = None
        self.job_handle = None

    def _bind_windows_job(self, proc: subprocess.Popen):
        """利用 Windows Job Object 确保父进程死后子进程被系统内核强杀"""
        if platform.system().lower() != "windows":
            return
        try:
            import ctypes
            from ctypes import wintypes

            kernel32 = ctypes.windll.kernel32
            job = kernel32.CreateJobObjectW(None, None)
            if not job:
                return

            # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000
            class JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
                _fields_ = [
                    ("PerProcessUserTimeLimit", wintypes.LARGE_INTEGER),
                    ("PerJobUserTimeLimit", wintypes.LARGE_INTEGER),
                    ("LimitFlags", wintypes.DWORD),
                    ("MinimumWorkingSetSize", ctypes.c_size_t),
                    ("MaximumWorkingSetSize", ctypes.c_size_t),
                    ("ActiveProcessLimit", wintypes.DWORD),
                    ("Affinity", ctypes.c_size_t),
                    ("PriorityClass", wintypes.DWORD),
                    ("SchedulingClass", wintypes.DWORD),
                ]

            class IO_COUNTERS(ctypes.Structure):
                # 六个字段名必须各不相同：布局只由类型与顺序决定（六个 ULONGLONG
                # = 48 字节），同名不会算错大小，但将来按名字读计数器就会读错。
                _fields_ = [
                    ("ReadOperationCount", ctypes.c_ulonglong),
                    ("WriteOperationCount", ctypes.c_ulonglong),
                    ("OtherOperationCount", ctypes.c_ulonglong),
                    ("ReadTransferCount", ctypes.c_ulonglong),
                    ("WriteTransferCount", ctypes.c_ulonglong),
                    ("OtherTransferCount", ctypes.c_ulonglong),
                ]

            class JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
                _fields_ = [
                    ("BasicLimitInformation", JOBOBJECT_BASIC_LIMIT_INFORMATION),
                    ("IoCounters", IO_COUNTERS),
                    ("ProcessMemoryLimit", ctypes.c_size_t),
                    ("JobMemoryLimit", ctypes.c_size_t),
                    ("PeakProcessMemoryLimit", ctypes.c_size_t),
                    ("PeakJobMemoryLimit", ctypes.c_size_t),
                ]

            info = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
            info.BasicLimitInformation.LimitFlags = 0x2000
            kernel32.SetInformationJobObject(
                job, 9, ctypes.byref(info), ctypes.sizeof(info)
            )

            # 句柄是 Popen 的私有属性：用 getattr 取，别为它挂类型抑制标记
            proc_handle = int(getattr(proc, "_handle"))
            kernel32.AssignProcessToJobObject(job, proc_handle)
            self.job_handle = job
        except Exception:
            # 作业对象绑定失败不阻塞主进程启动
            pass

    def start(self, config_dict: dict[str, Any], config_file: Path) -> None:
        config_file.parent.mkdir(parents=True, exist_ok=True)
        config_file.write_text(json.dumps(config_dict, indent=2), encoding="utf-8")

        cmd = [str(self.binary_path), "run", "-c", str(config_file)]
        # stderr 走日志文件而不是 PIPE：PIPE 无人消费时，弱网长跑累积的告警会写满
        # 管道缓冲（约 4~64KB）把 sing-box 挂死。Popen 会把句柄 dup 给子进程，
        # 所以 with 退出时关掉父进程这份副本是安全的。
        flags = (
            subprocess.CREATE_NO_WINDOW if platform.system().lower() == "windows" else 0
        )
        popen_kwargs: dict[str, Any] = {
            "stdout": subprocess.DEVNULL,
            "creationflags": flags,
        }
        if platform.system().lower() != "windows":
            # 让内核成为新会话首进程：stop() 时可按进程组收尾，把 sing-box
            # 可能派生的子进程一并带走（Windows 侧由 Job Object 负责）。
            popen_kwargs["start_new_session"] = True
        log_path = _core_log_path()
        log_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with open(log_path, "ab") as log_handle:
                self.process = subprocess.Popen(cmd, stderr=log_handle, **popen_kwargs)
        except OSError:
            # 日志打不开不该挡住启动，退回丢弃子进程 stderr
            self.process = subprocess.Popen(
                cmd, stderr=subprocess.DEVNULL, **popen_kwargs
            )
        self._bind_windows_job(self.process)

        # 等待端口就绪或进程异常退出
        inbound_port = None
        for inb in config_dict.get("inbounds", []):
            if inb.get("listen_port"):
                inbound_port = inb.get("listen_port")
                break

        if inbound_port:
            import socket

            start_t = time.perf_counter()
            ready = False
            while time.perf_counter() - start_t < 4.0:
                if self.process.poll() is not None:
                    raise RuntimeError(
                        f"sing-box 启动失败 (退出码 {self.process.returncode}): "
                        f"{_read_log_tail(log_path)}"
                    )
                try:
                    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                        s.settimeout(0.2)
                        if s.connect_ex(("127.0.0.1", inbound_port)) == 0:
                            ready = True
                            break
                except Exception:
                    pass
                time.sleep(0.1)
            if not ready:
                # 端口没就绪就是没就绪。原先这里多一个 “and 进程已退出” 的条件，
                # 于是“进程活着但端口始终没打开”会被当成启动成功，白走一轮转发验证。
                raise RuntimeError(
                    f"sing-box 启动超时（端口 {inbound_port} 未就绪）: "
                    f"{_read_log_tail(log_path)}"
                )

        # 就绪之后才登记为「本进程的活内核」：登记点越晚，能漏出的失败路径越少。
        # 提前登记的话，就绪失败留下的陈旧 pid 会在 Windows 复用 pid 时让守卫①
        # 误判成「自己的内核」而跳过清理——真孤儿反而永不被收。
        _LIVE_KERNEL_PIDS.add(self.process.pid)

    def stop(self) -> None:
        if self.process and self.process.poll() is None:
            if platform.system().lower() == "windows":
                self.process.terminate()
                try:
                    self.process.wait(timeout=2.0)
                except subprocess.TimeoutExpired:
                    self.process.kill()
            else:
                self._terminate_posix_group()
        self._release_job_handle()
        if self.process is not None:
            _LIVE_KERNEL_PIDS.discard(self.process.pid)
        self.process = None

    def _release_job_handle(self) -> None:
        """释放 Windows Job 句柄。

        不释放也不会漏到进程外（runner 失引用后 CPython 会回收，父进程退出时
        OS 也会收），但那属于依赖 GC 的隐式回收；Job 是随 runner 生命周期存在
        的资源，runner 停了就该显式释放。
        """
        handle = self.job_handle
        self.job_handle = None
        if handle is None or platform.system().lower() != "windows":
            return
        import ctypes

        with suppress(Exception):
            ctypes.windll.kernel32.CloseHandle(handle)

    def _terminate_posix_group(self) -> None:
        """正常停止时按进程组收尾（start() 里子进程已 setsid，pgid == pid）。

        macOS / Linux 没有 Windows Job Object 那种「父死子必死」语义：父进程
        被 kill -9 时内核仍可能沦为孤儿。这里是正常停止路径的保障；异常强杀
        场景由启动期自愈负责（见 sysproxy / tray）。
        """
        import os
        import signal

        # getpgid / killpg / SIGKILL 都是 POSIX 专有，Windows 上不存在；用 getattr
        # 动态取，既避开平台类型检查，也在非 POSIX 上自然早退。
        getpgid = getattr(os, "getpgid", None)
        killpg = getattr(os, "killpg", None)
        proc = self.process
        if getpgid is None or killpg is None or proc is None:
            return
        try:
            pgid = getpgid(proc.pid)
        except ProcessLookupError:
            return
        signals = (signal.SIGTERM, getattr(signal, "SIGKILL", signal.SIGTERM))
        for sig in signals:
            try:
                killpg(pgid, sig)
            except ProcessLookupError:
                return
            try:
                proc.wait(timeout=2.0)
                return
            except subprocess.TimeoutExpired:
                continue
