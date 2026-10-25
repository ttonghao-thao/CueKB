# 当前系统设计

更新：2026-10-25。本文描述已落地代码；接口字段在[接口规范](API_GUIDE.md)，未执行验收在[任务板](../TASK_BOARD.md)。

## 1. 模块与权威边界

| 模块 | 当前职责 |
| --- | --- |
| api/routes.py、api/v2.py、schemas.py | 鉴权后的V1管理/兼容检索、显式V2证据接口及严格DTO |
| services/retrieval.py、retrieval_v2.py | 分离的V1/V2检索流程；V2有真实融合评分、统一重排窗口及有界输出 |
| services/selection.py | V2动态证据集合、字面覆盖与保守差异保护、固定绑定的可选相关性门控 |
| services/parsing.py、evidence.py、context.py | 解析、来源位置、可信局部归属、必要依赖和上下文组装 |
| adapters/postgres.py、knowledge.py | PG权威内容/ACL/发布/删除/关系及读快照；V2读任务取消与SQL预算 |
| adapters/opensearch.py | BM25和k-NN候选ID/分数、V2投影映射、同步Worker与异步在线访问 |
| adapters/model_client.py | 外部Embedding与可选Reranker协议、索引回序和数值校验 |
| worker.py、services/ingestion.py | 租约任务、原件解析/恢复、编码、投影校验、ready/发布及outbox清理 |
| adapters/generations.py、services/generations.py | 独立投影重建、源profile快照、维护栅栏及模型/物理索引原子切换 |
| services/execution.py、api/v2_dependencies.py | 请求绝对时限、阶段统计、原生异步transport复用及进程内容量边界 |
| portal/ | 同源静态管理门户，显式选择V2或V1；始终单独展示命中原文，按真实chunk身份补充必要/相关上下文，保留预算及依赖缺失提示 |
| admin.py | Portal之外的HTTP管理命令，创建/列举KB、签发业务Key及授权；不绕过API直接写库 |

OpenSearch只返回候选ID/分数，不作为正文或权限权威。所有返回正文、归属、出处与关系来自PG，最终新事务复验；原件存储只供解析与授权下载。生产要求PG+OS，memory后端只为开发与测试。

## 2. 原件、版本和处理快照

一个逻辑document有多个不可变document_version，每个版本拥有独立chunk ID、原件hash和processing_profile。`structured-v1`为保留的旧处理方式；新导入默认使用`evidence-v2:<snapshot hash>`，快照冻结服务器审核实体/别名、客户端可信映射及处理配置。

API不接受客户端伪造approved_entities、来源spans或source_kind。实体ID必须属于目标KB；显示名取冻结实体。映射选择器是章节路径前缀和/或source_unit_id，处理时未匹配即失败。映射冲突保留ambiguous；body提及某产品不能自动成为该产品事实。

原件重处理复用存储文件，以新处理快照产生新版本；恢复复制旧PG事实、spans和结构，重新生成版本/块ID并重映射必要依赖，然后按当前输入模板编码。恢复不会复活被删除的文档/权限，也不复制人工关系。普通ingest/reprocess按文档+hash+profile防重复；有意restore允许同内容的新派生。

任务基于PG幂等键、租约和有界重试。Worker索引后校验已写ID、版本、原文及search_text，随后在租约、删除和模型配置修订栅栏下保存ready事实；publish才改变唯一default可见指针并递增内容修订。旧投影由outbox清理，即使清理延迟也不得进入最终返回。

## 3. 结构化证据和归属

Chunk保留`source_text`、`title_path`、兼容anchor、多位置source_spans和EvidenceStructure。结构含block_kind、source_unit_id、applicability、context_links及未解决依赖标志。spans可携带原始位置、parser item、表格行列；原解析器未提供的精度保持空，不造页码或bbox。

Markdown保留章节、段落、列表引导条件和表格整行/表头关系。长原块拆分仍共享源单元身份，必要续块直接关联，不能依赖无界递归。Docling保留其实际provenance；PDF/DOCX复杂表格的可靠程度须按真实样本检验。

applicability仅在可信映射或审核别名与结构匹配时resolved；未确认unknown，多个不一致结论ambiguous。同一稳定实体的缺失型号/版本与人工绑定按兼容信息合并，真正不同的非空型号/版本继续冲突；不同主体集合不被强行合并。归属与document metadata分开。V2同时按产品/软件过滤时，两个值须命中同一个resolved subject。evidence-v2.2处理修订冻结审核别名为内部retrieval_aliases，仅resolved归属可供词法投影，不增加对外subject字段。

必要依赖的读取与可选近邻先经过同KB/document/version、主体与条件兼容检查，再考虑位置；不靠同文档越过产品边界。入库拒绝悬空、跨版本、自引用、重复及循环依赖。依赖或预算不足显式unresolved/truncated，不将不完整上下文标成完整。

`search_text`可加入可信标题/产品/表头以改善召回，原文不改写。Embedding、generation与rerank输入复用同一模板构造函数；旧索引缺模板标识时按legacy处理，不默默按新模板查询。

## 4. V2请求链路

1. HTTP入口创建trace和绝对deadline；校验请求、principal及全部KB权限。按模型配置修订借用有容量上限的模型/OS客户端，并检查V2投影版本。
2. exact只做词法候选；hybrid并行做BM25与query Embedding/k-NN；related还在PG做有证据的一跳关系扩展。所有候选数、时间和调用次数有界。
3. 在权威快照读取当前可见chunk，过滤删除、ACL、文档及局部归属，剔除纯标题与不合法关系。V2的BM25/向量各以1权重参与RRF；关系先按查询词法信号排序，以0.5权重参与，最终复验失效关系同时移除其分数贡献。原始分数保留诊断，不跨量纲相加。
4. 归一正文去重时同时保留版本、主体、条件、否定等差异。有足够预算且实际配置Reranker才执行重排；未配置正常跳过。成功重排只返回同一窗口，故障整体回RRF并如实降级。
5. 新PG事务再次检查Key、ACL、内容修订、模型/索引绑定、全部候选/上下文/关系及原文结构。内容转变可在同一deadline内重试一次；模型绑定变化不能复用旧向量继续检索。
6. 在最终权威快照批量读取有限必要依赖，构造最小组；ranked按原排序选择可放入预算的组，adaptive按新增信息选择。过大组跳过后继续考察候选，必要组全部选定后才分配可选近邻。按精确UTF-8 JSON字节成本返回整块，不切半个事实。结果按原RRF/重排顺序编号，返回真实分数。

召回不足，或adaptive明确给出的query_facets尚未在候选原文观察到时，可全请求补召回一次：提高每通道候选上限、用新列表替代旧列表，复用同一query向量，不累计重复通道RRF贡献。不因返回条数未满循环检索。版本重试共享补召回和绝对时间预算。

### 4.1 动态证据选择

`selection_mode=adaptive`显式启用；默认ranked保持兼容。候选数、重排窗口和返回上限是三个独立概念。选择器每轮计算新facet/查询词项观察、已知必要依赖的补齐和新增数值/否定/条件差异，并用精确字节成本控制负载。没有新观察时仅允许保守文本多样性增益；已观察的范围重复则停止，不以凑满top_k为目标。数值、业务版本、局部主体/条件差异保留；没有普适的语义去重或全库冲突裁决。

每个请求的InformationSelector、候选、已选集合和覆盖观察独立。必要条件、表头及归属保持真实身份；已包含在必要上下文的同一块不再次占主结果名额。可选近邻排除其他结果已返回的块，并要求同原始单元、明确条件/例外链接或新增查询信息，避免单凭邻近注入无关介绍。共享必要依赖仍可重复序列化，实际重复字节完整计费，现有契约不改成悬空ID引用。

可选校准文件由`scripts/calibrate-retrieval.py`从冻结人工标签生成，按Embedding、Reranker、配置revision、active generation、词法revision及检索路径绑定。重排校准存在时优先使用；否则使用已校准向量/字面覆盖信号的OR接受策略，无适用分数时保留unassessed候选。未配置或绑定失配明确诊断，不能把未经校准的分数断崖、RRF名次或本查询归一化值当成无答案概率。

动态选择在候选耗尽、无新收益或到达数量/字节/上下文预算时停止；deadline始终覆盖整个流程。诊断的字面facet观察与evidence_status均不认证问题已完整回答。可选selection_query补充原始问题的选择语境，实际召回仍使用query，不能从短query恢复被消费端省略的信息。

`evidence_status`保持unassessed。去重、上下文、融合与多片段返回改善可用证据，不代表已证明全部属性齐备或冲突已裁决。关系supports/refutes保留来源立场，不决定事实真伪。

## 5. 异步、时限与隔离

V2模型和OS请求使用原生httpx.AsyncClient，按配置修订复用；切配置不关闭仍在使用的旧transport，退出后回收。单API进程默认16个active lease，超过直接503，最多4个同时存活修订；不是跨Worker/进程的模型配额。

PG驱动为同步psycopg：V2将一个完整读批次放入有执行上下文的工作线程；连接获取、SQL statement/lock timeout均受剩余预算约束。取消时驱动cancel_safe有界发起取消，数据库/驱动完成清理可能晚于HTTP deadline，不能声称外部GPU计算已终止。每个请求独立SearchExecution、候选和快照，transport共享不共享证据容器。

调用方断开会取消V2请求；错误响应只返回安全代码/trace。日志只记录事件、阶段、耗时和字节，不默认输出私有正文、query或Key。当前无查询缓存、自动全库冲突裁决和全局模型调度器。

## 6. generation与升级

generation维护锁阻止内容写入，查询继续。重建读取PG已存事实，记录build_source_profiles及其聚合标识，不重新解析原件。新投影带V2词法/局部归属字段、scope_text/table_labels/identifier_terms、`lexical_revision=scope-lexical-v2.2`及输入模板revision。验证完成后同一PG事务切换物理index、模型配置及revision。新字段由PG已冻结的结构与别名生成，不从查询或任意metadata猜归属。

解析/归属升级先重处理、人工检查并publish；Embedding/投影升级再generation。旧V1投影不具备V2字段，V2返回INDEX_UPGRADE_REQUIRED而不假装已使用新路径。旧配置rollback也重建当前PG事实，不将旧index直接当成当前内容。

完整数据库结构由`db/schema.sql`定义，包含处理快照、派生血缘、结构spans、同文档source_version FK、防重复部分索引、实体/关系、模型配置和generation。PostgreSQL首次在空数据目录启动时自动加载，整个建库文件在一个事务内执行；TCP健康后API/Worker才能启动。该流程不处理既有数据库，操作说明见[操作手册](OPERATIONS.md)。

## 7. 独立离线关联实验

远端main的C-lite实验保留在`src/cuekb/experiments/associations/`，使用显式冻结manifest、JSONL语料和候选录制，比较H/H+A/H+B/H+C/H-wide。它复用领域对象和RRF，执行同KB一跳导航、规则事实验证及离线评测，不读生产配置、不调用模型、不向PG/OS写数据、不接入V2生产检索链路。

使用方法见[实验说明](../experiments/associations/README.md)。真实语料、独立gold、Reranker对照、PG持久化及线上接点仍待独立验收。研究方案中的新表、自动关联参数和模型扩展不是当前对外契约；线上接入须复用V2的SearchExecution、最终权限/版本复验和预算，并单独验证兼容性。
