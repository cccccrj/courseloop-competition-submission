"""全局配置中心（CourseLoop）。

所有路径、模型位置、LLM 连接信息集中在此。env 前缀 CL_，从项目根目录 .env 读取。
LLM 未配置时系统降级为「纯检索 / 兜底对话」模式（见 llm_ready）。

所有业务配置统一使用 CL_ 环境变量前缀。
"""
from __future__ import annotations

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict
from backend.core.generation_contract import DEFAULT_TASK_MAX_TOKENS

# 项目根目录：backend/core/config.py -> backend -> 项目根
PROJECT_ROOT = Path(__file__).resolve().parents[2]

TASK_ROLES = {
    "diagnose": "workhorse",
    "solve": "workhorse",
    "grounded_qa": "workhorse",
    "practice": "workhorse",
}


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=PROJECT_ROOT / ".env",
        env_file_encoding="utf-8",
        env_prefix="CL_",
        extra="ignore",
    )

    # ---------- 本地模型路径（bge-m3 嵌入 + bge-reranker 精排）----------
    # 建议软链到共享模型目录，避免占满磁盘（见 docs/03 复用指导 §五）
    embed_model_path: str = str(PROJECT_ROOT / "model" / "bge-m3")
    reranker_model_path: str = str(PROJECT_ROOT / "model" / "bge-reranker-v2-m3")

    # ---------- 向量库（ChromaDB 持久化目录）----------
    chroma_dir: str = str(PROJECT_ROOT / "data" / "processed" / "chroma")
    chroma_collection: str = "courseloop"

    # ---------- RAG 运行模式 ----------
    # vector：原 bge-m3 + reranker 路径，适合有 GPU/充足内存的环境。
    # fts5：离线构建的 SQLite trigram 全文索引，适合 1C2G 无卡演示机。
    # off：完全关闭检索，只保留普通 API 对话。
    rag_mode: str = "vector"
    rag_fts_path: str = str(PROJECT_ROOT / "data" / "processed" / "rag_fts.db")

    # ---------- 鉴权 ----------
    # 令牌签名密钥。留空时 backend/core/security.py 会生成一个并存到
    # data/processed/.secret_key 复用；生产部署应显式配置 CL_SECRET_KEY。
    secret_key: str = ""

    # 部署版本号（docs/29 §3.6）。用压缩包部署、机器上没有 .git 时在 .env 里填，
    # 管理员 /admin/runtime 与演示前自检据此核对版本；公开 /health 仅报告
    # 存活和模型状态。有 .git 时留空即可，会自动读。
    build_commit: str = ""

    # ---------- 原始语料 & 业务库 ----------
    raw_dir: str = str(PROJECT_ROOT / "data" / "raw")
    # 撤下的资料停在这里。**必须在 raw_dir 之外**：discover_corpus 扫的是
    # raw_dir/<课程目录>/<类别>/，留在里面的话下一次整轮重建会把撤下的资料
    # 原样再索引回去 —— 撤下就成了只在页面上生效的假动作。
    withdrawn_dir: str = str(PROJECT_ROOT / "data" / "withdrawn")
    db_path: str = str(PROJECT_ROOT / "data" / "processed" / "courseloop.db")
    logs_dir: str = str(PROJECT_ROOT / "data" / "logs")   # LLM 请求 JSONL 埋点（SFT/分析底座）

    # ---------- 检索参数 ----------
    top_k: int = 8
    rerank_top_n: int = 3
    embed_device: str = "auto"          # auto=显存足够用 cuda 否则 cpu
    skip_warmup: bool = True            # 骨架期默认跳过模型预热，服务秒起

    @property
    def device(self) -> str:
        from backend.core.device import resolve_device
        return resolve_device(self.embed_device)

    # ---------- LLM（OpenAI 兼容接口）----------
    llm_base_url: str = ""
    llm_api_key: str = ""
    llm_text_base_url: str = ""
    llm_text_api_key: str = ""
    # 视觉可以独立密钥/供应商；留空兼容原有统一端点，不改变文本路由。
    llm_vision_base_url: str = ""
    llm_vision_api_key: str = ""

    # 四角色分工（沿用母项目）：
    #   expert    严谨生成（批改/文书）
    #   workhorse 路由 / RAG 总结 / 讲解 / 简单问答
    #   reasoner  强 CoT：步骤化解析 / 错因诊断（保留 <think>）
    #   vision    多模态图片转写，接入文本诊断与拍照批改链路
    llm_expert_model: str = ""
    llm_workhorse_model: str = ""
    llm_reasoner_model: str = ""
    llm_vision_model: str = ""

    # 任务级模型路由：用于同一 Qwen3-14B 基座动态切换不同 LoRA alias。
    # 任一字段留空都会回退统一文本端点 / workhorse 模型，保持完全兼容。
    llm_diagnose_base_url: str = ""
    llm_diagnose_api_key: str = ""
    llm_diagnose_model: str = ""
    llm_diagnose_temperature: float = 0.0
    llm_solve_base_url: str = ""
    llm_solve_api_key: str = ""
    llm_solve_model: str = ""
    llm_solve_temperature: float = 0.0
    llm_grounded_qa_base_url: str = ""
    llm_grounded_qa_api_key: str = ""
    llm_grounded_qa_model: str = ""
    llm_grounded_qa_temperature: float = 0.0
    llm_practice_base_url: str = ""
    llm_practice_api_key: str = ""
    llm_practice_model: str = ""
    llm_practice_temperature: float = 0.0

    # 任务级生产输出上限：防止本地 LoRA 无界生成，也作为离线评测的唯一口径。
    llm_diagnose_max_tokens: int = DEFAULT_TASK_MAX_TOKENS["diagnose"]
    llm_solve_max_tokens: int = DEFAULT_TASK_MAX_TOKENS["solve"]
    llm_grounded_qa_max_tokens: int = DEFAULT_TASK_MAX_TOKENS["grounded_qa"]
    llm_practice_max_tokens: int = DEFAULT_TASK_MAX_TOKENS["practice"]

    llm_expert_temperature: float = 0.1
    llm_workhorse_temperature: float = 0.3
    llm_reasoner_temperature: float = 0.4
    llm_vision_temperature: float = 0.2

    # 本地 no-think 微调模型（如 courseloop-408）需在渲染时关闭 Qwen3 思考模式，
    # 否则默认 enable_thinking=True 会污染 JSON 输出。置 True 时给文本调用带上
    # extra_body={"chat_template_kwargs": {"enable_thinking": False}}。切回 API 时置 False。
    llm_no_think: bool = False

    @property
    def llm_ready(self) -> bool:
        return bool(self.llm_base_url and self.llm_api_key
                    and (self.llm_workhorse_model or self.llm_expert_model))

    @property
    def text_llm_base_url(self) -> str:
        return self.llm_text_base_url or self.llm_base_url

    @property
    def text_llm_api_key(self) -> str:
        return self.llm_text_api_key or self.llm_api_key

    @property
    def text_llm_ready(self) -> bool:
        return bool(self.text_llm_base_url and self.text_llm_api_key)

    @property
    def expert_model(self) -> str:
        return self.llm_expert_model or self.llm_workhorse_model

    @property
    def workhorse_model(self) -> str:
        return self.llm_workhorse_model or self.llm_expert_model

    @property
    def reasoner_model(self) -> str:
        return self.llm_reasoner_model or self.workhorse_model

    @property
    def vision_model(self) -> str:
        return self.llm_vision_model

    @property
    def vision_llm_base_url(self) -> str:
        return self.llm_vision_base_url or self.llm_base_url

    @property
    def vision_llm_api_key(self) -> str:
        return self.llm_vision_api_key or self.llm_api_key

    @property
    def vision_ready(self) -> bool:
        return bool(self.vision_llm_base_url and self.vision_llm_api_key and self.llm_vision_model)

    def endpoint_for_task(self, task: str) -> tuple[str, str]:
        if task not in TASK_ROLES:
            raise ValueError(f"未知 LLM 任务：{task}")
        base = getattr(self, f"llm_{task}_base_url", "") or self.text_llm_base_url
        key = getattr(self, f"llm_{task}_api_key", "") or self.text_llm_api_key
        return base, key

    def model_for_task(self, task: str) -> str:
        if task not in TASK_ROLES:
            raise ValueError(f"未知 LLM 任务：{task}")
        return getattr(self, f"llm_{task}_model", "") or self.workhorse_model

    def temperature_for_task(self, task: str) -> float:
        if task not in TASK_ROLES:
            raise ValueError(f"未知 LLM 任务：{task}")
        configured = any((
            getattr(self, f"llm_{task}_model", ""),
            getattr(self, f"llm_{task}_base_url", ""),
            getattr(self, f"llm_{task}_api_key", ""),
        ))
        if not configured:
            return self.llm_workhorse_temperature
        return float(getattr(self, f"llm_{task}_temperature"))

    def max_tokens_for_task(self, task: str) -> int:
        if task not in TASK_ROLES:
            raise ValueError(f"未知 LLM 任务：{task}")
        return int(getattr(self, f"llm_{task}_max_tokens"))


settings = Settings()
