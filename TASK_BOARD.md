# CueKB 任务板

## 上期：导入审核与发布可用性（2026-10-28，已提交/待部署验收）

用户现场反馈：多产品片段无法确认、批量通用确认被拒绝、审核应用重复生成待发布版本，以及检索返回`relevance_profile_invalid`。该批修复已经提交至`main`（`8a5aacc`），生产部署与现场验收仍待执行。

| 里程碑 | 状态 | 工作与验收 |
| --- | --- | --- |
| 根因与调用链 | 完成 | 相同原文名称命中重叠目录身份却被当作多产品拆分；UI暴露服务端必拒绝操作；apply总是派生版本；可选校准加载异常在服务创建阶段阻断检索 |
| 分类选择式审核 | 完成 | 后端提供分类/可用操作/建议；复选共同适用产品；同名身份可选定一个；分别赋值规格保留拆分约束；暂缓与排除明确区分 |
| 当前草稿完成发布 | 完成 | 原version_id应用冻结manifest，幂等任务/失败重试不新增版本；已发布历史只读；阻止未应用决定绕过发布；0003迁移保留原始JSON与审核审计 |
| 检索配置故障 | 完成 | 无效校准继续未校准检索并返回degraded诊断；旧词法revision文件返回binding_mismatch；不使用失效阈值，搜索响应结构保持兼容 |
| 验证与交接 | 本地完成 | 真实PG、CPU DOCX、浏览器及契约验证通过；真实OpenSearch/外部模型及生产部署验收待执行 |

交互决定：默认逐项处理待办，以原文、问题类别、产品勾选及确认入口为主；分别赋值的产品规格仍应拆分或保留比较语义。“稍后处理”仍阻塞发布；“本次不纳入检索”保留原件和审核记录，必要依赖不可因排除被静默丢失。普通审核不新增文档或版本。旧代码已产生的派生版本保留，门户打开已有结果继续处理，不自动删除历史。

### 本轮验证

- 最终完整回归：**291 passed、2 skipped、0 failed**，2条既有FastAPI/Starlette弃用警告。使用隔离PostgreSQL 17.6；包含已有0002审核数据升级0003、原草稿发布、并发apply幂等、排除审计/完整源树、已发布只读、失败原任务重试、restore身份及权限、配置失效的HTTP 200/诊断回归。
- Worker投影保护3项受控测试验证仅对匹配当前version_id的review标记清理旧投影，restore继承的其他版本标记不触发清理；实际OpenSearch传输未配置，对应2项集成测试跳过。用户月饼原DOCX没有提供，本轮不声称已用原文件完成现场复验。
- 真实浏览器连接本地FastAPI+隔离PG，使用合成英文文档与受控内存搜索投影：验证多产品复选、通用确认、暂缓仍阻塞、排除后立即启用准备按钮、同一版本准备并发布；版本数仍为1，price精确检索返回原文且错误校准不再阻断请求。默认自动检索也返回HTTP 200；内存分词与真实BM25不同，不据此声称真实检索质量。页面无横向溢出、无浏览器error/warn。
- CPU Docling真实DOCX、TXT/Markdown结构与API上传/发布验证通过；本机按requirements.lock安装依赖，仅Torch使用同版本macOS构建（Linux +cpu轮子不适用于本机），生产锁文件未更改。PDF fixture可读取不等于真实PDF/OCR完整资源链路验收。
- OpenAPI/SQL导出freshness、Python编译、JS语法、Ruff F规则、59个本地文档链接/锚点及git diff检查通过；Wheel构建成功并包含0003迁移与门户资源；完整风格规则仍有导入排序/简写等诊断，不以局部检查声称全仓lint无告警。

### 部署与下一步

已提交至`main`，尚未部署。升级必须使用含0003迁移的新API与Worker镜像，操作步骤见[保留数据升级](docs/OPERATIONS.md#8-s1保留数据升级验证与回退)。部署后按[审核修复验收](docs/ACCEPTANCE.md#76-分类审核原草稿发布与校准故障修复验收)回传结果；生产校准文件的实际内容/权限问题仍需现场诊断，不把本地故障注入当作现场根因取证。

## 本期：S3问题级证据接口（2026-10-30，编码完成/待部署验收）

输入来自voice-service-agent现场验证需求：一次请求内完成完整问题召回、逐问题项支持判断、最小闭合证据选择和终态决策，避免消费端自行拆分及重复检索。本批实现独立`cuekb.evidence/2.1`严格契约、`POST /v2/evidence/query`、短期证据包复验、精简模型视图、真实tokenizer预算门禁及provider fixture；`/v2/search`继续保留且不把其`unassessed`升级为答案充分性。

安全边界：无校准支持模型时只允许可由原文、可信主体、S1源节点和完整必要关系证明的`direct`；不产生`entailed`。语义近似候选保留`unassessed`，技术降级不伪装为知识缺失。voice profile未登记真实tokenizer时拒绝请求，不用字符/词数冒充目标模型token。

### S3本批已实现

- 新增严格`cuekb.evidence/2.1`请求/响应及provider JSON Schema/OpenAPI/真实序列化fixture；新增`POST /v2/evidence/query`和`POST /v2/evidence/validate`，未知字段、修订和枚举拒绝。
- 一次请求复用V2混合召回、adaptive选择和服务内一次补召回；最多8 facet、5证据组。逐项返回direct/absent/ambiguous/conflicting/unassessed及全局answer/partial_answer/clarify/insufficient/conflict；当前不输出未经校准的entailed，continuation不授权重复检索。
- 确认实体在检索前核验并参与产品过滤；单实体使用product_entity_id，多实体使用product_entity_ids集合下推OpenSearch并在PG复验。多个确认实体下问题仍未指明主体时返回clarify，不任选一个。最终PG快照再次核验Key/ACL、内容和ACL修订、当前发布正文、必要块及S1 node映射。变化有界重跑一次；无源节点/依赖不完整不能认证direct。
- 完整审计组与精简model_view同时返回；预算不足整组移除并同步降级facet。standard使用固定tokenizer；voice必须从只读目录加载真实Hugging Face tokenizer JSON及ID，否则409。
- 短期签名包绑定principal/API Key、KB、request ID、input revision、内容/ACL修订与快照；validate只返回valid/stale，篡改、跨请求及跨principal拒绝，不回传旧正文。
- 新增严格生产金标验证器与模板：逐题检查decision/facet/missing、禁止产品、组数、tokenizer/预算、continuation及即时包复验，可重复采集脱敏trace、阶段时序和查询P50/P95/max；具体部署/回传见ACCEPTANCE 7.7。

### 本轮开发验证与现场边界

- 新增14项问题级单元/HTTP测试通过：Golden/Black隔离，组合问题部分回答与shipping absent，必要限定保留，未绑定及多确认主体clarify，多主体OpenSearch过滤，同产品数值conflict，无S1节点unassessed，包内容修订stale与50 KB边界，权威快照变化有界重选，签名篡改/跨主体/跨请求拒绝，整组预算回退同步，voice真实tokenizer/进程缓存门禁及闭合请求契约。
- 隔离真实PostgreSQL 17.6/OpenSearch 3.0.0执行完整回归：306 passed、1 skipped、0 failed（2条既有弃用警告）；skip为当前环境未配置用户月饼原件路径，不解释为语料通过。真实PG确认retrieval_unit_nodes、content/acl revision及当前发布正文在同一权威快照返回，真实OpenSearch传输验证多实体terms过滤。契约生成及freshness、Ruff F/E9、compileall、门户JS语法、金标模板解析、wheel源码清单、API target镜像构建及镜像内tokenizers 0.23.2与问题级路由检查通过；实际部署身份和生产语义质量仍须现场验收。
- 尚未使用原月饼全文、生产OpenSearch/Embedding、实际VoiceChat tokenizer、真实consumer或GPU/音频验收；本地规则通过不代表自动answer质量、P95或重复工具调用目标达标。S3部署后须按ACCEPTANCE第7.7节回传固定金标和时延报告，失败再修复。

## 本期：S1文档核心与门户操作（2026-10-27，编码完成/待部署验收）

用户要求先核对S0，本期完成S1编码，并完成删除导入状态同步、审核子页与返回、审核记录多选批量操作。阶段采用S0–S6，缺口F01–F12及P0/P1优先级在[重构设计](docs/ENTERPRISE_RAG_REDESIGN.md#111-编号与执行依据)维护。

### S0核对结果

- 消费端基线voice-service-agent `aba0b53`，严格契约补齐已推送；本轮重新执行43项相关契约/适配器测试通过，并用本轮CueKB的普通/adaptive/必要上下文3份公开响应验证真实严格消费者接受。
- 调用方已有输入/trace/结果注入阶段与token采集入口。既有432项契约/原生工具集成结果属于其历史开发验证，本轮不重复记为新增432通过。
- 当前回传证明CueKB部署、单条审核/批量校验行为，未包含新版VoiceChat完整时序及冻结运行模型/音频基线。**S0代码/契约检查已完成，现场基线仍待回传，不能声称S0已全部现场验收通过。**此前“按用户确认完成”表述已更正；不将“先核对完成”的要求误记为用户已确认通过。
- 本期继续无依赖的S1编码，生产/GPU测量不由本机测试代替。语音现场执行入口仍是调用方仓库docs/validation.md。

### S1交付范围

| 用户需求/阶段项 | 已实现 |
| --- | --- |
| 删除文档同步导入状态 | 清理该文档所有版本任务卡片与定时器；删除期间返回的旧轮询不复活卡片；服务端审计保留 |
| 审核独立子页/返回 | 审核与高级配置使用普通子页/URL；页面返回及浏览器后退恢复来源操作；同版筛选/勾选/滚动保留；换KB/修订变化清理旧状态 |
| 待确认记录多选 | 用户明确范围为审核记录；卡片勾选/本页勾选，跨页保留最多100条；绑定产品/通用/排除/暂缓只提交勾选ID，同批失败原子回滚并保留选择 |
| 规范源文档 | DocumentGraph/Node、Edge、ScopeAssertion、RetrievalUnit分层；分块前捕获章节、完整段落、列表、表格/表头、代码及显式脚注，真实定位与coarse/limited标识 |
| 结构与版本一致性 | 同名章节独立身份；脚注缺定义阻塞关联事实；必要闭包有界；审核派生/恢复映射新节点与事实ID，保存版本血缘及发布摘要；旧版缺树不伪造 |
| 通用资料与接口 | content_kind=general支持非产品文档，无需逐片段绑定产品；保存来源/生效区间声明；新增V2结构分页查询，权限/撤Key/删除检查 |
| 数据演进 | 包内0001/0002 SQL迁移+checksum，空库SQL同链导出；旧完整基线核对表/列类型/索引/枚举后升级；有待执行导入拒绝升级；事务失败回滚；保留扩展schema的应用回退检查 |
| 部署验证入口 | cuekb-db upgrade/check/rollback-check及cuekb-validate-s1；合成TXT/MD/DOCX/PDF经真实API/Worker验证并导出报告；操作、回退、采集与门户用例已落地 |

S1是文档核心和本批门户能力。S3问题级证据决策已由上节交付，但尚待生产语料验收；仍未交付S2通用隐含关系/异常问题队列、S4消费状态机、S5真实语音性能或S6上线替换。现有/v2/search仍返回unassessed，不把源树complete标成答案充分。

### 开发验证与现场边界

- 隔离真实PostgreSQL 17.6/OpenSearch 3.0.0：最终完整回归272 passed、1 skipped、0 failed（2条既有弃用警告）；包含升级幂等、空库一致性、checksum失配拒绝、原子失败回滚、待执行任务/残缺基线拒绝，以及结构/范围/派生/恢复/发布/ACL测试。
- 真实Chromium＋PG已验证审核子页及刷新后的已登录路由恢复、两页勾选仅提交2条、高级页返回、删除与延迟轮询竞态、无横向溢出/未捕获JS错误；服务端使用隔离测试数据，未测生产或GPU。
- DOCX通过真实CPU Docling；TXT/Markdown树与表格/脚注检查通过；部署验证器用真实API、PG和受控Worker流程验证TXT/MD/DOCX上传/解析/发布。合成PDF文件可由PDFium读取；本机未完成真实Docling PDF/OCR资源链路，不将其计为PDF现场通过。
- 用户原月饼TXT当前执行环境缺失，对应原始语料用例跳过，不以替代语料冒充复验。历史语料覆盖/误归属结果由Git fb4804c及此前任务板保留。
- 隔离pg_dump/pg_restore演练通过，源树摘要、检索块及schema checksum一致；新模块Ruff、Python编译、4个JS语法、契约/schema导出freshness、12份文档88个本地链接/23个锚点与Shell块语法检查通过；Wheel包含SQL迁移、导航资源和采集CLI。
- 不连接或部署用户生产。S1代码交付与现场验收分开；部署后按[操作手册第8节](docs/OPERATIONS.md#8-s1保留数据升级验证与回退)及[验收7.5](docs/ACCEPTANCE.md#75-s1文档核心与门户本批部署验收)执行并回传报告。

### 后续

收到部署报告后逐项记录通过、失败或证据不足；失败针对修复，不要求用户自行诊断。代码推送不等于生产验收；S2、S4–S6仍按计划推进，S0现场基线缺项需另行补采。
