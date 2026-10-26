# 部署与操作手册

更新：2026-10-26。下列是全新部署的运行步骤，不是本轮已部署的证据。真实环境执行记录在[任务板](../TASK_BOARD.md)。

## 1. 环境与配置

Python 3.11以上；Docker镜像基于Python 3.12，Compose使用PostgreSQL 17.6、OpenSearch 3.0单节点。应用分API和CPU Docling Worker，Embedding与Reranker由运行方单独提供。详细模型协议见[模型接入](MODEL_INTEGRATION.md)。

复制`deploy/production.env.example`为`.env.production`并替换所有占位密钥，权限设为600。PG密码若含URL保留字符，需与生成的数据库URL编码约束一起处理。不要提交env或把密钥放进诊断报告。

- `CUEKB_API_KEY_PEPPER`至少32字符，`CUEKB_BOOTSTRAP_API_KEY`至少24字符；bootstrap拥有系统管理员权限，只作初始化和管理。
- `CUEKB_MODEL_CONFIG_KEY`至少32字符，用于PG模型凭据加密，API/Worker必须相同且长期备份。更换后旧凭据不可读，不能仅靠PG备份恢复。
- 模型地址、API Key与model由门户系统管理员配置，不能直接用任意Embedding模型覆盖既有向量。缺配置时API可启动，但相关操作明确阻断。
- 默认API宿主端口8085；PG/OS不映射宿主端口。OS security plugin关闭，只应在隔离内部网络访问。Compose不提供TLS；对外访问由反向代理/网络层提供HTTPS与受控访问。

检索候选、时限与容量的默认值见[系统约束](SYSTEM_CONSTRAINTS.md)和env示例。调大deadline不是性能证明；应按目标硬件和服务延迟测量。多API进程容量会叠加，Worker模型任务不受V2进程内配额约束。

## 2. 手动构建和启动

按当前锁文件用venv/pip构建；Worker先安装CPU Torch，镜像构建和部署分离：

```sh
docker build --target api -t cuekb-api:latest .
docker build --target worker -t cuekb-worker:latest .
chmod 600 .env.production
./scripts/deploy-production.sh .env.production
```

自定义镜像标签须写入env中的`CUEKB_API_IMAGE`/`CUEKB_WORKER_IMAGE`。部署脚本只做Compose检查及`up --no-build`，不自动构建或拉取应用镜像。PostgreSQL首次在空数据目录启动时自动初始化当前完整结构；API/Worker等待PG和OS健康后启动。健康检查不代表模型质量已验收。

检查`GET /v2/health`、`GET /v2/ready`与`/portal/`。部署脚本的宿主机探针使用兼容旧版curl的`--fail --silent --show-error`，带连接/单次请求超时及有界重试。旧脚本若报`--fail-with-body: is unknown`，将该选项替换为`--fail --show-error`后重跑脚本即可，无需为此重建应用镜像。生产业务API使用Bearer Key；健康和ready公开但不返回凭据。创建业务Key并授予目标KB read/write/admin，第三方查询不要使用bootstrap。

知识库通过Portal的“知识库管理”或`python -m cuekb.admin ... kb create`创建；命令复用HTTP API，不直接写数据库。创建、列举、业务Key签发和授权的可执行命令，以及第三方所需的Base URL、业务Key和KB UUID，统一见[接入说明](INTEGRATION_GUIDE.md#6-创建知识库与第三方配置)。更新应用后须重新构建API镜像才能使用新页面及命令模块。

## 3. 空库初始化与搜索投影

`db/schema.sql`是完整结构的唯一来源，包含内容、权限、模型配置、关系、generation和V2证据字段及约束。Compose将它只读挂载到PostgreSQL官方初始化目录；数据库首次使用空数据目录时自动执行，无需手工数据库指令。该文件作为一个事务执行，初始化失败须先排查日志，不能将缺表数据库作为正常实例使用。

全新部署必须使用新的空PG数据卷。已有数据目录不会重新执行初始化文件，不属于本次部署输入；需要与旧实例隔离时可为新部署设置独立`COMPOSE_PROJECT_NAME`并处理宿主端口冲突。部署脚本不清理旧容器或数据卷，也不修改已有库结构。保留仓库中的`db/schema.sql`，缺文件时Compose挂载应直接失败；仅重建应用镜像不能改变已有数据库结构。

在门户配置Embedding后，Worker为新文档创建带`lexical_revision=scope-lexical-v2.3`的V2搜索投影。更换Embedding、维度、输入模板或词法投影时，由系统管理员通过`POST /v2/index-generations`创建重建任务，省略`chunking_version`，等待ready后activate。维护锁期间内容写入409；已兼容的查询继续，缺少新词法revision的V2查询明确INDEX_UPGRADE_REQUIRED，不临时把旧字段当作新字段。该流程只重建搜索投影，不改变数据库结构。

需要新的解析/归属时，对文档单独执行V2 reprocess，检查新版本再publish；generation不会自动重切块。想恢复旧事实则用restore生成全新块ID、等待ready后发布。generation rollback只重建当前事实的旧模型投影。

## 4. 文档日常操作

1. 上传前整理清晰章节/表头；多产品映射用scope_bindings，不能把全篇metadata标签当成局部证据。
2. `POST /v2/documents`带幂等键，默认auto_publish=false。轮询Job，failed处理具体错误，ready用版本review接口/门户审核归属、必要章节、原文与索引后显式publish；操作见[接入说明](INTEGRATION_GUIDE.md#4-文件导入与审核发布)。
3. 更新时传document_id；发布前旧published仍可见。未成功入索引的任务不能切发布指针。
4. 下载原件用授权接口，不直接暴露存储目录。删除首先使PG不可见，OS通过outbox清理；原件与备份的物理保留另按运行方策略处理。
5. reprocess/restore的幂等重试保持原payload/key；未知结果先查询Job。不要反复生成新key创建重复任务。
6. 当前处理修订为evidence-v2.4，增加CPU规则、事实级拆分、选择式审核与发布门槛。已发布事实不会自动改写；审核提示requires_reprocess时先按当前结构重解析，再配置新源单元并审核。API/Worker与门户须一起更新；第三方严格DTO先同步当前契约。数据库仍以完整schema全新初始化，不增加结构升级链。

## 5. 备份与恢复

备份包括PG（正文、版本、ACL、模型配置、任务、关系）、原件volume、模型加密密钥、API Key pepper和部署配置；OS可重建，不替代权威备份。备份包含私有内容，需加密与访问控制。

灾难恢复须使用同一schema及应用版本的PG、原件和密钥备份，在隔离网络核对发布与权限；该流程不兼容任意旧数据库。使用generation重建投影，确认ready后再开放业务。不将旧OS快照直接连到较新PG并视为已一致。

## 6. 诊断

按trace_id检查HTTP状态、retrieval_status、degraded_reasons、executed/skipped stages、backend_calls、timings和字节。Worker记录parse/embedding/index/ready_or_publish阶段耗时及任务失败；不需要收集完整原文或全量候选作常规诊断。

- INDEX_UPGRADE_REQUIRED：先重建V2 projection，不调整调用方分数阈值掩盖。
- SEARCH_CONFIGURATION_CHANGED：模型/索引在请求中变更，有界重试新请求；不得复用旧向量。
- RELEVANCE_PROFILE_INVALID：校准文件不可读、损坏、超限或缺少有效校准记录，修复配置或明确取消配置后重试；不要将409转换成无答案。
- SEARCH_CAPACITY_EXCEEDED：检查每进程容量、调用方并发及模型延迟；避免无限排队重试。
- DATABASE/SEARCH_BACKEND_UNAVAILABLE：检查连接、磁盘、OS只读block和健康，再重试；不把503转成无答案。
- degraded：看实际失败或省略原因；未配Reranker正常跳过，不应因此降级。
- keyword/vector_unavailable：也可能是OS返回HTTP 200但超时、提前终止或部分分片失败，不能只看HTTP状态判断召回完整。

OpenSearch磁盘水位、索引只读block或服务不健康时先修复资源，重跑失败的幂等任务并验证PG发布状态，不手工发布半成品。不得清理仍被PG可见指针引用的数据。性能测量流程见[验收方法](ACCEPTANCE.md)。

## 7. 相关性校准配置

adaptive可以在未配置校准时运行，diagnostics.selection.calibration_status为not_configured。要启用评分拒收，先冻结文档、授权、模型配置revision及active generation，在代表目标语言和查询类型的独立样本上人工标注相关/不相关。用include_diagnostics=true取得真实vector、rerank及adaptive lexical分项；不要用RRF名次或最终答案当标签。只从已返回命中采样会有选择偏差，须覆盖候选边界、无答案和困难负例，再独立验收。

校准输入为JSONL，每行一个人工标注评分；下面仅展示格式，实际值应从冻结运行复制，不能直接用于生产：

```json
{"sample_id":"query-001-hit-001","family":"technical-power","split":"calibration","channel":"vector","score":0.81,"relevant":true,"binding":{"embedding_model":"copy-actual-model","reranker_model":null,"config_revision":1,"active_generation":"copy-actual-generation","lexical_revision":"scope-lexical-v2.3","retrieval_path":"hybrid"}}
```

channel为vector、rerank或lexical；lexical是0—1的字面覆盖比例，其他分数采用实际服务的“越大越相关”方向。所有记录须同一binding；模型名、可空reranker名、revision和generation从授权的模型管理配置核对，空字符串和null须与实际一致。sample_id与channel组合唯一。calibration与test按family隔离，不能让同一产品/问题家族同时参与拟合和测试。

```sh
.venv/bin/python scripts/calibrate-retrieval.py \
  --records /secure/labelled-scores.jsonl \
  --output deploy/relevance/profile.json \
  --max-false-positive-rate 0.05
```

工具不调用模型/数据库；仅用calibration行在有限观察阈值中选择满足样本内误收上限且召回最高的阈值，拒绝无可用阈值、混合绑定、泄漏家族和不足标签。输出dataset hash、经验误收/召回和profile hash；test行只检查隔离，不参与拟合，效果需独立检索评测确认。每通道至少5正例、5负例及2家族只是工具下限。CLI不覆盖已有输出文件，应给新文件名，避免无意替换已发布配置。

生产Compose只读挂载deploy/relevance到API的/etc/cuekb/relevance；确保容器UID 10001能读取文件，在.env.production设置`CUEKB_V2_RELEVANCE_PROFILE_PATH=/etc/cuekb/relevance/profile.json`后重新创建API服务。本地运行可设置绝对路径。校准JSON已加入gitignore，不含原文、query或凭据，但仍按运行配置保管。初次部署留空即可，不提供默认通用阈值。

请求绑定精确匹配时状态applied；rerank阈值优先，否则向量/字面覆盖已校准信号按OR接受，无适用信号的候选保持未评估。模型、索引、配置或路径不匹配时状态binding_mismatch，门控不应用，继续有界检索且充分性仍unassessed；监控此状态，不把它当成低相关性已过滤。损坏/超限文件报配置错误，不悄悄当成未配置。换generation或模型后重新录制/校准/验收，不只手工改绑定掩盖变化。

## 8. 规则审核版本的部署

本轮包含API/Worker、门户与db/schema.sql变化，需要重建API/Worker镜像；仅拉取代码或重启旧镜像不会应用更新。数据库仍只支持完整schema全新初始化；不要把新应用直接连接旧结构，也不要删除生产卷重建。保留原环境、原件与备份，在独立新库初始化、核对目录/权限并重新导入审核，业务验收后切换。旧环境保留用于回退，数据迁移方式须单独核实。

解析和归属改变须reprocess原件、审核新版本再publish，generation只重建已有PG事实。旧OpenSearch投影触发INDEX_UPGRADE_REQUIRED时，在门户管理模型配置/generation，用匹配模型与实际维度创建重建任务，等待ready并切换；保留PG和原件，不手改_meta。/v2/ready现在返回明确503而非未捕获500；API容器仍能提供管理入口，检查Worker及generation任务进度。

auto_publish不再绕过审核。有阻塞时Job可succeeded且stage=needs_review；这表示待确认，不是模型或解析失败。只有已确认的通用unknown才允许发布，不应把全篇强制绑定某产品来清零unknown。解析超时可检查原件/CPU负载并调整有界worker_parse_timeout_seconds，不能无限重试。

PDF/OCR的Docling处理可能需要预先可用的解析/OCR模型资源；CPU运行不等于完全没有模型文件。应在受控部署环境预置并验证缓存/下载网络。模型资源不可用时先解决缓存或下载网络；TXT/DOCX通过不能替代PDF端到端验收。
