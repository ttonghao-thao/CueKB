# CueKB 第三方接口规范

更新：2026-10-23。本文描述当前代码契约，不是性能或模型质量承诺。新集成使用V2证据检索；管理能力同时复用V1。不要按版本号猜字段兼容性。

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
  "top_k": 8,
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
| filters.document_ids | [] | 最多100个UUID，只在这些文档中检索 |
| filters.product_model | null | 精确、区分大小写的**局部已确认产品型号**；不匹配随意上传的全篇标签或未知归属 |
| filters.software_version | null | 已确认局部软件版本；与产品条件同时给出时须属于同一个subject，不是document business_version |
| filters.category | null | 上传scope.category分类标签的精确匹配 |
| include_context | true | V2中只控制可选近邻；已知必要归属、表头和条件仍属于证据组 |
| include_diagnostics | false | 精简diagnostics始终返回；true才输出逐hit stage_scores，不输出全部候选正文 |
| max_evidence_bytes | 48000 | 4096—128000；最终hits JSON数组的UTF-8字节上限，另受服务端上限约束；不是token数量 |
| relations | 缺省对象 | 仅related使用；entity_ids最多20、types最多8、direction默认outgoing，可选含时区at；不执行无限图遍历 |

请求及filters/relations/归属映射中的未知字段返回422；`retrieval_mode`不是`mode`别名，也没有original_question、version_policy、历史版本检索等隐含字段。

auto以服务端标识符正则选择exact或hybrid。exact沿V1短语/型号字段等值路径跳过Embedding与重排；不保证任意自然语言查询的事实精确。hybrid使用OpenSearch BM25与Embedding/k-NN、RRF。related加有出处的一跳，scope_limited为true。

## 3. V2响应与排序

完整可校验示例见[请求](examples/search-v2-request.json)、[响应](examples/search-v2-response.json)；示例是合成契约数据，数值不是质量/性能实测。

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
| title_path / anchor | 章节路径与单一兼容位置摘要；anchor页码/offset/bbox可空 |
| source_spans | 真实多位置片段，含label/text/anchor、可空parser_item_id/span_id/行列索引；不能把缺位置当已准确定位 |
| applicability | status、subjects、conditions；独立于metadata标签 |
| rank / score / score_type | rank连续从1开始；score为实际RRF或同次重排分数，有限、越大越相关；score_type为rrf/rerank |
| retrieval_sources | 该hit真实参与的keyword/vector/relation通道 |
| metadata | 上传标签，例如business_version/product_model/software_version/category；不将标签自动提升为事实归属 |
| context / context_parts | 整块原文按次序组成的上下文；parts保留真实chunk_id、正文、锚点、spans、role和required标记 |
| context_state / context_omissions / context_truncated | 已识别依赖的完整性与省略信息，不能推导问题已被全部回答 |
| stage_scores | 仅include_diagnostics=true时存在，包含实际keyword/vector/rrf/rerank分项；不得跨量纲直接比较 |
| relations | 有出处且经过最终复验的关系；supports/refutes是证据立场，不是真伪裁决 |

applicability.status为resolved、unknown、ambiguous。subject携带可空entity_id、可读display_name、可空product_model/software_version、source_kind、provenance_id和source_spans；内部ID不是要求口述的产品名。source_kind为curated/reviewed_alias/parser：管理员标注不伪装成原文已陈述。conditions保留原文或明确标注的条件，不自动发明包装/开封等业务枚举。

定位约定：page从1开始，表格row_index/column_index从0开始（当前正文行通常从1起，表头独立保存）；TXT/Markdown的offset是UTF-8解码后Unicode字符位置，start包含、end不包含。Docling的charspan可能相对parser item，bbox沿解析器坐标/原点约定，不能统一解释为原文件字节或屏幕像素。anchor可能覆盖整个原块；多行、多页及单元格核验优先结合source_spans、parser_item_id、title_path和授权下载的对应version原件。chunk_id在一个处理版本内稳定，重处理/恢复后不能继续用旧ID指代新事实。

context_state优先级：必要依赖缺失unresolved；可选内容被预算省略truncated；已识别必要组完整complete；没有必要依赖且关闭可选上下文时disabled。context_truncated=false不代表充分；unknown产品归属不能被调用方补成查询中的产品。已知必要依赖缺失时，不应将裸source_text作为该产品的完整适用证据肯定作答。

RRF公式是`sum(1/(60+source_rank))`，source_rank从1开始；它是正式融合分数，不是按最终rank伪造的分数。V2成功重排只返回同次窗口内结果，不拼RRF尾部；重排故障整体保留RRF并标降级。未配置Reranker是正常路径。原始BM25、向量与重排分数不相加，也不等于事实正确率。当前支持方法的hit始终有有限score；未来不可评分方法若引入null将另行约定，不把NaN/Inf改成null掩盖错误。

排序复现还要固定query、filters、授权、PG发布修订、generation、processing_profile、代码和外部不可变模型修订；同名模型服务更换权重后不能仅凭ranking_version保证重复结果完全一致。

## 4. 诊断与空结果处理

diagnostics中：

- counts：keyword/vector/relation/union/authoritative/duplicate_removed/reranked/returned，均为最后一次尝试实际处理的有限候选，不是全库相关资料数量。
- omissions：scope_excluded、dependency_unresolved、candidate_limit、rerank_window_limit、payload_budget_exhausted、stale_candidate、context_budget_omitted、heading_only。只给计数，不返回全部省略正文/ID。
- sizes：source_text/context/context_parts的chars与bytes，及精确hits_bytes；重复字段计入实际传输大小，不能把字符当token。
- backend_calls：keyword/embedding/vector/relation/rerank累计调用次数；attempt_count最多2，包含一次版本过渡重试。
- reranker：configured、executed、skipped_reason；另有active_generation、processing_profiles、algorithm_revision和实际keyword/vector/relation limit。

| HTTP/状态 | 调用方应如何处理 |
| --- | --- |
| 200 + ok + hits | 可使用有据原文，但充分性仍未评估，检查每条归属/必要依赖 |
| 200 + not_found + [] | 正常流程本次没有合法候选；不等于整个知识库没有某事实 |
| 200 + degraded + hits | 合法证据可用但检索受限，保留原因，不能伪称完整 |
| 200 + degraded + [] | 区分依赖失败和预算/范围不可用，不能一律写成“查无资料” |
| 401/403 | 身份或授权问题；不放宽KB/filters重试 |
| 409 | 配置/索引/维护/幂等冲突，按error_code/detail处理 |
| 503/504 | 依赖、容量或总预算失败；不是正常空结果，不返回未复验正文 |

## 5. 导入、归属映射、更新与恢复

`POST /v2/documents`：multipart/form-data，字段`file`和字符串`metadata`，KB write，`Idempotency-Key`必填1—128字符，返回202 Job。支持PDF/DOCX/MD/Markdown/TXT；文件缺省最大50MiB。metadata字段：kb_id、name、business_version、scope、document_id、auto_publish，以及scope_bindings/document_default。未知字段422。

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

Job包含id、kb_id、document_id、version_id、status、stage、progress、error_code/error_message及时间。202只表示排队成功。`GET /v1/jobs/{job_id}`轮询；queued/running是处理中，failed查看错误，succeeded+ready尚未发布。`POST /v1/documents/{document_id}/publish`提交`{"version_id":"UUID","scope_key":"default"}`，write，204。

| 操作 | 契约 |
| --- | --- |
| 文件更新 | `/v2/documents`中给既有document_id，生成新不可变版本；整文件处理，不是段落增量补丁 |
| 版本信息 | `GET /v2/documents/{document_id}`，read；版本含processing_profile、derivation_kind、source_version_id，不默认返回全量处理快照 |
| 重处理原件 | `POST /v2/documents/{document_id}/reprocess`，write+幂等键；source_version_id可空（当前published），target_profile只允许evidence-v2；可给scope_bindings/document_default；auto_publish默认false |
| 恢复旧事实 | `POST /v2/documents/{document_id}/versions/{version_id}/restore`，body=`{}`，write+幂等键；创建全新版本/块ID，重新编码后ready，仍须显式发布 |
| 下载原件 | `GET /v1/documents/{document_id}/source?version_id=UUID`，read；省略版本下载当前published；删除/撤权后拒绝 |
| 删除 | `DELETE /v1/documents/{document_id}`，admin，204；PG立即不可见，OpenSearch异步清理，不承诺原件/备份硬删除 |

幂等键作用域为principal+KB：同键同payload返回原Job，同键不同payload返回409；重试同一写请求必须保持payload与key。普通ingest/reprocess同文档、同原件hash、同processing_profile拒绝重复；有意restore是审计过的新派生版本，允许同内容。旧版本不会因上传成功立即失效，直到新版本publish。恢复不恢复旧权限、删除状态或自动复制人工关系。

## 6. 公共管理接口目录

以下V1管理路径继续有效。所有请求/响应的完整嵌套字段以[OpenAPI](contracts/openapi.json)为准；不要将V2检索DTO用于这些接口。

| 方法/路径 | 权限 | 输入/结果 |
| --- | --- | --- |
| GET /v1/health | 无 | 进程存活；status/version |
| GET /v1/ready | 无 | 生产PG/OS就绪，不验证模型质量 |
| POST /v1/knowledge-bases | system admin | name/description → KnowledgeBase |
| GET /v1/knowledge-bases | 已鉴权 | 当前可访问KB列表及role |
| GET /v1/documents | KB read | kb_id，limit默认100、offset默认0；分页文档摘要 |
| GET /v1/documents/{id} | KB read | V1文档/版本视图，不包含V2处理扩展 |
| POST /v1/documents | KB write | 兼容文件上传，必须有Idempotency-Key，不接受V2归属映射 |
| POST /v1/documents/text | KB write | JSON kb_id/name/content/business_version/scope；生产异步任务且自动发布，不能替代可控文件审核流程 |
| POST /v1/api-keys | system admin | principal_name/label，明文Key仅创建时返回一次 |
| DELETE /v1/api-keys/{key_id} | system admin | 吊销Key，204 |
| PUT /v1/knowledge-bases/{kb_id}/grants | KB admin | principal_id/role(read/write/admin)，204 |
| DELETE /v1/knowledge-bases/{kb_id}/grants/{principal_id} | KB admin | 撤权，204 |
| GET /v1/knowledge-bases/{kb_id}/entities | KB read | limit/offset分页 |
| PUT /v1/knowledge-bases/{kb_id}/entities/{entity_id} | KB admin | name/kind/审核aliases/mention_chunk_ids |
| DELETE /v1/knowledge-bases/{kb_id}/entities/{entity_id} | KB admin | 删除及修订更新 |
| GET /v1/knowledge-bases/{kb_id}/relations | KB read | limit/offset分页 |
| PUT /v1/knowledge-bases/{kb_id}/relations/{relation_id} | KB admin | subject_id/object_id/relation_type/evidence及条件/时间；证据必须当前可见且同KB |
| DELETE /v1/knowledge-bases/{kb_id}/relations/{relation_id} | KB admin | 删除关系 |
| GET/PUT /v1/model-configuration | system admin | 外部Embedding/可选Reranker配置；Key不回显 |
| GET/POST /v1/index-generations | system admin | 查看/创建独立重建任务；POST模型三元组、dimension；省略chunking_version，由实际源profile记录，不重新解析 |
| POST /v1/index-generations/{id}/activate | system admin | 验证后原子切模型/索引；过期任务拒绝 |
| POST /v1/index-generations/{id}/rollback | system admin | 用旧模型配置重建当前PG数据；不是恢复旧事实 |
| DELETE /v1/index-generations/{id} | system admin | 取消未激活重建 |

V1 `POST /v1/search`固定D23形状：request_id、document_name/hash、score/score_type/stage_scores已存在；头部重排后可接RRF尾，不能跨score_type比较；include_context=false沿旧语义；产品/版本按文档metadata标签过滤。旧M3严格客户端不接受D23新增字段，**不能认为V1就天然兼容所有历史客户端**。新客户端明确选V2，升级和回退均显式，不自动换版本兜底。

## 7. 错误契约

```json
{"detail":"search_deadline_exceeded","error_code":"SEARCH_DEADLINE_EXCEEDED","trace_id":"22222222-2222-4222-8222-222222222222","request_id":null}
```

detail为字符串或校验错误列表（仅loc/msg/type，不回显input/ctx）；HTTP码和detail保留供既有调用方兼容。不得从错误文字推断知识不存在。

| HTTP | error_code / 情况 |
| --- | --- |
| 400 | BAD_REQUEST，例如生产上传缺幂等键 |
| 401 | INVALID_API_KEY |
| 403 | ACCESS_DENIED |
| 404/405 | NOT_FOUND / METHOD_NOT_ALLOWED |
| 409 | CONFLICT、INDEX_MAINTENANCE_IN_PROGRESS、INDEX_UPGRADE_REQUIRED、SEARCH_CONFIGURATION_CHANGED；detail区分重复版本、幂等payload冲突、缺Embedding配置等 |
| 413/415/422 | FILE_TOO_LARGE / UNSUPPORTED_FILE_TYPE / INVALID_REQUEST |
| 503 | DATABASE_UNAVAILABLE、SEARCH_BACKEND_UNAVAILABLE、SEARCH_CAPACITY_EXCEEDED、MODEL_CONFIGURATION_BUSY、SEARCH_RESPONSE_LIMIT_EXCEEDED |
| 504 | SEARCH_DEADLINE_EXCEEDED；不返回未完成最终复验的正文 |
| 500 | INTERNAL_ERROR；无驱动/模型密钥或完整请求回显 |

客户端超时、网络断连与HTTP504不同。读请求可以由调用方按业务预算有界退避重试；不得重复无限重试、扩大授权范围或把错误转换成空成功。写请求只用同一幂等键重试；未知处理结果先查询原Job。
