# CueKB 开发任务板

版本：v0.1。更新时间：2026-09-21。

当前编码状态：**M0、M1、M2、M3及管理门户已完成；M4待开始；生成式回答待确认。**

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
| 关系管理与检索 | 实体/审核别名/mention、支持及反驳证据、条件/时间/方向、一跳RRF融合；删除/版本过滤 | `adapters/knowledge.py`、`api/routes.py`、`schemas.py` |
| 完整上下文 | 章节层级写入与历史回填、相邻块、表头单位保留、字符/块预算与逐块锚点 | `services/context.py`、`services/parsing.py`、`adapters/postgres.py` |
| 事务快照 | 单次尝试共享REPEATABLE READ；新事务复验API Key/ACL/版本/关系/上下文，最多重试一次 | `adapters/knowledge.py`、`services/retrieval.py` |
| 索引generation | 持久重建任务、分页编码/验证、维护锁、原子切换模型与索引、旧模型重建回退；注册分块版本structured-v1 | `adapters/generations.py`、`services/generations.py`、`worker.py` |

## 待开始

| 能力 | 待实现内容 | 计划阶段 |
| --- | --- | --- |
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
| M3 轻量关系与上下文 | **已完成** | 关系、章节上下文、事务快照、索引generation |
| M4 质量与运行能力 | **待开始** | 证据判定、评测工具、统一错误和运行指标 |
| M5 门户与可选回答 | **部分完成** | 首期门户已完成；生成式回答及其部署边界待确认 |

## 验证活动

该表只记录验证是否实际执行，不用于判断功能编码是否完成。

| 验证活动 | 状态 | 当前记录 |
| --- | --- | --- |
| 本地单元/API/静态检查 | **已完成** | M3共50项Pytest（含11项实际隔离PostgreSQL 17.6测试）、Ruff check/format、Pyright 0 error、编译、JS/Shell语法、Compose/OpenAPI及diff检查通过；门户受控集成验证通过。具体环境与边界见下方M3检查点。 |
| 冻结依赖安全审计 | **已完成** | `pip-audit`检查全部extras，未发现已知漏洞 |
| Docker目标环境启动 | **待开始** | 当前环境无Docker CLI；OpenSearch 3启动需设置`CUEKB_OPENSEARCH_INITIAL_ADMIN_PASSWORD`。API runtime已包含Alembic迁移依赖、`migrations/`与`db/schema.sql`；生产Compose将容器内8080映射至宿主机`0.0.0.0:8085`。在Linux目标环境按README先停止旧API/Worker、重新构建两镜像并执行0003迁移/启动，核对镜像大小和Worker的`torch.version.cuda is None`，再完成门户模型配置、生产验收脚本及门户浏览器检查 |
| 真实语料质量评测 | **待开始** | 中文扫描件、复杂表格、型号/版本、无答案和多证据样本 |
| 时延与负载测试 | **待开始** | 记录硬件、模型、候选数、QPS、P50/P95和降级率 |
| 故障与恢复演练 | **待开始** | Worker崩溃、重复事件、索引延迟、模型超时、撤权、删除和PG不可用 |
| 备份恢复演练 | **待开始** | PostgreSQL与原文件备份恢复、OpenSearch重建 |

## 当前检查点

- P1至P4以及M0至M2的编码状态均为**已完成**。
- `exact`路径跳过Embedding和重排；`hybrid`执行BM25与向量召回；未配置重排地址时默认跳过重排，配置后继续受开关、deadline、候选数和模型负载控制。
- 生产模式强制使用PostgreSQL和OpenSearch，不会回退内存模式；未配置Embedding时可启动，但导入/检索返回409。外部模型三元组由系统管理员在门户维护，API与Worker在运行时读取。
- 首期门户编码已完成；下一项核心编码工作从M4开始；生成式回答、OIDC、对象存储、多`scope`和高可用拓扑保持**待确认**。
- 外部Embedding部署已从项目Compose、镜像、依赖和代码入口移除；Embedding和Reranker通过独立 OpenAI-compatible API的`base_url`、`api_key`和`model`调用。
- 应用镜像由操作员手动构建，Compose只使用本地镜像；模型密钥经`pgcrypto`加密，`CUEKB_MODEL_CONFIG_KEY`需稳定保存；现有索引不接受不匹配的Embedding Model。
- API与Worker默认本地镜像标签统一为`cuekb-api:latest`和`cuekb-worker:latest`；`CUEKB_API_IMAGE`和`CUEKB_WORKER_IMAGE`仍可覆盖为自定义标签。
- 部署脚本在Worker运行后继续等待API Docker healthcheck为`healthy`，再访问`/v1/ready`；API未就绪或健康检查失败时输出最近容器日志并退出。
- API与Worker使用独立dependency extra和精简runtime层；Worker锁定PyTorch CPU wheel，锁文件不再包含CUDA、cuDNN、NCCL或Triton包。Docling仍保留完整standard解析能力，避免在未完成真实语料回归前改变PDF OCR和表格行为。

## 云主机8085直连配置（2026-09-20）

- 已将API端口映射改为`0.0.0.0:8085:8080`，直接提供HTTP服务，不引入Nginx，不使用80/443端口；数据库和OpenSearch仍仅在容器网络内访问。
- README明确私网IP、公网`122.51.233.77`、域名`th.ppy123.xyz`的门户/API地址、DNS与TCP 8085放行要求、现有实例重建API容器及验证步骤。私网原始输入`10.0.05`需以云控制台核实，未擅自固化为`10.0.0.5`。
- 验证：Compose YAML解析、8085映射/数据服务不暴露端口断言、三个Host头的本地TestClient门户访问、`git diff --check`通过。TestClient提示现有httpx集成弃用警告，不影响检查结果。本机无Docker CLI，未执行Compose运行验证；云主机容器重建、DNS、安全组和三种地址实际访问仍待目标环境执行。
- 下一步：在云主机更新配置并重建API容器，完成本机、私网、公网IP和域名的就绪检查与鉴权API访问。

## M3实施计划（2026-09-20）

1. 关系与上下文：新增增量迁移；实体、审核别名、mention、带支持/反驳证据的关系管理；解析章节层级和表头保留；有界一跳及上下文拼装。
2. 一致性：检索权威读取使用同一REPEATABLE READ事务；返回前以新事务重新检查权限、版本、关系和上下文，最多重试一次。
3. generation：数据库记录模型/维度/分块版本；Worker分页重建、验证；事务切换索引与模型配置；回退重新构建旧配置，避免返回过期投影。重建期间采用维护锁拒绝内容写入，读取继续使用原generation。
4. 接口/门户/文档与验证：管理接口、门户related模式及上下文显示、风险测试、实际隔离PG迁移/事务验证和静态检查；区分本地验证与云端OpenSearch/Docker验收。
5. 发布：复核diff和远端main，提交本次M3及已确认8085修改，推送并核对远端哈希。

状态：M3代码、管理门户、文档及本地验证已完成；交付分支为main，提交记录以Git为准。未引入新生产基础设施、生成式模型或无限图遍历。

- 本次验证：50项Pytest全部通过（含11项实际PostgreSQL集成测试），Ruff check/format、Pyright（0 error）、Python编译、JavaScript语法、迁移SQL语法及diff检查通过。Pyright的4项warning来自本地API环境未安装可选Docling；测试有现有Starlette/httpx弃用提示。
- 数据库测试：临时目录中运行隔离PostgreSQL 17.6，覆盖新增迁移、旧章节回填、Worker章节持久化、证据跨库拒绝、条件/版本/删除过滤、SQL超时savepoint恢复、REPEATABLE READ、维护锁及锁内撤权、generation验证失败/过期拒绝、切换/回退和重试退避。未使用云主机业务数据。
- 浏览器验证：实际门户连接临时PG与受控内存搜索，完成实体创建、证据关系保存、exact/related查询和generation排队；截图布局正常、控制台无error/warn。没有调用真实Embedding或OpenSearch；该记录不是云部署或检索质量验收。
- 补充修复：旧publication outbox事件只清理PG中明确superseded的版本，保留新发布及ready投影；公网HTTP入口使用getRandomValues生成UUID，避免randomUUID安全上下文限制。
- 剩余验收：Docker镜像/Compose、真实OpenSearch及外部模型重建、真实PDF/OCR和语料质量、云主机私网/公网/域名8085访问。
- 下一步：按README先停旧API/Worker，构建两镜像、运行迁移0003并部署；在目标环境完成真实模型generation重建/切换/回退验收。核心后续编码阶段为M4。
