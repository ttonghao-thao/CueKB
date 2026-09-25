# 文档导航与按需加载

更新：2026-09-22。默认入口仅AGENTS、PROJECT_CORE、TASK_BOARD；这里按任务指向专题，不要求依次通读。

## 文档职责

| 文件 | 唯一维护的内容 | 什么时候读 |
| --- | --- | --- |
| [PROJECT_CORE](../PROJECT_CORE.md) | 产品范围和稳定边界 | 首次进入项目 |
| [TASK_BOARD](../TASK_BOARD.md) | 当前进度、验证、阻塞、下一步 | 开始/结束工作 |
| [DECISIONS](../DECISIONS.md) | 已接受的选择及原因 | 涉及取舍或变更边界，按D编号定位 |
| [SYSTEM_DESIGN](SYSTEM_DESIGN.md) | 当前组件、调用链、权威数据及实现限制 | 架构或跨模块修改 |
| [API_GUIDE](API_GUIDE.md) | 对外当前契约与示例 | 接入/接口修改，配合schemas/routes |
| [MODEL_INTEGRATION](MODEL_INTEGRATION.md) | 外部模型调用契约与生命周期 | 模型适配或配置问题 |
| [OPERATIONS](OPERATIONS.md) | 构建/部署/升级/8085访问/验收脚本 | 目标环境操作 |
| [ACCEPTANCE](ACCEPTANCE.md) | 质量、负载、恢复方法与待确认门槛 | 设计/执行验收 |
| [M4](plans/M4.md) | 六个待完成工作包及Q01—Q04优化 | 后续实施设计 |
| [EXTENSIONS](plans/EXTENSIONS.md) | C02—C09建议、待决参数、依赖与验收 | 确认候选能力 |

## 快速定位

- 检索/权限：SYSTEM_DESIGN §2、§4 → retrieval/knowledge/postgres → 对应tests；接口需要时再读API_GUIDE“检索证据”。
- 导入/解析/发布：SYSTEM_DESIGN §3 → worker/parsing/storage → API_GUIDE“导入语料”；OCR优化才读M4 Q02。
- 模型换型/索引：SYSTEM_DESIGN §5 → MODEL_INTEGRATION → OPERATIONS“M3升级与索引重建”。
- 超时/模型争用：M4 §3—4 → 对应在线I/O及Worker代码；不要读取全部候选扩展。
- 部署错误：OPERATIONS对应步骤 → Dockerfile/Compose/脚本与实际日志；先诊断再修改。
- 状态追溯：先TASK_BOARD验证摘要，只有需要原始历史证据时才读下方指定归档。

先用`rg -n '^##' 文档路径`找标题，再用`sed -n '起行,止行p' 文档路径`读取章节。按需扩读依赖，不用`cat docs/*.md`或递归加载归档。跨模块任务必须补齐相关边界，不能为了缩短上下文跳过必要源码验证。

## 历史与维护

[整理前快照索引](archive/2026-09-22-before-reorganization/INDEX.md)保存2026-09-22整理前7份原文及SHA-256，包括既有未提交修改。历史设计含未实现设想和旧路径，不能用于判断当前能力；旧的真实PG测试与受控浏览器验证记录在该快照TASK_BOARD中。

更新时只改事实的主文档，其他文档用链接；实现后将必要方案结论归入当前架构/接口，任务板更新状态，保留历史理由。任务板只保留最近检查点，详细旧记录按日期归档并保留入口。移动文件后检查本地链接、锚点、代码路径和旧引用；归档保留原文，不批量重写历史链接。

当前文档内容比简单删减更完整；减少的是每次默认加载量，不承诺固定token节省比例。字节/行数仅作可复核代理，实际token取决于模型和读取范围。
