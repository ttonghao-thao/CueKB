# CueKB 任务板

更新：2026-10-23。本轮已获用户授权实施证据检索设计、整理第三方接口文档，并提交推送到main；部署不在本次范围。

## 实施计划

| 里程碑 | 状态 | 交付 |
| --- | --- | --- |
| 核查现有链路和D23兼容边界 | 已完成 | 保留PG/OS/Worker、既有V1检索DTO及用户未提交修改 |
| 解析、局部归属与必要上下文 | 已完成 | evidence-v2、真实spans、冻结映射、依赖完整性、unknown/ambiguous |
| 持久化与版本演进 | 已完成 | 0004迁移、原件重处理、全新ID恢复、generation源profile快照 |
| V2混合排序与运行控制 | 已完成 | RRF、可选重排、最终复验、有限补召回、异步HTTP、绝对时限与有界输出 |
| 第三方契约与文档清理 | 已完成 | OpenAPI/JSON Schema/示例、接入说明、约束、部署及验收说明；删除已替代方案 |
| 最终回归与差异审查 | 本地已完成 | 实际结果如下，生产及语音验收另列 |

## 本轮验证

| 本轮操作 | 实际结果与边界 |
| --- | --- |
| `.venv/bin/pytest -o addopts='' -q` | **112 passed、14 skipped**；14项PG测试因未设置专用测试库URL跳过；2条Starlette/httpx/AnyIO依赖弃用警告 |
| 有风险路径的fixture/HTTP测试 | 真实RRF/可选重排、同主体过滤、冻结映射/原文spans、必要上下文/字节预算、一次补取及版本重试、最终复验、10个重叠请求隔离、transport容量/修订切换、取消交接、异常内容不回显 |
| 编译及脚本 | compileall、portal JavaScript语法、部署shell语法、pip check、git diff --check均通过；未运行Ruff/Pyright |
| 契约和文档 | export-contracts --check通过；28个路径/35个HTTP操作、V2 Schema/示例、精确hits字节、13份Markdown链接/锚点通过检查 |
| 迁移 | `0003_relations_generations:head --sql`成功生成0004升级SQL，head正确；**未在真实PG执行**。尝试全链离线SQL时，既有0001使用MockConnection不支持的exec_driver_sql，未通过；不将此描述为全链迁移已验证 |
| 本地启动 | uvicorn memory后端启动、health/ready/docs/OpenAPI及合成文本V2 exact检索通过，trace/原文正确，验证后已停止 |
| 浏览器门户 | 实际浏览器完成V2检索展示和显式切换V1检索；没有JS执行错误。memory模式模型配置探测返回预期409，不能解释为生产模型验证 |

以上不代表真实PG/OpenSearch/Docling/OCR/外部模型、生产负载或voice-service-agent端到端验收。没有实测Recall/MRR/证据完整率或生产P50/P95/P99数字。未启动Docker或调用外部模型；保留原有D23工作及`.zcode/`。

## main发布计划

- 用户已明确授权提交和推送；发布前重新执行112项测试通过、14项PG测试跳过，契约、编译、脚本语法及pip check通过。
- 远端新增b813cbb/81ce289包含离线关联实验，已保留其代码、示例、测试和研究方案；合并仅有文档冲突，生产源代码无冲突。保留远端D21，将原本地D21检索增量决策改编号D23，协议不变。
- 合并后本轮重新验证：**141 passed、14 skipped、2条依赖弃用警告**；PG跳过原因不变。契约freshness、compileall、15份Markdown本地链接、离线示例validate均通过；远端实验源码、合成数据和测试保持逐文件一致。以上仅为本地验证，不替代真实服务验收。
- 仅提交项目代码、测试、契约和文档；`.zcode/`保持本地、不纳入提交。不强推；最终远端同步状态以Git refs及交付回执为准。

## 待真实环境执行

1. 在专用测试PG执行0004升级和数据库测试，验证锁、取消、发布/删除竞争、恢复与旧清理隔离；迁移无自动降级，先备份。
2. 在真实OpenSearch重建V2投影并切换generation，核对BM25、向量、模型修订、过滤与发布一致性。
3. 固定英文通识、产品和技术文档，人工标注归属、条件、否定、冲突、无答案与等价证据；测Recall@K、MRR、证据完整率、误归属和空结果效果。
4. 分别验证无Reranker及有Reranker，以10个并发请求测端到端P50/P95/P99、错误率、稳定性与质量；冷/热状态、语料规模和硬件须入报告。
5. 与voice-service-agent同步严格DTO、引用映射及预算；再验证多产品、多属性、比例口述与实际语音。不改动其他项目作为本轮隐含交付。

## 后续范围

缓存、跨进程模型配额、自动覆盖分类器、冲突裁决、完整OCR质量保障、文档级ACL和历史版本检索尚未实现。是否实施由真实验收缺口决定，不预设新增服务。接口边界及当前资源上限以[系统约束](docs/SYSTEM_CONSTRAINTS.md)为准。

## 独立离线关联实验（远端合并保留）

C-lite离线代码已完成，使用方法见[实验说明](experiments/associations/README.md)，研究方案见[自动关联方案](docs/plans/AUTOMATIC_ASSOCIATION.md)。同KB、一跳、无LLM、无新增基础设施，仅消费冻结文件；真实语料/盲审、录制基线、X06持久化和X07线上接入未验收。历史发布与开发环境记录用Git追溯，不恢复为当前API能力说明。
