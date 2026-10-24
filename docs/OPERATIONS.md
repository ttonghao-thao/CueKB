# 部署与操作手册

更新：2026-10-24。下列是全新部署的运行步骤，不是本轮已部署的证据。真实环境执行记录在[任务板](../TASK_BOARD.md)。

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

检查`GET /v1/health`、`GET /v1/ready`与`/portal/`。生产业务API使用Bearer Key；健康和ready公开但不返回凭据。创建业务Key并授予目标KB read/write/admin，第三方查询不要使用bootstrap。

## 3. 空库初始化与搜索投影

`db/schema.sql`是完整结构的唯一来源，包含内容、权限、模型配置、关系、generation和V2证据字段及约束。Compose将它只读挂载到PostgreSQL官方初始化目录；数据库首次使用空数据目录时自动执行，无需手工数据库指令。该文件作为一个事务执行，初始化失败须先排查日志，不能将缺表数据库作为正常实例使用。

全新部署必须使用新的空PG数据卷。已有数据目录不会重新执行初始化文件，不属于本次部署输入；需要与旧实例隔离时可为新部署设置独立`COMPOSE_PROJECT_NAME`并处理宿主端口冲突。部署脚本不清理旧容器或数据卷，也不修改已有库结构。保留仓库中的`db/schema.sql`，缺文件时Compose挂载应直接失败；仅重建应用镜像不能改变已有数据库结构。

在门户配置Embedding后，Worker为新文档创建V2搜索投影。此后需要更换Embedding、维度或输入模板时，仍由系统管理员通过`POST /v1/index-generations`创建重建任务，省略`chunking_version`，等待ready后activate。维护锁期间内容写入409，查询继续；这是搜索投影重建，不改变数据库结构。

需要新的解析/归属时，对文档单独执行V2 reprocess，检查新版本再publish；generation不会自动重切块。想恢复旧事实则用restore生成全新块ID、等待ready后发布。generation rollback只重建当前事实的旧模型投影。

## 4. 文档日常操作

1. 上传前整理清晰章节/表头；多产品映射用scope_bindings，不能把全篇metadata标签当成局部证据。
2. `POST /v2/documents`带幂等键，默认auto_publish=false。轮询Job，failed处理具体错误，ready检查归属、原文与索引后显式publish。
3. 更新时传document_id；发布前旧published仍可见。未成功入索引的任务不能切发布指针。
4. 下载原件用授权接口，不直接暴露存储目录。删除首先使PG不可见，OS通过outbox清理；原件与备份的物理保留另按运行方策略处理。
5. reprocess/restore的幂等重试保持原payload/key；未知结果先查询Job。不要反复生成新key创建重复任务。

## 5. 备份与恢复

备份包括PG（正文、版本、ACL、模型配置、任务、关系）、原件volume、模型加密密钥、API Key pepper和部署配置；OS可重建，不替代权威备份。备份包含私有内容，需加密与访问控制。

灾难恢复须使用同一schema及应用版本的PG、原件和密钥备份，在隔离网络核对发布与权限；该流程不兼容任意旧数据库。使用generation重建投影，确认ready后再开放业务。不将旧OS快照直接连到较新PG并视为已一致。

## 6. 诊断

按trace_id检查HTTP状态、retrieval_status、degraded_reasons、executed/skipped stages、backend_calls、timings和字节。Worker记录parse/embedding/index/ready_or_publish阶段耗时及任务失败；不需要收集完整原文或全量候选作常规诊断。

- INDEX_UPGRADE_REQUIRED：先重建V2 projection，不调整调用方分数阈值掩盖。
- SEARCH_CONFIGURATION_CHANGED：模型/索引在请求中变更，有界重试新请求；不得复用旧向量。
- SEARCH_CAPACITY_EXCEEDED：检查每进程容量、调用方并发及模型延迟；避免无限排队重试。
- DATABASE/SEARCH_BACKEND_UNAVAILABLE：检查连接、磁盘、OS只读block和健康，再重试；不把503转成无答案。
- degraded：看实际失败或省略原因；未配Reranker正常跳过，不应因此降级。

OpenSearch磁盘水位、索引只读block或服务不健康时先修复资源，重跑失败的幂等任务并验证PG发布状态，不手工发布半成品。不得清理仍被PG可见指针引用的数据。性能测量流程见[验收方法](ACCEPTANCE.md)。
