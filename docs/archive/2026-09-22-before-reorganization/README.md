# CueKB · 线索知识库

CueKB接收PDF、DOCX、Markdown和TXT语料，向客服或其他第三方系统返回可定位的原文证据。普通检索不生成答案。

## 当前编码状态

- **已完成**：M0工程与版本基线、M1文档与关键词检索、M2混合检索、M3关系与上下文、管理门户。
- **待开始**：M4质量与运行能力。
- **待确认**：OIDC、对象存储、多`scope`和高可用拓扑。
- **产品边界**：CueKB专注快速、准确、可追溯的检索；外部系统自主决定是否将结果交给自己的LLM，并负责模型集成与回答生成。CueKB不提供内置生成式回答或门户问答。

生产环境运行和性能测试单独记录，不改变上述编码状态。完整清单见[任务板](TASK_BOARD.md)。

## 阅读入口

| 读者 | 文档 |
| --- | --- |
| 第三方接入人员 | [语料导入与检索API](docs/API_GUIDE.md) |
| 开发人员 | [架构、组件职责与数据流](docs/SYSTEM_DESIGN.md#2-内部开发视图架构组件与数据流) |
| 项目负责人 | [已完成、待开始与待确认清单](TASK_BOARD.md) |

## 生产组件

单机Compose部署包含PostgreSQL、OpenSearch、迁移任务、API和入库Worker。PostgreSQL保存权威内容/权限/发布版本及模型服务配置；OpenSearch保存BM25和向量索引；Embedding与重排均通过外部 OpenAI-compatible HTTP API调用，模型部署、权重缓存、容量和可用性不属于本项目。启动后由系统管理员在门户填写Embedding的`base_url`、`api_key`、`model`；重排默认关闭，可在同一页面配置。原文件、数据库和索引使用独立命名卷。

生产模式有以下强制门禁：API Key密钥材料和`CUEKB_MODEL_CONFIG_KEY`不能为空；模型API Key由该密钥通过PostgreSQL `pgcrypto`加密保存，密钥需与数据库备份一同安全保管；Python依赖由`uv.lock`冻结；迁移成功后API/Worker才启动；API不会回退到内存后端；PG和OpenSearch不暴露宿主机端口。未配置Embedding时API/Worker仍能启动，`/v1/ready`仍检查数据库与OpenSearch，导入和检索返回`409 embedding_service_not_configured`。生产Compose将容器内8080映射到宿主机所有IPv4接口的`8085`端口（`0.0.0.0:8085:8080`），由API直接提供门户和HTTP接口，不需要Nginx或其他反向代理，也不使用80/443端口。

### 部署

需要Docker Engine及Compose v2。外部模型服务需要自行承载Embedding与Reranker的权重、CPU/GPU、网络和容量；具体容量必须在目标环境实测。

```bash
cp deploy/production.env.example .env.production
# 替换数据库密码、OpenSearch初始管理员密码、API Key pepper、bootstrap key和模型配置加密密钥
chmod 600 .env.production
docker build --target api -t cuekb-api:latest .
docker build --target worker -t cuekb-worker:latest .
# Worker应显示CPU版PyTorch且不包含CUDA运行时
docker run --rm --entrypoint python cuekb-worker:latest -c \
  "import torch; print(torch.__version__, torch.version.cuda); assert torch.version.cuda is None"
./scripts/deploy-production.sh .env.production
docker compose --env-file .env.production logs -f api worker
```

构建镜像是独立的手动步骤；Compose中的API、Worker与迁移任务仅使用已存在的本地镜像，不自动构建或拉取应用镜像。OpenSearch 3即使关闭安全插件仍要求启动时提供`OPENSEARCH_INITIAL_ADMIN_PASSWORD`，项目从`.env.production`中的`CUEKB_OPENSEARCH_INITIAL_ADMIN_PASSWORD`传入；请使用随机强密码，不能留示例值。Dockerfile按角色安装依赖：API镜像不包含Docling，Worker镜像不包含FastAPI、Uvicorn和Alembic；Docling使用CPU版PyTorch，避免在不运行本地GPU模型的Worker中安装CUDA运行库。构建缓存只保留在BuildKit缓存中，不进入最终runtime镜像。如使用自定义标签，同时修改`.env.production`中的`CUEKB_API_IMAGE`和`CUEKB_WORKER_IMAGE`。部署脚本会等待Worker运行、API Docker healthcheck变为`healthy`，再用bootstrap key检查`/v1/ready`；API healthcheck失败或120秒内未就绪时会输出API最近日志并退出。启动后用bootstrap API Key打开`/portal/`的“模型配置”页，填写Embedding服务地址、API Key和Model；Reranker可选。配置保存后API与Worker会读取新配置，无需重启。模型服务的网络和健康由运行方单独检查。停止服务但保留数据：

```bash
docker compose --env-file .env.production down
```

不要在没有备份确认的情况下增加`--volumes`。升级前备份PostgreSQL、原文件卷及`CUEKB_MODEL_CONFIG_KEY`；OpenSearch是可重建投影。Embedding model变化时必须使用新索引generation并重建全部向量，配置接口会拒绝与现有索引不匹配的Model，不能直接复用旧索引；使用门户“模型配置”下的generation重建、验证、切换和回退流程；当前支持已注册的`structured-v1`分块版本，不接受未知分块算法。旧版本环境变量`CUEKB_EMBEDDING_*`和`CUEKB_RERANKER_*`不再读取；升级后需要在门户重新配置。

### 云主机访问地址（直接使用8085端口）

当前公网IP为`122.51.233.77`，域名为`th.ppy123.xyz`。将域名的DNS A记录指向`122.51.233.77`，并在云主机安全组和系统防火墙中允许所需客户端访问TCP `8085`。公网IP通常由云平台映射到主机，不需要将它写入容器监听地址。私网访问要求客户端与云主机私网可路由。

此前提供的私网地址`10.0.05`不是标准四段IPv4写法，请以云控制台显示的真实私网IP为准；下表中的`<私网IP>`必须替换后使用。若实际为`10.0.0.5`，API基地址就是`http://10.0.0.5:8085`。

| 访问方式 | 管理门户 | API基地址 | 检索接口 |
| --- | --- | --- | --- |
| 私网IP | `http://<私网IP>:8085/portal/` | `http://<私网IP>:8085` | `POST http://<私网IP>:8085/v1/search` |
| 公网IP | `http://122.51.233.77:8085/portal/` | `http://122.51.233.77:8085` | `POST http://122.51.233.77:8085/v1/search` |
| 域名 | `http://th.ppy123.xyz:8085/portal/` | `http://th.ppy123.xyz:8085` | `POST http://th.ppy123.xyz:8085/v1/search` |

三种地址访问同一服务，API路径、Bearer API Key鉴权和知识库权限完全一致；接口契约见[API接入说明](docs/API_GUIDE.md)。门户使用当前访问地址下的`/v1/`接口，无需单独配置前端API地址。生产Compose中的数据库和OpenSearch继续使用内部服务名，不改成公网或私网IP，也不开放它们的宿主机端口。

当前直接提供HTTP，未配置TLS；访问时使用`http://`，API Key和内容在传输层不加密。管理门户使用现有的API Key，Embedding/Reranker仍在门户“模型配置”中维护。

已部署实例更新端口映射后，在项目目录执行以下命令重建API容器即可，无需重新构建应用镜像或重置数据库、密钥：

```bash
docker compose --env-file .env.production up -d --no-build --no-deps api
# 在云主机本机检查依赖就绪情况
curl --fail-with-body http://127.0.0.1:8085/v1/ready
```

从外部电脑验证公网IP和域名入口：

```bash
curl --fail-with-body http://122.51.233.77:8085/v1/ready
curl --fail-with-body http://th.ppy123.xyz:8085/v1/ready
# 鉴权接口示例：先在客户端设置已有的 CUEKB_API_KEY
curl --fail-with-body \
  -H "Authorization: Bearer $CUEKB_API_KEY" \
  http://th.ppy123.xyz:8085/v1/knowledge-bases
```

私网客户端可将上述基地址替换为`http://<私网IP>:8085`；公网IP也可用于相同的鉴权API调用。若本机就绪检查失败，查看`docker compose --env-file .env.production logs --tail 100 api worker`；本机正常而远端超时，则检查安全组、防火墙和网络路由；仅域名失败时检查DNS解析。

### M3升级与索引重建

M3新增`0003_relations_generations`迁移，保留既有文档/块ID，按已保存标题路径补齐历史章节。升级前备份数据库、原文件和稳定的`CUEKB_MODEL_CONFIG_KEY`，先停止旧API/Worker，避免新旧代码混跑；构建新镜像后执行迁移与部署：

```bash
docker compose --env-file .env.production stop api worker
docker build --target api -t cuekb-api:latest .
docker build --target worker -t cuekb-worker:latest .
docker compose --env-file .env.production run --rm migrate
./scripts/deploy-production.sh .env.production
```

知识库管理员通过“实体与关系”维护实体、审核别名、原文提及与支持/反驳证据；检索验证选择`related`可执行有界一跳查询。普通检索可返回带锚点的章节和相邻上下文；新解析的长表格在每段保留原有表头与单位。旧文档的解析缺失不会被迁移凭空修复，需要重新导入修订后的文档。精确字段见[API接入说明](docs/API_GUIDE.md)。

更换Embedding模型或维度时，在“模型配置”填写目标三元组及维度，点击“创建重建任务”，由Worker从PostgreSQL权威块分页重新计算向量、创建独立物理索引并校验。`ready`后点击“切换使用”；数据库在同一事务中切换模型配置和索引指针。当前generation记录`structured-v1`分块版本，重建复用权威块，不重新解析文件或任意改写分块。新增分块算法需另行注册和数据迁移，不能伪装成同一版本。

- 重建/切换持有数据库维护锁：内容及模型配置写入返回`409 index_maintenance_in_progress`，查询继续读旧索引。API Key吊销和知识库授权/撤权仍可立即执行。
- 重建自动重试不超过`CUEKB_WORKER_MAX_ATTEMPTS`，采用有界退避；Worker崩溃后重新构建同一未激活索引。错误记录仅保存代码，不保存外部模型密钥。
- 从重建到切换之间若内容或模型配置变化，切换返回`409 generation_stale_rebuild_required`。取消该任务并重新创建；不会忽略差异切换。
- 回退选择`retired`记录，使用其加密保存的旧模型配置创建新重建任务，再校验和切换。回退不恢复旧内容/旧权限，也不直接启用过期索引；旧模型服务仍须可访问。
- 原有物理索引保留以支持在途查询，不自动删除。失败/取消的索引也保留供排查；容量规划和确认无引用后的清理由运行方负责。

M3提供默认上限：每次最多50个知识库、20个起始实体、一跳30条证据（可配置至100）、关系SQL默认100ms；上下文最多8块/命中、4000字符/命中、12000字符/响应。环境变量分别为`CUEKB_RELATION_CANDIDATES`、`CUEKB_RELATION_TIMEOUT_MS`、`CUEKB_CONTEXT_MAX_CHUNKS`、`CUEKB_CONTEXT_PER_HIT_CHARS`和`CUEKB_CONTEXT_MAX_CHARS`。关系与上下文仍执行知识库权限、删除和发布版本校验，不将得分标记为事实置信度。

### 生产验收

只在专用验收实例执行以下脚本；它会创建并删除自己的测试文档、授权和API Key：

```bash
set -a
. ./.env.production
set +a
.venv/bin/python scripts/acceptance-production.py \
  --bootstrap-key "$CUEKB_BOOTSTRAP_API_KEY"
```

验收前先在门户配置Embedding。验收覆盖API Key、ACL、幂等上传、Worker处理、原文下载、exact跳过Embedding和重排、hybrid执行Embedding、默认跳过或按配置执行重排、版本切换、撤权和删除。实际执行记录独立维护在[任务板的验证活动](TASK_BOARD.md#验证活动)，不改变功能编码状态。

## 本地开发

本地默认使用进程内存适配器，便于快速运行测试；重启会清空数据，也不执行API鉴权、PDF/DOCX解析或真实向量检索。它不能代表生产行为。

```bash
/opt/homebrew/opt/python@3.12/bin/python3.12 -m venv .venv
.venv/bin/pip install -e '.[api,dev]'
.venv/bin/pytest -q
.venv/bin/uvicorn cuekb.main:app --reload --host 127.0.0.1 --port 8080
```

健康入口：`GET /v1/health`只检查进程存活；`GET /v1/ready`在生产模式检查PostgreSQL和OpenSearch。交互契约位于`http://127.0.0.1:8080/docs`。

管理门户位于`http://127.0.0.1:8080/portal/`，根路径会自动跳转到该页面。输入API Key后可选择有权访问的知识库，完成文档批量导入、任务进度查看、新版本导入与发布、原文下载、删除和检索验证。系统管理员额外可配置Embedding和Reranker服务；门户不会回显模型API Key，留空密钥字段表示保留现有值。API Key仅保存在当前浏览器标签页的`sessionStorage`；关闭标签页后需要重新输入。

项目目标和约束见[PROJECT_CORE.md](PROJECT_CORE.md)，决策见[DECISIONS.md](DECISIONS.md)，协作约定见[AGENTS.md](AGENTS.md)。

### M3 PostgreSQL集成测试

```bash
# 指向专用测试数据库；测试创建随机隔离schema，结束后删除该schema。
# 数据库用户需有CREATE SCHEMA权限；首次测试还需安装pgcrypto的权限。
CUEKB_TEST_DATABASE_URL='postgresql+psycopg://user:password@127.0.0.1:5432/cuekb_test' \
  .venv/bin/pytest tests/test_m3_postgres.py
```

未设置该变量时数据库测试明确跳过；普通单元/API测试不调用外部模型。测试不会替代Docker、OpenSearch和真实模型的目标环境验收。
