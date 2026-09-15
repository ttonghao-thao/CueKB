# CueKB 开发任务板

版本：v0.1。更新时间：2026-09-15。

当前编码状态：**M0、M1、M2已完成；M3、M4待开始；M5待确认。**

## 状态口径

| 状态 | 唯一含义 |
| --- | --- |
| **已完成** | 对应功能已有完整代码、配置或文档实现 |
| **待开始** | 需求和方向已经明确，但功能代码尚未开始实现 |
| **待确认** | 候选范围尚未获得明确需求确认，不进入编码 |

编码状态只描述仓库中的实现进度。生产环境运行、真实语料质量、性能和恢复演练记录在“验证活动”中，不改变已经完成的编码状态。

## 已完成

下表中的`adapters/`、`api/`、`services/`及Python文件路径均相对于`src/cuekb/`。

| 能力 | 已完成内容 | 主要代码或配置 |
| --- | --- | --- |
| 工程与部署 | 冻结Python依赖、多阶段Dockerfile、Compose、迁移门禁、健康检查、生产配置门禁和部署脚本 | `pyproject.toml`、`uv.lock`、`Dockerfile`、`docker-compose.yml`、`scripts/deploy-production.sh` |
| 权威仓储 | PostgreSQL知识库、文档、版本、正文、任务、发布指针、ACL和outbox仓储；Alembic初始迁移 | `db/schema.sql`、`migrations/`、`adapters/postgres.py` |
| 身份与权限 | API Key签发、哈希保存、吊销；read/write/admin知识库ACL；检索和原文返回前复验 | `security.py`、`api/routes.py`、`adapters/postgres.py` |
| 语料导入 | 文本与multipart文件接口；PDF、DOCX、Markdown、TXT；原文件保存；Docling/OCR；基础质量门禁和结构化分块 | `api/routes.py`、`services/parsing.py`、`adapters/storage.py` |
| 后台任务 | 幂等提交、PG任务租约、续租、确定性chunk ID、有界退避、失败状态、自动发布和outbox清理 | `worker.py`、`adapters/postgres.py` |
| 发布与删除 | 新版本写入、ready、手动/自动发布、唯一`default`版本、版本切换、撤权、删除和索引清理事件 | `api/routes.py`、`worker.py`、`adapters/postgres.py` |
| 关键词检索 | OpenSearch CJK BM25、知识库/文档/产品/版本过滤和有界候选 | `adapters/opensearch.py`、`services/retrieval.py` |
| 语义检索 | 固定修订BGE-M3、文档向量、查询向量、OpenSearch k-NN、模型和索引维度校验 | `model_server.py`、`model_client.py`、`adapters/opensearch.py` |
| 检索编排 | `auto`/`exact`/`hybrid`/`related`路由、BM25与向量并行召回、RRF、PG最终过滤和版本切换重试 | `services/retrieval.py` |
| 重排与降级 | 固定修订reranker、有界候选、deadline和负载门禁、模型故障降级、执行阶段返回字段 | `model_server.py`、`model_client.py`、`services/retrieval.py` |
| 对外接口 | 知识库、API Key、ACL、文件/文本导入、任务、发布、原文、删除、检索和健康接口 | `api/routes.py`、`schemas.py`、`docs/API_GUIDE.md` |
| 自动化检查 | API/检索/解析/路径选择/配置门禁/密钥/文件边界测试及生产验收脚本 | `tests/`、`scripts/acceptance-production.py` |

## 待开始

| 能力 | 待实现内容 | 计划阶段 |
| --- | --- | --- |
| 轻量关系写入 | 实体、别名、关系及来源证据的DDL、写入服务和管理接口 | M3 |
| `related`关系扩展 | 知识库内有界一跳查询、候选融合及权限/版本最终过滤 | M3 |
| 完整上下文组装 | 父章节、相邻步骤、表头、单位和总上下文预算 | M3 |
| 索引generation管理 | 新模型/分块配置的自动建索引、验证、切换和回退 | M3 |
| 单事务读取快照 | 在一次检索中用数据库事务覆盖全部权威读取步骤 | M3 |
| 证据状态评估 | `sufficient`、`insufficient`、`conflicting`判定及可追溯评估字段 | M4 |
| 统一错误契约 | 所有错误响应统一提供`error_code`和`trace_id` | M4 |
| 请求硬截止与取消 | 将总deadline覆盖PG、OpenSearch、模型及并行子任务，并停止超时后的后台工作 | M4 |
| 模型资源隔离 | 为批量入库与在线查询配置独立模型队列或资源配额 | M4 |
| 质量评测工具 | 标注集、冻结测试集、BM25/向量/重排消融、无答案校准和多证据集合指标 | M4 |
| 运行指标 | 指标导出、任务积压、模型队列、降级率、发布延迟、告警和运行看板 | M4 |

## 待确认

| 候选能力 | 需要确认的范围 |
| --- | --- |
| Web门户 | 搜索、导入、原文和版本管理页面的首期页面范围 |
| 生成式回答 | 是否提供回答API、模型部署方式、引用格式和客服系统责任边界 |
| OIDC身份适配 | 生产身份提供方、issuer、audience、角色和知识库映射 |
| 对象存储适配 | 目标对象存储产品、凭据方式、加密、保留和备份策略 |
| 多`scope`发布 | 可见范围模型、优先级、冲突规则和API契约 |
| 文档级ACL | 是否需要文档级权限，以及与知识库ACL的组合规则 |
| 应用级检索缓存 | 是否需要缓存、缓存介质、容量和一致性策略 |
| 外部平台连接器 | 需要接入的平台、同步方向、权限和增量协议 |
| 数据保留与硬删除 | 软删除恢复期、硬删除时机、备份到期清除和审计要求 |
| 高可用拓扑 | 目标SLA、QPS、RTO/RPO、PG/OpenSearch副本和模型容量 |

## 里程碑编码状态

| 阶段 | 编码状态 | 范围 |
| --- | --- | --- |
| M0 工程与版本基线 | **已完成** | 依赖锁、模型revision、迁移、容器和部署入口 |
| M1 文档与关键词检索 | **已完成** | 导入、解析、原件、PG、权限、版本、删除和BM25 |
| M2 混合检索 | **已完成** | Embedding、向量召回、RRF、重排、deadline路由和降级 |
| M3 轻量关系与上下文 | **待开始** | 关系写入、一跳扩展和完整上下文预算 |
| M4 质量与运行能力 | **待开始** | 证据判定、评测工具、统一错误和运行指标 |
| M5 门户与可选回答 | **待确认** | 门户、生成式回答及其部署边界 |

## 验证活动

该表只记录验证是否实际执行，不用于判断功能编码是否完成。

| 验证活动 | 状态 | 当前记录 |
| --- | --- | --- |
| 本地单元/API/静态检查 | **已完成** | 13项Pytest、Ruff、Pyright、compileall、Compose schema、YAML、SQL解析、OpenAPI和文档示例通过 |
| 冻结依赖安全审计 | **已完成** | `pip-audit`检查全部extras，未发现已知漏洞 |
| Docker目标环境启动 | **待开始** | 执行Compose构建、迁移、健康检查和生产验收脚本 |
| 真实语料质量评测 | **待开始** | 中文扫描件、复杂表格、型号/版本、无答案和多证据样本 |
| 时延与负载测试 | **待开始** | 记录硬件、模型、候选数、QPS、P50/P95和降级率 |
| 故障与恢复演练 | **待开始** | Worker崩溃、重复事件、索引延迟、模型超时、撤权、删除和PG不可用 |
| 备份恢复演练 | **待开始** | PostgreSQL与原文件备份恢复、OpenSearch重建 |

## 当前检查点

- P1至P4以及M0至M2的编码状态均为**已完成**。
- `exact`路径跳过Embedding和重排；`hybrid`执行BM25与向量召回；重排受配置、deadline、候选数和模型负载控制。
- 生产模式强制使用PostgreSQL、OpenSearch、固定模型revision和API密钥，不会回退内存模式。
- 下一项编码工作从M3开始；门户、生成式回答、OIDC、对象存储、多`scope`和高可用拓扑保持**待确认**。
- 本轮修改尚未提交或推送远程。
