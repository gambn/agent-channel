"""
零依赖原生测试套件运行脚本

自动发现 `tests/test_*.py` 里的**零参** `test_*` 函数并逐个调用。不用 pytest（见 README §十），
也不用 unittest 的 loader/suite —— 本仓的测试是裸函数 + assert，不是 TestCase。

**以「模块名.函数名」唯一标识。** 同名函数出现在两个模块时两条都会跑；此前的手工清单按
函数名去重，`test_get_free_port` 在 `test_integration.py` 与 `test_utils.py` 各有一份，
导致其中一条**从未运行**。

**测试隔离不在这里做**：配置目录指向临时目录、系统代理写操作打桩、进程查杀打桩，全部由
`tests/__init__.py` 在导入时完成（见该文件）。本文件只负责发现与运行。
"""

import importlib
import inspect
import sys
import traceback
from collections.abc import Callable
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
# 先于 tests 包解析：本地 tests 赢过 site-packages 的同名包
sys.path.insert(0, str(PROJECT_ROOT))

TESTS_DIR = PROJECT_ROOT / "tests"


def discover() -> list[tuple[str, Callable[[], object]]]:
    """扫描 tests/test_*.py，返回 [(「模块名.函数名」, 函数对象)]。

    - 只收本模块自己定义的函数（`__module__` 一致），避免把 `from ... import` 进来的
      测试重复登记；
    - 只收零参函数：带必填参数的属 fixture 式写法，不是本 runner 的调用约定。
    """
    found: list[tuple[str, Callable[[], object]]] = []
    for path in sorted(TESTS_DIR.glob("test_*.py")):
        module = importlib.import_module(f"tests.{path.stem}")
        for name, obj in vars(module).items():
            if not name.startswith("test_") or not inspect.isfunction(obj):
                continue
            if obj.__module__ != module.__name__:
                continue
            required = [
                param
                for param in inspect.signature(obj).parameters.values()
                if param.default is param.empty
                and param.kind in (param.POSITIONAL_ONLY, param.POSITIONAL_OR_KEYWORD)
            ]
            if required:
                continue
            found.append((f"{module.__name__}.{name}", obj))
    return found


def main() -> int:
    tests = discover()
    print(f"=== 运行 agent-channel通道代理 原生测试套件 (共 {len(tests)} 项测试) ===")
    passed = 0
    failed = 0
    for test_id, fn in tests:
        try:
            fn()
            print(f" [PASS] {test_id}")
            passed += 1
        except Exception as exc:  # 运行器的职责就是如实报出一切失败
            print(f" [FAIL] {test_id}: {exc}")
            traceback.print_exc()
            failed += 1

    print("=" * 55)
    print(f"测试结果: {passed} 通过, {failed} 失败")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
