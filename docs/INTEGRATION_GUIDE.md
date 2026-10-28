# 第三方接入与使用说明

更新：2026-10-26。所有CueKB接口均使用`/v2`，包括知识库、Key、Job和发布管理。配合[接口规范](API_GUIDE.md)、[系统约束](SYSTEM_CONSTRAINTS.md)及[OpenAPI](contracts/openapi.json)使用。本文不要求调用方安装CueKB服务代码。

## 1. 获取接入资料

向运行方取得base URL、受限API Key、授权KB UUID、服务器搜索预算和已启用契约版本。第三方只读应用使用read Key，不使用bootstrap/system admin Key。业务用户的KB范围由集成服务端校验，不能把前端提交的kb_ids直接当作用户授权。

知识库名称只用于展示，可以重复；请求的`kb_ids`必须填写创建结果的`id`（UUID）。运行方通过Portal或后台命令创建KB、签发业务Key并授权，完整步骤及交接清单见[运行方配置](#6-创建知识库与第三方配置)。创建Key返回的`id`是Key ID，`principal_id`用于KB授权，都不能代替KB UUID或密钥。

先请求`GET /v2/health`和`GET /v2/ready`确认路径/依赖；ready不证明模型已配置或质量通过。使用Bearer Key调用`GET /v2/knowledge-bases`，确认KB ID和role。生产未配置Embedding时导入/搜索会409；旧投影未升级时V2会`INDEX_UPGRADE_REQUIRED`，应让管理员重建generation，不能忽略错误继续使用不匹配的投影。

## 2. 最小搜索调用

在调用方环境设置`CUEKB_BASE_URL`、`CUEKB_API_KEY`、`CUEKB_KB_ID`；这些是示例变量，实际变量名由客户端实现决定。Base URL不带`/v2`。示例request.json中的KB UUID必须替换为被授权的真实值；不要把示例业务内容当成生产知识。

```bash
curl --fail-with-body "$CUEKB_BASE_URL/v2/knowledge-bases" \
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

显示主事实使用source_text，上下文从context_parts补充；不要用非空context替代source_text。预算限制下context可能只包含条件段，主事实仍完整保存在source_text。同一chunk_id的主块无需重复显示；必要上下文不能因此被删掉。门户按此方式区分“命中原文”“必要上下文”和“相关上下文”，并提示未解决依赖/预算省略。

CueKB max_evidence_bytes约束API hits数组；调用方的模型包可能还有独立字符/token/字节限制。分别记录CueKB返回数、Adapter保留数、模型实际接收数和省略原因。top_k=20不保证模型收到了20条。

## 4. 文件导入与审核发布

向`POST /v2/documents`提交file和metadata字符串，必须有write权限与Idempotency-Key。metadata样例在[接口规范](API_GUIDE.md#5-导入归属映射更新与恢复)。

```bash
curl --fail-with-body "$CUEKB_BASE_URL/v2/documents" \
  -H "Authorization: Bearer $CUEKB_WRITE_KEY" \
  -H 'Idempotency-Key: manual-import-20261023-1' \
  -F 'metadata=<metadata.json' -F 'file=@manual.md;type=text/markdown'
```

记录202返回的Job ID；通过`GET /v2/jobs/{id}`轮询。失败记录error_code；ready时使用`GET /v2/documents/{document_id}`检查新版本/profile，并用`GET /v2/documents/{document_id}/versions/{version_id}/review`分页核对产品归属与必要章节，需要时下载原件。汇总为全版本统计，不能只看第一页就认为其余块已通过。随后用write权限调用publish：

```bash
curl --fail-with-body "$CUEKB_BASE_URL/v2/documents/$CUEKB_DOCUMENT_ID/publish" \
  -H "Authorization: Bearer $CUEKB_WRITE_KEY" -H 'Content-Type: application/json' \
  --data-binary @publish.json
```

publish.json为`{"version_id":"待发布版本UUID","scope_key":"default"}`。默认未自动发布；202、任务succeeded与ready均不等于已成为搜索可见版本。

同一原件换解析/范围映射使用reprocess，不能靠generation改变块边界。restore会产生新版本，先ready后显式publish；不要直接激活superseded版本，也不要复用旧块ID作为新证据。幂等请求超时保持相同key/payload查询或重试，不能每次换key制造重复任务。

门户默认先处理再审核。已有产品可直接上传；规则识别产品标题、明确主体属性及表格产品轴，分离单品规格与混合套件。首次维护产品目录后跨文档复用；目录项目应为product/model/bundle类型，并保存型号/别名。上传可选择规则模板及文本结构模式。

打开“审核归属与必要章节”进入“文档审核与发布”，默认只看待办。每条卡片先显示问题类别、完整原文、位置和处理建议，再勾选实际适用的产品。单选确认单产品，多选确认同一说明共同适用；目录身份重叠需核对名称、型号及短编号。分别赋值的规格应按完整原文拆分，比较内容保留比较事实，套件选择套件本身；不再对异质待办统一绑定单个产品或全部确认为通用资料。常规操作无需JSON/UUID，不改原文值。目录缺产品时可在折叠区添加，需要KB admin权限。

选择自动保存为审核草稿。可以切到全部事实检查自动确认，准备发布前可撤回。批量处理按各条卡片已勾选的产品提交；跨页统一操作仅提供暂缓或不纳入检索。修订冲突会保留已经成功的操作并要求重新核对。

完成后点击“完成审核，准备发布”。页面留在当前版本并显示索引任务进度，完成后点击“发布当前版本”，无需生成另一份文档或再次审核新版本。失败时审核保留，点击“重试索引准备”继续同一任务及版本。没有待办且未修改自动结果时可直接发布；只有后端发布指针切换完成才对检索可见。已发布版本审核只读；更改已发布内容使用文档管理的显式新版本导入/重处理。

“稍后处理”保存待办但继续阻塞发布；“本次不纳入检索”移除该片段的检索资格，保留原文与审核记录，准备发布前可撤回。两者不混称“忽略”。

已确认通用资料保持unknown是合法状态。歧义产品、来源/表格损坏、未解决必要条件须处理或明确排除；排除不删除原件，若排除了主事实需要的条件，主事实仍不能发布。整份内容全部排除时也不能发布。UI、直接publish API及auto_publish共用服务端门槛，不再提供忽略全部警告后发布。

结构版本变化时，可先用高级章节配置按当前结构重解析，再审核新单元；只做generation不会修正PG归属。必要关系保留各条件自己的unknown身份，不将它们自动变成主事实的产品。重名/缺失引用需选择真实原文目标；一条选择不能解除其他未匹配引用或续块/超限问题。

规则模板由管理员显式保存，只影响未来选择该模板的任务。属性词配置辅助识别属性，不证明数值真实；产品与模板不会因某次选择自动训练或扩散。没有型号的产品使用filters.product_entity_id查询，直接取目录实体UUID；无需为了查询填写假型号。

## 5. 严格客户端升级

V2响应新增applicability.reason_codes、hit.dependency_issues、context_parts.applicability/link_provenance。严格DTO应先同步当前生成契约；不能丢弃未知归属条件的真实状态或将其继承为主事实产品。ranked不附加selection字段；只有显式adaptive响应包含选择诊断。新增字段即使为null也可能被严格客户端拒绝，按以下顺序切换：

1. 先发布支持目标DTO的消费端，保留extra=forbid或等效严格校验。
2. 使用已导出的契约和合成示例做解析/引用映射测试，必要归属必须进入实际模型证据包，不能被metadata allowlist丢掉。
3. 在固定语料/权限/query及相同负载预算下对照ranked/adaptive，确认selection诊断可解析，再显式发送selection_mode=adaptive。不启用时省略selection_query/query_facets。
4. 回退选择策略时用ranked并移除动态专用输入。未知版本或坏响应明确失败，不自动重试另一个检索协议。

voice-service-agent尤其需要同步请求/响应模型、Adapter、Citation/ContextPart、native evidence选择、外部LLM证据及错误映射。现有“任何空hits都视作无资料”和“完整主块可丢弃所有截断上下文”的规则不足以消费V2必要依赖。这些第三方改造由调用方执行，本仓库交付不代表其他仓库已自动完成升级。

## 6. 创建知识库与第三方配置

第三方必须使用**知识库 ID（UUID）**，检索请求字段为 `kb_ids: ["<UUID>"]`。知识库名称 `name` 只用于展示，允许同名，不能代替 ID，也不能作为唯一查找条件。ID 由 CueKB 自动生成；创建接口返回的 `id` 就是检索所用的知识库 ID。

创建知识库需要 **system admin** Key，通常使用部署时设置的 `CUEKB_BOOTSTRAP_API_KEY`。单个知识库的 `admin` 角色不代表系统管理员。创建只需要名称（去除首尾空白后 1–200 字符），描述可选（最多 2000 字符）；无需指定产品、模型或 ID。两种创建入口都调用 `POST /v2/knowledge-bases`，创建后是空库，仍需配置 Embedding、导入并发布文档才能检索证据。

### 方式一：Portal

1. 打开运行方提供的 `<Base URL>/portal/`，默认宿主端口为 `8085`。
2. 使用系统管理员 Key 连接，进入左侧 **知识库管理**。全新实例没有知识库时会自动进入该页。
3. 填写名称及可选描述，点击 **创建知识库**；成功后自动选中新库。页面显示完整 UUID、名称、描述和当前 Key 的权限，可复制 ID 和 V2 检索请求。
4. 在 **模型配置** 配置 Embedding；已有有效配置可复用。然后进入 **导入文档** 上传，确认处理和发布成功，再到 **检索验证** 检查原文证据。

普通业务 Key 在此页只能查看、选择已授权的知识库和复制接入信息，创建表单不可用。页面显示的 Base URL 是浏览器当前地址，须确认第三方服务器实际可达；第三方容器中的 `localhost` 指向第三方容器自身。

### 方式二：后台命令

在部署仓库目录执行下列命令。API 容器已带管理模块，`--bootstrap` 读取容器已有的 `CUEKB_BOOTSTRAP_API_KEY`，无需把密钥写入命令参数：

```sh
docker compose --env-file .env.production exec -T api \
  python -m cuekb.admin --base-url http://127.0.0.1:8080 --bootstrap \
  kb create --name '产品资料库' --description '面向客服的产品和技术原文'

docker compose --env-file .env.production exec -T api \
  python -m cuekb.admin --base-url http://127.0.0.1:8080 --bootstrap kb list
```

成功输出 JSON；创建结果的 `id` 为新知识库 UUID，列表包含 `role`。命令复用 API 鉴权和 PostgreSQL 权威存储，不是数据库初始化指令。创建不幂等，也不会按名称复用旧库；超时、断连或结果不确定时先 `kb list` 核查，避免重复创建。

在安装了本项目的主机上也可用 `.venv/bin/python -m cuekb.admin` 或安装生成的 `cuekb-admin`。连接远端时设置 `CUEKB_BASE_URL` 和管理员 `CUEKB_API_KEY`，或使用 `--api-key-file /安全目录/admin.key`；通用选项放在 `kb` / `key` 前。`--help` 查看完整参数。请求默认超时 10 秒，可用 `--timeout` 设置大于 0、不超过 60 秒；命令不会自动重试创建或跟随重定向。退出码：0 成功、1 请求失败、2 参数或凭据配置错误；成功 JSON 写 stdout，错误写 stderr。

### 为第三方创建业务 Key 并授权

知识库创建不会自动给第三方授权。先用系统管理员创建单独的业务 Key：

```sh
docker compose --env-file .env.production exec -T api \
  python -m cuekb.admin --base-url http://127.0.0.1:8080 --bootstrap \
  key create --principal-name voice-service-agent --label retrieval-read
```

输出 `id`（Key ID）、`principal_id`（授权主体 ID）、`api_key`（业务密钥）。**明文密钥仅此次返回**，应保存到第三方密钥管理中，不写入工单、日志或 Git。Key 创建后没有任何 KB 权限；用实际知识库 UUID 和返回的 `principal_id` 替换下面变量，再授予 `read`：

```sh
KB_ID='替换为知识库返回的 id'
PRINCIPAL_ID='替换为业务 Key 返回的 principal_id'
docker compose --env-file .env.production exec -T api \
  python -m cuekb.admin --base-url http://127.0.0.1:8080 --bootstrap \
  kb grant --kb-id "$KB_ID" --principal-id "$PRINCIPAL_ID" --role read
```

`read` 允许检索及读取；`write` 增加导入/发布权限；`admin` 增加该 KB 的管理权限。业务查询通常只授予 `read`，不要向第三方交付 bootstrap Key、数据库凭据或模型密钥。多个 KB 要逐个授权。完整 Key 吊销及撤权接口见[接口规范](API_GUIDE.md#6-公共管理接口目录)。

### 交给第三方的资料

| 信息 | 必需性与用法 |
| --- | --- |
| Base URL | 必需；从第三方服务器可达的 CueKB 根地址，不带 `/v2` 或 `/portal/`；远程生产使用 HTTPS 或受控加密链路 |
| 业务 API Key | 必需；已对目标 KB 授权，放在 `Authorization: Bearer <api_key>`；不要把 Key ID 或 `principal_id` 当密钥 |
| 知识库 UUID 列表 | 必需；放入 `kb_ids`，例如 `["11111111-1111-4111-8111-111111111111"]`；使用实际 ID，名称仅供展示 |
| 接口和契约 | 新接入为 `POST /v2/search`；提供 [OpenAPI](contracts/openapi.json)、[接口规范](API_GUIDE.md)及[接入说明](INTEGRATION_GUIDE.md) |
| 运行预算 | 提供服务端检索时限和建议客户端超时，约定 `top_k` 与消费端证据预算；默认值与上限见[系统约束](SYSTEM_CONSTRAINTS.md) |
| 选择策略与校准 | 建议新客户端显式使用 `selection_mode=adaptive`，升级严格DTO后启用；确认运行方是否提供匹配的相关性校准，未校准不能保证低相关拒收 |
| 知识库名称、描述 | 可选展示信息，不参与权限判断或按名路由 |

### 门户产品目录与跨页审核常见问题

“手工声明的产品名”只用于显式全文/章节映射，不创建目录实体；没有勾选全文归属或添加章节映射时，仅填写名称也不会绑定正文。审核页的产品下拉框读取当前KB的product/model/bundle目录，不读取该输入框。使用“目录中缺少产品时新建”保存，或由管理员在“实体与关系”中创建正确类型的实体；term不属于产品候选。

品牌介绍可以确认为通用资料；多产品资料应分别建立单品，混合礼盒建立bundle类型，不能将品牌名称当作所有单品/组合装的统一归属。创建目录项不会自动重判既有审核草稿；可在草稿选择确认，或按章节映射重处理。旧解析结果全部为“正文”时，自动检测选项不会追溯改变已生成版本，需重解析新版本。

单条decisions返回200只代表该条草稿保存成功，不等于全部通过或已发布。`bundle_requires_bundle_identity`表示跨页选中记录包含套件事实，但目标是非bundle目录项；也可能是规则误识别，需要核对触发片段与原件，不能为了通过把普通单品改成bundle。每批最多100条，失败批次整批回滚，此前成功批次保留；不会自动跳过失败记录。

门户修复后，新建/CSV导入会同步刷新跨页产品下拉框；422后解除浏览器的原失败操作续接，保留服务器已完成批次，重新读取草稿。用户可调整筛选/操作后重新预览，不会因旧续接记录继续执行原操作。修改前的页面如遇重复相同422，可关闭并重新打开该门户标签页，清除该页会话中的旧续接状态；已保存的审核决定不受影响。通用资料、单品、套件应分别处理，不能将全部未知记录一键绑定同一产品。

## 7. S1审核子页与指定记录多选

导入完成后点“审核处理结果”，进入带URL的审核子页；高级章节配置也是子页。顶部/底部“返回上一次操作”和浏览器后退均可返回来源页面，文档详情入口可恢复原详情；审核返回时保留同版的页码、筛选、勾选和滚动位置。切换KB或审核修订变化会清理旧勾选，避免处理不再适用的记录。

每条卡片勾选“选择此记录”，可跨页保留，最多100条。选择“对勾选记录执行”的操作及目标产品，点“预览并处理勾选记录”。仅提交明确勾选的ID，同批校验失败整批不保存，勾选保留供调整；成功后清空。它与“跨页处理当前筛选结果”不同，后者冻结整个筛选集合。产品、通用资料、套件应分别处理，批量操作不放宽必要条件或套件身份校验。

删除文档后，门户导入页同步移除该文档全部版本的进度卡片并停止轮询；删除期间返回的旧轮询不会重建卡片。服务器任务与审核审计保留，不等于删除审计记录。

非产品制度/流程在上传“文档内容类型”选择相应选项；产品手册保持自动或产品模式。旧版本缺规范树时子页显示提示，可点“按当前结构重新解析原件”，产生新版本后审核发布；原发布版本及旧草稿保留。升级/格式验证命令统一见[验收7.5](ACCEPTANCE.md#75-s1文档核心与门户本批部署验收)。
