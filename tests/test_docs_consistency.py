"""
test_docs_consistency.py — 文档与代码的一致性门禁

## 为什么要这个文件

`docs/DELIVERY_READINESS.md` 曾经长期停在"无 /healthz、日志不落盘、157 项用例"，
而代码早已不是那样；README 里也写死了"206 passed""49 个接口""app.js 1134 行"。
这类漂移不是有人偷懒，而是**同一个事实被手抄到多个地方，且没有任何检查**。

靠"记得同步"是治不住的，只能把**能被机器判断的部分**变成用例。本文件只检查
客观可判定的不变式，不检查文风、不统计数字：

  1. README 与 docs/ 里所有相对链接都能解析（移动/改名前必须同步）；
  2. `docs/` 根目录下的现行文档都登记在 `docs/README.md` 索引里
     ——  防止"已经过期的文档悄悄留在现行目录冒充现状"；
  3. README 的目录结构块里列出的条目真实存在
     ——  防止树里写着已经改名/删除的文件；
  4. `pyproject.toml` 与 `main.py` 里的版本号一致（单一事实来源）。

刻意**不**检查的东西（避免把易变量固化进门禁）：用例数量、代码行数、接口数量、
响应时间——这些请一律写成"以 CI / 端点返回为准"。
"""
import pathlib
import re
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
README = ROOT / "README.md"
DOCS = ROOT / "docs"
DOC_INDEX = DOCS / "README.md"

# README 目录结构里属于「运行期产物」而非仓库文件的条目：
# 新克隆的仓库里它们本来就不存在（.gitignore 覆盖），因此不参与存在性检查。
RUNTIME_ONLY_TREE_ENTRIES = {"data", ".env"}


def _current_docs():
    """docs/ 根目录下的现行文档（不含索引自身与 archive/ 存档）。"""
    return sorted(p for p in DOCS.glob("*.md") if p.name != "README.md")


def _markdown_links(path: pathlib.Path):
    """抽取 markdown 里的相对链接目标（跳过外链与锚点）。"""
    text = path.read_text(encoding="utf-8")
    for target in re.findall(r"\[[^\]]*\]\(([^)]+)\)", text):
        target = target.strip()
        if not target or target.startswith(("http://", "https://", "mailto:", "#")):
            continue
        yield target.split("#")[0]


class DocsLinkIntegrityTest(unittest.TestCase):
    """1. 相对链接必须可解析。"""

    def test_all_relative_links_resolve(self):
        broken = []
        for md in [README, *sorted(DOCS.rglob("*.md"))]:
            for target in _markdown_links(md):
                if not target:
                    continue
                if not (md.parent / target).resolve().exists():
                    broken.append(f"{md.relative_to(ROOT).as_posix()} -> {target}")
        self.assertEqual(broken, [], "存在失效的相对链接：\n" + "\n".join(broken))


class DocsIndexCoverageTest(unittest.TestCase):
    """2. 现行文档必须登记在索引里（过期文档要移进 archive/）。"""

    def test_every_current_doc_is_indexed(self):
        index_text = DOC_INDEX.read_text(encoding="utf-8")
        missing = [p.name for p in _current_docs() if p.name not in index_text]
        self.assertEqual(
            missing, [],
            "以下文档留在 docs/ 现行目录却没有登记进 docs/README.md 索引。"
            "如果它描述的是某个时点的状态，请移到 docs/archive/ 并在索引里标注。\n"
            + "\n".join(missing))

    def test_archive_is_indexed(self):
        """archive/ 目录本身要在索引里出现，避免存档变成无人知晓的黑洞。"""
        self.assertIn("archive/", DOC_INDEX.read_text(encoding="utf-8"))


class ReadmeTreeTest(unittest.TestCase):
    """3. README 目录结构里列出的条目必须真实存在。"""

    @staticmethod
    def _tree_paths():
        """从 README 的目录结构代码块里解析出「相对路径」列表。

        只解析两层：顶层条目与 `│   ├──` 形式的子条目。刻意不实现完整的树解析，
        够用且不会因缩进风格微调而误报。
        """
        text = README.read_text(encoding="utf-8")
        block = re.search(r"## 目录结构\s*```[a-zA-Z]*\n(.*?)```", text, re.S)
        if not block:
            return []
        paths, top = [], None
        for line in block.group(1).splitlines():
            m_top = re.match(r"^[├└]──\s+(\S+)", line)
            if m_top:
                top = m_top.group(1).rstrip("/")
                paths.append(top)
                continue
            m_child = re.match(r"^│\s+[├└]──\s+(\S+)", line)
            if m_child and top:
                paths.append(f"{top}/{m_child.group(1).rstrip('/')}")
        return paths

    def test_tree_block_exists(self):
        self.assertTrue(self._tree_paths(), "README 里没有解析到目录结构块")

    def test_tree_entries_exist(self):
        missing = []
        for rel in self._tree_paths():
            if rel in RUNTIME_ONLY_TREE_ENTRIES:
                continue
            if not (ROOT / rel).exists():
                missing.append(rel)
        self.assertEqual(
            missing, [],
            "README 目录结构里列出了不存在的条目（已改名或删除？）：\n" + "\n".join(missing))


class VersionSingleSourceTest(unittest.TestCase):
    """4. 版本号只有一个事实来源。"""

    def test_pyproject_and_app_version_match(self):
        pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
        m = re.search(r'^version\s*=\s*"([^"]+)"', pyproject, re.M)
        self.assertIsNotNone(m, "pyproject.toml 里没找到 version")
        # 只取 [project] 段里的第一个 version
        project_version = m.group(1)

        main_src = (ROOT / "muti_llm_agent" / "main.py").read_text(encoding="utf-8")
        m2 = re.search(r'version\s*=\s*"([^"]+)"', main_src)
        self.assertIsNotNone(m2, "main.py 的 FastAPI(...) 里没找到 version")
        self.assertEqual(
            project_version, m2.group(1),
            "pyproject.toml 与 main.py 的版本号不一致；请把版本收敛到一处再同步另一处")


if __name__ == "__main__":
    unittest.main()
