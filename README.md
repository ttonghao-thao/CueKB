# CueKB

CueKB是独立的多语言原文证据检索系统，向外部应用提供有来源、版本、归属和排序信息的多条证据。系统不生成最终答案。

生产采用全新部署，PostgreSQL空数据目录首次启动自动加载`db/schema.sql`。

生产架构为PostgreSQL权威存储、OpenSearch BM25/向量投影、外部Embedding与可选Reranker、确定性Worker及API内置管理门户。新增集成使用`POST /v2/search`；现有V1检索契约单独保留。

## 使用入口

- 第三方：[接口规范](docs/API_GUIDE.md)、[接入说明](docs/INTEGRATION_GUIDE.md)、[系统约束](docs/SYSTEM_CONSTRAINTS.md)、[OpenAPI](docs/contracts/openapi.json)。
- 部署维护：[操作手册](docs/OPERATIONS.md)、[模型接入](docs/MODEL_INTEGRATION.md)、[验收方法](docs/ACCEPTANCE.md)。
- 开发协作：[项目核心](PROJECT_CORE.md)、[任务板](TASK_BOARD.md)、[当前架构](docs/SYSTEM_DESIGN.md)、[架构决策](DECISIONS.md)、[文档导航](docs/README.md)。

另有独立的[自动关联离线实验](experiments/associations/README.md)，不接入生产API/Worker，不代表真实检索质量已验收。

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
