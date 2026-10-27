# 文档导航

当前规范更新于2026-10-26。源码、初始化schema和生成契约共同描述实现；测试结论以[任务板](../TASK_BOARD.md)为准。

| 阅读目的 | 主文档 |
| --- | --- |
| 产品范围与不变边界 | [项目核心](../PROJECT_CORE.md) |
| 当前需求满足度、实际验证与未验收项 | [任务板](../TASK_BOARD.md) |
| 第三方路径、字段、状态与权限 | [接口规范](API_GUIDE.md) |
| curl/Python、证据消费、上传发布和客户端升级 | [接入说明](INTEGRATION_GUIDE.md) |
| 归属、充分性、负载、超时与系统限制 | [系统约束](SYSTEM_CONSTRAINTS.md) |
| 当前架构、设计与六项优化映射 | [系统设计](SYSTEM_DESIGN.md) |
| 企业知识库重构路线（已批准，阶段状态见任务板） | [重构设计](ENTERPRISE_RAG_REDESIGN.md) |
| 空库/保留数据升级、备份、索引与排错 | [操作手册](OPERATIONS.md) |
| Embedding和可选Reranker协议 | [模型接入](MODEL_INTEGRATION.md) |
| V2完整测试、质量、更新一致性和10并发测量方法 | [验收方法](ACCEPTANCE.md) |
| 动态选择、相关性门控和校准操作 | [系统设计](SYSTEM_DESIGN.md#41-动态证据选择)、[操作手册](OPERATIONS.md#7-相关性校准配置) |
| unknown归属修复与必要章节审核 | [设计](SYSTEM_DESIGN.md#31-归属审核与必要章节)、[导入与审核操作](INTEGRATION_GUIDE.md#4-文件导入与审核发布) |
| 规则导入、选择式审核与事实级检索改进 | [当前设计](SYSTEM_DESIGN.md#32-规则导入与选择式审核)、[批准稿入口](IMPORT_REVIEW_DESIGN.md) |
| 自动关联独立离线实验 | [实验说明](../experiments/associations/README.md) |
| 已接受的架构决策 | [DECISIONS](../DECISIONS.md) |
| 机器可读契约 | [OpenAPI](contracts/openapi.json)、[V2请求Schema](contracts/search-v2-request.schema.json)、[V2响应Schema](contracts/search-v2-response.schema.json) |
| 可校验的合成示例 | [请求](examples/search-v2-request.json)、[响应](examples/search-v2-response.json) |

已被实现替代的审查方案和使用旧源码基线的关联研究方案已删除，当前能力与操作以上表主文档为准；独立离线实验保留实际可运行说明。历史设计用Git追溯，不再维护与当前实现冲突的副本。每项事实只在其主文档维护。

全部CueKB接口和处理链统一为V2，测试覆盖现存接口与组件。README只作入口，操作步骤统一在接入/操作文档；设计、方法与执行结果分别维护，历史测试数字及已被替代方案通过Git追溯。
