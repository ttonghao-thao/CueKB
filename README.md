# CueKB

CueKB是独立的多语言原文证据检索系统，向外部应用返回有来源、版本、归属及排序信息的证据，不生成最终答案。

所有CueKB接口统一使用`/v2`，包括检索、导入、知识库、Key、Job、发布与模型管理。接口、处理链与测试均采用V2。

## 使用入口

- 产品范围：[项目核心](PROJECT_CORE.md)。
- 当前架构与设计：[系统设计](docs/SYSTEM_DESIGN.md)；历史理由在[架构决策](DECISIONS.md)。
- 第三方集成：[接口规范](docs/API_GUIDE.md)、[接入说明](docs/INTEGRATION_GUIDE.md)、[OpenAPI](docs/contracts/openapi.json)。
- 运行维护：[操作手册](docs/OPERATIONS.md)、[模型协议](docs/MODEL_INTEGRATION.md)、[系统约束](docs/SYSTEM_CONSTRAINTS.md)。
- 验证方法：[验收与评测](docs/ACCEPTANCE.md)；实际结果、需求复核及未验收项只在[任务板](TASK_BOARD.md)维护。
- 完整索引：[文档导航](docs/README.md)。独立离线关联实验见[实验说明](experiments/associations/README.md)，未接入生产检索。

## 创建知识库与第三方配置

知识库名称仅供展示，第三方检索使用创建结果的UUID与已获read授权的业务Key。Portal、后台命令、Key授权与交接清单统一见[运行方配置](docs/INTEGRATION_GUIDE.md#6-创建知识库与第三方配置)。

## 本地开发

使用Python 3.11以上、标准venv/pip；Node.js用于门户渲染回归：

```sh
python3 -m venv .venv
.venv/bin/pip install -c requirements.lock -e '.[api,dev]'
CUEKB_BACKEND=memory CUEKB_ENV=development .venv/bin/uvicorn cuekb.main:app --reload --host 127.0.0.1 --port 8080
```

默认memory后端仅供开发，不鉴权、重启丢数据，没有生产向量服务。`/portal/`提供管理界面，`/docs`提供交互接口文档。生产依赖PostgreSQL、OpenSearch、Worker及外部Embedding，按[操作手册](docs/OPERATIONS.md)使用空数据库全新部署。

验证命令与各层前提统一见[验收方法](docs/ACCEPTANCE.md#1-v2完整验证)。契约变更时用`make contracts`重新导出，再用`make check-contracts`校验；不以重新导出掩盖未解释的契约变化。
