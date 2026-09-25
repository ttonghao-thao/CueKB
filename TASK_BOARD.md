# CueKB 开发任务板

更新：2026-09-25。源码核对基线：`1b9379d`及当前工作区。M0—M3和管理门户代码已完成；M4待开始。生成式回答归外部系统（D19）。

## 状态口径

“已完成”指仓库实现完成；“待开始”指明确范围尚未编码；“待确认”指候选范围/参数未确认，不进入编码。生产验收单独记录，代码完成不代表Docker、真实模型、质量或SLA通过。枚举/表结构存在不等于完整能力实现。

## 当前计划与检查点

- 本轮范围：保留并交付已完成的文档重组；从开发、依赖解析和Docker构建链路移除额外依赖管理器，改为Python内置`venv`、pip和单一生产约束文件；完成本地验证并提交、推送`main`。
- 依赖实现：删除原锁文件及全部活跃配置/命令引用；`requirements.lock`以118个精确版本覆盖Python 3.12/Linux的API与Docling Worker联合闭包；Docker按角色执行pip安装，Worker先从PyTorch CPU索引安装锁定Torch，其余依赖只从PyPI解析，最终镜像仍不包含构建缓存。
- 已完成的文档重组：核对入库/发布、检索/复验、关系/上下文、generation、模型、schema/迁移和部署文档；拆出运行手册、模型契约、验收设计、M4与候选方案；精简默认入口；旧原文含历史检查点以SHA-256清单留存。
- 文档重组纠正：无历史检索version_policy、无自动澄清、无端到端硬取消、无模型配额、无过滤后专门补召回；当前PDF/DOCX只取首个provenance，不能声称多页多锚点完整支持。
- 文档重组验证：13份活跃文档的57个本地链接/锚点、7份归档SHA-256、17处完整源码路径、API指南显式方法/路径与30条路由定义、当前filters/评估字段边界、M4/Q/C编号及`git diff --check`检查通过。无业务测试、部署或外部模型调用。
- 加载量：AGENTS+PROJECT_CORE+TASK_BOARD由整理前约37KB缩至约13KB（UTF-8字节，约64%减少，非精确token测量）；细节按专题加载，历史不默认读取。
- 本轮验证：39项本地测试通过、11项真实PostgreSQL测试因未配置专用数据库而跳过；API pip约束解析通过；Worker联合闭包在本机等价Torch版本下解析通过；Linux CPython 3.12/manylinux_2_28 CPU Torch解析通过；`pip check`、`compileall`、TOML解析、118项约束唯一/精确/排序、活跃文件旧命令与配置扫描、`git diff --check`通过。
- 未验证边界：本机没有Docker，未实际构建API/Worker镜像；未执行PostgreSQL、OpenSearch、真实模型、云主机或生产验收。跨平台Worker检查分别验证完整闭包与Linux CPU Torch，不能替代目标Linux Docker构建。
- 下一步：在有Docker的目标环境分别构建API/Worker并核对Worker的`torch.version.cuda is None`，随后按运行手册完成M3真实服务验收；功能开发继续从M4-01开始，C02—C09保留待确认。

## 已完成

下表中的`adapters/`、`api/`、`services/`及Python文件路径均相对于`src/cuekb/`。

| 能力 | 已完成内容 | 主要代码或配置 |
| --- | --- | --- |
| 工程与部署 | pip约束且按角色拆分Python依赖、CPU版Docling/Torch、精简runtime镜像、多阶段Dockerfile、手动镜像构建、Compose本地镜像引用、迁移门禁、健康检查和部署脚本 | `pyproject.toml`、`requirements.lock`、`Dockerfile`、`docker-compose.yml`、`scripts/deploy-production.sh` |
| 权威仓储 | PostgreSQL知识库、文档、版本、正文、任务、发布指针、ACL、outbox及加密模型配置仓储；Alembic初始迁移和模型配置迁移 | `db/schema.sql`、`migrations/`、`adapters/postgres.py` |
| 身份与权限 | API Key签发、哈希保存、吊销；read/write/admin知识库ACL；检索和原文返回前复验 | `security.py`、`api/routes.py`、`adapters/postgres.py` |
| 语料导入 | 文本与multipart文件接口；PDF、DOCX、Markdown、TXT；原文件保存；Docling/OCR；基础质量门禁和结构化分块 | `api/routes.py`、`services/parsing.py`、`adapters/storage.py` |
| 后台任务 | 幂等提交、PG任务租约、续租、确定性chunk ID、有界退避、失败状态、自动发布和outbox清理 | `worker.py`、`adapters/postgres.py` |
| 发布与删除 | 新版本写入、ready、手动/自动发布、唯一`default`版本、版本切换、撤权、删除和索引清理事件 | `api/routes.py`、`worker.py`、`adapters/postgres.py` |
| 关键词检索 | OpenSearch CJK BM25、知识库/文档/产品/版本过滤和有界候选 | `adapters/opensearch.py`、`services/retrieval.py` |
| 语义检索 | OpenAI-compatible BGE-M3外部API、文档向量、查询向量、OpenSearch k-NN、模型和索引维度校验 | `adapters/model_client.py`、`adapters/opensearch.py` |
| 检索编排 | `auto`/`exact`/`hybrid`/`related`路由、BM25与向量并行召回、RRF、PG最终过滤和版本切换重试 | `services/retrieval.py` |
| 重排与降级 | 独立 OpenAI-compatible 重排API；未配置时默认跳过；配置后执行model校验、有界候选、deadline门禁/外部429故障降级和故障降级 | `adapters/model_client.py`、`services/retrieval.py` |
| 对外接口 | 知识库、API Key、ACL、文件/文本导入、任务、发布、原文、删除、检索和健康接口 | `api/routes.py`、`schemas.py`、`docs/API_GUIDE.md` |
| 管理门户 | API Key连接、知识库选择、批量文件导入、任务轮询、文档与版本管理、原文下载、删除、检索验证及系统管理员模型配置 | `portal/`、`api/routes.py`、`adapters/postgres.py` |
| 自动化检查 | API/检索/解析/路径选择/配置门禁/密钥/文件边界测试及生产验收脚本 | `tests/`、`scripts/acceptance-production.py` |
| 关系管理与检索 | 实体/审核别名/mention、支持及反驳证据、条件/时间/方向、一跳RRF融合；删除/版本过滤 | `adapters/knowledge.py`、`api/routes.py`、`schemas.py` |
| 完整上下文 | 章节层级写入与历史回填、相邻块、表头单位保留、字符/块预算与逐块锚点 | `services/context.py`、`services/parsing.py`、`adapters/postgres.py` |
| 事务快照 | 单次尝试共享REPEATABLE READ；新事务复验API Key/ACL/版本/关系/上下文，最多重试一次 | `adapters/knowledge.py`、`services/retrieval.py` |
| 索引generation | 持久重建任务、分页编码/验证、维护锁、原子切换模型与索引、旧模型重建回退；注册分块版本structured-v1 | `adapters/generations.py`、`services/generations.py`、`worker.py` |


## 待开始与建议顺序

具体设计、影响模块、兼容性和验收见[M4方案](docs/plans/M4.md)。

| 编号 | 工作包 | 依赖/完成标准 |
| --- | --- | --- |
| M4-01 | 统一错误与trace | 保留detail/状态码，贯穿鉴权、校验、异常与门户 |
| M4-02 | 总deadline和取消 | 共用绝对预算、覆盖PG与最终复验、重试不重置、无失控后台任务 |
| M4-03 | 模型资源隔离 | 在线/批量配额与背压，说明跨进程/外部服务边界 |
| M4-04 | 指标与诊断 | 随01—03铺设，低基数、脱敏、积压/降级/取消可观测 |
| M4-05 | 质量评测工具 | 多语言冻结语料、消融、质量与延迟；准备可先行 |
| M4-06 | 证据状态评估 | 依赖05校准；不适用时仍unassessed，不以分数判事实真实 |

旧设计待实现目标统一收录为[M4评测驱动优化项](docs/plans/M4.md#8-评测驱动优化项)：Q01 Tokenizer分块、Q02高级OCR/表格门禁与多锚点、Q03有界补召回、Q04行业词典/澄清。先以真实样本确认收益和范围，不作为本轮新增编码承诺。旧设计的持久审计/search trace纳入M4-04方案，保留期另行落定。

## 待确认

| 编号 | 候选能力 | 启动条件 |
| --- | --- | --- |
| C02 | OIDC | IdP、最终用户身份与授权责任明确 |
| C03 | 对象存储 | 跨主机/容量需求及存储目标明确 |
| C04 | 多scope发布 | 业务范围选择、缺省与冲突规则明确 |
| C05 | 文档ACL | KB拆分不能满足真实权限场景 |
| C06 | 检索缓存 | 测量证明重复查询收益，命中仍PG复验 |
| C07 | 平台连接器 | 首个平台、权限映射及撤回时限明确 |
| C08 | 保留与硬删除 | 恢复期、备份期限、共享原件和审计规则明确 |
| C09 | 高可用 | SLA/RTO/RPO、容量与预算明确；先恢复演练 |

[候选详细方案](docs/plans/EXTENSIONS.md)仅按项读取。C01已确认不纳入CueKB，保留编号与D19决策，不再作为待完成工作。若上线SLA或保留规则为硬要求，提前确认C08/C09，不等M4全部结束。

## 验证活动

历史结果来自整理前仓库记录，本轮未重跑。详见[历史检查点](docs/archive/2026-09-22-before-reorganization/INDEX.md)。

| 活动 | 已有证据或待执行内容 |
| --- | --- |
| M3本地验证（历史） | 50项Pytest通过，含11项隔离PostgreSQL 17.6；Ruff、Pyright 0 error、编译、JS/Shell/迁移及diff检查；Docling可选依赖warning与httpx弃用提示已记录 |
| 门户验证（历史） | 实际浏览器+临时PG+受控内存搜索，实体/关系/exact/related/generation排队通过；非真实模型/OpenSearch验收 |
| 依赖安全审计（历史） | 当时pip-audit未发现已知漏洞；不是当前重新审计结论 |
| Docker/云主机 | 待目标环境构建、0003迁移、CPU Torch核对、门户配置及私网/公网/域名8085访问 |
| 真实模型/OpenSearch | 待导入、发布、权限/删除及generation重建/切换/回退验收 |
| 质量/负载/恢复 | 待多语言真实语料、扫描件/复杂表格、并发导入、故障与PG/原件备份恢复 |

执行方法与待确认门槛见[验收设计](docs/ACCEPTANCE.md)，命令见[运行手册](docs/OPERATIONS.md)。剩余条件：专用运行环境、合法授权的真实语料和模型服务、容量/质量目标及保留要求。提交与推送状态以Git和本次交接为准。
