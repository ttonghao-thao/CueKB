# CueKB · 线索知识库

CueKB接收PDF、DOCX、Markdown和TXT语料，向客服或其他第三方系统返回可定位的原文证据。普通检索不生成答案。

## 当前编码状态

- **已完成**：M0工程与版本基线、M1文档与关键词检索、M2混合检索、首期管理门户。
- **待开始**：M3轻量关系与上下文、M4质量与运行能力。
- **待确认**：生成式回答，以及OIDC、对象存储、多`scope`和高可用拓扑。

生产环境运行和性能测试单独记录，不改变上述编码状态。完整清单见[任务板](TASK_BOARD.md)。

## 阅读入口

| 读者 | 文档 |
| --- | --- |
| 第三方接入人员 | [语料导入与检索API](docs/API_GUIDE.md) |
| 开发人员 | [架构、组件职责与数据流](docs/SYSTEM_DESIGN.md#2-内部开发视图架构组件与数据流) |
| 项目负责人 | [已完成、待开始与待确认清单](TASK_BOARD.md) |

## 生产组件

单机Compose部署包含PostgreSQL、OpenSearch、迁移任务、API、入库Worker和本地BGE模型服务。PostgreSQL保存权威内容/权限/发布版本；OpenSearch保存BM25和向量索引；Embedding计算文档与查询向量。重排默认关闭，只有配置`CUEKB_RERANKER_SERVICE_URL`后才按查询路径和剩余时间选择执行。原文件、数据库、索引和模型缓存使用独立命名卷。

生产模式有以下强制门禁：API Key密钥材料不能为空；Embedding模型必须固定到具体提交；配置重排地址时也必须固定reranker revision；Python依赖由`uv.lock`冻结；迁移成功后API/Worker才启动；API不会回退到内存后端；PG和OpenSearch不暴露宿主机端口。API只绑定`127.0.0.1:8080`，应由同机TLS反向代理对外发布。

### 部署

需要Docker Engine及Compose v2。模型首次启动需要下载约数GB权重，并需要能够承载BGE-M3和reranker的CPU/GPU及内存；具体容量必须在目标主机实测。

```bash
cp deploy/production.env.example .env.production
# 替换数据库密码、API Key pepper和bootstrap key；按需配置重排地址
chmod 600 .env.production
./scripts/deploy-production.sh .env.production
docker compose --env-file .env.production logs -f api worker model
```

部署脚本校验Compose、构建镜像、启动服务，等待固定修订模型加载和Worker运行，再用bootstrap key检查`/v1/ready`。停止服务但保留数据：

```bash
docker compose --env-file .env.production down
```

不要在没有备份确认的情况下增加`--volumes`。升级前备份PostgreSQL和原文件卷；OpenSearch是可重建投影。模型修订变化时必须使用新索引generation并重建全部向量，不能直接复用旧索引。

### 生产验收

只在专用验收实例执行以下脚本；它会创建并删除自己的测试文档、授权和API Key：

```bash
set -a
. ./.env.production
set +a
.venv/bin/python scripts/acceptance-production.py \
  --bootstrap-key "$CUEKB_BOOTSTRAP_API_KEY"
```

验收覆盖API Key、ACL、幂等上传、Worker处理、原文下载、exact跳过Embedding和重排、hybrid执行Embedding、默认跳过或按配置执行重排、版本切换、撤权和删除。实际执行记录独立维护在[任务板的验证活动](TASK_BOARD.md#验证活动)，不改变功能编码状态。

## 本地开发

本地默认使用进程内存适配器，便于快速运行测试；重启会清空数据，也不执行API鉴权、PDF/DOCX解析或真实向量检索。它不能代表生产行为。

```bash
/opt/homebrew/opt/python@3.12/bin/python3.12 -m venv .venv
.venv/bin/pip install -e '.[dev]'
.venv/bin/pytest -q
.venv/bin/uvicorn cuekb.main:app --reload --host 127.0.0.1 --port 8080
```

健康入口：`GET /v1/health`只检查进程存活；`GET /v1/ready`在生产模式检查PostgreSQL和OpenSearch。交互契约位于`http://127.0.0.1:8080/docs`。

管理门户位于`http://127.0.0.1:8080/portal/`，根路径会自动跳转到该页面。输入API Key后可选择有权访问的知识库，完成文档批量导入、任务进度查看、新版本导入与发布、原文下载、删除和检索验证。API Key仅保存在当前浏览器标签页的`sessionStorage`；关闭标签页后需要重新输入。

项目目标和约束见[PROJECT_CORE.md](PROJECT_CORE.md)，决策见[DECISIONS.md](DECISIONS.md)，协作约定见[AGENTS.md](AGENTS.md)。
