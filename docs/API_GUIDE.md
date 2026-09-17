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

系统管理员可通过`GET /v1/model-configuration`读取模型配置状态，通过`PUT /v1/model-configuration`保存Embedding与可选Reranker的`base_url`、`model`、`api_key`。GET只返回`*_api_key_set`布尔值，不返回密钥；PUT中密钥为`null`表示保留现有值。首次保存Embedding时必须提供密钥，Reranker启用时也须提供完整三元组。已有OpenSearch索引与新Embedding Model不匹配时返回409；更换模型需先完成索引重建方案。

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
| `mode=related` | 当前使用hybrid主链路并标记范围受限；关系扩展代码状态为**待开始** |
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
