# CourseLoop · 专业课学习闭环平台

比赛独立提交版 V1.0。四门课程：数据结构、计算机组成原理、操作系统、计算机网络。
核心流程：写思路 → 分级提示 → 首错／错因 → 纠正重讲 → 变式复测 → 学习记录 → 个性化复习，支持学生、助教、教师与教务协同。

本地演示入口：`http://127.0.0.1:8000/`，部署步骤见下文；虚构演示账号见 [ACCESS.md](ACCESS.md)。线上体验地址与评审凭据通过比赛指定渠道单独提供。
正式资料：[交付索引](docs/submission/00-交付索引.md) · [技术报告](docs/submission/02-技术报告.md) · [测试报告](docs/submission/05-测试报告.md)。

提交版包含原子学习记录回写、SQLite并发与查询优化、解析预热保护及交付工具，并已在独立测试环境完成验收。源码核对与提交版模块说明见 [SOURCE_BASELINE.json](SOURCE_BASELINE.json)。在线演示与本地交付分别提供功能体验和可复现部署。
本次交付4810道处理后题目、2646个检索块及配套课程、知识点与判题配置。历史容量实验使用4808题快照，GPU实验使用36题原创演示库，各自保留实测口径。

| 环境 | 内容规模／身份 | 模型方式 | 用途 |
|---|---|---|---|
| 在线演示原型 | 四课程功能演示；专用评委学生账号 | 外部文本与视觉API | 核心功能在线体验 |
| 独立提交版测试环境 | 4808题、2646检索块；虚构身份 | 容量测试使用CPU确定性链路 | 验证事务、并发与查询优化 |
| 下载运行包v1.0 | 4810题、2646检索块；虚构身份 | 无密钥基础模式，可配置自有API | 完整处理后内容部署及多角色体验 |
| 4090D独立实验部署 | 36题、4原创讲义；虚构身份 | Qwen3-14B-FP8＋LoRA | GPU性能与应用接入验证 |

正式交付数据见[处理后数据交付范围](docs/submission/处理后数据交付范围.md)及[数据清单](docs/submission/processed-data-inventory.json)。部分数据来源于南京理工大学计算机科学与工程学院本科生自建数据库。

## 本地启动：无 GPU、无 API 密钥

依赖：Python 3.10+、Node 20.19+ 或 22.12+、npm、支持 FTS5 trigram 的 SQLite。Linux 验证通过，Windows 的代码资源限制沙箱需另验证。

```bash
git clone https://github.com/cccccrj/courseloop-competition-submission.git
cd courseloop-competition-submission
git checkout v1.0
python3 -m venv .venv
.venv/bin/python -m pip install -i https://pypi.tuna.tsinghua.edu.cn/simple -r requirements.txt
.venv/bin/python scripts/submission_prepare.py bootstrap
cd frontend
npm ci
npm run build
cd ..
.venv/bin/python -m uvicorn scripts.submission_serve:app --host 127.0.0.1 --port 8000 --workers 1
```

访问 `http://127.0.0.1:8000/`，登录账号见 ACCESS.md。`bootstrap` 只允许新建空库，不覆盖已有数据；本地账号从 ACCESS.md 导入，密码不输出。
可选开发方式：后端 `uvicorn backend.api.main:app`，前端 `cd frontend && npm run dev`，访问 `http://localhost:6006`。

无密钥基础模式支持课程、题库、数值与代码判分、错题、学情、复习及教学工作台；配置API后开启问答、模型诊断与出题。流程桩与真实模型调用分别验收。
本地 `demo_student`／`demo_ta` 的历史弱密码账号只供回环演示，部署公网前停用，使用 HTTPS、限流与独立签名密钥。

## Release 运行包

正式提交使用 [v1.0 Release](https://github.com/cccccrj/courseloop-competition-submission/releases/tag/v1.0)，提供完整源码、编译前端、4810题处理后数据库、2646块检索索引、9份正式报告与测试证据，附数据清单及SHA256SUMS。提交版包含事务与并发优化及4090D GPU实测；各项实验保留实际数据、设备与源码快照口径。仓库仅保留main分支和v1.0正式标签。
运行包解压后安装后端依赖，直接启动 `scripts.submission_serve:app`；已含完整处理后内容库，不再运行 bootstrap。源码由 GitHub 自动提供 ZIP／tar.gz。
运行包采用虚构评审身份，模型端点与凭据由部署者自行配置。

以下命令部署v1.0运行包。`gh`需使用已获私有仓库访问权限的账号登录；在新建下载目录执行，避免覆盖已有环境：

```bash
gh release download v1.0 --repo cccccrj/courseloop-competition-submission \
  --pattern 'courseloop-v1.0-runtime.tar.gz' --pattern SHA256SUMS
sha256sum --ignore-missing -c SHA256SUMS
tar xzf courseloop-v1.0-runtime.tar.gz
cd courseloop-submission
python3 -m venv .venv
.venv/bin/python -m pip install -i https://pypi.tuna.tsinghua.edu.cn/simple -r requirements.txt
.venv/bin/python -m uvicorn scripts.submission_serve:app --host 127.0.0.1 --port 8000 --workers 1
```

演示视频约4分39秒及33页答辩PPT已纳入V1.0 Release附件，见[演示材料与文件校验](docs/submission/演示材料.md)。完整交付清单见[交付索引](docs/submission/00-交付索引.md)。

## 配置

生成脚本默认 `.env` 使用 `CL_RAG_MODE=fts5`。`.env.example` 提供可选模型配置；不要提交真实密钥。

| 配置项 | 用途 |
|---|---|
| CL_LLM_BASE_URL / CL_LLM_API_KEY | OpenAI 兼容模型服务与凭据 |
| CL_LLM_WORKHORSE_MODEL / EXPERT_MODEL / REASONER_MODEL | 文本任务、严谨生成与流式讲解角色 |
| CL_LLM_VISION_MODEL | 拍照转写模型；线上已启用 qwen3-vl-30b-a3b-instruct |
| CL_LLM_VISION_BASE_URL / VISION_API_KEY | 独立视觉端点与密钥；留空兼容统一端点，不覆盖文本配置 |
| CL_LLM_TEXT_BASE_URL / TEXT_API_KEY | 文本端点可单独配置 |
| CL_LLM_{DIAGNOSE,SOLVE,GROUNDED_QA,PRACTICE}_* | 任务独立端点、model、temperature、max_tokens |
| CL_DB_PATH / CL_RAG_FTS_PATH / CL_LOGS_DIR | 业务库、全文索引和运行日志 |
| CL_RAG_MODE | fts5（无卡默认）、vector（需重依赖及模型）、off |
| CL_SECRET_KEY | 独立令牌签名密钥；未填则首次本地生成 `.secret_key` |
| CL_BUILD_COMMIT | 无 Git 环境部署的版本标识；有 Git 时以源码提交核对 |
| CL_BACKEND_URL / CL_ALLOWED_HOSTS | Vite 开发代理目标和允许域名 |

完整设置：[backend/core/config.py](backend/core/config.py)；部署与运维：[01](docs/submission/01-部署与访问.md)、[deploy/](deploy/)。
模型 API 有成本及数据发送边界；任务 token 上限只作用于相应 `ask_task` 路径，学习大厅 reasoner stream 没有显式 token 上限。

## 测试与复现

```bash
.venv/bin/python -m pytest -q
.venv/bin/python -m scripts.smoke_demo_path --out artifacts/evidence/demo-smoke.json
.venv/bin/python -m scripts.check_learning
.venv/bin/python scripts/submission_algorithms.py --out artifacts/evidence/algorithms.json
cd frontend && npm run type-check && npm run build
```

真实模型烟测：配置自己的密钥后执行 `scripts/submission_test.py live --base http://127.0.0.1:8000/api --out artifacts/evidence/live.json`。
功能测试读取 `.private/accounts.json`，克隆后先运行 bootstrap 或 `submission_prepare.py credentials` 从 ACCESS.md 导入。
浏览器脚本需额外 `npm install --no-save playwright-core`，设置 `CL_PLAYWRIGHT_MODULE`（模块绝对路径）、`CL_CHROMIUM`、`CL_BROWSER_BASE` 后执行 `node scripts/submission_browser.mjs`。

压力写入工具仅允许 `courseloop-submission-test-*` 独立测试目录与回环入口，并使用独立50个虚构账号；不得对公网生产地址执行 load。
本次全部结果、原始日志、JUnit与截图在 [artifacts/evidence/](https://github.com/cccccrj/courseloop-competition-submission/tree/v1.0/artifacts/evidence)，汇总在 [05](docs/submission/05-测试报告.md)；桩、真实模型、历史训练与4090D本地GPU推理分别标注。[08 GPU专项报告](docs/submission/08-GPU部署与性能报告.md)记录单请求、长上下文、4／8路并发和实际应用接入。

## 源码结构

| 目录 | 职责 |
|---|---|
| backend/api、core | HTTP、身份、课程、SQLite、错误编号和模型客户端 |
| backend/agents | TutorGraph、结构化解析、诊断、变式、周报 |
| backend/rag、verification | 资料分块、检索、引用、数值／代码／规则验证 |
| backend/learning | 画像、掌握度、预算、复习、摸底、模考、教学协同 |
| frontend/src | 页面、导航、状态、SSE、公式与代码渲染 |
| scripts、tests | 数据与训练研究、交付工具及回归用例 |
| docs/submission | 正式MD／DOCX／PDF、数据清单、演示入口与字段字典 |

## 常见问题

- 获取交付材料：源码与Release按同一标签提供；当前访问权限由仓库所有者管理，线上评审凭据单独交付。
- 缺 GPU：FTS5＋外部 API 或无密钥降级模式不需要显卡；向量模型及自训推理另配。
- 如何开启本地 AI 功能：填写自己的模型端点、密钥与模型名，重启后通过 `/api/health` 确认就绪；采用外部API时支持无卡运行。
- 拍照批改状态：在线演示服务已启用视觉 API，并通过图片转写→错因诊断→SSE 完成的实测。下载包采用自有凭据配置，自行部署填写视觉端点、密钥及模型，并分别检查 `/api/health` 的 `llm_ready` 与 `vision_ready`。出现调用异常时，结合模型配置、供应商响应、网络及上传限制定位原因。
- 本地内容与线上体验：运行包含4810题与2646个检索块，支持本地完整内容体验；源码bootstrap提供36题原创轻量样例，两者独立标明。
- bootstrap 提示库存在：为防数据损失正常拒绝；换全新目录或使用随包数据，别手工覆盖生产库。
- `seed_problems` 能否重跑：该旧脚本会清空数据结构题库，不能当增量迁移工具。
- 清华源超时：若环境有失效代理，可仅对安装命令移除代理；不要修改系统或泄露代理凭据。
- 历史评测与原创演示的复现入口：历史`verify_all`使用对应授权评测资产；原创演示采用submission_algorithms与smoke。
- 公式与代码如何保障结果质量：系统提供可追溯的资料引用辅助核查事实依据，结合数值校验、代码测试和规则判分验证相应结果；对自动验证尚不能判定的结果（`None`），保留人工复核流程，形成“依据可追溯、结果可验证、疑点可复核”的质量保障机制。
- 如何复现 GPU 测试：见 [08](docs/submission/08-GPU部署与性能报告.md)，应用环境安装requirements.txt，独立GPU环境安装requirements-gpu-submission.txt；通过submission_gpu_deploy.py启动服务、submission_gpu_test.py测量。训练或跨设备对比按 [07](docs/submission/07-版本冻结与后续更新.md)扩展。

## 数据与提交边界

正式验收采用最终提交代码的完整实测结果，见 [05 测试报告](docs/submission/05-测试报告.md) 与 artifacts/evidence/v1.0/；开发资料单独管理。独立账号写入对账与同账号掌握度累积分别验证。本轮容量对应SQLite单写者、单worker部署，多机容量、通用重试幂等与长期真人学习效果作为后续验证或工程增强方向，具体当前行为见技术报告。

[03 数据说明](docs/submission/03-数据说明.md) 解释来源、字段、预处理、脱敏和版权限制；[04 模型算法](docs/submission/04-模型与算法.md) 区分线上外部模型与历史 QLoRA／GRPO。
历史合成学习曲线用于流程与算法性质验证，真人教育收益通过后续对照实验评价。线上视觉API已接入并完成原创印刷体图片和公网拍照流程验收；下载包采用自有密钥配置，广泛手写OCR准确率另设专项评测。4090D独立部署已完成现有FP8基座与LoRA推理性能实测，模型摘要、环境锁及原始结果见08；授权权重独立管理。最终视频与PPT原样交付，入口见[演示材料](docs/submission/演示材料.md)。
源码、报告与运行包采用统一交付版本；ACCESS.md仅包含本地虚构演示账号。线上凭据、部署配置及内部运维信息独立管理。公开发布条件与第三方授权核对见 [PUBLICATION.md](PUBLICATION.md)。
