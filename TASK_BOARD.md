# CueKB 开发任务板

更新：2026-10-10。源码基线：`0ab322ffa26351d218712a845726e61decae179c`；后续提交以Git记录为准。M0—M3和管理门户代码已完成；M4待开始；自动关联的离线实验代码已实现，真实质量/生产验收未完成。生成式回答归外部系统（D19）。

## 状态口径

“已完成”指仓库实现完成；“待开始”指明确范围尚未编码；“待确认”指候选范围/参数未确认，不进入编码。生产验收单独记录，代码完成不代表Docker、真实模型、质量或SLA通过。枚举/表结构存在不等于完整能力实现。

## 当前计划与检查点

### 本地更新提交与推送（2026-10-10）

- 用户明确要求将本地更新推送至GitHub `main`；范围为既有架构文档、离线实验模块、合成示例、测试及配套文档，不扩展业务实现。
- 提交前已抓取`origin/main`，与本地基线一致；全部20份更新已提交到本地`main`，工作区干净，本地领先远端1个提交。
- 本轮验证：68项测试通过，11项PostgreSQL集成测试因未配置专用数据库跳过；`pip check`、compileall、示例validate及diff检查通过。`.env`、`.venv`和`data/`输出保持Git忽略。
- 推送阻塞：`git push origin main`返回403，当前Git及GitHub应用账号均为`snowl1122`；GitHub仓库权限明确为`push=false`、`pull=true`。远端`main`未更新。需切换具有写权限的账号或由仓库所有者授予该账号写权限后，再正常推送；未强推或创建替代仓库。
- 提交最终标识以Git记录为准；真实模型、OpenSearch、PG及生产质量/性能验收仍未执行。

### 自动关联实验实现（2026-10-08，离线代码已完成）

- 用户已指示“根据方案设计编码”，并选择“先完成离线实验代码和示例”；按D21交付X00—X05所需的离线工具，不将受控fixture误记为30份真实语料验收。
- 实验范围：同KB、一跳、无LLM、无新增基础设施/依赖；复用现有领域对象与RRF，独立命令入口，不启用线上关联。X06/X07仍按方案要求以真实收益和M4预算契约为工程化前提；两跳/后台LLM不纳入本轮。
- 实现：`src/cuekb/experiments/associations/`包含严格manifest/JSONL与录制候选校验、有限导航构建、带极性/角色条件及quote的规则提取、一跳动态扩展、最终冻结状态复验、H/H+A/H+B/H+C/H-wide运行器、证据/完整覆盖/反例指标及成对家族bootstrap。独立关系/路径标注可评估准确率、召回、错误率及Wilson区间；缺少标注保留null，不自动填100%。
- 示例及使用：[实验说明](experiments/associations/README.md)，10个合成块（含旧版本和另一KB）、5个问题；已执行validate/build/run，生成24条导航边、9条来源验证规则断言和25份五组请求结果，模型调用0。输出位于被忽略的`data/associations/build.json`、`report.json`；指标属于synthetic_fixture，HTTP性能和生产门槛标not_measured/not_evaluated。
- 本轮验证：68项测试通过，11项真实PG测试因未配置`CUEKB_TEST_DATABASE_URL`跳过；新增29项测试覆盖近型号/否定、后缀及元数据条件、quote/旧版/删除/权限、binding和同ID正文变化、篡改断言、exact/off、单轮/预算、指标去重/替代证据、回放无降级伪装、输入限制和原子输出。`pip check`、compileall及diff检查通过；已有Starlette/httpx和AnyIO弃用提示仍在。未新增运行时依赖或迁移，原API/Worker业务代码无改动。
- 边界及下一步：这是可运行的离线工具，不代表X01真实采集/双人标注、X02真实H基线或X03—X05真实质量收益已验收；Reranker对照、通用证据槽位和候选池错误关联率尚未实现。准备30份合法语料、真实候选录制及独立gold后按设计门槛评估，再推进X06增量PG/Worker与X07线上接点。未提交或推送。

### 架构设计记录（2026-10-08，已授权先实施离线阶段）

- 已完成[自动知识关联与动态联想架构评审稿](docs/plans/AUTOMATIC_ASSOCIATION.md)，源码基线`0ab322ffa26351d218712a845726e61decae179c`；核对检索、关系表/类型、版本与ACL复验、Worker/outbox、模型和generation，明确现有能力与新增部分。
- 推荐C-lite：语义导航候选＋规则验证事实，首轮同KB一跳、无查询LLM；以H、H+A、H+B、H+C及候选预算对照判断收益。文档包含4张流程图、逻辑schema、增量失效协议、兼容/回退、约30份英文文档实验、指标和X00—X08任务。
- 边界：只新增/修改设计、导航与本任务板；未修改业务代码、迁移、运行时依赖或accepted决策；未执行关联实验或重跑业务测试。LLM抽取配置例外、两跳和生产启用均待单独评审，M4继续独立推进。
- 文档验证：本地链接、表格列数、JSON示例、4幅Mermaid的静态结构及diff检查通过；未做Mermaid图形渲染验证。保留上轮开发环境记录和其他既有内容，未提交/推送。
- 下一步：等待本稿评审，先确认实验范围、谓词、数据/模型资源及判定门槛，再按X00—X02准备实验；收益、延迟和资源数字均是目标或假设，尚无实测结论。

### 前次依赖与文档检查点（2026-09-25）

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

### 2026-10-08 云工作区开发环境

- 仓库位置：`/workspace/CueKB`；Python 3.12.14；使用内置`venv`创建`.venv`，执行`.venv/bin/python -m pip install -c requirements.lock -e '.[api,dev]'`安装开发/API依赖；`.env`从`.env.example`复制并设为权限600，使用`development`/`memory`模式。
- 本轮验证：39项测试通过，11项PostgreSQL集成测试因未配置`CUEKB_TEST_DATABASE_URL`跳过；`pip check`通过。测试报告Starlette/httpx及AnyIO弃用提示，不影响本轮通过结果。
- 开发服务通过工具托管的长运行会话启动，Uvicorn监听`0.0.0.0:8080`；实际HTTP验证`/v1/health`、`/v1/ready`、`/portal/`、`/docs`和`/openapi.json`返回200，根路径307跳转至门户。日志输出至托管会话；环境或会话结束后需要重新启动。
- 重启命令：在仓库目录运行`.venv/bin/uvicorn cuekb.main:app --host 0.0.0.0 --port 8080`；开发入口为`http://127.0.0.1:8080/portal/`，接口文档为`http://127.0.0.1:8080/docs`。`127.0.0.1`指云工作区本机，外部浏览器需工作区端口转发。
- 边界及下一步：本轮仅搭建开发环境，内存数据重启即清空；未安装Docling入库扩展，未部署PostgreSQL、OpenSearch或Worker，未调用外部模型或执行生产验收。若需要生产环境，按运行手册部署完整依赖并配置Embedding服务；未提交或推送。

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
