# CourseLoop GPU 本地部署与推理性能专项报告

正式交付版本：v1.0。实测时间：2026-10-09T11:58:42.176714+00:00至2026-10-09T12:04:13.363344+00:00。应用源码快照：3a265650eb7cdd715adcb3a9c78855bffccdd8be。本报告组织已完成的4090D实验，保留测试环境业务容量验收与公网视觉验收的独立口径；实测快照标识用于证据追溯。

## 一、实验目标与硬件环境

在租借的独立4090D服务器部署CourseLoop，验证现有基座与LoRA的本地推理、批量并发和应用调用，补充可复现的GPU性能证据。测试环境正式服务保持现有部署。

GPU：NVIDIA GeForce RTX 4090 D, 570.124.04, 24564 MiB, 425.00 W；CUDA计算能力：[8, 9]。容器配额见inventory.json的cgroup字段，cpu.max=1800000 100000对应18核配额，memory.max=64424509440对应60GiB；宿主CPU为AMD EPYC 9754，宿主总资源与容器配额分别记录。

Python=3.10.15；vLLM=0.10.2；torch=2.8.0；torch CUDA=12.8；transformers=4.55.2；tokenizers=0.21.4；triton=3.4.0。完整依赖见GPU和应用环境锁定文件。

## 二、模型资产与服务配置

使用项目已有Qwen3-14B-FP8基座及courseloop-408 rank16／alpha32 LoRA，以courseloop-default别名提供兼容模型接口。FP8基座采用128×128块量化，推理dtype=bfloat16。LoRA源自历史4bit训练路线，本轮在FP8基座上验证加载与推理；量化条件作为本实验配置单独记录。

基座文件清单SHA256：47080db0eaab6917bcdf153974964487e368431c0d13ed160e474edd8d5c08b3；LoRA文件清单SHA256：ed8dab5514892a6a5c307c78515338e466e4f111e499b2f8bfe0d204d68a8927。各文件SHA256、大小见inventory.json，模型资产按授权独立保管，不随源码分发。

服务配置：单GPU、单模型进程，max_model_len=4096、max_num_seqs=8、max_num_batched_tokens=4096、gpu_memory_utilization=0.90、max_loras=1、max_lora_rank=16；采用eager执行，关闭前缀缓存，temperature=0、seed=42、enable_thinking=false。完整启动命令见service-command.json。

## 三、实验设计与指标定义

四组负载串行测试：短上下文单请求、长上下文单请求、4并发批量、8并发批量。每组先预热1批（请求数与并发一致），再重复5批；计时样例使用原创材料，固定输出128或256tokens（ignore_eos=true），确保输出长度可比较。长上下文原始提示和实际输入tokens保存在benchmark.json，benchmark_script_sha256记录实际测试脚本摘要。

端到端时延由服务器回环HTTP客户端测量，包含HTTP、排队和GPU执行；TTFT为收到首个非空内容事件的时间。整体输出tokens/s=实际输出tokens总数÷该组墙钟时间。平均值、中位数、P95与每次原始计时均在JSON中记录，小样本P95采用线性插值。

每200ms调用nvidia-smi采样GPU利用率、驻留显存、功率与温度。驻留显存包含模型、服务缓冲与KV缓存，采样峰值与框架张量分配峰值属于不同指标。固定长度生成用于性能测试；自然结束的真实客户端调用另列为应用接入验收。

## 四、GPU性能实测

| 负载 | 并发 | 批次／请求 | 输出tokens | 平均时延ms | P95时延ms | 首token P95ms |
|---|---:|---|---:|---:|---:|---:|
| single-short | 1 | 5／5 | 128 | 10527.754 | 11408.28 | 132.626 |
| single-long-context | 1 | 5／5 | 256 | 21050.364 | 21498.537 | 257.276 |
| batch-4 | 4 | 5／20 | 128 | 10820.146 | 11464.665 | 280.44 |
| batch-8 | 8 | 5／40 | 128 | 10821.892 | 11497.432 | 422.217 |

| 负载 | 总输出tokens | 整体tokens/s | 显存峰值MiB | 利用率峰值% |
|---|---:|---:|---:|---:|
| single-short | 640 | 12.158 | 21655.0 | 42.0 |
| single-long-context | 1280 | 12.161 | 21889.0 | 100.0 |
| batch-4 | 2560 | 47.204 | 21889.0 | 54.0 |
| batch-8 | 5120 | 94.422 | 21889.0 | 84.0 |

各组请求全部完成，输出长度核对通过，GPU利用率采样确认计算设备实际参与。完整分位数、批次耗时、输入／输出token用量、输出及GPU采样见benchmark.json。

## 五、应用部署与集成验收

独立原创演示数据库36题、4讲义，配置本地GPU文本模型，应用同源提供前端与API。功能、权限与教学协同检查86/86通过，原始证据见functional.json。

四课程通过项目真实ask_task客户端与grounded-qa提示调用本地LoRA，4次响应传输与非空输出检查通过，原始输入、输出与耗时见benchmark.json。本项确认真实应用接入，答案准确率采用独立人工标注评价。

公网视觉继续采用已验收的智增增专用API；本GPU部署聚焦本地文本推理，视觉结果按vision-live.json单独呈现。当前检索为FTS5，向量embedding／rerank GPU基准按采用向量路线时另行开展。

## 六、部署访问与复现

应用默认回环端口18080，模型默认回环端口18003。服务器地址由部署者私下提供，账号见ACCESS.md；SSH口令、供应商密钥与原始.env保持独立管理。可通过SSH端口转发访问：http://127.0.0.1:18080/。

上述端口与服务配置属于GPU实验环境的复现方式；日常评审访问采用在线演示服务入口。GPU实验服务是否在线由部署者管理，归档测试结果与复现参数独立提供。

```bash
ssh -L 18080:127.0.0.1:18080 -p <SSH端口> <用户>@<GPU服务器地址>
# 从仓库导出快照，部署目录名须含courseloop-gpu-submission-且无.git
# 将授权基座放在model/Qwen3-14B-FP8，适配器放在model/courseloop-408
# 编译frontend/dist；在部署目录分别创建应用与GPU虚拟环境
<应用环境>/bin/pip install -i https://pypi.tuna.tsinghua.edu.cn/simple -r requirements.txt
<GPU环境>/bin/pip install -i https://pypi.tuna.tsinghua.edu.cn/simple -r requirements-gpu-submission.txt
<应用环境>/bin/python scripts/submission_gpu_deploy.py configure --source-commit <40位源码提交>
<应用环境>/bin/python scripts/submission_gpu_deploy.py model --vllm <GPU环境>/bin/vllm
# 另一个终端启动应用；模型就绪后再运行基准，避免并行负载干扰
<应用环境>/bin/python scripts/submission_gpu_deploy.py app
<应用环境>/bin/python scripts/submission_gpu_test.py --out .private/gpu-benchmark.json --repeats 5
```

通过源码快照、模型文件摘要、完整服务命令和环境锁复现；CPU业务容量见05报告，API视觉链路见06报告，本地GPU性能见本报告。

## 七、适用范围与扩展方向

本轮验证4090D上的现有模型推理与应用部署，训练参数和历史训练结果见04报告；本轮按推理实验执行。与双V100的硬件对比需固定同一模型、量化、上下文和服务参数后分别实测；FP8与历史4bit条件分别记录。长时间GPU容量、更多上下文长度、向量模型与GPU训练时长可按07协议扩展。

技术依据：[vLLM FP8量化](https://docs.vllm.ai/en/v0.10.2/features/quantization/fp8.html)、[vLLM LoRA](https://docs.vllm.ai/en/v0.10.2/features/lora.html)。

浏览器验收：23/23通过，JS错误0；界面截图和结果见ui/。

真实应用SSE：2/2通过，使用本地GPU模型完成材料检索、流式回答与最终事件，见application-sse.json。
