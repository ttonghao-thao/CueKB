# 当前系统设计

更新：2026-10-26。本文是当前架构与设计的主文档；接口字段在[接口规范](API_GUIDE.md)，验证方法在[验收方法](ACCEPTANCE.md)，实际需求复核/验证结果及未验收项只在[任务板](../TASK_BOARD.md)维护。全部CueKB接口、处理链与验证统一为V2。

## 1. 模块与权威边界

| 模块 | 当前职责 |
| --- | --- |
| api/routes.py、api/v2.py、schemas.py | V2管理、证据检索与文件处理接口及严格DTO |
| services/retrieval_v2.py | V2真实融合评分、统一重排窗口、最终权威复验及有界输出 |
| services/selection.py | V2动态证据集合、字面覆盖与保守差异保护、固定绑定的可选相关性门控 |
| services/parsing.py、evidence.py、context.py | 解析、来源位置、可信局部归属、必要依赖和上下文组装 |
| adapters/postgres.py、knowledge.py | PG权威内容/ACL/发布/删除/关系及读快照；V2读任务取消与SQL预算 |
| adapters/opensearch.py | BM25和k-NN候选ID/分数、V2投影映射、同步Worker与异步在线访问 |
| adapters/model_client.py | 外部Embedding与可选Reranker协议、索引回序和数值校验 |
| worker.py、services/ingestion.py | 租约任务、原件解析/恢复、编码、投影校验、ready/发布及outbox清理 |
| adapters/generations.py、services/generations.py | 独立投影重建、源profile快照、维护栅栏及模型/物理索引原子切换 |
| api/middleware.py、services/execution.py、api/v2_dependencies.py | 完整ASGI请求绝对时限、阶段统计、原生异步transport复用及进程内容量边界 |
| portal/ | 同源静态管理门户，V2支持ranked/adaptive选择；始终单独展示命中原文，按真实chunk身份补充必要/相关上下文，保留预算及依赖缺失提示 |
| admin.py | Portal之外的HTTP管理命令，创建/列举KB、签发业务Key及授权；不绕过API直接写库 |

OpenSearch只返回候选ID/分数，不作为正文或权限权威。所有返回正文、归属、出处与关系来自PG，最终新事务复验；原件存储只供解析与授权下载。生产要求PG+OS，memory后端只为开发与测试。

## 2. 原件、版本和处理快照

一个逻辑document有多个不可变document_version，每个版本拥有独立chunk ID、原件hash和processing_profile。导入使用`evidence-v2:<snapshot hash>`，快照冻结服务器审核实体/别名、客户端可信映射及处理配置。

API不接受客户端伪造approved_entities、来源spans或source_kind。实体ID必须属于目标KB；显示名取冻结实体。映射选择器是章节路径前缀和/或source_unit_id，处理时未匹配即失败。映射冲突保留ambiguous；body提及某产品不能自动成为该产品事实。

原件重处理复用存储文件，以新处理快照产生新版本；恢复复制旧PG事实、spans和结构，重新生成版本/块ID并重映射必要依赖，然后按当前输入模板编码。恢复不会复活被删除的文档/权限，也不复制人工关系。普通ingest/reprocess按文档+hash+profile防重复；有意restore允许同内容的新派生。

任务基于PG幂等键、租约和有界重试。Worker索引后校验已写ID、版本、原文及search_text，随后在租约、删除和模型配置修订栅栏下保存ready事实；publish才改变唯一default可见指针并递增内容修订。旧投影由outbox清理，即使清理延迟也不得进入最终返回。

## 3. 结构化证据和归属

Chunk保留`source_text`、`title_path`、单一位置anchor、多位置source_spans和EvidenceStructure。结构含block_kind、source_unit_id、applicability、context_links及未解决依赖标志。spans可携带原始位置、parser item、表格行列；原解析器未提供的精度保持空，不造页码或bbox。

Markdown保留章节、段落、列表引导条件和表格整行/表头关系。长原块拆分仍共享源单元身份，必要续块直接关联，不能依赖无界递归。Docling保留其实际provenance；PDF/DOCX复杂表格的可靠程度须按真实样本检验。

applicability仅在可信映射或审核别名与结构匹配时resolved；未确认unknown，多个不一致结论ambiguous。同一稳定实体的缺失型号/版本与人工绑定按兼容信息合并，真正不同的非空型号/版本继续冲突；不同主体集合不被强行合并。归属与document metadata分开。V2同时按产品/软件过滤时，两个值须命中同一个resolved subject。处理快照冻结审核别名为内部retrieval_aliases，仅resolved归属可供词法投影，不增加对外subject字段。reason_codes区分未提供映射、未匹配审核信息、缺少结构及冲突；不会根据查询、文件名或上传标签补产品。

必要依赖的读取与可选近邻先经过同KB/document/version、主体与条件兼容检查，再考虑位置；不靠同文档越过产品边界。入库拒绝悬空、跨版本、自引用、重复及循环依赖。依赖或预算不足显式unresolved/truncated，不将不完整上下文标成完整。

### 3.1 归属审核与必要章节

evidence-v2.5将产品判定与结构依赖分开。Worker按真实章节边界生成section，重复同名章节保留独立身份；表格逐行使用独立source_unit_id，续块共享该行的源单元。内部parser_unit_key及冻结的origin_unit_aliases用于重处理定位，restore同时重映射关系两端的来源ID，不能把旧块ID当作新事实。

自动必要关系只识别原文中显式的中英文章节引用及能确定类型的适用范围/条件/例外标题，含数字章节号。独立条件标题、位置相邻或正文提及产品都不会自动产生依赖。缺失/重名引用、数量溢出、循环或明确归属冲突保留未解决诊断；不递归读取全文。其他语种、隐含语义及任意远距指代由审核配置补齐，不宣称自动理解。

人工dependency_bindings按精确章节路径或原版本源单元选择source/target，声明必要角色与审核说明，并随处理快照冻结。每块最多20条直接关系；模糊或未匹配人工选择器、自引用及环使任务明确失败且不自动重试。关系链在查询时不跟随第二跳；审核应将主事实需要的条件直接列入同一有界组。

ContextLink.provenance记录parser_reference、curated或parser_continuation及两端源单元；原文引用还必须存在于主块原文。只有这些来源可核验的必要关系可以连接unknown块，且仍拒绝跨KB/document/version、ambiguous、明确产品或条件冲突。普通近邻不因unknown而放开。返回context_parts携带每块自己的applicability与link_provenance，不继承主事实的产品归属。

审核接口在写权限与同一PG读快照中读取包括ready版本的事实，返回全版本汇总和有界分页、当前可重用的映射配置。门户默认先ready后审核；发布必须通过服务端资格校验，auto_publish遵守同一规则，有阻塞则保持ready并显示needs_review。归属unknown不等于未审核，已确认的通用资料仍可发布。

`search_text`可加入可信标题/产品/表头以改善召回，原文不改写。Embedding、generation与rerank输入复用同一模板构造函数；缺少有效模板标识的索引明确拒绝，不按另一种处理方式猜测文本。

### 3.2 规则导入与选择式审核

`fact_rules.py`复用冻结产品目录，按标题、列表、句内明确主体与表格产品轴处理。TXT可检测Markdown标记及编号标题；text_mode=plain关闭标题探测。列表按源事实拆分，条件引导段保留；只有每个子句都有明确主体才自动按分号/句界拆分多产品段。多产品列拆为带产品标题、行名和值的独立证据，原始spans保留来源，不重复附带其他产品值。

事实拥有独立source_unit_id、parser_unit_key及parent_unit_id。适用范围与审核状态独立：`applicability=resolved/unknown/ambiguous`；`review_state=pending/rule_confirmed/human_confirmed/deferred/excluded`。fact_kind区分statement/specification/comparison/bundle/general。通用资料可unknown且已确认；套件使用独立bundle实体，组件不继承其规格或价格。无型号产品可按稳定product_entity_id过滤，不需要虚构型号；身份、型号与软件版本同时提供时须命中同一个主体。比较事实需明确确认，保留比较原文而不抽成单品属性。用户可通过复选框显式确认整段同一结论适用的多个主体，无需特定语言的Both/均关键词；分别赋值或混合规格仍须拆分或保留比较语义。同一原文位置匹配多个目录身份单列为catalog_collision，由用户核对选择，不自动合并实体。

明确的审核映射、唯一结构产品、可验证的句内产品属性可以rule_confirmed；多产品或套件身份不明转待办。型号匹配保留字母数字边界，A100不匹配A1000；模糊猜测不构成归属。所用规则为scope-rules-1，不调用LLM、不自动训练。KB可保存版本化processing_templates，配置精确通用章节标题、领域属性词及文本模式；处理任务冻结模板内容和修订，更新模板/目录不改变旧版本。模板不执行自定义代码。

ready事务创建review_plan及review_facts，保存完整的不可变初始事实和可编辑草稿。显式创建接口复用该计划；无需再次解析，202返回当前计划。审核UI显示原文、位置、产品/数值、疑点、最多5个候选及操作，按待办或全部事实分页。用户选择目录产品、通用资料、拆分范围、比较/套件身份及必要条件，常规操作无需JSON、源ID或自由填写。新产品信息可预填；目录新增/批量导入及模板写入沿用KB admin，审核与应用为KB write。

决定按plan revision进行乐观锁校验，一批最多100项且事务原子提交；追加review_decisions记录认证主体/Key、选择、时间与修订。撤回追加新决定；拆分覆盖所有非空原文，不能编辑值或丢掉中间限定。排除只移除当前未发布草稿的检索事实，保留原件和审计；排除必要条件仍阻塞主事实。拆分已有依赖的事实须先通过当前结构/高级配置保存完整关系，不能用拆分清除依赖警告。

apply锁定计划、未发布版本并校验全部事实，冻结manifest摘要，复用PG幂等任务在**原version_id**准备检索投影。仅从未发布的ready草稿允许原位更新；已发布/替换版本只读，显式reprocess/restore仍产生新版本。旧发布内容不会被审核点击改写。Worker校验manifest、同KB/文档/版本，重建块ID与关系并保留原源树节点身份；原片段完整JSON和追加审核记录独立于可替换chunks保存。Worker先删除该草稿旧投影，再写入并核验全部新投影，PG事务替换事实/映射后恢复ready。准备期间版本不可发布；有尚未应用的人工决定时也不得发布旧事实。无需每次选择启动Embedding，完成审核时统一准备；已应用计划冻结，重复apply返回同一Job，失败后显式apply重试同一Job/版本。

发布在同一PG事务检查所有非排除事实：必须已确认、无归属冲突、无缺失必要依赖，未知主体须为已确认通用角色；至少一个事实可检索。直接API、UI、auto_publish与restore均遵守门槛。直接API/审核写入复验Key与ACL，自动发布复验任务主体当前写权限。新ready版本在显式publish前不切换原发布指针。

门户以分类卡片及原文为主，后端提供category、allowed_actions、recommended_action、guidance；多产品逐项勾选，批量产品确认按各卡片选择提交，不把异质记录强制绑定单个产品。暂缓仍阻塞发布；不纳入检索保留原件与审计，准备前可撤回。门户支持本页确认及跨页筛选集合。跨页操作先在PG冻结fact_ids、digest和revision，每次应用最多100项并原子保存进度；操作类型固定，重复请求续接进度。中途其他决定改变revision时停止并保留已完成批次，不能扩大到后来进入筛选的事实。每计划最多100个冻结集合。候选不足时可搜索最多1000项的有界目录、切换全部事实、采用高级章节映射或替换原件；未实现任意自然语言推理或远距指代。原件解析在Worker独立进程中受180秒默认时限约束，超时终止子进程并明确失败。

## 4. V2请求链路

1. HTTP入口创建trace和绝对deadline；校验请求、principal及全部KB权限。按模型配置修订借用有容量上限的模型/OS客户端，并检查V2投影版本。
2. exact只做词法候选；hybrid并行做BM25与query Embedding/k-NN；related还在PG做有证据的一跳关系扩展。所有候选数、时间和调用次数有界。
3. 在权威快照读取当前可见chunk，过滤删除、ACL、文档及局部归属，剔除纯标题与不合法关系。V2的BM25/向量各以1权重参与RRF；关系先按查询词法信号排序，以0.5权重参与，最终复验失效关系同时移除其分数贡献。原始分数保留诊断，不跨量纲相加。
4. 归一正文去重时同时保留版本、主体、条件、否定等差异。有足够预算且实际配置Reranker才执行重排；未配置正常跳过。成功重排只返回同一窗口，故障整体回RRF并如实降级。
5. 新PG事务再次检查Key、ACL、内容修订、模型/索引绑定、全部候选/上下文/关系及原文结构。内容转变可在同一deadline内重试一次；模型绑定变化不能复用旧向量继续检索。
6. 在最终权威快照批量读取有限必要依赖，构造最小组；ranked按原排序选择可放入预算的组，adaptive按新增信息选择。过大组跳过后继续考察候选，必要组全部选定后才分配可选近邻。按精确UTF-8 JSON字节成本返回整块，不切半个事实。结果按原RRF/重排顺序编号，返回真实分数。

召回不足，或adaptive明确给出的query_facets尚未在候选原文观察到时，可全请求补召回一次：提高每通道候选上限、用新列表替代旧列表，复用同一query向量，不累计重复通道RRF贡献。不因返回条数未满循环检索。版本重试共享补召回和绝对时间预算。

### 4.1 动态证据选择

`selection_mode=adaptive`显式启用；默认ranked按实际排序选择。候选数、重排窗口和返回上限是三个独立概念。选择器每轮计算新facet/查询词项观察、已知必要依赖的补齐和新增数值/否定/条件差异，并用精确字节成本控制负载。没有新观察时仅允许保守文本多样性增益；已观察的范围重复则停止，不以凑满top_k为目标。数值、业务版本、局部主体/条件差异保留；数值提取保留Unicode正负号，避免负温度被当作相同正值去重。内部选择规则revision参与adaptive的ranking_version指纹，公开selection.version保持evidence-set-v1。没有普适的语义去重或全库冲突裁决。

每个请求的InformationSelector、候选、已选集合和覆盖观察独立。必要条件、表头及归属保持真实身份；已包含在必要上下文的同一块不再次占主结果名额。可选近邻排除其他结果已返回的块，并要求同原始单元、明确条件/例外链接或新增查询信息，避免单凭邻近注入无关介绍。共享必要依赖仍可重复序列化，实际重复字节完整计费，现有契约不改成悬空ID引用。

可选校准文件由`scripts/calibrate-retrieval.py`从冻结人工标签生成，按Embedding、Reranker、配置revision、active generation、词法revision及检索路径绑定。重排校准存在时优先使用；否则使用已校准向量/字面覆盖信号的OR接受策略，无适用分数时保留unassessed候选。未配置或绑定失配明确诊断，不能把未经校准的分数断崖、RRF名次或本查询归一化值当成无答案概率。

动态选择在候选耗尽、无新收益或到达数量/字节/上下文预算时停止；deadline始终覆盖整个流程。诊断的字面facet观察与evidence_status均不认证问题已完整回答。可选selection_query补充原始问题的选择语境，实际召回仍使用query，不能从短query恢复被消费端省略的信息。

`evidence_status`保持unassessed。去重、上下文、融合与多片段返回改善可用证据，不代表已证明全部属性齐备或冲突已裁决。关系supports/refutes保留来源立场，不决定事实真伪。

## 5. 异步、时限与隔离

API在lifespan就绪前通过公开OpenAPI初始化完成惰性路由/依赖准备。`V2DeadlineMiddleware`直接包围`/v2/search`的完整下游ASGI调用，鉴权、模型配置依赖与endpoint共用同一SearchExecution。截止时取消实际异步调用，等待其finally清理；endpoint开始前再次checkpoint，防止同步依赖耗尽预算后仍启动检索。搜索响应暂存到下游完成并通过最终checkpoint后才提交，不能在预算耗尽后发送成功证据。冷启动、慢依赖和在途检索须分别验证，不能用预热掩盖取消漏洞。

V2模型和OS请求使用原生httpx.AsyncClient，按配置修订复用；切配置不关闭仍在使用的旧transport，退出后回收。单API进程默认16个active lease，超过直接503，最多4个同时存活修订；不是跨Worker/进程的模型配额。

PG驱动为同步psycopg：V2将一个完整读批次放入有执行上下文的工作线程；连接获取、SQL statement/lock timeout均受剩余预算约束。取消时驱动cancel_safe有界发起取消，数据库/驱动完成清理可能晚于HTTP deadline，不能声称外部GPU计算已终止。每个请求独立SearchExecution、候选和快照，transport共享不共享证据容器。

调用方断开会取消V2请求；错误响应只返回安全代码/trace。同步线程和阻塞事件循环的计算不能被asyncio硬终止；超时后的资源清理可能稍晚于HTTP预算，必须独立验证真实驱动行为。日志只记录事件、阶段、耗时和字节，不默认输出私有正文、query或Key。当前无查询缓存、自动全库冲突裁决和全局模型调度器。

## 6. generation与升级

generation维护锁阻止内容写入，查询继续。重建读取PG已存事实，记录build_source_profiles及其聚合标识，不重新解析原件。新投影带V2词法/局部归属字段、applicable_entity_ids/fact_kind、scope_text/table_labels/identifier_terms、`lexical_revision=scope-lexical-v2.3`及输入模板revision。验证完成后同一PG事务切换物理index、模型配置及revision。新字段由PG已冻结的结构与别名生成，不从查询或任意metadata猜归属。

解析/归属升级先重处理、人工检查并publish；Embedding/投影升级再generation。旧投影缺少V2字段时，V2返回INDEX_UPGRADE_REQUIRED而不假装已使用新路径。旧配置rollback也重建当前PG事实，不将旧index直接当成当前内容。

规范schema来自包内0001基线及0002文档核心SQL迁移，`db/schema.sql`由同链导出。schema_revisions保存迁移checksum；空库与升级到达相同结构。升级在单事务和advisory lock内执行，失败整批回滚；旧完整基线可核验后登记。ready对未升级/摘要不匹配返回503 SCHEMA_UPGRADE_REQUIRED。应用回退保留扩展表和数据，不执行破坏性降级。操作见[操作手册](OPERATIONS.md)。

## 7. 六项准确性优化与验证边界

六项复核按D25、47f20bc及当前调用链归并为下表功能范围；原聊天建议的逐字内容未保存在仓库，不把此映射当作原文还原。算法实现、规则测试与目标语料达标分别判定。

| 需求 | 当前实现主位置 | 需要保持的边界及验证入口 |
| --- | --- | --- |
| 同实体归属信息合并 | services/evidence.py、adapters/knowledge.py | 仅合并兼容信息；非空型号/版本冲突及不同主体不能放宽。见test_evidence_structure与专用test_v2_postgres |
| 搜索部分失败识别 | adapters/opensearch.py | HTTP200不等于完整召回；timed_out、terminated_early、失败分片须按失败处理。见test_v2_transports |
| 可选相关性校准门控 | services/selection.py | 只用人工标签校准并严格绑定模型/配置/路径；不匹配时明确未应用，不能宣称自动拒收低相关。见test_adaptive_selection及校准工具验证 |
| 整组精确预算与替补 | services/retrieval_v2.py | 必要条件整组计费；按精确JSON字节跳过超预算组，继续考察候选，不切半个事实。见test_adaptive_selection与test_retrieval_v2 |
| 动态新增信息与差异保护 | services/selection.py、retrieval_v2.py | 输出不凑数，保留数值、正负号、条件、否定、产品及版本差异；字面观察不等于语义充分。见test_adaptive_selection |
| 可信词法投影与关系排序/降权 | services/evidence.py、adapters/opensearch.py、adapters/knowledge.py、retrieval_v2.py | 只投影冻结可信信息，精确标识符不接受型号近邻；关系同KB一跳、按查询信号排序，以0.5权重融合，最终失效时移除贡献。见test_v2_transports、test_adaptive_selection与test_retrieval_v2 |

方法与结果不复制到本表：质量/负载和消融方法统一在[验收方法](ACCEPTANCE.md)，生产校准操作统一在[操作手册](OPERATIONS.md#7-相关性校准配置)，本轮实际满足度和剩余风险在任务板。

## 8. 独立离线关联实验

`src/cuekb/experiments/associations/`是独立C-lite离线工具，不接入当前API/Worker检索链路。它复用领域对象与RRF，使用显式冻结manifest、JSONL语料和候选录制，比较同KB一跳导航及规则事实方案；不读生产配置、不调用外部模型、不写PG/OS。

命令、实验结构和gold口径统一见[实验说明](../experiments/associations/README.md)。PG持久化和线上接入没有隐含启用，不能把离线合成示例当成线上召回或模型质量验证。

## 9. S1规范源文档核心

`domain/document_graph.py`定义DocumentGraph、DocumentNode、EvidenceEdge、ScopeAssertion及RetrievalUnit。TXT/Markdown、Docling DOCX/PDF在切片前捕获完整源段、章节、列表、表格行列/表头、代码与显式脚注；同名章节保留物理身份，Docling使用标题自身level。原始源节点与检索切片独立，后者通过parser_key/source_spans映射源节点；无法唯一定位时显式unresolved，不选择最近节点冒充出处。

PG持久化document_graphs/document_nodes/evidence_edges/retrieval_units/retrieval_unit_nodes/scope_assertions；节点父子、边端点和检索映射均有版本约束。必要闭包最多3层、16个节点，循环/超限标limited；结构/顺序边本身不是必要关系。显式脚注缺定义会阻塞相关片段发布。complete只表示已识别结构的完整性，不认证OCR、隐含关系或回答充分性。Docling表格序列化位置标coarse，不伪造PDF单元格精确坐标。

处理修订evidence-v2.5冻结unicode-lexical-v1分窗预算320及1200字符上限；按句子/列表边界切分，强制续片仍带依赖。该CPU词法预算不是实际Embedding/Nano tokenizer计数；模型精确负载与问题级支持判定属于后续S3/S4。完整长段保留在源树，不随窗口被截去。

上传content_kind=general声明非产品文档，未存在明确产品/套件冲突的正文可规则确认，不要求逐片段绑定产品；范围断言保存product not_applicable，搜索原有applicability仍unknown以兼容严格V2客户端。source_metadata保存语言、负责人、生效区间及来源策略引用；声明不自动提高权威或授予访问权限。

Worker在save_ready的同一事务保存事实、树、范围和单元映射；发布记录publication_snapshots中的图摘要与处理修订。未发布草稿审核保留完整源树及节点身份，替换检索单元；显式派生/恢复重映射节点/边/切片ID并保存source_version_id，排除事实不再生成检索单元。旧扁平版本返回legacy_unavailable，必须从保留原件重新解析，不能由现有块拼出声称完整的源树。新树不触发旧版本自动发布。跨文档业务推断、通用隐含条件识别和问题级证据交付仍按S2/S3演进。
