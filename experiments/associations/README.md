# C-lite 离线关联实验

实现入口：`python -m cuekb.experiments.associations`。代码位于`src/cuekb/experiments/associations/`，不被API或Worker导入，不安装新依赖、不写生产数据库、不调用外部模型。

这是独立离线关联工具，当前运行约束以下文为准；旧源码基线的研究方案已清理，需要历史时查Git。真实语料、人工标注、真实模型/OpenSearch性能、PG持久化和线上接入仍需分别验证。这里的示例是**虚构产品、手写向量和受控状态**，只能证明命令及算法能运行。生产V2动态选择由[当前系统设计](../../docs/SYSTEM_DESIGN.md#41-动态证据选择)定义，离线实验不自动接入该链路。

## 快速运行

在仓库根目录使用已有开发虚拟环境：

```bash
.venv/bin/python -m cuekb.experiments.associations validate experiments/associations/example
.venv/bin/python -m cuekb.experiments.associations build experiments/associations/example data/associations/build.json
.venv/bin/python -m cuekb.experiments.associations run experiments/associations/example data/associations/build.json data/associations/report.json --dynamic
```

已有输出默认拒绝覆盖，重复运行时显式添加`--overwrite`。文件使用原子写入；失败不会留下半份报告。输出放在被Git忽略的`data/`中。检查报告的`run_manifest`、`quality`、`paired_comparisons`及逐请求`results`，不要只读取百分比。

关闭动态搜索：不加`--dynamic`；关闭关联：`--mode off`；保守自动门控：`--mode auto`；主实验固定`--mode on`。`--split development|calibration|test`选择一个冻结集合，默认test。`--k`指定评分K，不修改输入请求中的top_k；若希望@10，请把所有组的请求top_k也统一设为10。

## 数据契约

| 文件 | 内容 |
| --- | --- |
| `manifest.json` | 数据集来源类别、带时区as_of快照时间、源代码commit/依赖锁hash、模型与编码binding、当前发布指针、删除记录、身份KB读授权、内容修订、实体/审核别名目录、exact路由正则和固定Policy |
| `corpus.jsonl` | 每行`{"chunk": <现有Chunk对象>, "vector": [...]}`；最多3,000块，ID唯一，向量维度/有限数值检查。可保留旧存储版本，但构建及查询只用manifest中的当前发布版本 |
| `queries.jsonl` | id、family、split、category、principal_id、现有SearchRequest、已计算query vector、显式entity→condition context及独立gold；同家族不得跨split |
| `recordings.jsonl` | 真实候选回放时必填。严格记录每次搜索的输入、KB、filters、候选ID/score及captured_limit；缺少调用或数量不足直接失败，无本地回退 |
| `relation_labels.jsonl` | 可选独立关系盲审：claim_id、correct、family、split；包含金标未被抽取的正确claim_id可用于抽取召回统计 |
| `path_labels.jsonl` | 可选独立扩展路径盲审：query_id、path_sha256、correct；路径key由报告的`label_sha256`提供。不要拿生成器的自评分作为标注 |

Pydantic模型与验证器是实际schema：`models.py`、`corpus.py`。示例文件可复制后替换真实内容。空白行允许；超大输入、未知字段、重复ID、非有限向量、模糊别名、错误版本归属、家族split泄漏和未知金标块会拒绝。清单不包含模型Key、数据库密码或任意网络地址；不要在数据文件保存凭据。

`provenance=synthetic_fixture`使用本地BM25参考算法及精确cosine，仅允许≤256块、≤2048维；它不是生产OpenSearch的CJK分析器/HNSW，H组在报告中标记为`local_bm25_cosine_fixture`。真实候选需要`provenance=recorded_candidates`：从已授权、冻结的PG发布/ACL快照及匹配的OpenSearch generation导出正文与向量，再录制候选；本轮工具只消费已有文件，不负责读取线上凭据或自动抓取文档。

实体目录是标识与审核别名输入，不是手工知识关系。最长名称匹配避免把“Bulb A1 Pro”合并为“Bulb A1”；未知实体/复杂句型宁可漏抽取。目录改变、向量binding改变或原文指纹改变时，旧关联不可继续充当有效依据。

离线运行不读取`.env`装配生产Settings；模型binding、路由正则和权威状态来自显式manifest。关系时间默认使用冻结as_of，显式request.relations.at可以覆盖；两者均须带时区，且仍不能绕过当前发布指针。

## 真实候选回放格式

每行只选keyword或vector输入，filters要与输入查询完全一致，kb_ids顺序不影响匹配。示意（UUID仅为格式示例，必须替换）：

```json
{
  "method": "keyword",
  "query": "Bulb A1 Hub G protocol requirements",
  "kb_ids": ["00000000-0000-0000-0000-000000000001"],
  "filters": {"document_ids": [], "product_model": null, "software_version": null},
  "captured_limit": 62,
  "hits": [["00000000-0000-0000-0000-000000000002", 4.2]]
}
```

vector行改为`"method":"vector","vector":[...]`并省略query；向量必须与manifest同维度和模型/编码修订。录制范围包含：每个可见源块search_text的keyword及源vector检索（构建用）；每个query的keyword/query-vector检索（H/H-wide用，至少62候选预算）；若启用dynamic，还包括原query＋初始种子实体名称的至多两次keyword检索。请求不足会输出`missing_or_insufficient_recording`，不会假装语义/混合实验成功。

回放时重新按冻结权威状态过滤ID，因此原始候选中的未发布、删除、超范围内容不会被当作合法证据。冻结快照中的read_grants只是实验输入，不能替代生产PG实时授权；不要把这个模块作为对外HTTP服务部署。HTTP响应延迟、真实吞吐和权限并发安全须在未来PG集成中验证。

## 实现的关联边界

- 每块最多8条导航出边，其中语义最多4条，每目标文档最多2条；同文档上下文不重复物化。语义相似不变为事实。
- 规则支持`requires/depends on`、`supports/does not support`、`is compatible with/is not compatible with`等严格句型；支持对象限协议或功能类别。只有完整识别主体、对象、极性和后缀条件时才标source_verified。
- 支持`only with <明确实体> firmware <版本> [or later]`，版本比较限vendor_numeric_v1；缺少条件或未知格式保持unknown，不激活该断言。其他限制后缀进入unresolved；没有LLM补猜。
- 来源元数据中的software_version/region也必须保留为条件；能匹配明确产品角色时绑定该实体，无法确定条件属于谁时保持unresolved，不当作无条件断言。
- 每块最多10条经验证事实、20条提取候选；同断言最多3个支持集合，每集合最多4块。quote用Unicode字符offset与内容指纹校验。支持集合内AND、集合之间OR；原文或版本改变即失效。
- 最多4个原始种子、一跳、32条预选边、24条新增块、96个证明依赖块。动态发现最多2次搜索，并共享新增候选配额；新目标永远不变成下一轮种子。
- 关联阶段用200ms单调时间预算停止新增工作；这是本地离线阶段门控，**不保证真实网络/SQL取消或生产端到端硬截止**。
- 查询阶段不读gold；只返回原文、锚点、合法路径及有限证据包。一般问题coverage始终unknown、evidence_status始终unassessed，不生成回答或推断传递兼容性。
- exact和mode=off完全跳过关联。最终重新核对权限、发布和全部路径依赖；受控状态变化测试通过fresh_snapshot注入，不能把该测试机制称为生产事务。

## 报告与验证

五组固定为H、H+A、H+B、H+C、H-wide；全部共享输入、top_k、候选预算及关闭的Reranker。H-wide使用62＋62候选作为更多资源的对照。`results`记录原文命中、路径、停止原因和调用/候选计数；`run_manifest`记录数据/artifact/query hash、模块源文件hash、实际代码commit/dirty状态、Python/依赖版本和模式。

计算去重Precision@K、Recall@K、必要文档覆盖、多证据AND/OR完整覆盖、明确反例/无答案误检及按问题家族成对bootstrap区间。不同集合的金标只在评测中使用；少于5个独立家族的区间标`insufficient_families`。缺少独立关系/路径标注时准确率和错误关联率为null，已标部分必须同时报告未标数量和Wilson区间。候选池错误关联率尚未计量，不能用最终命中指标代替。

`replay_cpu_ms`只测本地回放CPU墙钟时间；`http_latency_ms.measured=null`，`production_gates.status=not_evaluated`。示例的任何百分比都不能作为真实产品检索质量、延迟或生产准入证据。关系全文抽取、复杂条件、通用证据槽位、Reranker对照、实时增量任务、生产PG接入及线上API均不在此阶段。

```bash
.venv/bin/pytest tests/test_association_experiments.py
.venv/bin/pytest
```

下一步是准备合法真实语料与盲审，录制H基线及候选，冻结开发/校准/测试集并跑四方案对照；满足设计门槛后再做X06/X07。M4保持独立，可复用本工具的证据单元和报告口径。
