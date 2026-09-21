# CueKB 对外API接入说明

第三方系统只需要服务方提供的Base URL、API Key和`kb_id`。每次请求携带：

```http
Authorization: Bearer <API_KEY>
```

API Key只返回一次；调用方不得记录到源码、URL或普通业务日志。服务方必须授予该Key对应主体对知识库的`read`或`write`权限。

## 1. 导入语料

### 正式文件接口

`POST /v1/documents`使用`multipart/form-data`，支持`.pdf`、`.docx`、`.md`、`.markdown`和`.txt`。请求必须携带唯一的`Idempotency-Key`；相同Key和相同请求返回原任务，相同Key但内容不同返回409。

```bash
curl --fail-with-body "$CUEKB_URL/v1/documents" \
  -H "Authorization: Bearer $CUEKB_API_KEY" \
  -H "Idempotency-Key: manual-2026-09-v1" \
  -F 'metadata={"kb_id":"替换为UUID","name":"设备手册","business_version":"V1","scope":{"product_model":"MODEL_X","software_version":"V1"},"auto_publish":true};type=application/json' \
  -F 'file=@/absolute/path/manual.pdf;type=application/pdf'
```

`metadata`字段：

| 字段 | 含义 |
| --- | --- |
| `kb_id`、`name` | 必填；目标知识库UUID和文档名称 |
| `business_version` | 可选业务版本标识 |
| `scope` | 可选适用范围；当前检索过滤支持`product_model`和`software_version` |
| `document_id` | 上传同一逻辑文档的新版本时填写首次任务返回的文档UUID |
| `auto_publish` | `true`表示质量与索引通过后自动切换为可见版本；默认`false`，由管理员另行发布 |

成功返回HTTP 202和任务。以下UUID与时间是字段示例，不是一次生产运行记录：

```json
{
  "id": "任务UUID",
  "kb_id": "知识库UUID",
  "document_id": "逻辑文档UUID",
  "version_id": "此次版本UUID",
  "status": "queued",
  "stage": "uploaded",
  "progress": 0,
  "error_code": null,
  "error_message": null,
  "created_at": "2026-09-14T00:00:00Z",
  "updated_at": "2026-09-14T00:00:00Z"
}
```

轮询`GET /v1/jobs/{id}`。可检索完成状态为`status=succeeded`且`stage=published`；`stage=ready`表示处理完成但尚未发布。失败时读取`error_code`和`error_message`，不要在未知结果时换一个幂等键盲目重传。

### 文本JSON接口

`POST /v1/documents/text`保留给直接提交UTF-8文本的系统。生产模式同样必须提供`Idempotency-Key`，成功后由Worker自动发布。

## 管理查询接口

管理门户使用下列只读接口；它们与导入、检索接口使用相同的Bearer API Key和知识库ACL。

| 接口 | 返回内容 | 最低权限 |
| --- | --- | --- |
| `GET /v1/knowledge-bases` | 当前API Key可访问的知识库及其`read/write/admin`角色 | 已认证 |
| `GET /v1/documents?kb_id=<UUID>&limit=100&offset=0` | 未删除文档、最新状态、当前发布版本和版本数量 | `read` |
| `GET /v1/documents/{document_id}` | 文档信息、全部版本、状态和当前发布版本 | `read` |

内置管理门户位于`/portal/`。API Key仅进入当前标签页的`sessionStorage`，浏览器请求仍直接调用本章接口，不建立额外Cookie会话。

系统管理员可通过`GET /v1/model-configuration`读取模型配置状态，通过`PUT /v1/model-configuration`保存Embedding与可选Reranker的`base_url`、`model`、`api_key`。GET只返回`*_api_key_set`布尔值，不返回密钥；PUT中密钥为`null`表示保留现有值。首次保存Embedding时必须提供密钥，Reranker启用时也须提供完整三元组。已有OpenSearch索引与新Embedding Model不匹配时返回409；更换模型通过下文generation重建与切换接口完成。

```json
{
  "kb_id": "替换为UUID",
  "name": "故障处理说明",
  "content": "E102 表示光链路异常。\n\n检查光功率。",
  "business_version": "V1",
  "scope": {"product_model": "MODEL_X", "software_version": "V1"}
}
```

当前单文件上限由服务端`CUEKB_MAX_UPLOAD_BYTES`配置，默认50MiB。原文件按内容哈希保存；Worker异步执行Docling/OCR、表格解析、基础质量检查、分块、Embedding和OpenSearch索引。空内容、无效UTF-8、高替换字符比例或处理失败的版本不会发布；中文扫描件和复杂表格的更完整质量规则仍需真实样本验收。

## 2. 检索证据

`POST /v1/search`：

```json
{
  "query": "设备升级后为什么容易断开？",
  "kb_ids": ["替换为UUID"],
  "mode": "auto",
  "top_k": 8,
  "filters": {"product_model": "MODEL_X", "software_version": "V1"},
  "include_context": true
}
```

| 字段 | 说明 |
| --- | --- |
| `mode=auto` | 明确标识符走exact短路径，其他问题走hybrid |
| `mode=exact` | BM25关键词路径，明确跳过Embedding和重排 |
| `mode=hybrid` | BM25与查询Embedding/向量召回并行并用RRF融合；仅在服务端配置重排地址后才可能重排 |
| `mode=related` | hybrid主链路加有证据的一跳扩展，RRF融合；始终标记范围受限 |
| `top_k` | 1–20，默认8；合法结果不足时不会放松权限或版本约束凑满 |

成功返回HTTP 200。`hits`是调用方应展示或交给自身回答模块的原文证据。以下数值是契约示例，不是性能实测：

```json
{
  "trace_id": "检索UUID",
  "retrieval_status": "ok",
  "evidence_status": "unassessed",
  "degraded_reasons": [],
  "scope_limited": false,
  "content_revisions": {"知识库UUID": 3},
  "timings_ms": {"keyword": 8.1, "embedding": 21.4, "vector": 13.8, "total": 60.0},
  "retrieval_path": "hybrid",
  "executed_stages": ["keyword", "embedding", "vector", "rrf"],
  "skipped_stages": [
    {"stage": "route", "reason": "auto_natural_language"},
    {"stage": "rerank", "reason": "reranker_service_unconfigured"}
  ],
  "hits": [{
    "chunk_id": "内容块UUID", "document_id": "文档UUID", "version_id": "版本UUID",
    "rank": 1, "source_text": "原文证据", "context": "必要上下文",
    "title_path": ["故障处理"],
    "anchor": {"page": 3, "heading_path": ["故障处理"], "start_offset": 0, "end_offset": 20, "bbox": null},
    "metadata": {"business_version": "V1", "product_model": "MODEL_X"},
    "retrieval_sources": ["keyword", "vector"]
  }]
}
```

`retrieval_status=degraded`时仍可使用已经通过PG权限/删除/发布版本校验的`hits`，同时必须保留`degraded_reasons`；常见值为`vector_unavailable`和`rerank_unavailable`。`not_found`表示合法候选为空，不等于知识事实不存在。`evidence_status=unassessed`表示系统尚未判定证据充分性，排名和模型分数不是事实真实性概率。

`retrieval_path`、`executed_stages`和`skipped_stages`说明本次真实执行路径。例如exact请求的`executed_stages`不应包含`embedding`；未配置重排地址时记录`reranker_service_unconfigured`，其他跳过原因包括显式关闭、候选不足和剩余预算不足。

这些选择不是调用模型临时猜测，也不是只能改源码的硬编码。调用方可用`mode`明确选择exact或hybrid；auto根据服务端可配置的标识符规则确定路径。Embedding服务的`base_url`、`api_key`和`model`由系统管理员在门户保存；配置完成前导入和检索返回`409 embedding_service_not_configured`。外部API契约及model校验见[系统设计](SYSTEM_DESIGN.md#222-外部模型-api-契约)。重排默认不执行；服务端完整配置Reranker后，还会检查`CUEKB_RERANK_ENABLED`、候选数量、`CUEKB_RERANK_MIN_REMAINING_MS`和请求总deadline。重排模型繁忙时会快速返回429，检索服务保留已经通过权威校验的RRF结果并标记`rerank_unavailable`。

原件可通过`GET /v1/documents/{document_id}/source`下载当前发布版本；指定历史版本可附`?version_id=<UUID>`，仍执行知识库读权限和删除状态检查。

## 错误处理

| 状态 | 含义 |
| --- | --- |
| 400 | 缺少`Idempotency-Key`等请求前置条件 |
| 401 | API Key缺失、无效或已吊销 |
| 403 | 对指定知识库没有所需权限 |
| 404 | 知识库、文档、版本或任务不存在 |
| 409 | 幂等冲突、版本未ready或开发/生产模式不匹配 |
| 413 / 415 | 文件过大 / 文件类型不支持 |
| 422 | JSON、UUID或字段范围校验失败 |
| 5xx | 服务或依赖异常；不得转换成“无结果” |

运行实例的`/openapi.json`是字段级契约。部署方变更API、模型或索引generation时应通知调用方并完成兼容性验证。

## 3. M3关系与上下文

`POST /v1/search`新增可选`relations`字段，仅`mode=related`使用：

```json
{
  "entity_ids": ["起始实体UUID"],
  "types": ["depends_on", "references"],
  "direction": "outgoing",
  "at": "2026-09-21T00:00:00+08:00"
}
```

`entity_ids`最多20个；也会从query精确匹配名称/审核别名，以及普通合法召回块的mention确定起点。`direction`可选outgoing/incoming/both，默认outgoing；types为空不过滤类型；at默认当前时间，有效区间为`[valid_from, valid_until)`，时间必须包含时区。带型号/软件条件的关系须与请求filters显式匹配。所有实体、关系和证据须属于请求授权的同一知识库，扩展不跨库，不走第二跳。

响应`retrieval_sources`新增`relation`。每个hit新增：

- `relations`：匹配的relation_id、subject_id、object_id、relation_type、conditions、stance和chunk_id；stance为supports/refutes，不能解释为事实真假已判定。
- `context_parts`：组成context的各个chunk_id、source_text、anchor和title_path，均为同一文档同一当前版本。
- `context_truncated`：上下文受字符/块预算限制。总预算耗尽时context可为空字符串，source_text仍提供命中原文。include_context=false时context为null、context_parts为空。

### 实体和关系管理

只在production后端开放；读取需要知识库read，维护需要知识库admin。列表使用`limit=50`（最多200）、`offset=0`。

| 方法与路径 | 行为 |
| --- | --- |
| GET `/v1/knowledge-bases/{kb_id}/entities` | 列出实体、别名和当前可见mention块ID |
| PUT `/v1/knowledge-bases/{kb_id}/entities/{entity_id}` | 以调用方稳定UUID创建/完整替换实体、别名、mention；成功204 |
| DELETE `/v1/knowledge-bases/{kb_id}/entities/{entity_id}` | 删除实体及其关系、别名和mention；成功204 |
| GET `/v1/knowledge-bases/{kb_id}/relations` | 列出仍有当前发布证据的关系；不代表条件和时间必然适用 |
| PUT `/v1/knowledge-bases/{kb_id}/relations/{relation_id}` | 稳定UUID创建/完整替换关系及证据；成功204 |
| DELETE `/v1/knowledge-bases/{kb_id}/relations/{relation_id}` | 删除关系及证据引用；成功204 |

实体PUT请求：

```json
{"name":"设备R1","kind":"device","aliases":["路由器R1"],"mention_chunk_ids":["当前发布原文块UUID"]}
```

关系PUT请求：

```json
{
  "subject_id":"起点实体UUID","object_id":"终点实体UUID","relation_type":"depends_on",
  "evidence":[{"chunk_id":"当前发布原文块UUID","stance":"supports"}],
  "conditions":{"product_model":"R1"},
  "valid_from":null,"valid_until":null
}
```

关系类型限belongs_to、adjacent_to、alias_of、revises、replaces、references、depends_on、applies_to；别名最多50条，每条最多200字符；mention最多100块；每个关系1–50条证据。UUID所属知识库或证据可见性不匹配返回422；唯一冲突返回409；权限不足403。文档删除或发布版本替换后，旧关系证据立即停止返回，不依赖OpenSearch清理完成。重复PUT不会增加重复证据，但会递增内容修订。

## 4. 索引generation管理

仅system admin，所有配置密钥加密保存在PG，GET不返回密钥。普通模型配置接口仍拒绝直接更换已有Embedding model；首次模型配置完成后才可创建generation。

| 方法与路径 | 行为 |
| --- | --- |
| GET `/v1/index-generations` | 最近50条状态、模型、维度、分块版本、块数、尝试次数及安全错误代码；未验证块数为null |
| POST `/v1/index-generations` | 排队重建，202返回id与queued状态 |
| POST `/v1/index-generations/{id}/activate` | ready校验后切换，成功204 |
| POST `/v1/index-generations/{id}/rollback` | 根据retired旧配置重建当前数据，202返回新任务；ready后仍需activate |
| DELETE `/v1/index-generations/{id}` | 取消未激活任务，成功204；正在持锁重建时409，稍后重试 |

创建请求沿用`ModelConfigurationUpdate`字段，再加`dimension`（1–65536）与`chunking_version`（当前仅`structured-v1`）。密钥null保留现有密钥；目标服务使用不同凭据时须显式填写。

```json
{
  "embedding_base_url":"https://embedding.example/v1",
  "embedding_api_key":"目标服务密钥",
  "embedding_model":"目标模型标识",
  "reranker_base_url":"","reranker_model":"","reranker_api_key":null,
  "dimension":1024,"chunking_version":"structured-v1"
}
```

状态：queued → building → ready → active → retired；失败有界退避重试，耗尽后failed。至多一个未完成重建。building期间内容/模型写入返回`409 index_maintenance_in_progress`，查询及撤权保持可用。重建完成到切换之间发生内容/配置变化时返回`409 generation_stale_rebuild_required`，必须取消并重建。回退不会恢复被删除内容或被撤销权限。模型服务可用性及真实向量质量由目标环境验收。
