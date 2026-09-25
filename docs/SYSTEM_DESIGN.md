# CueKB 当前系统设计

更新：2026-09-22，核对基线：`1b9379d`及当前工作区。本文说明当前实现与边界；进度见[任务板](../TASK_BOARD.md)，未实现设计见[M4](plans/M4.md)及[扩展方案](plans/EXTENSIONS.md)。不以架构描述替代真实环境验收。

## 1. 组件与源码导航

| 组件 | 职责 | 源码（相对仓库根） |
| --- | --- | --- |
| FastAPI与静态门户 | 校验、鉴权入口、管理与证据展示 | `src/cuekb/main.py`、`api/routes.py`、`portal/`（后两项相对`src/cuekb/`） |
| 依赖装配 | 内存/生产选择、匹配模型与索引、客户端生命周期 | `src/cuekb/api/dependencies.py` |
| 检索编排 | 路由、并行召回、RRF、有限重排、最终复验 | `src/cuekb/services/retrieval.py` |
| 权威仓储 | 内容、API Key、KB ACL、发布指针、任务/outbox | `src/cuekb/adapters/postgres.py` |
| 关系与快照 | 实体/别名/证据、一跳查询、REPEATABLE READ | `src/cuekb/adapters/knowledge.py` |
| 搜索投影 | CJK BM25、k-NN、有界候选、模型维度检查 | `src/cuekb/adapters/opensearch.py` |
| 外部模型适配 | Embedding与独立可选Reranker HTTP协议 | `src/cuekb/adapters/model_client.py` |
| 入库Worker | 租约、解析、分块、编码、索引、发布、退避 | `src/cuekb/worker.py`、`src/cuekb/services/parsing.py` |
| 原件与上下文 | 哈希原件、路径边界；章节/近邻/表头预算 | `src/cuekb/adapters/storage.py`、`src/cuekb/services/context.py` |
| generation | 分页重建、验证、维护锁、切换与旧模型重建回退 | `src/cuekb/services/generations.py`、`src/cuekb/adapters/generations.py` |

模块通过领域对象及`src/cuekb/ports.py`边界协作；新增能力优先复用现有适配器，不另建通用执行平台。生产API和Worker连接同一PG、OpenSearch及原件卷；模型服务在Compose之外。API提供门户，门户复用API，不拥有权限裁决权。

开发模式使用`adapters/memory.py`和`services/ingestion.py`同步文本导入；重启丢数据，不鉴权，无真实向量、PDF/DOCX、关系和生产发布事务。生产模式不回退内存。

## 2. 权威数据与一致性

PG表定义由`db/schema.sql`及全部`migrations/versions/`共同确定，不能只看初始DDL。0002增加加密模型配置，0003增加关系与generation并回填章节。升级使用增量迁移，不改初始迁移冒充升级。

- `principals/api_keys/kb_grants`管理身份、密钥哈希和知识库read/write/admin；正文范围不从请求体身份字段推断。
- `documents/document_versions/document_publications`区分逻辑文档、版本和唯一`default`发布指针；`deleted_at`优先于发布状态。
- `sections/chunks`保存权威正文、标题路径与锚点；OpenSearch只返回候选ID，最终正文从PG读取。
- `ingestion_jobs/outbox_events`支持持久任务和同事务记录的派生清理；索引最终一致不改变PG裁决。
- `entities/entity_aliases/mentions/relation_assertions/relation_evidence`保存管理员维护且有出处的关系；支持与反驳是证据立场，不是事实真伪判定。
- `model_configurations/index_generations`保存模型配置、加密凭据、物理索引与重建状态；新旧向量不能混用。

每次检索尝试共享REPEATABLE READ。完成候选处理后，开启新事务复验API Key、ACL、修订、发布版本、关系与上下文；修订变化最多重新检索一次。连续变化时过滤当前合法结果并标记`index_transition`。保证范围是最终复验快照；不能撤回已经发送的字节。过滤不足返回较少结果，当前没有专门的补召回循环。

## 3. 入库、发布与删除

生产调用链：`routes`校验格式/大小/授权和幂等键 → 本地原件按摘要保存 → PG文档/版本/任务 → Worker租约及续租 → Docling/OCR或文本解析 → 基础质量检查与结构分块 → 外部Embedding → OpenSearch写入并等待refresh → PG保存正文/章节并进入`ready` → 手工或请求显式启用的自动发布。

发布事务切换`default`指针、递增内容修订并写outbox；Worker清理旧投影。索引中尚未发布或已失效的块不得通过PG最终校验。上传成功和`ready`均不等于已发布。Worker采用确定性chunk ID、有限重试和有界退避；同幂等键同payload返回原任务，冲突payload拒绝。

当前按整文件重新解析新版本，不做文件内部增量差分。删除立即设置PG软删除及修订并写outbox；不等后台清理才隐藏。未提供恢复API、原件硬删除或备份清除承诺（C08）。

当前解析按结构项/段落及字符预算分块，长表格重复已有表头；不是Tokenizer级分块。PDF/DOCX使用Docling首个provenance构造锚点；没有完整多页多锚点表达。基础门禁不等于已完成页覆盖、OCR置信、阅读顺序或复杂表格质量判定。改进项见M4的Q01/Q02。

## 4. 检索路径与边界

`routes.search` → API Key与KB ACL → 读取匹配的模型/generation → `RetrievalService.search_evidence` → 路由 → OpenSearch候选 → PG正文 → 可选重排 → 上下文/关系 → 新事务最终复验 → 证据JSON。

| 路径 | 当前执行 |
| --- | --- |
| `exact` | BM25，跳过Embedding和重排；生产仍要求已保存Embedding配置 |
| `hybrid` | BM25与query Embedding/向量召回并行，RRF融合，可选重排 |
| `related` | 混合检索加有证据一跳，`scope_limited=true` |
| `auto` | 配置的确定性标识符正则选择exact或hybrid |

query/文档向量保持同模型和编码配置。RRF按排名融合；重排只对有限头部候选处理。Reranker未配置直接跳过；已配置还检查开关、候选数与剩余预算。当前没有独立模型配额或在线/入库资源隔离；429经外部HTTP错误进入降级，不等于实现本地负载调度。

关系起点来自显式实体、名称/审核别名精确匹配及合法候选mention，最多20个起点；默认最多30条证据、100ms关系SQL预算，仅一跳。方向、类型、时间与显式条件过滤，条件不匹配不扩展，不自动推断因果或传递关系。

上下文当前默认最多8块/命中、4000字符/命中、12000字符/响应；按原文及近邻组装，逐块锚点与截断标记随结果返回。不补造缺失表头、单位或事实。实际限制以`config.py`为准。

### 故障与未实现能力

向量失败可返回合法BM25并标记`vector_unavailable`；重排失败保留RRF并标记`rerank_unavailable`。关系SQL使用局部statement_timeout和savepoint，超时保留普通候选并报告降级。PG或关键词后端不可用返回错误，不能伪装正常无结果或绕过权限。

`search_deadline_ms`用于阶段预算，尚非端到端硬截止：同步future等待、执行器退出、PG读和最终复验不共享可取消总预算；重试会重建内部计时。M4-02处理这些缺口。

当前`evidence_status`固定为`unassessed`。`needs_clarification`虽在枚举中，未实现自动澄清判定；无命中是`not_found`，不是“客观上不存在”。当前filters只有文档ID、产品型号和软件版本；不提供历史版本检索或`version_policy`。显式历史版本原件下载不等于可检索历史版本。

## 5. generation与模型

重建使用PG维护锁：Worker分页读取当前published及ready权威块，编码到独立物理索引并验证。内容及模型配置写入受锁约束；查询继续读取旧generation，Key吊销与ACL更新仍可执行。重建期间故障可按有界退避重试；取消/失败索引不自动清理。

管理员切换前复验源修订、模型、维度和块数，再在同一PG事务切换模型配置与active_index。过期重建拒绝切换。回退用retired记录的旧配置重建当前PG数据，再验证切换，不恢复旧权限或旧正文。当前只注册`structured-v1`，generation不负责重新解析或任意改变分块。

操作及维护窗口见[运行手册](OPERATIONS.md#m3升级与索引重建)。模型HTTP协议单独维护在[模型集成](MODEL_INTEGRATION.md)，管理HTTP契约见[API指南](API_GUIDE.md)。

## 6. 优化设计入口

优先完成[M4](plans/M4.md)中的错误追踪、硬截止、资源隔离、指标、评测和证据评估；再用测量决定分块/OCR优化。不引入生成式检索循环或新的基础设施。

OIDC、对象存储、多scope、文档ACL、缓存、连接器、保留和HA均保持[候选方案](plans/EXTENSIONS.md)状态。部署、恢复和性能目标见[验收设计](ACCEPTANCE.md)，已接受取舍见[决策记录](../DECISIONS.md)。
