# CueKB 任务板

## 门户收尾与动态证据选择（2026-10-25）

六项准确性改进已在47f20bc交付。本轮按用户要求补齐此前因锁屏中断的门户复测，并检查相关编码缺口；当前实现以源码、完整初始化schema和生成契约为准。

| 里程碑 | 状态 | 交付 |
| --- | --- | --- |
| 查询准确性与动态选择 | 已完成 | 同实体归属合并、部分搜索失败识别、可选校准门控、预算替补、新增信息选择、词法投影及关系排序 |
| 门户原文展示修复 | 已完成 | 非空context不再覆盖source_text；单独展示主事实、必要/相关上下文，按chunk身份避免主块重复，保留预算/依赖缺失提示 |
| 真实门户与HTTP对照 | 已完成 | Safari与隔离localhost HTTP完成下列场景；没有使用生产数据或真实模型 |
| 回归、文档与交付 | 已完成 | 全量测试和契约检查通过，新增渲染回归与验收步骤；继续此前授权的提交推送，结果以Git refs和最终回执为准 |

本轮定位的编码问题：当主事实长于上下文预算、context仅容得下条件段时，API仍保留完整source_text，但旧门户使用context优先的表达式，导致主事实不可见。现已独立渲染source_text并补充真实context_parts；V1没有parts时仍保留其上下文回退。没有改动搜索API契约、评分或数据库结构。

## 本轮实际验证

全量测试：**194 passed、16 skipped、2条依赖弃用警告**。新增4项[门户渲染回归](tests/test_portal_evidence.py)实际执行app.js函数，覆盖长主事实及必要条件、身份去重、HTML转义、依赖缺失提示和V1回退；4项均执行通过。16项真实PG测试因未配置专用测试库跳过，不算通过真实PG验收。

| 隔离验证场景 | 实际结果 |
| --- | --- |
| adaptive与ranked对照 | 相同受控候选及上限20，分别返回3条属性证据和5条排序证据；adaptive的正文/可选上下文均无无关介绍 |
| 必要条件 | 一条主命中同时显示“Storage: 45 days.”与“Only when sealed; opened storage is 3 days.”，依赖不重复占主结果名额 |
| 上下文预算 | 5221字符主事实完整保存在API及实际JS渲染输出；Safari显示主事实、必要条件和预算提示，非空context不再隐藏主事实 |
| 依赖缺失与空结果 | unresolved显示明确提示；正常无候选显示not_found，证据充分性均保留unassessed |
| V1与ranked兼容 | 门户请求成功，动态专用字段没有泄漏；V1主事实及上下文可见。隔离V1未配置模型，保留其原有vector_unavailable提示，不声称验证了真实向量召回 |
| HTTP隔离与校验 | 10个同时HTTP请求的trace与KB修订隔离通过；9个facet请求按约定422；排名连续且分数降序 |

上述HTTP请求是真实localhost HTTP，检索数据、候选和模型响应仍为隔离合成夹具；没有将页面显示的毫秒数当作生产P95。此前“修正后页面未复测”的事项已关闭。测试标签页及隔离服务均已关闭。

另已执行：Python编译、JavaScript语法、OpenAPI/JSON Schema/示例freshness、本地文档链接/锚点和diff检查。搜索DTO与生成契约未变化。没有运行Docker镜像构建、真实PG/OS、Docling扫描件或外部模型服务；本机无Docker CLI，也未配置专用测试PG。

## 当前规范

接口在[API_GUIDE](docs/API_GUIDE.md)，接入在[INTEGRATION_GUIDE](docs/INTEGRATION_GUIDE.md)，架构在[SYSTEM_DESIGN](docs/SYSTEM_DESIGN.md)，校准/投影操作在[OPERATIONS](docs/OPERATIONS.md)，限制在[SYSTEM_CONSTRAINTS](docs/SYSTEM_CONSTRAINTS.md)，验收在[ACCEPTANCE](docs/ACCEPTANCE.md)。旧阶段设计、历史测试和发布记录由Git追溯，不维护另一份现行规范。

## 待真实环境验收

1. 专用PG完整初始化、发布/删除/撤权竞争、归属修订持久化、SQL取消与连接释放；真实OS新映射、BM25/k-NN和generation切换。
2. 固定英文通识、产品与技术文档并保留多语言回归，独立人工标注产品/版本、条件/否定、无答案与可替代完整证据方案。
3. 按文档/产品/问题家族隔离calibration/test，录制实际评分并拟合校准；当前未提供或部署适用于真实语料的实测阈值。
4. 同文档版本、授权、候选/字节预算和硬件下，对照ranked/adaptive、无/有Reranker，测Recall/MRR、完整率、误收/误拒、实际precision和负载。
5. 真实10并发测P50/P95/P99、错误/降级率、稳定性和质量；记录冷/热状态、语料规模和模型/网络条件。内存及localhost并发隔离不等于生产吞吐验收。
6. voice-service-agent先升级严格DTO、选择诊断和引用映射，再端到端验证多产品、多属性、比例口述和实际语音。本仓库不代替第三方升级。

## 后续边界

缓存、跨进程模型配额、通用语义覆盖分类、自动冲突裁决、完整OCR质量保障、文档级ACL和历史版本检索尚未实现。自动关联仍为同KB、一跳、无LLM的独立离线实验，PG持久化与线上接入没有隐含启用。

本期采用全新部署，完整db/schema.sql由空PostgreSQL数据目录首次启动加载；不维护既有数据库结构升级链。新词法投影使用scope-lexical-v2.2，原件处理修订为evidence-v2.2。用户本地.zcode/保持原样，不纳入提交。提交/推送结果以Git refs和最终回执为准。
