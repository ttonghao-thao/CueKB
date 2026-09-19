# CueKB 开发任务板

版本：v0.1。更新时间：2026-09-19。

当前编码状态：**M0、M1、M2及首期管理门户已完成；M3、M4待开始；生成式回答待确认。**

## 当前优化计划（2026-09-19）

1. 拆分 API/Worker 镜像的手动 `docker build` 与仅引用本地镜像的 Compose 部署。
2. 将 Embedding/Reranker 服务配置迁至 PostgreSQL，提供系统管理员门户配置入口；API/Worker 在运行时读取，并保留索引模型一致性约束。
3. 按API、Worker运行角色拆分Python依赖；API移除Docling/Torch和未使用的Uvicorn标准extras，Worker移除Web/迁移依赖并固定CPU版PyTorch。
4. 使用builder/runtime镜像阶段和BuildKit缓存挂载，最终镜像不携带uv、依赖下载缓存及构建文件。
5. 更新部署、架构与 API 文档，运行针对性测试及静态检查；Docker 实机验证单独记录。

状态：编码与本地静态验证已完成；Docker目标环境验收待执行。

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
| 工程与部署 | 冻结且按角色拆分Python依赖、CPU版Docling/Torch、精简runtime镜像、多阶段Dockerfile、手动镜像构建、Compose本地镜像引用、迁移门禁、健康检查和部署脚本 | `pyproject.toml`、`uv.lock`、`Dockerfile`、`docker-compose.yml`、`scripts/deploy-production.sh` |
| 权威仓储 | PostgreSQL知识库、文档、版本、正文、任务、发布指针、ACL、outbox及加密模型配置仓储；Alembic初始迁移和模型配置迁移 | `db/schema.sql`、`migrations/`、`adapters/postgres.py` |
| 身份与权限 | API Key签发、哈希保存、吊销；read/write/admin知识库ACL；检索和原文返回前复验 | `security.py`、`api/routes.py`、`adapters/postgres.py` |
| 语料导入 | 文本与multipart文件接口；PDF、DOCX、Markdown、TXT；原文件保存；Docling/OCR；基础质量门禁和结构化分块 | `api/routes.py`、`services/parsing.py`、`adapters/storage.py` |
| 后台任务 | 幂等提交、PG任务租约、续租、确定性chunk ID、有界退避、失败状态、自动发布和outbox清理 | `worker.py`、`adapters/postgres.py` |
| 发布与删除 | 新版本写入、ready、手动/自动发布、唯一`default`版本、版本切换、撤权、删除和索引清理事件 | `api/routes.py`、`worker.py`、`adapters/postgres.py` |
| 关键词检索 | OpenSearch CJK BM25、知识库/文档/产品/版本过滤和有界候选 | `adapters/opensearch.py`、`services/retrieval.py` |
| 语义检索 | OpenAI-compatible BGE-M3外部API、文档向量、查询向量、OpenSearch k-NN、模型和索引维度校验 | `model_client.py`、`adapters/opensearch.py` |
| 检索编排 | `auto`/`exact`/`hybrid`/`related`路由、BM25与向量并行召回、RRF、PG最终过滤和版本切换重试 | `services/retrieval.py` |
| 重排与降级 | 独立 OpenAI-compatible 重排API；未配置时默认跳过；配置后执行model校验、有界候选、deadline/负载门禁和故障降级 | `model_client.py`、`services/retrieval.py` |
| 对外接口 | 知识库、API Key、ACL、文件/文本导入、任务、发布、原文、删除、检索和健康接口 | `api/routes.py`、`schemas.py`、`docs/API_GUIDE.md` |
| 管理门户 | API Key连接、知识库选择、批量文件导入、任务轮询、文档与版本管理、原文下载、删除、检索验证及系统管理员模型配置 | `portal/`、`api/routes.py`、`adapters/postgres.py` |
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
| M0 工程与版本基线 | **已完成** | 依赖锁、模型标识、迁移、容器和部署入口 |
| M1 文档与关键词检索 | **已完成** | 导入、解析、原件、PG、权限、版本、删除和BM25 |
| M2 混合检索 | **已完成** | Embedding、向量召回、RRF、重排、deadline路由和降级 |
| M3 轻量关系与上下文 | **待开始** | 关系写入、一跳扩展和完整上下文预算 |
| M4 质量与运行能力 | **待开始** | 证据判定、评测工具、统一错误和运行指标 |
| M5 门户与可选回答 | **部分完成** | 首期门户已完成；生成式回答及其部署边界待确认 |

## 验证活动

该表只记录验证是否实际执行，不用于判断功能编码是否完成。

| 验证活动 | 状态 | 当前记录 |
| --- | --- | --- |
| 本地单元/API/静态检查 | **已完成** | 本次镜像精简后20项Pytest、Ruff check/format、compileall、JavaScript语法、Shell语法、Compose YAML与Dockerfile结构解析、`uv lock --check`通过；Pyright检查`src`为0 error（未在API开发环境安装可选Docling产生4个warning）。独立API环境共36包、97MB且无Docling/Torch；独立Worker环境共112包、1.2GB且无FastAPI/Uvicorn/Alembic，CPU Torch、Docling导入、Markdown与DOCX表格解析通过。环境大小来自macOS arm64虚拟环境，不等同于Linux Docker镜像大小。 |
| 冻结依赖安全审计 | **已完成** | `pip-audit`检查全部extras，未发现已知漏洞 |
| Docker目标环境启动 | **待开始** | 当前环境无Docker CLI；OpenSearch 3启动需设置`CUEKB_OPENSEARCH_INITIAL_ADMIN_PASSWORD`。API runtime已包含Alembic迁移依赖、`migrations/`与`db/schema.sql`；生产Compose将容器内8080映射至宿主机`127.0.0.1:8085`。在Linux目标环境重新构建API镜像后执行Compose迁移/启动，核对镜像大小和Worker的`torch.version.cuda is None`，再完成门户模型配置、生产验收脚本及门户浏览器检查 |
| 真实语料质量评测 | **待开始** | 中文扫描件、复杂表格、型号/版本、无答案和多证据样本 |
| 时延与负载测试 | **待开始** | 记录硬件、模型、候选数、QPS、P50/P95和降级率 |
| 故障与恢复演练 | **待开始** | Worker崩溃、重复事件、索引延迟、模型超时、撤权、删除和PG不可用 |
| 备份恢复演练 | **待开始** | PostgreSQL与原文件备份恢复、OpenSearch重建 |

## 当前检查点

- P1至P4以及M0至M2的编码状态均为**已完成**。
- `exact`路径跳过Embedding和重排；`hybrid`执行BM25与向量召回；未配置重排地址时默认跳过重排，配置后继续受开关、deadline、候选数和模型负载控制。
- 生产模式强制使用PostgreSQL和OpenSearch，不会回退内存模式；未配置Embedding时可启动，但导入/检索返回409。外部模型三元组由系统管理员在门户维护，API与Worker在运行时读取。
- 首期门户编码已完成；下一项核心编码工作从M3开始；生成式回答、OIDC、对象存储、多`scope`和高可用拓扑保持**待确认**。
- 外部Embedding部署已从项目Compose、镜像、依赖和代码入口移除；Embedding和Reranker通过独立 OpenAI-compatible API的`base_url`、`api_key`和`model`调用。
- 应用镜像由操作员手动构建，Compose只使用本地镜像；模型密钥经`pgcrypto`加密，`CUEKB_MODEL_CONFIG_KEY`需稳定保存；现有索引不接受不匹配的Embedding Model。
- API与Worker默认本地镜像标签统一为`cuekb-api:latest`和`cuekb-worker:latest`；`CUEKB_API_IMAGE`和`CUEKB_WORKER_IMAGE`仍可覆盖为自定义标签。
- API与Worker使用独立dependency extra和精简runtime层；Worker锁定PyTorch CPU wheel，锁文件不再包含CUDA、cuDNN、NCCL或Triton包。Docling仍保留完整standard解析能力，避免在未完成真实语料回归前改变PDF OCR和表格行为。
