"""
test_eval_baseline.py — P0-2 质量基线脚本测试

覆盖 scripts/eval_baseline.py：
  1. parse_export 从导出 Markdown 提取结构化信号；
  2. score_report 五维度打分：健康讨论高分、退化讨论低分；
  3. 基线对比：同基线无回归，删除结论后检出回归；
  4. CLI：文本/JSON 输出、缺失文件退出码 1、回归退出码 2。

运行方式：
    python -m unittest tests.test_eval_baseline -v
"""
import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.eval_baseline import (
    WEIGHTS,
    compare_baseline,
    format_text,
    main,
    parse_export,
    score_report,
)

# ---------------------------------------------------------------- 夹具

HEALTHY = """# 多 Agent 辩论记录

- 主题：AI 学习助手产品方案
- 轮次：4/8
- 状态：done / 阶段：finished
- Trace ID：abc123
- Token 用量（只读遥测）：累计 300000 / 输出 100000
- 时间：2026-08-31 10:00:00
- 参与 Agent：
  - 主持（主） | 角色：主持人 | 立场：主持人 | 模型：deepseek-chat @ https://api.deepseek.com
  - 正方 | 角色：产品经理 | 立场：正方 | 模型：deepseek-chat @ https://api.deepseek.com
  - 反方 | 角色：技术专家 | 立场：反方 | 模型：deepseek-chat @ https://api.deepseek.com
- 独立裁判：独立裁判 | 模型：deepseek-chat @ https://api.deepseek.com

## 运行摘要

- 持续时间：123.4 秒
- 实际轮次：4/8
- 收敛触发：是（连续2轮相似）
- 收敛检测模式：文本相似度（降级，嵌入模型不可用）
- RAG 证据轮数：2
- 任务：2（完成 2）
- 历史压缩次数：1
- Token 用量（只读遥测）：累计 300000 / 输出 100000（不再作为约束）
- 预估费用（P1-6）：≈ ¥1.4000
- 公平性治理：全部通过（0 警告 / 0 违约）
  - L1 输出上限：ok — 硬性字符上限 3000，相对比例 1.5；无截断
  - L5 采样参数对齐：ok — 辩论 temperature=0.7 top_p=1.0

## 发言记录

**主持**（第1轮）：

大家好，我是主持人，先请正方发表立论，并说明其核心主张与理由。

**正方**（第1轮）：

我们的方案强调以学生为中心，通过自适应学习路径与即时反馈提升学习效率，同时注重数据隐私保护，这是我们的核心主张与设计原则。

**反方**（第1轮）：

我们提出反对意见，认为现有技术栈在推理成本与延迟上存在瓶颈，需要优先解决知识库检索的准确性问题，否则产品体验难以达标。

**正方**（第2轮）：

针对反方观点，我们补充说明自适应算法可以在低算力设备上运行，并通过模型蒸馏降低推理成本，同时引入人工审核保障输出质量。

**反方**（第2轮）：

我们承认成本可控，但仍需指出评测体系缺失会导致质量无法衡量，建议先建立回归基线再做规模化推广，这是我们的立场与建议。

## 最终讨论结果（主持 整理）

## 核心结论

本场讨论达成共识：以学生为中心的自适应学习助手可行，需优先建设质量评测基线。

## 各方立场与论点摘要

- 正方：自适应路径 + 即时反馈；反方：成本与检索准确性优先。

## 裁判裁决

正方以内容完整性与可行性略胜，胜方为正方。

## 分歧与待办

评测体系落地节奏存在分歧；待办：建立评测基线。

## 裁判裁决

- 得分：{'pos': 8.5, 'neg': 7.8}
- 辩手评分：
  - 正方：{'content': 8.5, 'argument': 8.2, 'evidence': 7.9, 'expression': 8.0}
  - 反方：{'content': 7.8, 'argument': 7.5, 'evidence': 7.7, 'expression': 7.6}
- 胜方：正方
- 收敛终止：是
- 评语：双方论证充分，正方在可行性论证上更完整。

## 任务结果

| 任务# | 接收方 | 变量 | 完成 | 结果 |
| --- | --- | --- | --- | --- |
| 1 | 正方 | design | 是 | 输出方案 |

## 各 Agent 记忆快照

- 主持：短期 50 / 长期 200 / 情境 5
- 正方：短期 60 / 长期 210 / 情境 4
- 反方：短期 55 / 长期 190 / 情境 6
"""

DEGENERATE = """# 多 Agent 辩论记录

- 主题：测试主题
- 轮次：1/1
- 状态：done / 阶段：finished
- Trace ID：1630773f337e
- Token 用量（只读遥测）：累计 2 / 输出 0
- 时间：2026-08-31 23:42:38
- 参与 Agent：
  - 主持（主） | 角色：主持 | 立场：主持人 | 模型：test-model @ https://example.invalid/v1
  - 正方 | 角色：正方 | 立场：正方 | 模型：test-model @ https://example.invalid/v1

## 运行摘要

- 持续时间：0.2 秒
- 实际轮次：1/1
- 收敛触发：否
- 收敛检测模式：向量余弦相似度（嵌入模型）
- RAG 证据轮数：0
- 任务：0（完成 0）
- 历史压缩次数：0
- Token 用量（只读遥测）：累计 2 / 输出 0（不再作为约束）
- 预估费用（P1-6）：≈ ¥0.0000
- 公平性治理：全部通过（1 警告 / 0 违约）
  - L6 长度中性评审：warning — 裁判提示词尚未注入长度中性要求

## 发言记录

**主持**（第1轮）：

发言

**正方**（第1轮）：

发言

## 最终讨论结果（主持 整理）

测试总结

## 裁判裁决

- 得分：{}
- 胜方：PRO
- 收敛终止：否
- 评语：测试裁决

## 任务结果

（本次讨论未产生任务分派）

## 各 Agent 记忆快照

- 主持：短期 24 / 长期 66 / 情境 0
- 正方：短期 24 / 长期 77 / 情境 0
"""


class ParseExportTest(unittest.TestCase):
    def test_healthy_signals(self):
        s = parse_export(HEALTHY)
        self.assertEqual(s["topic"], "AI 学习助手产品方案")
        self.assertEqual((s["rounds_actual"], s["rounds_max"]), (4, 8))
        self.assertTrue(s["converged"])
        self.assertEqual(s["convergence_mode"], "文本相似度（降级，嵌入模型不可用）")
        self.assertEqual(len(s["agents"]), 3)
        self.assertTrue(s["agents"][0]["name"].startswith("主持"))
        self.assertTrue(s["judge"])
        self.assertEqual((s["tokens_total"], s["tokens_output"]), (300000, 100000))
        self.assertAlmostEqual(s["cost_rmb"], 1.4)
        self.assertEqual((s["tasks_total"], s["tasks_completed"]), (2, 2))
        self.assertEqual(s["fairness"]["warnings"], 0)
        self.assertEqual(s["fairness"]["violations"], 0)
        self.assertEqual(len(s["fairness"]["layers"]), 2)
        self.assertTrue(s["has_judgment"])
        self.assertEqual(s["judge_winner"], "正方")
        self.assertTrue(s["has_conclusion"])
        self.assertEqual(len(s["conclusion_sections"]), 4)
        self.assertEqual(len(s["speeches"]), 5)
        self.assertEqual(len(s["speakers"]), 3)
        self.assertEqual(s["memory_snapshots"], 3)

    def test_degenerate_signals(self):
        s = parse_export(DEGENERATE)
        self.assertEqual(len(s["agents"]), 2)
        self.assertFalse(s["converged"])
        self.assertFalse(s["has_conclusion"])
        self.assertEqual(s["fairness"]["warnings"], 1)
        self.assertEqual(len(s["speeches"]), 2)

    def test_weights_sum_to_one(self):
        self.assertAlmostEqual(sum(WEIGHTS.values()), 1.0, places=6)


class ScoreReportTest(unittest.TestCase):
    def test_healthy_scores_high(self):
        r = score_report(parse_export(HEALTHY), HEALTHY)
        self.assertGreaterEqual(r["overall"], 85.0)
        for dim in WEIGHTS:
            self.assertGreaterEqual(r["dimensions"][dim]["score"], 60.0)

    def test_degenerate_scores_lower(self):
        healthy = score_report(parse_export(HEALTHY), HEALTHY)["overall"]
        degen = score_report(parse_export(DEGENERATE), DEGENERATE)["overall"]
        self.assertLess(degen, healthy)

    def test_degenerate_lacks_conclusion(self):
        r = score_report(parse_export(DEGENERATE), DEGENERATE)
        self.assertLess(r["dimensions"]["completeness"]["score"], 80.0)


class BaselineCompareTest(unittest.TestCase):
    def test_no_regression_vs_itself(self):
        r = score_report(parse_export(HEALTHY), HEALTHY)
        diff = compare_baseline(r, r, tolerance=5.0)
        self.assertEqual(diff["regressed"], [])

    def test_removing_conclusion_is_regression(self):
        good = score_report(parse_export(HEALTHY), HEALTHY)
        degraded_md = HEALTHY.replace("\n## 核心结论", "\n## 缺失结论").replace(
            "## 分歧与待办\n", "")
        bad = score_report(parse_export(degraded_md), degraded_md)
        diff = compare_baseline(bad, good, tolerance=5.0)
        self.assertIn("completeness", diff["regressed"])
        self.assertIn("overall", diff["regressed"])

    def test_format_text_contains_summary(self):
        r = score_report(parse_export(HEALTHY), HEALTHY)
        out = format_text(r)
        self.assertIn("质量基线评估", out)
        self.assertIn("结构完整", out)


class CliTest(unittest.TestCase):
    def _write(self, name, content):
        p = Path(tempfile.mkdtemp(prefix="eval_base_")) / name
        p.write_text(content, encoding="utf-8")
        return p

    def test_text_mode(self):
        p = self._write("good.md", HEALTHY)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = main([str(p)])
        self.assertEqual(rc, 0)
        self.assertIn("质量基线评估", buf.getvalue())

    def test_json_mode(self):
        p = self._write("good.md", HEALTHY)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = main([str(p), "--json"])
        self.assertEqual(rc, 0)
        payload = json.loads(buf.getvalue())
        self.assertIn("overall", payload)
        self.assertIn("completeness", payload["dimensions"])

    def test_missing_file_exit_1(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            rc = main([str(Path(tempfile.mkdtemp()) / "no_such.md")])
        self.assertEqual(rc, 1)
        self.assertIn("不存在", err.getvalue())

    def test_baseline_regression_exit_2(self):
        good_p = self._write("good.md", HEALTHY)
        base_p = Path(tempfile.mkdtemp(prefix="eval_base_")) / "base.json"
        rc = main([str(good_p), "--make-baseline", str(base_p)])
        self.assertEqual(rc, 0)
        # 退化版：去掉四段式结论（核心结论…分歧与待办 段）
        head = HEALTHY.split("\n## 核心结论")[0]
        tail = HEALTHY.split("\n## 裁判裁决\n\n- 得分")[1]
        degraded = head + "\n## 裁判裁决\n\n- 得分" + tail
        bad_p = self._write("bad.md", degraded)
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            rc = main([str(bad_p), "--baseline", str(base_p)])
        self.assertEqual(rc, 2)
        self.assertIn("回归项", err.getvalue())


if __name__ == "__main__":
    unittest.main()
