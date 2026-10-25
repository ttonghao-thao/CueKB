# CueKB 任务板

## 统一V2接口与实现（2026-10-25）

当前项目仅提供V2接口与处理链。所有健康、就绪、知识库、授权、文档、任务、模型、索引generation及检索路径均在/v2，门户、管理命令和部署入口使用同一契约。历史方案由Git追溯，本文只记录当前交付与本轮实际验证。

## 本轮变更

- API：独立闭合的V2请求、命中、响应、上下文及文档DTO；所有管理能力迁入/v2。文件导入统一POST /v2/documents，冻结处理快照并排队；不提供绕过该处理链的文本直写入口。
- 核心：保留V2唯一检索、解析、局部归属、上下文、索引与generation流程；公共查询路由和RRF放入services/ranking.py。domain与数据库默认快照使用相同规范JSON及合法evidence-v2哈希，解析器校验冻结快照的完整性。
- 客户端：门户固定V2，无接口切换或响应兼容回退；CLI、功能验收脚本、部署探针和Compose健康检查统一/v2。外部Embedding/Reranker仍按第三方模型服务的真实协议通信。
- 测试：删除不再对应产品实现的专用用例，将ACL、Worker、发布/删除、关系、generation与PG事务风险覆盖迁到当前契约。make test执行完整集合，make test-v2为同一入口，不设排除筛选。
- 文档：README、范围、现行决策、架构、接口、集成、操作与验收方法统一到当前V2。OpenAPI与请求/响应Schema重新导出；架构/设计、验证方法和实际结果分别以SYSTEM_DESIGN、ACCEPTANCE和本任务板为主位置。

## 实际验证

| 检查 | 本轮结果与条件 |
| --- | --- |
| 完整测试 | **208 passed，0 skipped，0 failed，2条依赖弃用警告**。设置CUEKB_TEST_DATABASE_URL连接本轮隔离PostgreSQL 17.6-alpine，全部16项真实PG测试实际执行；未使用业务库 |
| PG初始化与持久化 | 各数据库fixture加载完整db/schema.sql，验证快照/默认profile、幂等、派生版本/恢复、ACL及关系、Worker/generation、SQL取消及连接回收等既有风险用例 |
| API与DTO审查 | OpenAPI的24个路径全部属于/v2，无重复method/path；V2 DTO字段集合、必填约束和默认值对照无遗失。新增统一命名空间、管理、文件导入及处理默认值验证 |
| 实际HTTP | 独立Uvicorn首次请求直接V2搜索成功；正负温度证据保留；慢依赖约0.103秒取消并返回504；9个facets返回422；10个并发请求的KB、命中、request_id及trace隔离通过。事实与候选为合成夹具，不代表真实模型质量或生产P95 |
| 门户与客户端 | 实际Node执行门户渲染及管理/ranked/adaptive请求构建；CLI与专用实例验收工具的隔离协议回归通过，包含V2上传、详情、发布切换、撤权和吊Key |
| 契约与静态检查 | OpenAPI/Schema/示例freshness、Python编译、JavaScript与部署Shell语法、diff检查通过 |
| 文档 | 本地链接/锚点及Python/JSON/Shell示例语法检查通过；源码、测试、契约及现行说明已完成残留引用检查 |

本轮PG容器仅供隔离验证，使用临时数据目录；检查后关闭清理。默认不设置CUEKB_TEST_DATABASE_URL时，数据库用例仍会明确跳过，不可据此计为通过。

## 当前能力与验收边界

六项准确性优化均有当前实现及规则回归：归属兼容合并/冲突保留、搜索部分失败识别、人工校准门控、整组精确预算与替补、动态新增信息/差异保护、可信词法投影及有界关系排序。实现映射统一见[系统设计](docs/SYSTEM_DESIGN.md#7-六项准确性优化与验证边界)，质量与消融方法见[验收方法](docs/ACCEPTANCE.md)。

原文、多证据、出处、不可变快照、有界执行及最终权威复验保留；evidence_status仍为unassessed。真实OpenSearch召回/故障/generation、Docling复杂PDF/OCR、外部模型与人工标签校准、目标环境10并发质量/延迟和第三方语音端到端尚未验收。本轮真实PG测试通过不代表这些环节已通过。

缓存、跨进程全局配额、通用语义覆盖分类、自动冲突裁决、完整OCR质量保障、文档级ACL与历史版本检索未实现。独立自动关联实验采用当前请求DTO，未接入生产检索。

## 部署与交接

部署需重新构建API/Worker镜像并同步消费端路径，按[操作手册](docs/OPERATIONS.md)使用当前完整schema进行全新初始化。数据恢复和generation仍是既有业务能力，不引入数据库结构升级链。本轮未操作已有数据卷、生产数据或外部模型；本地文件保持原样。提交/推送以Git refs和最终交付回执为准。
