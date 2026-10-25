# 外部模型接入

更新：2026-10-25。CueKB只调用Embedding和可选Reranker，不调用生成式答案模型。管理员通过`GET/PUT /v1/model-configuration`或门户配置，字段Schema以[OpenAPI](contracts/openapi.json)为准。

## 1. Embedding协议

base URL必须为HTTP/HTTPS且路径严格等于`/v1`，不带查询串、fragment或尾斜杠；CueKB在其后请求`POST /embeddings`，使用该服务独立Bearer Key：

```json
{"model":"embedding-model-revision","input":["first passage","second passage"],"encoding_format":"float"}
```

响应必须回显相同model，data完整包含每个input的唯一index及浮点向量，允许按index乱序：

```json
{"model":"embedding-model-revision","data":[{"index":1,"embedding":[0.2,0.4]},{"index":0,"embedding":[0.1,0.3]}]}
```

返回index必须是0至N-1的整数且不重复；每个值必须是有限数字，不接受bool/NaN/Inf。向量维度须匹配index配置。2维只为协议示例，生产dimension按实际模型配置，查询和文档必须一致。未返回model或同名不同权重服务均不能据此声称可复现。

## 2. Reranker协议

Reranker独立地址/Key/model，可不配置。未配置时V2正常RRF排序，不发请求、不标降级；关闭或剩余时间不足时诊断说明跳过原因。

`POST /rerank`：

```json
{"model":"reranker-model-revision","query":"input voltage","documents":["first passage","second passage"]}
```

```json
{"model":"reranker-model-revision","results":[{"index":1,"relevance_score":0.3},{"index":0,"relevance_score":0.9}]}
```

完整回传唯一index、同一model和有限relevance_score；分数越大越相关，接受未校准logit，不要求0—1。缺项、重复、越界、非有限或模型不一致均视作该阶段失败。V2成功只返回同次窗口中的结果；失败整体回RRF并标降级，不混用两种量纲继续排序。分数不解释为事实正确率或完整率。

## 3. 配置与索引一致性

首次配置Embedding后才允许相关生产处理。Embedding地址/Key/model、dimension及输入模板必须与索引构建配置匹配；变更受generation/内容栅栏约束，不能直接给旧向量换查询模型。Reranker配置独立，不能默认借Embedding地址代替。

generation记录build_source_profiles，以现有PG chunk和注册模板构建search_text及向量，不重新解析。解析/切块/归属变化先用reprocess生成新版本。回退模型仍要重建当前事实，不恢复历史chunk。

V2模型transport按配置修订复用并原生异步调用，连接/读取受阶段及全请求剩余预算约束；Worker保留同步路径。Worker模型时限独立且较长，V2进程容量不约束Worker或其他API进程。取消客户端连接不保证模型服务终止已开始的GPU计算。

## 4. 隐私与验证

模型会收到query及候选/文档检索文本。运行方须自行确定模型服务的数据边界与权限，不能为了测试擅自上传私有文档到公共付费服务。模型Key加密保存在PG，接口不回显明文，日志不输出Key或正文。

接入先在获授权的非私有合成数据上验证index回序、维度、finite校验、超时和取消，再用固定真实语料做无Reranker/有Reranker对照。默认BGE类模型是既有质量基线选择，不代表所有硬件、语言或文档的效果保证。
