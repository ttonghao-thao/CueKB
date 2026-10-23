# CueKB 项目核心

更新：2026-10-23。本文维护产品范围；进度、验证记录在[任务板](TASK_BOARD.md)。

## 目标

向客服、语音Agent及其他外部应用提供准确、可核验、负载有界的原文证据。核心链路是多格式解析、局部知识归属、一跳关联、混合检索、可选重排、多证据及结构化出处。最终回答、比例口述、引用呈现与语音播放由调用方负责。

## 当前范围

- PDF、DOCX通过Docling解析；Markdown、TXT通过文本解析器处理。保留原件、解析位置、章节与多位置spans；不承诺任意PDF/OCR表格均无损恢复。
- `evidence-v2`按冻结的解析、归属映射和审核别名快照创建不可变版本；原件重处理与旧事实恢复是新派生版本，不能静默覆盖已发布正文。
- PostgreSQL维护正文、KB ACL、删除、发布指针、任务、关系与模型配置；OpenSearch是BM25/k-NN候选投影。最终证据和上下文在新权威快照复验。
- V2提供exact/hybrid/related、RRF真实融合分数、可选Reranker、局部归属过滤、可追溯上下文和精简诊断；V1保留D21检索形状及原语义。
- 局部产品归属依赖可信映射、审核别名与结构线索。无法判定时保留unknown/ambiguous；相邻或同文档不能自动认作同产品。
- 候选、关系、上下文、输出字节、补召回、重试和总时限均有上限；配置不等于生产达标。没有缓存或跨进程全局模型配额。
- 管理门户复用同一API Key/ACL及文档发布、实体关系、模型配置、generation接口。

## 不变设计

检索相关性与证据充分性分开，当前`evidence_status=unassessed`。不在CueKB内增加生成式答案、业务Agent循环、无限图遍历、独立向量库或新消息中间件。所有消费者明确选择接口版本，不靠静默回退掩盖不兼容。

解析/分块/归属变更通过重处理；Embedding/输入模板或搜索投影变更通过generation重建现有PG事实。generation回退不恢复历史事实。恢复旧事实也不恢复旧权限或自动移植人工关系。

## 交付与边界

完整接口、调用步骤与系统限制分别在[API_GUIDE](docs/API_GUIDE.md)、[INTEGRATION_GUIDE](docs/INTEGRATION_GUIDE.md)、[SYSTEM_CONSTRAINTS](docs/SYSTEM_CONSTRAINTS.md)维护。历史决策保留在[DECISIONS](DECISIONS.md)，已失效的方案文档不再作为接入依据。

质量与性能须在固定文档版本、权限、索引、模型和配置下测量。当前没有真实语料Recall/MRR/完整率或目标主机P95达标结论；本地并发隔离测试也不能替代10并发生产验收。
