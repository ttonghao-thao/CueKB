# 文档导航

当前规范更新于2026-10-24。源码、初始化schema和生成契约共同描述实现；测试结论以[任务板](../TASK_BOARD.md)为准。

| 阅读目的 | 主文档 |
| --- | --- |
| 产品范围与不变边界 | [项目核心](../PROJECT_CORE.md) |
| 当前工作、验证与未验收项 | [任务板](../TASK_BOARD.md) |
| 第三方路径、字段、状态、权限与兼容 | [接口规范](API_GUIDE.md) |
| curl/Python、证据消费、上传发布和客户端升级 | [接入说明](INTEGRATION_GUIDE.md) |
| 归属、充分性、负载、超时与系统限制 | [系统约束](SYSTEM_CONSTRAINTS.md) |
| 当前模块与数据流 | [系统设计](SYSTEM_DESIGN.md) |
| 全新部署、备份、索引与排错 | [操作手册](OPERATIONS.md) |
| Embedding和可选Reranker协议 | [模型接入](MODEL_INTEGRATION.md) |
| 质量、更新一致性和10并发测量方法 | [验收方法](ACCEPTANCE.md) |
| 自动关联独立离线实验 | [实验说明](../experiments/associations/README.md)、[后续研究方案](plans/AUTOMATIC_ASSOCIATION.md) |
| 已接受的架构决策 | [DECISIONS](../DECISIONS.md) |
| 机器可读契约 | [OpenAPI](contracts/openapi.json)、[V2请求Schema](contracts/search-v2-request.schema.json)、[V2响应Schema](contracts/search-v2-response.schema.json) |
| 可校验的合成示例 | [请求](examples/search-v2-request.json)、[响应](examples/search-v2-response.json) |

本轮删除了已被实现替代的计划与旧归档副本，保留远端独立离线关联实验及其研究方案，避免其字段、能力和进度与当前规范冲突；历史变更可用Git追溯。每项事实在上表主文档维护，不复制为另一套接口或验收结论。
