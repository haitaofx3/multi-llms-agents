# 文档索引

| 文档 | 定位 | 维护状态 |
| --- | --- | --- |
| [`../README.md`](../README.md) | 使用说明：六项能力、配置项、运行与测试方式、已知边界 | **现行（迁移后重写）** |
| [`API.md`](API.md) | 接口参考：7 模块分组 + 横切约定（session_id / 错误形状 / trace 头 / 鉴权）+ SSE 帧与 WS 事件表（清单以 `/api/dev/endpoints` 为准，本文不写死数量） | **现行** |
| [`MIGRATION.md`](MIGRATION.md) | 迁移记录：`mini_test`/`app` → `Muti_LLM_Agent`/`muti_llm_agent` 做了什么、与旧项目的行为差异 | **现行** |
| [`CODE_LOGIC.md`](CODE_LOGIC.md) | 代码逻辑详解：模块职责 → 关键函数 → 数据流（写于迁移前，文中 `app/` 按 `muti_llm_agent/` 等价阅读） | 参考 |
| [`archive/DELIVERY_READINESS_评估快照.md`](archive/DELIVERY_READINESS_评估快照.md) | 可交付性自评**快照**（上线必需 / 工程化 / 质量 / 产品化缺口） | **已过期（已归档）** |
| [`../多Agent辩论系统架构设计文档.md`](../多Agent辩论系统架构设计文档.md) | 架构目标态（Supervisor / 辩手 / Judge / RAG） | 参考 |
| [`../P2优化方案.md`](../P2优化方案.md) | 平台化演进方案（多实例 / 人工审核 / 知识库 / 模板） | 参考 |
| [`../三方Agent框架对比与迁移利弊分析.md`](../三方Agent框架对比与迁移利弊分析.md) | 自研框架与三方框架的取舍分析 | 参考 |
| [`archive/`](archive/) | 历史存档（已过期，仅作决策记录） | **不维护** |

## 归档规则

一份文档一旦「描述的是某个时点的状态」而不是「当前事实」，就移到 `archive/` 并在开头写清过期原因。
留在 `docs/` 根目录的文档必须能对应当前代码——`tests/test_docs_consistency.py` 会检查：
`docs/` 下的现行文档都必须能被本索引链接到，且所有相对链接都可解析。
