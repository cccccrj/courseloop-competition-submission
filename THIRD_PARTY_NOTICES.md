# 第三方资源与许可边界

## 自研代码与交付范围

自主研发源码的许可意向为MIT；最终LICENSE在共同权利人授权及第三方组合分发义务核对后，随冻结版本确定。当前仓库保持私有，不通过本文件改变第三方软件、资料、模型、数据或API服务的原许可。报告、账号说明和受限数据按比赛评审用途管理。

数据资产与是否随包分发见 [数据说明](docs/submission/03-数据说明.md)。训练／评测代码、模型资产和数据内容分别管理；源码可交付不代表全部原始内容均可再分发。

## 关键依赖与实现边界

下表是核心资源披露入口，非完整传递依赖清单。安装版本以锁文件和实验环境锁为准；Python发行包的LICENSE／元数据及Node发行包的license字段提供核对依据，分发时保留适用的LICENSE、NOTICE和署名。

| 资源 | 版本入口 | 许可类型／使用方式 |
|---|---|---|
| FastAPI／Pydantic | [平台依赖](requirements.txt) | MIT；API与数据契约 |
| Uvicorn／HTTPX | [平台依赖](requirements.txt) | BSD-3-Clause；服务与HTTP |
| OpenAI Python SDK | [平台依赖](requirements.txt) | Apache-2.0；兼容接口客户端，非供应商模型授权 |
| Vue／Vue Router／Pinia／Axios／Marked／KaTeX | [前端依赖锁](frontend/package-lock.json) | MIT；页面、状态与内容渲染 |
| highlight.js | [前端依赖锁](frontend/package-lock.json) | BSD-3-Clause；代码高亮 |
| TypeScript | [前端依赖锁](frontend/package-lock.json) | Apache-2.0；构建与类型检查 |
| PyMuPDF | [平台依赖](requirements.txt)／[材料工具锁](requirements-materials.txt) | AGPL或商业许可；见下方专项说明 |
| python-docx | [材料工具锁](requirements-materials.txt) | MIT；正式文档生成 |
| Qwen3-14B-FP8＋LoRA | [模型来源](https://huggingface.co/Qwen/Qwen3-14B-FP8)、[GPU资产盘点](artifacts/evidence/v1.0/gpu-4090d/inventory.json) | 基座和adapter分别核对来源、许可与文件摘要；不随运行包分发权重 |
| Unsloth／TRL训练工具 | [训练脚本](scripts/train_sft.py)、[模型与算法说明](docs/submission/04-模型与算法.md) | 训练工具按各自发行包许可；与当前推理环境分开 |

## 模型、数据与第三方服务

- BGE-M3：[官方模型与许可证入口](https://huggingface.co/BAAI/bge-m3)。模型资产独立获取，来源／文件摘要按对应GPU实验记录核对。
- 外部文本与视觉API：采用OpenAI兼容协议并由部署者配置自有凭据。供应商模型名、参数及使用方式见算法报告；计费、输入处理与服务条款以对应账户约定为准。
- 数据交付已明确为处理后内容，具体组成见数据说明。原始资料不随包交付；处理后内容的公众再分发授权与比赛提交许可分别核对，来源署名保留。
- AI辅助开发及材料整理：用于代码与文档修订、测试工具和报告组织；实际参数、数据和结论通过源码、归档响应、测试及人工检查核对。AI生成内容按其来源与适用服务条款进行权利审核。

## PyMuPDF专项许可核对

[官方许可说明](https://pymupdf.readthedocs.io/en/latest/about.html#license-and-copyright)列明AGPL及商业许可两种方式。当前使用该组件进行PDF解析／材料检查；最终分发或公开前，应核对具体使用与组合方式所对应的义务，并采用满足相应义务的发布方式或合法商业授权。不得把依赖自身许可改成MIT，也不在本阶段宣称整套组合已完成MIT公开合规验收。

本清单用于私有材料整理；面向公众的许可证、源码提供义务及版权附件将在最终冻结前继续核对。
