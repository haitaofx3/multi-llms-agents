"""
test_dependency_closure.py — CI 依赖必须覆盖所有「模块级导入」

## 为什么要有这个用例

`ci.yml` 里那行手写的 `pip install numpy pydantic openai fastapi httpx ruff mypy`
曾经漏掉 `pytest` 和 `websockets`，结果 CI 在**采集阶段**就崩，而本地因为环境里
恰好什么都有，永远复现不出来。这类"依赖清单与代码实际导入不一致"的缺陷，
靠 review 是看不出来的——只能机器比。

## 规则（刻意简单到不会误报）

  - 只看**模块级**导入：函数体内的 `import` 属于「延迟导入」，缺失时按设计降级，
    不必进 CI 依赖（`docx` / `qdrant_client` / `sentence_transformers` 都属此类）。
  - 类体内的导入算模块级（类定义时就会执行）。
  - 标准库、本项目自身的包、相对导入一律忽略。

于是不变式只有一句话：**能在导入期就炸的第三方包，必须出现在 requirements-ci.txt 里。**
"""
import ast
import pathlib
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
CI_REQUIREMENTS = ROOT / "requirements-ci.txt"

FIRST_PARTY = {"muti_llm_agent", "tests", "scripts", "conftest"}
SCAN_DIRS = ["muti_llm_agent", "tests", "scripts"]
SCAN_FILES = ["start.py"]

# distribution 名与 import 名不一致时的映射（当前没有，留好位置）
DIST_TO_MODULE = {}


def _normalize(name: str) -> str:
    return name.strip().lower().replace("-", "_")


def _ci_dependencies() -> set:
    """requirements-ci.txt 里声明的包名（去掉版本约束与注释）。"""
    deps = set()
    for raw in CI_REQUIREMENTS.read_text(encoding="utf-8").splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line or line.startswith("-"):
            continue
        name = line
        for sep in ("==", ">=", "<=", "~=", "!=", ">", "<", "[", ";", " "):
            name = name.split(sep, 1)[0]
        deps.add(_normalize(DIST_TO_MODULE.get(_normalize(name), name)))
    return deps


def _module_scope_imports(path: pathlib.Path) -> set:
    """收集在**导入期**就会执行的第三方包顶层名。"""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: set = set()

    def walk(node: ast.AST) -> None:
        for child in ast.iter_child_nodes(node):
            # 函数体里的导入是延迟导入，按设计允许缺失
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if isinstance(child, ast.Import):
                for alias in child.names:
                    found.add(alias.name.split(".")[0])
            elif isinstance(child, ast.ImportFrom):
                if child.level == 0 and child.module:
                    found.add(child.module.split(".")[0])
            else:
                walk(child)

    walk(tree)
    return found


class DependencyClosureTest(unittest.TestCase):
    """requirements-ci.txt 必须覆盖所有模块级第三方导入。"""

    def test_module_scope_imports_are_covered_by_ci_requirements(self):
        declared = _ci_dependencies()
        stdlib = set(sys.stdlib_module_names)
        offenders = []

        paths = list(SCAN_FILES)
        for d in SCAN_DIRS:
            paths.extend(str(p.relative_to(ROOT).as_posix())
                         for p in (ROOT / d).rglob("*.py"))

        for rel in paths:
            path = ROOT / rel
            for mod in _module_scope_imports(path):
                if mod in stdlib or mod in FIRST_PARTY or mod in declared:
                    continue
                if mod.startswith("_"):  # 私有/相对形式，忽略
                    continue
                offenders.append(f"{rel}: 模块级导入了 `{mod}`")

        self.assertEqual(
            offenders, [],
            "以下第三方包在导入期就会被加载，但不在 requirements-ci.txt 里，"
            "CI 会在采集/导入阶段直接失败。要么把它加进 requirements-ci.txt，"
            "要么改成函数内延迟导入：\n" + "\n".join(sorted(offenders)))

    def test_ci_requirements_file_exists(self):
        self.assertTrue(CI_REQUIREMENTS.is_file(),
                        "缺少 requirements-ci.txt：CI 依赖清单必须可被机器校验")

    def test_no_duplicate_entries(self):
        names = []
        for raw in CI_REQUIREMENTS.read_text(encoding="utf-8").splitlines():
            line = raw.split("#", 1)[0].strip()
            if line and not line.startswith("-"):
                names.append(_normalize(line))
        self.assertEqual(len(names), len(set(names)),
                         f"requirements-ci.txt 里有重复条目：{names}")


if __name__ == "__main__":
    unittest.main()
