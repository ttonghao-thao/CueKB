# CueKB 第三方接口规范

更新：2026-10-26。本文描述当前代码契约，不是性能或模型质量承诺。全部CueKB接口使用`/v2`，检索、处理和管理采用同一契约版本。

## 1. 契约文件与通用约定

- [完整OpenAPI 3.1](contracts/openapi.json)：全部HTTP方法、路径、请求体、响应、字段类型、枚举、必填项和默认值。
- [V2请求JSON Schema](contracts/search-v2-request.schema.json)、[V2响应JSON Schema](contracts/search-v2-response.schema.json)：可用于第三方生成类型或严格校验，嵌套类型在`$defs`。
- [接入步骤与代码](INTEGRATION_GUIDE.md)、[系统约束](SYSTEM_CONSTRAINTS.md)、[操作手册](OPERATIONS.md)。

部署base URL由运行方提供，例如私网`http://host:8085`；生产远程集成应由运行方提供HTTPS或受控加密链路。JSON使用UTF-8、`Content-Type: application/json`，时间为含时区ISO 8601，ID为UUID。`null`表示没有可用值，不伪造名称、版本或时间。

业务接口使用`Authorization: Bearer <CueKB API Key>`。Key关联可信principal与知识库read/write/admin授权；system admin管理Key/模型。客户端自行传身份字段不会获得权限。memory后端不鉴权，仅开发使用，不可用于生产。

每个HTTP响应有`X-Trace-ID`；搜索成功体`trace_id`与其一致。请求`request_id`是调用方关联值，服务器不会用它替代trace。错误响应也包含trace；可解析出合法搜索请求后，错误体才可能带request_id。

## 2. V2检索请求

`POST /v2/search`，KB read；成功HTTP200。

```json
{
  "query": "What are the storage conditions for Product A after opening?",
  "kb_ids": ["11111111-1111-4111-8111-111111111111"],
  "request_id": "order-support-42",
  "mode": "hybrid",
  "selection_mode": "adaptive",
  "query_facets": ["storage"],
  "top_k": 20,
  "filters": {"product_model": "A"},
  "include_context": true,
  "include_diagnostics": false,
  "max_evidence_bytes": 48000
}
```

| 字段 | 必填/缺省 | 语义和约束 |
| --- | --- | --- |
| query | 必填 | 1—2000字符；完整检索问题，保留产品/参数/否定/所问属性；系统不会恢复原始语音或对话历史 |
| kb_ids | 必填 | 1—50个不重复UUID；全部须有read权限；不能通过扩大请求范围获得授权 |
| request_id | null | 可选1—128字符，成功原样回传；不提供幂等或缓存语义 |
| mode | auto | auto、exact、hybrid、related |
| top_k | 8 | 1—20，最终hit数量上限，允许少返，不放松权限/版本/预算凑数 |
| selection_mode | ranked | ranked按原排序逐组装入预算、过大组继续替补；adaptive按新增证据收益动态选择，实际数量可小于上限 |
| selection_query | null | 仅adaptive；1—2000字符的可选原始完整问题，用于选择、型号约束和字面覆盖观察；不替换query的Embedding/词法召回文本 |
| query_facets | [] | 仅adaptive；最多8项、每项1—200字符且不能全为空白。调用方关心的子项；字面命中用于选择和一次补召回触发，不是通用语义充分性判定 |
| filters.document_ids | [] | 最多100个UUID，只在这些文档中检索 |
| filters.product_model | null | 精确、区分大小写的**局部已确认产品型号**；不匹配随意上传的全篇标签或未知归属 |
| filters.software_version | null | 已确认局部软件版本；与产品条件同时给出时须属于同一个subject，不是document business_version |
| filters.category | null | 上传scope.category分类标签的精确匹配 |
| include_context | true | V2中只控制可选近邻；已知必要归属、表头和条件仍属于证据组 |
| include_diagnostics | false | 精简diagnostics始终返回；true才输出逐hit stage_scores，不输出全部候选正文 |
| max_evidence_bytes | 48000 | 4096—128000；最终hits JSON数组的UTF-8字节上限，另受服务端上限约束；不是token数量 |
| relations | 缺省对象 | 仅related使用；entity_ids最多20、types最多8、direction默认outgoing，可选含时区at；不执行无限图遍历 |

请求及filters/relations/归属映射中的未知字段返回422；`retrieval_mode`不是`mode`别名，也没有original_question、version_policy、历史版本检索等隐含字段。

auto以服务端标识符正则选择exact或hybrid。V2 exact对单个含字母和数字的标识符使用保留标点、大小写归一化的keyword token等值，其他文本使用分析器短语匹配，跳过Embedding与重排。hybrid使用OpenSearch BM25与Embedding/k-NN、RRF。related加有出处的一跳，scope_limited为true。

adaptive对问题中`A-100`等含字母、数字及分隔符的标识符施加有界token约束；多个标识符按OR处理，PG最终也复验，`A-1000`不能冒充`A-100`。这不是产品自动消歧器，不推断无明确标识符的产品归属。规范名、冻结审核别名、型号、表格标签参与新的V2词法投影；缺少`scope-lexical-v2.3`投影时返回INDEX_UPGRADE_REQUIRED。

动态选择将已知必要依赖视作证据组，优先补充不同属性/适用范围，保护数值、条件和否定差异，按精确JSON成本跳过过大的组；无新增收益、候选耗尽或到达预算时停止。未提供query_facets时仍可按问题词项和保守多样性选择，不保证任意复合问题全部召回。有明确未观察到的facet且预算允许时，至多一次扩大原始召回窗口，复用原query向量；不自动改写query或递归搜索。

相关性接受门控只在运行方提供人工样本生成、且绑定匹配的校准文件时启用。缺配置或绑定不匹配时继续有界选择，并明确报告未应用校准；不使用未经验证的通用分数阈值。部署方法见[操作手册](OPERATIONS.md#7-相关性校准配置)。

## 3. V2响应与排序

完整可校验示例见[请求](examples/search-v2-request.json)、[响应](examples/search-v2-response.json)；示例是合成契约数据，数值不是质量/性能实测。

动态策略示例：[adaptive请求](examples/search-v2-adaptive-request.json)、[adaptive响应](examples/search-v2-adaptive-response.json)。它们由同一导出脚本生成，适合严格客户端先验证新诊断字段。

| 响应字段 | 语义 |
| --- | --- |
| schema_version | 固定字符串`2`；必须与选用的DTO一致 |
| trace_id / request_id | 服务端trace UUID、调用方关联值 |
| retrieval_status | ok/degraded/not_found；预留needs_clarification不代表已实现自动澄清 |
| evidence_status | 当前固定unassessed；不将高分或有命中当作充分或真实 |
| degraded_reasons | 本次实际依赖失败、索引过渡或选择受限原因；可与空hits并存 |
| scope_limited | related限定一跳的范围标志；不是充分性、真实性或全库无答案结论 |
| content_revisions | 当前响应最终权威快照中的KB内容修订，键为KB UUID |
| retrieval_path | 本次实际exact/hybrid/related路径 |
| executed_stages / skipped_stages | 实际执行的阶段与跳过原因；配置不等于执行 |
| ranking_method | keyword_rrf、rrf、rrf_related、rrf_then_rerank |
| ranking_version | `sha256:`加64位摘要；标识无密钥排序配置，不是外部模型确定性保证 |
| timings_ms | 各阶段墙钟累计及total；并行阶段不能相加；total到最终编码前，客户端端到端时延另测 |
| diagnostics | 候选、返回、去重、省略、模型调用、大小与配置边界；详见下表 |
| hits | 已经PG授权/删除/发布版本最终复验的证据，按rank排序 |

| 每条hit字段 | 语义 |
| --- | --- |
| chunk_id / document_id / version_id | 真实片段、逻辑文档和不可变处理版本UUID |
| document_name / content_sha256 | PG读取的文档名、该版本原件SHA-256；历史/开发数据允许null |
| processing_profile | 解析/分块/范围映射快照标识；不等于Embedding model或索引generation |
| source_unit_id / block_kind | 原段/列表/表格单元标识，及text/list/table/heading；标题不单独占最终结果名额 |
| source_text | PG解析原文；不按query补产品名或改事实。PDF/表格为可回原件的解析表示，不是二进制字节逐字等价 |
| title_path / anchor | 章节路径与单一位置摘要；anchor页码/offset/bbox可空 |
| source_spans | 真实多位置片段，含label/text/anchor、可空parser_item_id/span_id/行列索引；不能把缺位置当已准确定位 |
| applicability | status、subjects、conditions；独立于metadata标签 |
| rank / score / score_type | rank连续从1开始；score为实际RRF或同次重排分数，有限、越大越相关；score_type为rrf/rerank |
| retrieval_sources | 该hit真实参与的keyword/vector/relation通道 |
| metadata | 上传标签，例如business_version/product_model/software_version/category；不将标签自动提升为事实归属 |
| context / context_parts | 整块原文按次序组成的上下文；parts保留真实chunk_id、正文、锚点、spans、role和required标记 |
| context_state / context_omissions / context_truncated | 已识别依赖的完整性与省略信息，不能推导问题已被全部回答 |
| stage_scores | 仅include_diagnostics=true时存在，包含实际keyword/vector/rrf/rerank分项；adaptive额外含lexical字面词项覆盖比例，用于校准记录，不是BM25或事实正确率；不得跨量纲直接比较 |
| relations | 有出处且经过最终复验的关系；supports/refutes是证据立场，不是真伪裁决 |

applicability.status为resolved、unknown、ambiguous。subject携带可空entity_id、可读display_name、可空product_model/software_version、source_kind、provenance_id和source_spans；内部ID不是要求口述的产品名。source_kind为curated/reviewed_alias/parser：管理员标注不伪装成原文已陈述。conditions保留原文或明确标注的条件，不自动发明包装/开封等业务枚举。

applicability.reason_codes为有界列表：missing_binding表示未提供可用映射/审核实体，no_reviewed_match表示现有审核信息未匹配结构，structure_missing表示缺少章节/行标签，scope_conflict表示归属冲突。resolved通常为空；已有事实的详细原因通过重处理生成，不根据query追溯猜测。

hit.dependency_issues包含unmatched_reference、ambiguous_reference、dependency_limit_exceeded、dependency_cycle或unsafe_dependency，表示原文引用缺失、重名、关系上限、环或不安全归属。它与context_state/context_omissions一起使用，不是检索充分性结论。

context_parts每项包含自身applicability及可空link_provenance。后者字段为kind（parser_reference/curated/parser_continuation）、source_unit_id、target_unit_id、provenance_id及可空reference_text。主块的link_provenance为null。unknown必要条件可因可信关系被附带，但不能据此补成主块的产品；显式归属冲突和跨版本仍排除。

可检查[必要上下文请求](examples/search-v2-required-context-request.json)及[响应](examples/search-v2-required-context-response.json)：include_context=false时仍返回原文明确引用的unknown条件，主事实resolved，条件不冒充该产品。示例由实际确定性解析生成，仅为合成契约夹具。

定位约定：page从1开始，表格row_index/column_index从0开始（当前正文行通常从1起，表头独立保存）；TXT/Markdown的offset是UTF-8解码后Unicode字符位置，start包含、end不包含。Docling的charspan可能相对parser item，bbox沿解析器坐标/原点约定，不能统一解释为原文件字节或屏幕像素。anchor可能覆盖整个原块；多行、多页及单元格核验优先结合source_spans、parser_item_id、title_path和授权下载的对应version原件。chunk_id在一个处理版本内稳定，重处理/恢复后不能继续用旧ID指代新事实。

context_state优先级：必要依赖缺失unresolved；可选内容被预算省略truncated；已识别必要组完整complete；没有必要依赖且关闭可选上下文时disabled。context_truncated=false不代表充分；unknown产品归属不能被调用方补成查询中的产品。已知必要依赖缺失时，不应将裸source_text作为该产品的完整适用证据肯定作答。

V2加权RRF公式是`sum(weight/(60+source_rank))`，keyword/vector权重各1，relation权重0.5，source_rank从1开始。关系SQL先按查询词法相关性排列有界候选，应用层按原文查询词项交集排序；UUID只作同分确定性排序。降低相关关系通道的投票权重，避免将其当作独立的第三路等权召回。评分是正式融合分数，不是按最终rank伪造的分数。

V2成功重排只返回同次窗口内结果，不拼RRF尾部；重排故障整体保留RRF并标降级。adaptive只选择集合，输出仍按实际RRF/重排分数排序，score不会改成选择收益。未配置Reranker是正常路径。原始BM25、向量与重排分数不相加，也不等于事实正确率。当前hit始终有有限score，不把NaN/Inf改成null掩盖错误。

排序复现还要固定query、filters、授权、PG发布修订、generation、processing_profile、代码和外部不可变模型修订；同名模型服务更换权重后不能仅凭ranking_version保证重复结果完全一致。

## 4. 诊断与空结果处理

diagnostics中：

- counts：keyword/vector/relation/union/authoritative/duplicate_removed/reranked/returned，均为最后一次尝试实际处理的有限候选，不是全库相关资料数量。
- omissions：scope_excluded、dependency_unresolved、candidate_limit、rerank_window_limit、payload_budget_exhausted、stale_candidate、context_budget_omitted、heading_only。只给计数，不返回全部省略正文/ID。
- sizes：source_text/context/context_parts的chars与bytes，及精确hits_bytes；重复字段计入实际传输大小，不能把字符当token。
- backend_calls：keyword/embedding/vector/relation/rerank累计调用次数；attempt_count最多2，包含一次版本过渡重试。
- reranker：configured、executed、skipped_reason；另有active_generation、processing_profiles、algorithm_revision和实际keyword/vector/relation limit。
- selection：**仅显式adaptive响应存在**；ranked不输出该属性，兼容既有严格V2消费者。字段见下表，旧客户端必须先升级DTO再启用adaptive。

| selection字段 | 语义 |
| --- | --- |
| method / version | adaptive / evidence-set-v1；选择算法版本独立于原RRF核心revision |
| calibration_status / profile_id | not_configured、applied或binding_mismatch；可空的校准文件SHA-256标识。applied表示绑定匹配，不是生产准确率已验收 |
| considered / assessed / rejected / redundant | 最终合法候选数、实际有适用校准分数的候选数、校准拒收数、无新增收益/已由必要上下文包含的候选数；不是全库统计 |
| stop_reason | no_candidates、relevance_exhausted、no_information_gain、max_hits、payload_budget、candidates_exhausted；数量/预算可能先于覆盖完成 |
| facet_count / observed_facet_indices | 请求facet数及观察到完整字面词项的0起始索引；不等于这些属性已给出完整事实或所有产品均已覆盖 |
| coverage_assessment | 固定unassessed；避免将字面覆盖解释为充分性 |

OpenSearch HTTP 200中若timed_out、terminated_early或失败分片非零，该通道按失败处理；其他健康通道仍可提供明确降级证据。失败造成的空结果不会被当作正常not_found。

| HTTP/状态 | 调用方应如何处理 |
| --- | --- |
| 200 + ok + hits | 可使用有据原文，但充分性仍未评估，检查每条归属/必要依赖 |
| 200 + not_found + [] | 本次没有合法/经过已应用门控的可接受候选；不等于整个知识库没有某事实 |
| 200 + degraded + hits | 合法证据可用但检索受限，保留原因，不能伪称完整 |
| 200 + degraded + [] | 区分依赖失败和预算/范围不可用，不能一律写成“查无资料” |
| 401/403 | 身份或授权问题；不放宽KB/filters重试 |
| 409 | 配置/索引/维护/幂等冲突，按error_code/detail处理 |
| 503/504 | 依赖、容量或总预算失败；不是正常空结果，不返回未复验正文 |

## 5. 导入、归属映射、更新与恢复

`POST /v2/documents`：multipart/form-data，字段`file`和字符串`metadata`，KB write，`Idempotency-Key`必填1—128字符，返回202 Job。支持PDF/DOCX/MD/Markdown/TXT；文件缺省最大50MiB。metadata字段：kb_id、name、business_version、scope、document_id、auto_publish，以及scope_bindings/document_default/dependency_bindings、text_mode（auto/plain）、processing_template_id。未知字段422。

```json
{
  "kb_id": "11111111-1111-4111-8111-111111111111",
  "name": "Product manual",
  "scope": {"category": "manual"},
  "auto_publish": false,
  "scope_bindings": [
    {"title_path": ["Product A"], "subjects": [{"display_name": "Product A", "product_model": "A"}]},
    {"title_path": ["Product B"], "subjects": [{"display_name": "Product B", "product_model": "B"}]}
  ]
}
```

scope_bindings最多100个，每项用非空title_path（最多20级、前缀精确匹配）或source_unit_id选择器，至少一个；两个同时给出则都须匹配。每项subjects为1—8个，conditions最多20个`{text,provenance_id?}`；subject必填display_name，可选entity_id/product_model/software_version/provenance_id。source_unit_id映射只用于指定原版本的reprocess；首次上传只能用章节路径。document_default用同样subjects/conditions，表示调用方明确确认覆盖全篇；与局部映射冲突会ambiguous。不能将多产品文档套一个未经确认的全篇产品。

客户端不能提交approved_entities、source_kind、source_spans来冒充审核/原文出处。服务器在入队时冻结本次审核别名和映射。给entity_id时必须属于该KB，显示名从冻结实体读取。没有映射不从query猜归属。无法匹配的选择器使处理任务失败，而非默默应用到其他范围。

dependency_bindings最多100项，声明直接必要关系，示例：

```json
{
  "source": {"title_path": ["Product A", "Specifications"]},
  "target": {"title_path": ["Shared conditions"]},
  "role": "condition",
  "provenance_id": "document-owner-review-20261025"
}
```

source/target均使用非空title_path（最多20级，每级500字符，**精确匹配完整路径，不是前缀**）和/或source_unit_id；同时给出则同时匹配。源单元定位仅用于reprocess，并须属于source_version_id。role为attribution/table_header/condition/exception/continuation；provenance_id为1—500字符非空审核说明。服务器构建关系来源，客户端不能提交provenance或伪装parser_reference。一个章节可包含多个块，全部目标超过20条时保留未完整诊断；不是只取第一块并宣称完整。

可选reference_text为1—500字符的原文引用，用于人工确认解析标题与引用文字不一致时的目标。该文本必须实际存在于所选主事实源单元中，且只能对应一条自动引用；只解除这一引用的缺失/歧义，其他引用仍单独检查。不存在原文或混入多条引用时Job分别失败为dependency_reference_not_in_source或dependency_reference_must_identify_one_reference。审核说明不是审核者身份凭据，提交主体记录在任务中。

人工选择器未匹配、同名路径对应多个章节、自引用或循环时，Job分别失败为dependency_selector_not_found、ambiguous_dependency_selector、context_self_link或context_reference_cycle，不自动重试。原文自动引用无法匹配或存在歧义时保留ready事实和dependency_issues供审核，不自动猜目标。结构与匹配规则见[系统设计](SYSTEM_DESIGN.md#31-归属审核与必要章节)。

Job包含id、kb_id、document_id、version_id、status、stage、progress、error_code/error_message及时间。202只表示排队成功。`GET /v2/jobs/{job_id}`轮询；queued/running是处理中，failed查看错误，succeeded+ready尚未发布。`POST /v2/documents/{document_id}/publish`提交`{"version_id":"UUID","scope_key":"default"}`，write，204。

| 操作 | 契约 |
| --- | --- |
| 文件更新 | `/v2/documents`中给既有document_id，生成新不可变版本；整文件处理，不是段落增量补丁 |
| 版本信息 | `GET /v2/documents/{document_id}`，read；版本含processing_profile、derivation_kind、source_version_id，不默认返回全量处理快照 |
| 重处理原件 | `POST /v2/documents/{document_id}/reprocess`，write+幂等键；source_version_id可空（当前published），target_profile只允许evidence-v2；可给scope_bindings/document_default/dependency_bindings；auto_publish默认false |
| 审核处理结果 | `GET /v2/documents/{document_id}/versions/{version_id}/review`，KB write；limit默认25、1—100，offset默认0、0—1000000；可查看ready版本，未知/其他文档版本404 |
| 恢复旧事实 | `POST /v2/documents/{document_id}/versions/{version_id}/restore`，body=`{}`，write+幂等键；创建全新版本/块ID，重新编码后ready，仍须显式发布 |
| 下载原件 | `GET /v2/documents/{document_id}/source?version_id=UUID`，read；省略版本下载当前published；删除/撤权后拒绝 |
| 删除 | `DELETE /v2/documents/{document_id}`，admin，204；PG立即不可见，OpenSearch异步清理，不承诺原件/备份硬删除 |

幂等键作用域为principal+KB：同键同payload返回原Job，同键不同payload返回409；重试同一写请求必须保持payload与key。普通ingest/reprocess同文档、同原件hash、同processing_profile拒绝重复；有意restore是审计过的新派生版本，允许同内容。旧版本不会因上传成功立即失效，直到新版本publish。恢复不恢复旧权限、删除状态或自动复制人工关系。

审核响应含document_id/kb_id/version_id、status、processing_profile、requires_reprocess，total/resolved/unknown/ambiguous/unresolved_dependencies全版本计数，reason_counts/warnings、可重用的三类审核配置，以及当前页items。item含chunk_id/source_unit_id/title_path、500字符source_preview、归属、context_links、dependency_unresolved和dependency_issues；预览不重复返回归属来源的长spans，原件/搜索仍保留真实来源。分页不改变全版本计数，也不将尚在处理中的0块误报为审核通过。warnings区分processing_not_ready、all_scope_unknown/scope_unknown、scope_conflict、dependency_unresolved、processing_revision_changed。

requires_reprocess=true表示处理结构或定位需刷新。门户先以空映射按当前结构生成新待审核版本，再配置归属/关系；不在原发布版本上直接改事实。当前配置中的源单元会在能一一对应时转换到所审核版本；不静默选择一对多映射中的任意一块。接口不暴露approved_entities等完整服务器快照。

### 5.1 选择式审核接口

ready结果包含review_plan_id、blocking_facts及publish_eligible。unknown不是发布阻塞的充分条件；审核状态和通用角色见[系统设计](SYSTEM_DESIGN.md#32-规则导入与选择式审核)。发布遇到未解决内容返回409 `review_required_before_publish`。auto_publish=true也必须通过门槛，有阻塞时Job仍succeeded、stage=needs_review，版本保持ready。

| 操作 | 路径及主要字段 |
| --- | --- |
| 创建/复用分析计划 | POST `/v2/documents/{d}/versions/{v}/review-plans`，body={}，202 ReviewPlanInfo；与ready自动创建的计划复用 |
| 计划汇总 | GET `/v2/review-plans/{p}`；包含revision、draft/applied、counts、blocking、publish_eligible及applied_job_id |
| 问题与全部事实 | GET `/v2/review-plans/{p}/issues`或`/facts`；limit=25/最多100，offset≤1000000，可选product_model/reason/section_id过滤；返回revision、total、items |
| 保存选择 | POST `/v2/review-plans/{p}/decisions`，body={revision,decisions:[...]}；最多100项且fact_id不重复，整批原子写入；过期修订/已冻结409 |
| 应用 | POST `/v2/review-plans/{p}/apply`，body={revision}，202 Job；冻结manifest并创建新ready版本，重复调用返回同一Job；有阻塞或全排除409 |

跨页批量确认先POST `/v2/review-plans/{p}/selections`，提交{revision,issues,reason,section_id,product_model}冻结当前具体记录，返回{id,count,completed,revision,digest,operation}。GET `/v2/review-selections/{id}`读取进度；POST同路径`/apply`提交{action,entity_id}，支持general/assign/exclude/defer，每次最多处理100项并保存进度。开始后操作不可更换，重复调用续接下一批；其他决定导致修订变化时409，已完成批次保留，需重新预览剩余内容。每计划最多100个冻结集合，不将更新后的筛选结果自动加入原集合。

以上均须KB write；计划不存在/文档已删404，跨KB事实/错误产品/无效拆分422，撤权/Key失效拒绝。memory开发backend不提供该管理链路。创建计划从已存PG事实形成摘要，不重新解析；结构升级仍走原件reprocess。

fact项包含id/source_chunk_id、source_text/title_path/anchor、applicability/fact_kind/review_state/reason、最多5项产品candidates及必要条件dependency_choices（原文摘要、标题、角色）、split_suggestions。提案不等于已发布事实，不返回完整服务器处理快照。

decision包含fact_id及action：assign/accept选择entity_id；general确认通用；comparison确认原文比较主体；shared确认原文明示同一条结论共同适用的主体（分别赋值的混合规格仍拒绝）；exclude/defer/reset分别排除、暂缓、撤回；split给原文字符ranges；dependency选择target_fact_id及role。原文值、规则证明及操作者由服务器维护，客户端不能改写source_text或传入伪造approved字段。拆分不能省略非空原文，也不能通过拆分清除已有依赖。明确主体证据与所选产品冲突、混合单品规格整体赋给一个产品、套件赋给普通单品均拒绝。

示例保存选择：

```json
{"revision":0,"decisions":[{"fact_id":"55555555-5555-4555-8555-555555555555","action":"assign","entity_id":"66666666-6666-4666-8666-666666666666"}]}
```

filters.product_entity_id为稳定产品UUID，可用于没有型号的食品或服务产品；与product_model/software_version同时指定时，三个条件必须属于同一已确定主体，提及或套件组件关系不授予身份。

KB目录仍用entities接口，kind=product/model/bundle参与自动产品识别；term不参与。实体增加product_model、software_version。POST `/v2/knowledge-bases/{kb}/entities/import`接收{rows:[{id,name,kind,product_model,aliases,...}]}，KB admin，最多100项，冲突整批回滚；门户提供CSV预览后调用，不发送CSV至解析模型。

GET `/v2/knowledge-bases/{kb}/processing-templates`（read）返回最多100个模板；PUT同路径`/{template_id}`（admin）保存{name,general_headings,attribute_terms,text_mode}并递增revision。每个标题/属性词列表最多100项；模板不接受脚本或任意正则。导入/reprocess指定processing_template_id时服务器冻结内容与修订，跨KB模板422。模板变更不会反向修改旧版本。

V2搜索hit增加fact_kind，表示原文事实类型；不改变evidence_status=unassessed。严格DTO需同步生成契约。

## 6. 公共管理接口目录

以下为V2管理接口。所有请求/响应的完整嵌套字段以[OpenAPI](contracts/openapi.json)为准；不要将V2检索DTO用于这些接口。

### 知识库创建与标识

`POST /v2/knowledge-bases`请求为`{"name":"产品资料库","description":"客服原文"}`。name去除首尾空白后1–200字符，description可省略（默认空串，最多2000字符）；生产仅system admin可创建。成功HTTP201返回`id`（新UUID）、name、description、content_revision、acl_revision、created_at，并授予创建者该KB的admin权限。创建无需模型配置，但空库尚无可检索证据。

name允许重复，仅作展示；第三方检索必须用返回的id填`kb_ids`，上传则填`kb_id`。不能按名称唯一定位，不会自动按同名返回旧库。创建不支持幂等键，未知结果先查询`GET /v2/knowledge-bases`确认，不能盲目重试。

`GET /v2/knowledge-bases`成功HTTP200返回当前Key可访问的列表，每项含上述字段和有效`role`；未获授权的业务Key返回空列表，system admin可见所有KB。业务Key创建和KB授权是两个独立步骤，授权使用Key返回的`principal_id`，不能使用Key ID。Portal和后台命令的操作步骤在[接入说明](INTEGRATION_GUIDE.md#6-创建知识库与第三方配置)。

| 方法/路径 | 权限 | 输入/结果 |
| --- | --- | --- |
| GET /v2/health | 无 | 进程存活；status/version |
| GET /v2/ready | 无 | 生产PG/OS就绪，不验证模型质量 |
| POST /v2/knowledge-bases | system admin | name/description → KnowledgeBase |
| GET /v2/knowledge-bases | 已鉴权 | 当前可访问KB列表及role |
| GET /v2/documents | KB read | kb_id，limit默认100、offset默认0；分页文档摘要 |
| GET /v2/documents/{id} | KB read | 唯一文档详情视图，包含处理profile、版本派生及来源信息 |
| POST /v2/documents | KB write | 文件＋metadata，必须有Idempotency-Key；支持归属映射，详见第5节 |
| POST /v2/api-keys | system admin | principal_name/label，明文Key仅创建时返回一次 |
| DELETE /v2/api-keys/{key_id} | system admin | 吊销Key，204 |
| PUT /v2/knowledge-bases/{kb_id}/grants | KB admin | principal_id/role(read/write/admin)，204 |
| DELETE /v2/knowledge-bases/{kb_id}/grants/{principal_id} | KB admin | 撤权，204 |
| GET /v2/knowledge-bases/{kb_id}/entities | KB read | limit/offset分页 |
| PUT /v2/knowledge-bases/{kb_id}/entities/{entity_id} | KB admin | name/kind/审核aliases/mention_chunk_ids |
| DELETE /v2/knowledge-bases/{kb_id}/entities/{entity_id} | KB admin | 删除及修订更新 |
| GET /v2/knowledge-bases/{kb_id}/relations | KB read | limit/offset分页 |
| PUT /v2/knowledge-bases/{kb_id}/relations/{relation_id} | KB admin | subject_id/object_id/relation_type/evidence及条件/时间；证据必须当前可见且同KB |
| DELETE /v2/knowledge-bases/{kb_id}/relations/{relation_id} | KB admin | 删除关系 |
| GET/PUT /v2/model-configuration | system admin | 外部Embedding/可选Reranker配置；Key不回显 |
| GET/POST /v2/index-generations | system admin | 查看/创建独立重建任务；POST模型三元组、dimension；省略chunking_version，由实际源profile记录，不重新解析 |
| POST /v2/index-generations/{id}/activate | system admin | 验证后原子切模型/索引；过期任务拒绝 |
| POST /v2/index-generations/{id}/rollback | system admin | 用旧模型配置重建当前PG数据；不是恢复旧事实 |
| DELETE /v2/index-generations/{id} | system admin | 取消未激活重建 |


## 7. 错误契约

```json
{"detail":"search_deadline_exceeded","error_code":"SEARCH_DEADLINE_EXCEEDED","trace_id":"22222222-2222-4222-8222-222222222222","request_id":null}
```

detail为字符串或校验错误列表（仅loc/msg/type，不回显input/ctx）；HTTP码、detail与error_code共同表示错误。不得从错误文字推断知识不存在。

| HTTP | error_code / 情况 |
| --- | --- |
| 400 | BAD_REQUEST，例如生产上传缺幂等键 |
| 401 | INVALID_API_KEY |
| 403 | ACCESS_DENIED |
| 404/405 | NOT_FOUND / METHOD_NOT_ALLOWED |
| 409 | CONFLICT、INDEX_MAINTENANCE_IN_PROGRESS、INDEX_UPGRADE_REQUIRED、SEARCH_CONFIGURATION_CHANGED、RELEVANCE_PROFILE_INVALID；detail区分重复版本、幂等payload冲突、缺Embedding配置或损坏校准文件等 |
| 413/415/422 | FILE_TOO_LARGE / UNSUPPORTED_FILE_TYPE / INVALID_REQUEST |
| 503 | DATABASE_UNAVAILABLE、SEARCH_BACKEND_UNAVAILABLE、SEARCH_CAPACITY_EXCEEDED、MODEL_CONFIGURATION_BUSY、SEARCH_RESPONSE_LIMIT_EXCEEDED |
| 504 | SEARCH_DEADLINE_EXCEEDED；不返回未完成最终复验的正文 |
| 500 | INTERNAL_ERROR；无驱动/模型密钥或完整请求回显 |

客户端超时、网络断连与HTTP504不同。读请求可以由调用方按业务预算有界退避重试；不得重复无限重试、扩大授权范围或把错误转换成空成功。写请求只用同一幂等键重试；未知处理结果先查询原Job。
