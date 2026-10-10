"""计算设备选择。

常与 GPU 训练任务共享显卡，默认 "auto"：显存充足用 cuda，不足或无 GPU 自动回退 cpu。
也可在 .env 用 CL_EMBED_DEVICE 显式指定 cuda / cpu。
"""
from __future__ import annotations


def resolve_device(preferred: str, min_free_mib: int = 5000) -> str:
    """把配置中的设备偏好解析为实际可用设备。"""
    if preferred == "cpu":
        return "cpu"
    try:
        import torch
        if not torch.cuda.is_available():
            return "cpu"
        free, _total = torch.cuda.mem_get_info()
        free_mib = free / 1024 / 1024
        if preferred == "cuda":
            if free_mib < min_free_mib:
                print(f"[device] 警告：GPU 空闲仅 {free_mib:.0f}MiB，仍按设置使用 cuda，"
                      f"如遇 OOM 请把 CL_EMBED_DEVICE 设为 cpu 或 auto。")
            return "cuda"
        if free_mib >= min_free_mib:
            return "cuda"
        print(f"[device] GPU 空闲不足（{free_mib:.0f}MiB < {min_free_mib}MiB），本次使用 CPU。")
        return "cpu"
    except Exception as e:  # noqa: BLE001
        print(f"[device] 设备检测异常({e})，回退 CPU。")
        return "cpu"


def load_with_fallback(loader, device: str, label: str = "模型"):
    """用指定设备加载模型；若 CUDA OOM，则清缓存后自动回退 CPU 重试。"""
    if device != "cuda":
        return loader(device), device
    try:
        return loader("cuda"), "cuda"
    except RuntimeError as e:
        if "out of memory" not in str(e).lower():
            raise
        print(f"[device] {label} 加载时 GPU 显存不足，清理缓存并回退 CPU。")
        try:
            import torch
            torch.cuda.empty_cache()
        except Exception:  # noqa: BLE001
            pass
        return loader("cpu"), "cpu"
