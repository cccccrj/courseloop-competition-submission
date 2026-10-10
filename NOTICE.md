# NOTICE

本仓库提供应用源码、比赛正式材料及归档测试证据。运行包提供处理后课程内容与虚构身份，不携带原始课件、试卷附件、真实用户与学习记录、模型权重、供应商API密钥或服务器访问凭据。ACCESS.md仅包含本地虚构演示账号，线上评审凭据单独交付。

自主源码的MIT许可意向、第三方资源与组合分发核对见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。数据与模型各自适用独立授权，公开条件见 [PUBLICATION.md](PUBLICATION.md)；确认前仓库保持私有。

## 可选模型

- 嵌入模型：BAAI/bge-m3
- 精排模型：BAAI/bge-reranker-v2-m3

模型权重不随仓库分发，可通过 `.env` 配置本地路径。大模型接口采用 OpenAI 兼容协议，也可接入本地推理服务。

## 可选公开语料

`scripts/fetch_corpus.sh` 中的公开语料各自适用独立许可证。使用和再分发前请核对原始项目的授权条款并保留必要署名。
