# CueKB · 线索知识库

面向外部系统的多语言证据检索服务：导入PDF、DOCX、Markdown、TXT，返回经过权限和版本校验的原文及出处。生成式回答由外部系统负责。

## 阅读入口

| 任务 | 首选文档 |
| --- | --- |
| 已完成、待完成、验证与下一步 | [任务板](TASK_BOARD.md) |
| 项目边界与协作规则 | [项目核心](PROJECT_CORE.md)、[AGENTS.md](AGENTS.md) |
| 内部架构与源码入口 | [系统设计](docs/SYSTEM_DESIGN.md) |
| 第三方接入 | [API接入说明](docs/API_GUIDE.md) |
| 构建、部署、升级、模型配置、8085访问 | [运行手册](docs/OPERATIONS.md) |
| M4待完成方案与优化顺序 | [M4实施设计](docs/plans/M4.md) |
| 候选能力及待决条件 | [扩展方案](docs/plans/EXTENSIONS.md) |

文档所有权、专题定位及历史查询规则见[文档导航](docs/README.md)。

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

完整阅读路径见[文档导航](docs/README.md)。

### M3 PostgreSQL集成测试

```bash
# 指向专用测试数据库；测试创建随机隔离schema，结束后删除该schema。
# 数据库用户需有CREATE SCHEMA权限；首次测试还需安装pgcrypto的权限。
CUEKB_TEST_DATABASE_URL='postgresql+psycopg://user:password@127.0.0.1:5432/cuekb_test' \
  .venv/bin/pytest tests/test_m3_postgres.py
```

未设置该变量时数据库测试明确跳过；普通单元/API测试不调用外部模型。测试不会替代Docker、OpenSearch和真实模型的目标环境验收。
