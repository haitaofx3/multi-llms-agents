#!/usr/bin/env python
"""scripts/gate.py — 质量门禁的**唯一入口**

## 为什么需要这个文件（不是"再多一道检查"）

在这之前，门禁是散落的两套：

  - CI 里 6 个独立 step（ruff / mypy / mypy --strict / compileall / node --check / pytest）；
  - 本地靠人记得分别敲这几条命令。

于是出现了两次真实事故，根因完全一样——**本地没有"跑全部门禁"的动作**：

  1. `ci.yml` 的依赖清单漏了 `pytest` 和 `websockets`，CI 在采集阶段就崩，
     而本地因为环境里恰好都有，永远复现不出来；
  2. 一次重构后本地只跑了 ruff 和 pytest，没跑 mypy，于是把一个类型错误
     （`temperature` 的 `Any | float | None`）提交了上去——mypy 门禁当时是红的，
     但没人知道。

所以本文件不是新增检查，而是把**已有的检查收敛成一个入口**，并让 CI 调这个入口。
CI 与本地跑的是同一段代码，就不存在"两边不一致"的可能。

## 用法

    python scripts/gate.py            # 全部门禁（提交前 / CI）
    python scripts/gate.py --fast     # 只跑静态检查，秒级（适合 pre-commit）
    python scripts/gate.py --list     # 看有哪些步骤

## 设计约束（刻意为之）

  - **前置依赖缺失 = 失败，不是跳过。** 一道跑不起来的门禁比没有门禁更糟——它会
    让人以为"绿了"。缺 `coverage` 就报错并给出安装命令，绝不静默降级。
  - 唯一例外是 `node`：它在本地可能没装，而 CI 一定装了（`setup-node`）。
    所以 `node` 缺失时本地跳过并**大声警告**，`--ci` 下直接失败。
  - 步骤失败即中止（fail-fast）：先修好第一个，再谈后面。
"""
from __future__ import annotations

import argparse
import importlib.util
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PY = sys.executable

# 覆盖率下限。实测基线 64%（224 个用例、5921 条语句）。
# 卡在 60 而不是 64：给平台差异（本机 Windows / CI Ubuntu 的分支统计）留余量，
# 避免门禁因为 1-2 个点的抖动变成噪声。**补测之后请把这个数字往上调**，
# 它的作用只是防回退，不是"及格线"。
COVERAGE_MIN = 60

JS_FILES = [
    "muti_llm_agent/static/app.js",
    "muti_llm_agent/static/studio.js",
    "muti_llm_agent/static/console.js",
]


class GateError(RuntimeError):
    """某道门禁没通过。"""


def _say(msg: str) -> None:
    print(f"\n\033[1m=== {msg}\033[0m" if sys.stdout.isatty() else f"\n=== {msg}")


def _run(name: str, argv: list[str]) -> None:
    _say(f"[{name}] {' '.join(argv)}")
    rc = subprocess.run(argv, cwd=ROOT).returncode  # noqa: S603 - 参数由本文件写死
    if rc != 0:
        raise GateError(f"{name} 未通过（exit={rc}）")


def _have_module(mod: str) -> bool:
    return importlib.util.find_spec(mod) is not None


# --------------------------------------------------------------------------
# 前置依赖：缺了就报错，绝不静默跳过
# --------------------------------------------------------------------------
REQUIRED_MODULES = {
    "ruff": "ruff",
    "mypy": "mypy",
    "pytest": "pytest",
    "coverage": "coverage",
}


def preflight() -> None:
    missing = [dist for mod, dist in REQUIRED_MODULES.items() if not _have_module(mod)]
    if missing:
        raise GateError(
            "门禁依赖缺失，无法执行：" + ", ".join(missing)
            + "\n  修复：pip install -r requirements-dev.txt"
            + "\n  （门禁跑不起来一律按失败处理——静默跳过会让人误以为已经绿了）")


# --------------------------------------------------------------------------
# 各道门禁
# --------------------------------------------------------------------------
def gate_lint() -> None:
    _run("lint", [PY, "-m", "ruff", "check", "muti_llm_agent", "tests", "scripts"])


def gate_types() -> None:
    # 配置在 pyproject.toml 的 [tool.mypy]，files 指向整包
    _run("types", [PY, "-m", "mypy"])


def gate_types_strict() -> None:
    # 协议层契约用严口径单独把关（见 pyproject 注释）
    _run("types-strict", [
        PY, "-m", "mypy", "--strict", "--follow-imports=normal",
        "muti_llm_agent/protocol.py"])


def gate_compile() -> None:
    _run("compile", [PY, "-m", "compileall", "-q", "muti_llm_agent"])


def gate_js(ci: bool) -> None:
    node = shutil.which("node")
    if not node:
        msg = "node 未安装，跳过 JS 语法检查"
        if ci:
            raise GateError(msg + "（CI 里必须能检查，请确认 setup-node 步骤）")
        _say(f"[js] ⚠ {msg}（本地可忽略；CI 会强制执行）")
        return
    for rel in JS_FILES:
        _run("js", [node, "--check", rel])


def gate_tests() -> None:
    """跑用例并卡覆盖率下限。

    用 `coverage run -m pytest` 而不是 pytest-cov：少一个依赖，且 .coverage 文件
    可以直接给 IDE / 本地排查复用。配置取自 pyproject 的 [tool.coverage.*]。
    """
    _run("tests", [PY, "-m", "coverage", "run", "-m", "pytest", "tests", "-q"])
    _run("coverage", [
        PY, "-m", "coverage", "report", f"--fail-under={COVERAGE_MIN}"])


# --------------------------------------------------------------------------
# 编队
# --------------------------------------------------------------------------
STEPS = [
    # (标识, 是否属于 --fast, 说明)
    ("lint", True, "ruff 静态检查"),
    ("types", True, "mypy 整包类型检查"),
    ("types-strict", True, "mypy --strict 校验协议层契约"),
    ("compile", False, "字节码编译（语法/导入期错误的兜底）"),
    ("js", False, "前端 JS 语法检查"),
    ("tests", False, f"全量用例 + 覆盖率下限 {COVERAGE_MIN}%"),
]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="项目质量门禁的唯一入口（CI 与本地共用）")
    parser.add_argument("--fast", action="store_true",
                        help="只跑静态检查（lint / types），适合 pre-commit")
    parser.add_argument("--ci", action="store_true",
                        help="CI 模式：node 缺失也视为失败")
    parser.add_argument("--list", action="store_true", help="只列出步骤")
    args = parser.parse_args(argv)

    if args.list:
        for name, fast, desc in STEPS:
            print(f"{'[fast]' if fast else '      '} {name:14s} {desc}")
        return 0

    selected = [s for s in STEPS if (s[1] or not args.fast)]
    print(f"门禁入口：共 {len(selected)} 步（{'--fast 静态检查' if args.fast else '全量'}）")

    try:
        preflight()
        for name, _fast, _desc in selected:
            if name == "lint":
                gate_lint()
            elif name == "types":
                gate_types()
            elif name == "types-strict":
                gate_types_strict()
            elif name == "compile":
                gate_compile()
            elif name == "js":
                gate_js(args.ci)
            elif name == "tests":
                gate_tests()
    except GateError as exc:
        print(f"\n\033[31m✗ 门禁未通过：{exc}\033[0m" if sys.stdout.isatty() else f"\n✗ 门禁未通过：{exc}")
        return 1

    print("\n✓ 全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
