# 第三方接入与使用说明

更新：2026-10-25。配合[接口规范](API_GUIDE.md)、[系统约束](SYSTEM_CONSTRAINTS.md)及[OpenAPI](contracts/openapi.json)使用。本文不要求调用方安装CueKB服务代码。

## 1. 获取接入资料

向运行方取得base URL、受限API Key、授权KB UUID、服务器搜索预算和已启用契约版本。第三方只读应用使用read Key，不使用bootstrap/system admin Key。业务用户的KB范围由集成服务端校验，不能把前端提交的kb_ids直接当作用户授权。

知识库名称只用于展示，可以重复；请求的`kb_ids`必须填写创建结果的`id`（UUID）。运行方通过Portal或后台命令创建KB、签发业务Key并授权，完整步骤及交接清单见[README](../README.md#创建知识库与第三方配置)。创建Key返回的`id`是Key ID，`principal_id`用于KB授权，都不能代替KB UUID或密钥。

先请求`GET /v1/health`和`GET /v1/ready`确认路径/依赖；ready不证明模型已配置或质量通过。使用Bearer Key调用`GET /v1/knowledge-bases`，确认KB ID和role。生产未配置Embedding时导入/搜索会409；旧投影未升级时V2会`INDEX_UPGRADE_REQUIRED`，应让管理员重建generation，不自动退回V1掩盖问题。

## 2. 最小搜索调用

在调用方环境设置`CUEKB_BASE_URL`、`CUEKB_API_KEY`、`CUEKB_KB_ID`；这些是示例变量，实际变量名由客户端实现决定。Base URL不带`/v1`或`/v2`。示例request.json中的KB UUID必须替换为被授权的真实值；不要把示例业务内容当成生产知识。

```bash
curl --fail-with-body "$CUEKB_BASE_URL/v1/knowledge-bases" \
  -H "Authorization: Bearer $CUEKB_API_KEY"

curl --fail-with-body "$CUEKB_BASE_URL/v2/search" \
  -H "Authorization: Bearer $CUEKB_API_KEY" \
  -H 'Content-Type: application/json' --data-binary @request.json
```

Python异步例子（调用方安装httpx）：

```python
import asyncio, os, uuid
import httpx

async def main():
    async with httpx.AsyncClient(base_url=os.environ["CUEKB_BASE_URL"], timeout=10) as client:
        response = await client.post("/v2/search",
            headers={"Authorization": "Bearer " + os.environ["CUEKB_API_KEY"]},
            json={"query": "What are the storage conditions after opening?",
                  "kb_ids": [os.environ["CUEKB_KB_ID"]],
                  "request_id": str(uuid.uuid4()), "mode": "hybrid",
                  "selection_mode": "adaptive", "top_k": 20,
                  "query_facets": ["storage"]})
        if response.status_code != 200:
            error = response.json()
            raise RuntimeError((response.status_code, error["error_code"], error["trace_id"]))
        result = response.json()
        if result["schema_version"] != "2":
            raise RuntimeError("Unsupported CueKB contract")
        if result["retrieval_status"] == "degraded":
            print("Limited retrieval:", result["degraded_reasons"])
        for hit in result["hits"]:
            # Keep the complete evidence group, including required conditions.
            print(hit["rank"], hit["source_text"], hit["applicability"], hit["context_parts"])

asyncio.run(main())
```

客户端timeout应覆盖服务端预算加网络余量；示例10秒不是服务器SLA。生产代码应按提供的JSON Schema校验整个嵌套响应、处理非JSON网关错误，并保留HTTP状态、trace、request_id。不要仅检查hits是否非空。

新客户端建议显式启用adaptive，把top_k当安全上限，主要通过max_evidence_bytes约束消费负载。query应保留完整检索需求；selection_query可以补充原始问题用于选择，但不改变召回文本，不能弥补召回query缺失产品/属性造成的遗漏。query_facets可提供最多8个关注子项；它们是字面观察线索，不是必须返回的业务字段，也不承诺语义覆盖。

## 3. 证据消费规则

1. 按rank消费，score只用于当前同方法排序解释，不跨问题设“正确率阈值”。高分不升级unassessed。
2. 展示/保存document_id、version_id、content_sha256、chunk_id、anchor/source_spans，用于引用和原件核验；不要捏造updated_at、页码或文档名。
3. 产品归属与条件随事实一起处理。applicability未知/歧义时不从query补产品；required context缺失或context_state=unresolved时，不把裸正文作为完整适用结论。
4. 必要条件、否定和例外不因节省token被删除。若消费端预算装不下完整证据组，省略整个组并记录原因，不改写source_text，也不声称已完全回答。
5. 200 not_found、200 degraded空结果、HTTP错误分别处理。没有检索到价格/运输信息，不等于全库没有这些资料。
6. 相关证据可以保留多个产品/多个条件，但不能跨产品拼成一条事实；实体ID用于关联，可读名称用于展示/口述。

CueKB max_evidence_bytes约束API hits数组；调用方的模型包可能还有独立字符/token/字节限制。分别记录CueKB返回数、Adapter保留数、模型实际接收数和省略原因。top_k=20不保证模型收到了20条。

## 4. 文件导入与审核发布

向`POST /v2/documents`提交file和metadata字符串，必须有write权限与Idempotency-Key。metadata样例在[接口规范](API_GUIDE.md#5-导入归属映射更新与恢复)。

```bash
curl --fail-with-body "$CUEKB_BASE_URL/v2/documents" \
  -H "Authorization: Bearer $CUEKB_WRITE_KEY" \
  -H 'Idempotency-Key: manual-import-20261023-1' \
  -F 'metadata=<metadata.json' -F 'file=@manual.md;type=text/markdown'
```

记录202返回的Job ID；通过`GET /v1/jobs/{id}`轮询。失败记录error_code；ready时使用`GET /v2/documents/{document_id}`检查新版本/profile，需要时下载原件核对。随后用write权限调用publish：

```bash
curl --fail-with-body "$CUEKB_BASE_URL/v1/documents/$CUEKB_DOCUMENT_ID/publish" \
  -H "Authorization: Bearer $CUEKB_WRITE_KEY" -H 'Content-Type: application/json' \
  --data-binary @publish.json
```

publish.json为`{"version_id":"待发布版本UUID","scope_key":"default"}`。默认未自动发布；202、任务succeeded与ready均不等于已成为搜索可见版本。

同一原件换解析/范围映射使用reprocess，不能靠generation改变块边界。restore会产生新版本，先ready后显式publish；不要直接激活superseded版本，也不要复用旧块ID作为新证据。幂等请求超时保持相同key/payload查询或重试，不能每次换key制造重复任务。

## 5. 严格客户端升级

当前默认V2 ranked响应保持既有闭合形状，不附加selection字段；只有显式adaptive响应才包含新选择诊断。新增字段即使为null也可能被严格客户端拒绝，按以下顺序切换：

1. 先发布支持目标DTO的消费端，保留extra=forbid或等效严格校验。
2. 使用已导出的契约和合成示例做解析/引用映射测试，必要归属必须进入实际模型证据包，不能被metadata allowlist丢掉。
3. 在固定语料/权限/query及相同负载预算下对照ranked/adaptive，确认selection诊断可解析，再显式发送selection_mode=adaptive。不启用时省略selection_query/query_facets。
4. 回退选择策略时用ranked并移除动态专用输入。V1调用继续使用其独立DTO；不得使用V2局部过滤解释V1文档标签过滤。未知版本或坏响应明确失败，不自动重试另一个协议。

voice-service-agent尤其需要同步请求/响应模型、Adapter、Citation/ContextPart、native evidence选择、外部LLM证据及错误映射。现有“任何空hits都视作无资料”和“完整主块可丢弃所有截断上下文”的规则不足以消费V2必要依赖。这些第三方改造由调用方执行，本仓库交付不代表其他仓库已自动完成升级。
