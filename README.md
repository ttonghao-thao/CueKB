# CueKB

CueKB是独立的多语言原文证据检索系统，向外部应用提供有来源、版本、归属和排序信息的多条证据。系统不生成最终答案。

生产采用全新部署，PostgreSQL空数据目录首次启动自动加载`db/schema.sql`。

生产架构为PostgreSQL权威存储、OpenSearch BM25/向量投影、外部Embedding与可选Reranker、确定性Worker及API内置管理门户。新增集成使用`POST /v2/search`；现有V1检索契约单独保留。

## 使用入口

- 第三方：[接口规范](docs/API_GUIDE.md)、[接入说明](docs/INTEGRATION_GUIDE.md)、[系统约束](docs/SYSTEM_CONSTRAINTS.md)、[OpenAPI](docs/contracts/openapi.json)。
- 部署维护：[操作手册](docs/OPERATIONS.md)、[模型接入](docs/MODEL_INTEGRATION.md)、[验收方法](docs/ACCEPTANCE.md)。
- 开发协作：[项目核心](PROJECT_CORE.md)、[任务板](TASK_BOARD.md)、[当前架构](docs/SYSTEM_DESIGN.md)、[架构决策](DECISIONS.md)、[文档导航](docs/README.md)。

另有独立的[自动关联离线实验](experiments/associations/README.md)，不接入生产API/Worker，不代表真实检索质量已验收。

## 创建知识库与第三方配置

第三方必须使用**知识库 ID（UUID）**，检索请求字段为 `kb_ids: ["<UUID>"]`。知识库名称 `name` 只用于展示，允许同名，不能代替 ID，也不能作为唯一查找条件。ID 由 CueKB 自动生成；创建接口返回的 `id` 就是检索所用的知识库 ID。

创建知识库需要 **system admin** Key，通常使用部署时设置的 `CUEKB_BOOTSTRAP_API_KEY`。单个知识库的 `admin` 角色不代表系统管理员。创建只需要名称（去除首尾空白后 1–200 字符），描述可选（最多 2000 字符）；无需指定产品、模型或 ID。两种创建入口都调用 `POST /v1/knowledge-bases`，创建后是空库，仍需配置 Embedding、导入并发布文档才能检索证据。

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

`read` 允许检索及读取；`write` 增加导入/发布权限；`admin` 增加该 KB 的管理权限。业务查询通常只授予 `read`，不要向第三方交付 bootstrap Key、数据库凭据或模型密钥。多个 KB 要逐个授权。完整 Key 吊销及撤权接口见[接口规范](docs/API_GUIDE.md#6-公共管理接口目录)。

### 交给第三方的资料

| 信息 | 必需性与用法 |
| --- | --- |
| Base URL | 必需；从第三方服务器可达的 CueKB 根地址，不带 `/v1`、`/v2` 或 `/portal/`；远程生产使用 HTTPS 或受控加密链路 |
| 业务 API Key | 必需；已对目标 KB 授权，放在 `Authorization: Bearer <api_key>`；不要把 Key ID 或 `principal_id` 当密钥 |
| 知识库 UUID 列表 | 必需；放入 `kb_ids`，例如 `["11111111-1111-4111-8111-111111111111"]`；使用实际 ID，名称仅供展示 |
| 接口和契约 | 新接入为 `POST /v2/search`；提供 [OpenAPI](docs/contracts/openapi.json)、[接口规范](docs/API_GUIDE.md)及[接入说明](docs/INTEGRATION_GUIDE.md) |
| 运行预算 | 提供服务端检索时限和建议客户端超时，约定 `top_k` 与消费端证据预算；默认值与上限见[系统约束](docs/SYSTEM_CONSTRAINTS.md) |
| 选择策略与校准 | 建议新客户端显式使用 `selection_mode=adaptive`，升级严格DTO后启用；确认运行方是否提供匹配的相关性校准，未校准不能保证低相关拒收 |
| 知识库名称、描述 | 可选展示信息，不参与权限判断或按名路由 |

调用方变量名由其实现决定；以下以 `CUEKB_BASE_URL`、`CUEKB_API_KEY`、`CUEKB_KB_ID` 为例。这些是调用方配置，不是新增的 CueKB 服务端设置。先用业务 Key 调用 `GET /v1/knowledge-bases` 确认目标 ID 和权限，再检索：

```sh
curl --fail-with-body "$CUEKB_BASE_URL/v1/knowledge-bases" \
  -H "Authorization: Bearer $CUEKB_API_KEY"

curl --fail-with-body "$CUEKB_BASE_URL/v2/search" \
  -H "Authorization: Bearer $CUEKB_API_KEY" \
  -H 'Content-Type: application/json' \
  --data "{\"query\":\"What are the storage conditions?\",\"kb_ids\":[\"$CUEKB_KB_ID\"],\"mode\":\"hybrid\",\"selection_mode\":\"adaptive\",\"top_k\":20,\"max_evidence_bytes\":48000}"
```

200 但 `retrieval_status=not_found` 表示本次未召回证据，可能尚未发布文档；不代表系统错误或整个知识库不存在。401 检查 Key，403 检查 KB 授权，422 检查 UUID/请求字段。检索高分不等于证据充分，第三方应保留归属、条件、出处和 `evidence_status`；消费规则见接入说明。

adaptive按必要证据组、新增信息、去重与预算动态决定条数，top_k只是安全上限。可选query_facets提供关注项，selection_query提供原始选择语境，完整字段与停止原因见[接口规范](docs/API_GUIDE.md#2-v2检索请求)。门户检索验证页可切换动态/排序策略。人工相关性校准、只读配置挂载及新词法投影要求见[操作手册](docs/OPERATIONS.md)；初次全新部署无需旧库转换。

## 本地开发

使用Python 3.11以上与标准venv/pip：

```sh
python3 -m venv .venv
.venv/bin/pip install -c requirements.lock -e '.[api,dev]'
.venv/bin/pytest -q
.venv/bin/uvicorn cuekb.main:app --reload --host 127.0.0.1 --port 8080
```

缺省memory后端仅用于开发和契约测试，没有生产鉴权或真实向量服务。生产运行方式见操作手册。`/portal/`提供管理界面，`/docs`提供交互接口文档。

```sh
make contracts
make check-contracts
```

契约和示例由当前代码生成，不连接数据库或模型。当前已验证与尚未执行的验收见任务板，不能把开发测试数字解释为生产检索质量或延迟。
