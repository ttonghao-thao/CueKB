# 已批准的导入审核设计

用户已于2026-10-26批准编码及推送。完整批准稿保存在Git提交`230a284`；本文作为原设计链接的导航入口。

实施后的规则与边界分别维护在主文档，避免两套设计说明：

- [当前架构与审核一致性](SYSTEM_DESIGN.md#32-规则导入与选择式审核)
- [V2审核接口](API_GUIDE.md#51-选择式审核接口)
- [导入与审核操作](INTEGRATION_GUIDE.md#4-文件导入与审核发布)
- [运行约束](SYSTEM_CONSTRAINTS.md)与[部署操作](OPERATIONS.md)
- [验收方法](ACCEPTANCE.md#43-用户monkey月饼语料复验)及[本轮实际结果](../TASK_BOARD.md)

审核表示归属及结构已经确认；不表示原文已被证明真实。CPU规则无需LLM/GPU，检索Embedding仍使用配置的模型。
