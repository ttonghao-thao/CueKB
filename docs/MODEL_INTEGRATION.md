# 外部模型集成

更新：2026-09-22。契约依据当前`src/cuekb/adapters/model_client.py`、`schemas.py`及索引适配器；本文未进行外部服务核验。

CueKB只作为客户端调用模型服务。系统管理员通过门户提交Embedding和可选Reranker各自的`base_url`、`api_key`和`model`三元组；API和Worker从PostgreSQL读取最新配置。`base_url`必须以`/v1`结尾且不含末尾斜杠；运行方负责TLS、网络访问控制、鉴权代理、模型部署、容量、健康检查和权重生命周期。模型API Key以`pgcrypto`加密保存，`CUEKB_MODEL_CONFIG_KEY`是部署环境中独立的稳定加密密钥；管理API只返回密钥是否已设置，不回显明文。

| 能力 | 请求 | 成功响应 | CueKB校验 |
| --- | --- | --- | --- |
| Embedding（导入/检索前必配） | `POST {embedding_base_url}/embeddings`，Bearer `api_key`，`{"model":"...","input":["..."],"encoding_format":"float"}` | OpenAI `{"model":"...","data":[{"index":0,"embedding":[...]}]}` | 返回model必须等于配置model；按`index`还原输入顺序；每条输入应有一条向量，向量维度由OpenSearch映射校验 |
| Rerank（可选） | `POST {reranker_base_url}/rerank`，Bearer `api_key`，`{"model":"...","query":"...","documents":["..."]}` | OpenAI-compatible `{"model":"...","results":[{"index":0,"relevance_score":0.9}]}` | 返回model必须等于配置model；按`index`还原输入顺序；每个候选应有一个分数 |

Embedding遵循 OpenAI `POST /embeddings` 契约；重排不是 OpenAI 官方端点，采用 vLLM 等 OpenAI-compatible 服务使用的`POST /rerank`扩展。服务应以非2xx表达鉴权、限流或不可用；CueKB按既有deadline处理为向量或重排降级，绝不回退为项目内推理。若需要其他第三方协议，必须在`model_client.py`增加明确适配器，不能把第三方响应泄漏到核心检索逻辑。


## 生命周期与优化边界

配置存PG，API每次检索读取匹配模型及物理索引并创建HTTP客户端，在依赖退出时关闭。Worker按运行配置执行批量编码；在线与入库尚无共享全局配额。不能把单个请求超时等同总请求硬截止；连接复用、资源隔离及取消方案见[M4](plans/M4.md)。

换模型需新generation；操作见[运行手册](OPERATIONS.md#m3升级与索引重建)。客户端不实现本地推理；严格离线部署应自行准备本地模型服务及OCR资源。
