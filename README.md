# CueKB · 线索知识库

从多个线索，快速找到正确证据。

英文名称：CueKB（Cue + Knowledge Base）；代码仓库与目录名：`cuekb`；中文名称：线索知识库。名称为项目工作名，未进行商标或包名唯一性核验。

## 当前状态

**项目版本：v0.1｜状态：IMPLEMENTATION_STARTED｜日期：2026-09-09**

设计已获用户确认并开始编码。当前提供开发用内存适配器，可运行最小API纵切；PostgreSQL、OpenSearch、Docling、向量与重排的生产适配器仍按任务板实施。开发实现会明确报告降级，不把关键词结果标成完整混合检索。

核心目标：快速、准确检索出适用范围正确、上下文完整且可追溯的证据。生产客服与个人知识库共用核心技术栈，独立部署、独立数据。

## 阅读顺序

| 文件 | 用途 |
| --- | --- |
| [PROJECT_CORE.md](PROJECT_CORE.md) | 项目目标、范围、技术基线和约束 |
| [系统设计方案](docs/SYSTEM_DESIGN.md) | 架构、数据、入库、检索、接口、部署与验收 |
| [DECISIONS.md](DECISIONS.md) | 架构决策与尚待确认的假设 |
| [TASK_BOARD.md](TASK_BOARD.md) | 确认后按顺序执行的开发计划 |
| [AGENTS.md](AGENTS.md) | 后续编码智能体的工作约定 |

## 本期设计概要

- FastAPI 提供检索和文档管理接口；独立Worker执行入库。
- PostgreSQL保存权威内容、版本、权限、关系与任务；OpenSearch保存可重建的关键词和向量索引。
- 普通查询采用关键词与向量并行召回、RRF融合、有限候选重排。
- 精确查询走短路径，关联查询按需进行一跳扩展。
- BGE-M3与bge-reranker-v2-m3作为初始质量基线，性能和效果需评测。
- 原文定位、版本正确性和权限贯穿整个流程；大模型生成回答不属于检索的必经路径。
- 首期交付API与评测闭环；轻量Web门户列为后续阶段，避免在检索质量未验证前扩大范围。

## 当前运行方式

```bash
cd /Users/snowking/Documents/CueKB
python3 -m venv .venv
.venv/bin/pip install -e '.[dev]'
.venv/bin/pytest
.venv/bin/uvicorn cuekb.main:app --reload --host 127.0.0.1 --port 8080
```

访问`http://127.0.0.1:8080/docs`查看OpenAPI。当前`POST /v1/documents/text`用于开发验证；正式文件上传、后台Worker与持久化将在M1完成。

项目已创建在用户指定目录，并发布到 GitHub：[`ttonghao-thao/CueKB`](https://github.com/ttonghao-thao/CueKB)。本项目不包含原始业务资料、模型权重或凭据。
