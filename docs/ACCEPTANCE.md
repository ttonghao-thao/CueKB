# 验收与评测方法

更新：2026-10-25。本文定义如何验证；本轮实际执行结果仅在[任务板](../TASK_BOARD.md)维护。这里不预填预期Recall或P95为已达成。

## 1. 本地与数据库验证

```sh
.venv/bin/pytest -o addopts='' -q
make check
make check-contracts
node --check src/cuekb/portal/app.js
sh -n scripts/deploy-production.sh
```

真实PG测试只指向**专用测试库**，设置`CUEKB_TEST_DATABASE_URL`后运行`tests/test_m3_postgres.py`和`tests/test_v2_postgres.py`；fixture创建独立随机schema并直接加载完整初始化结构，结束后drop该schema，必要时创建public.pgcrypto，不得指向业务库。未设置时跳过，不算通过真实PG验收。

数据库测试与新部署共用完整`db/schema.sql`，重点验证空库建表、不可变快照/幂等、source_version同文档FK、恢复新ID、旧清理不误删新块、发布删除/撤权竞争、pg_sleep超时取消与连接回收。OS重点验证V2映射、实际BM25、k-NN、过滤投影及索引写后校验。HTTP MockTransport仅证明适配协议，不证明真实服务质量。

## 2. 固定验收数据

使用英文通识、两种以上产品及技术手册，固定原件hash、版本/processing_profile、PG内容修订、模型不可变revision、dimension、generation、运行代码和配置。相同权限、相同query/filter/top_k做前后对照，保留冷/热状态。

人工标注每题必要证据单元，允许多个等价chunk；标注产品归属、型号参数、单位/比例、适用/限制条件、否定、例外、重复、冲突及无答案。不把模型输出当唯一gold。文档变化后chunk ID会改变，必须重新绑定标注，不能跨版本直接计算旧gold。

至少包括：单片段；跨块/章节/文档；型号与技术参数；多条件例外；未知/歧义归属；无匹配与低相关；冲突；更新/删除/撤权；必要依赖超预算；未配及已配Reranker。两种月饼等业务样例由获授权数据提供，不能在产品代码硬编码。

## 3. 指标口径

| 指标 | 计算与限制 |
| --- | --- |
| Recall@K | K为最终hit上限；hit与其合法context_parts形成返回证据组，命中gold必要单元数/必要单元总数；等价ID不重复计数 |
| MRR | 第一条命中任意gold单元的hit组名次倒数；有gold但失败/空结果计0 |
| 证据完整率 | 所有必要单元同时返回；gold_plans可声明多个OR替代方案，每个方案unit_ids为AND；context_state=unresolved组不计完整覆盖，不等于系统自动充分性判定 |
| 无答案效果 | 人工无答案集统计not_found正确识别、误有答案、错误/降级；高分不作为充分性标签 |
| 归属与事实保持 | 人工检查产品/版本、表头、数值、单位、比例、否定及限定条件，无误归属或编造 |
| 延迟与稳定性 | 所有请求（含错误）P50/P95/P99、HTTP错误/超时、吞吐、资源占用；同时检查并发质量变化 |
| 负载 | 返回hits精确bytes、正文及context大小、省略原因；语音模型实际注入token另测 |
| 动态选择 | 同预算下实际返回precision、平均条数/字节、Recall与完整率，停止原因及校准状态；不能仅靠多返回内容宣称改进 |

验收阈值应在目标语料和业务风险下确认；当前没有经过实测的通用通过阈值。相关性校准先按独立family划分calibration/test，拟合方法见[操作手册](OPERATIONS.md#7-相关性校准配置)。对比ranked、adaptive无校准及adaptive已校准，并分别跑无Reranker/有Reranker；逐项消融词法字段、预算替补、覆盖选择和门控。单元测试只验证算法规则，未校准不能保证低相关即空结果。冲突证据可以并列返回，系统不自动裁决新旧或真伪。

## 4. 可执行质量与10并发工具

`scripts/evaluate-retrieval.py`只调用V2检索，不写数据。输入JSONL，每行：

```json
{"sample_id":"voltage","request":{"query":"Input voltage","kb_ids":["11111111-1111-4111-8111-111111111111"],"mode":"hybrid","top_k":8},"gold_evidence":[{"unit_id":"voltage-with-condition","chunk_ids":["55555555-5555-4555-8555-555555555555"]}],"forbidden_chunk_ids":[],"expected_status":"ok"}
```

chunk_ids是一个单元的OR等价ID列表；事实、必要条件分别列为AND单元，不能把它们误填为等价项。可选gold_plans形如`[{"unit_ids":["fact","condition"],"document_ids":["来源文档UUID"]},{"unit_ids":["complete-alternative"]}]`，方案之间OR，方案内AND。未提供时所有gold单元均必需。完整率排除unresolved上下文，普通Recall仍统计实际返回ID；不能把一个孤立高分块算成完整证据。

无答案题设置answerable=false、空gold及expected_status=not_found；有答案题设置answerable=true，分别计算误收和空返率。forbidden_chunk_ids可标记过期、错误产品或撤权证据；未标负例需人工检查。gold不能通过同名产品无版本地匹配。precision_returned采用与离线关联实验相同的最大匹配口径，重复等价单元不会多得分；一组含多个必要单元时完整率与precision口径需分别理解。

在安全环境设置`CUEKB_TEST_API_KEY`为专用read Key后：

```sh
.venv/bin/python scripts/evaluate-retrieval.py \
  --url https://cuekb.example.internal \
  --cases /secure/frozen-cases.jsonl \
  --output /tmp/cuekb-evaluation \
  --concurrency 10 --repeat 20 --timeout 10
```

结果summary.json含Recall/MRR/完整率、expected status计数、错误与延迟；requests.jsonl含sample_id、trace、修订、诊断，不输出完整证据或query。工具保留HTTP/transport错误对质量的0分影响，百分位用nearest-rank并包含所有请求。concurrency是闭环最多同时10请求，非10 QPS；延迟从dispatch开始，不含调度前队列，需另做目标到达率/饱和压测才能评估排队效应。少量样本的P99不能当稳定容量结论。

分别跑无Reranker和有Reranker，检查实际executed_stages、ranking_method及backend_calls，不只看配置开关。记录总请求数、运行时长、语料块数、目标主机CPU/GPU/内存、模型服务、网络和超时。固定query不代表每次排序完全确定，外部模型可变修订要注明。

## 5. 端到端独立验收

CueKB验收只证明本次返回的原文证据。voice-service-agent必须独立升级严格DTO和引用映射，保留必要依赖、按预算取证，验证复合问题逐项回答、比例口述、否定/例外表达及实际播放。调用方裁剪后遗漏条件不能由CueKB的top_k或未裁剪响应承担保证。
