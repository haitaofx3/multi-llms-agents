"""
test_gate_hygiene.py — 防止已修好的结构性问题被重新引入

这里放的都是「一次修好、但很容易被无意改回去」的约束。每条都对应一次真实事故，
不是假想的洁癖。

  1. **测试模块不得在模块级持有管理器单例。**
     三个测试模块曾在导入时执行 `manager.get_or_create("default")`，整个会话共用
     同一个可变编排器，还原靠人工 delattr 白名单——用例顺序一变，故障现象也跟着变。
     现在每个用例都由 `conftest.FreshOrchestratorMixin` 装入全新实例；这条用例
     就是防止有人图省事又把它写回模块级。

  2. **手动端到端脚本必须留在 pytest 采集之外。**
     `test_e2e.py` 没有任何 test_ 函数，却会在导入时连真实服务器、`import websockets`。
     一旦被采集到，缺依赖时会让 pytest 以 "Interrupted: 1 error during collection"
     中断**整个**会话。排除配置写在 conftest 里，这条用例盯着它别被删掉。
"""
import ast
import pathlib
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
TESTS_DIR = ROOT / "tests"


def _module_level_manager_calls(path: pathlib.Path):
    """找出模块级（而非函数/方法内）调用 manager.get_or_create(...) 的位置。

    关键点：要**跳过函数体**。用例内部用 `manager.get_or_create(唯一 sid)` 建会话是
    正当用法（且自带 teardown）；真正的问题是把它放到模块级，让整个会话共享一个实例。
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    hits: list[int] = []

    def walk(node: ast.AST) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue  # 函数/方法体内的调用不算模块级
            if (isinstance(child, ast.Call)
                    and isinstance(child.func, ast.Attribute)
                    and child.func.attr == "get_or_create"):
                hits.append(child.lineno)
            walk(child)

    walk(tree)
    return hits


class NoSharedOrchestratorTest(unittest.TestCase):
    """1. 测试模块不得在模块级创建/复用管理器单例。"""

    def test_no_module_level_get_or_create(self):
        offenders = []
        for path in sorted(TESTS_DIR.glob("*.py")):
            for lineno in _module_level_manager_calls(path):
                offenders.append(f"{path.name}:{lineno}")
        self.assertEqual(
            offenders, [],
            "这些测试文件在模块级调用了 manager.get_or_create(...)，会让整个 pytest "
            "会话共享同一个可变编排器（用例之间互相污染，且顺序一变故障现象就变）。\n"
            "改用 conftest.FreshOrchestratorMixin，在用例内通过 self.orch 访问。\n"
            + "\n".join(offenders))


class E2EScriptStaysOutOfCollectionTest(unittest.TestCase):
    """2. 手动端到端脚本必须留在采集范围之外。"""

    def test_collect_ignore_still_excludes_e2e(self):
        from tests import conftest

        ignored = getattr(conftest, "collect_ignore", [])
        self.assertIn(
            "test_e2e.py", list(ignored),
            "conftest.collect_ignore 里丢了 test_e2e.py：它没有 test_ 函数，却会在导入时"
            "连真实服务器并 import websockets，缺依赖时会中断整个测试会话。")

    def test_e2e_really_has_no_test_functions(self):
        """确认排除它的前提仍然成立（否则说明脚本被改成了真正的测试）。"""
        tree = ast.parse((TESTS_DIR / "test_e2e.py").read_text(encoding="utf-8"))
        test_fns = [n.name for n in ast.walk(tree)
                    if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                    and n.name.startswith("test_")]
        self.assertEqual(
            test_fns, [],
            "test_e2e.py 里出现了 test_ 函数：它现在既是手动脚本又像测试，"
            "请拆开——要么并进 pytest 用例，要么把脚本移出 tests/。")


if __name__ == "__main__":
    unittest.main()
