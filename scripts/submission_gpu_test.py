"""本地 GPU 推理验收：固定长度性能负载与自然结束的应用调用分别记录。

仅允许回环模型端点。固定长度负载用于计时，不参与答案准确率评价。
进程显存通过 nvidia-smi 采样，指标为服务进程驻留显存而非张量分配峰值。
"""
from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import json
import math
import os
import statistics
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def distribution(values):
    ordered = sorted(values)
    if not ordered:
        return None
    def percentile(p):
        index = (len(ordered) - 1) * p
        lo, hi = math.floor(index), math.ceil(index)
        return round(ordered[lo] + (ordered[hi] - ordered[lo]) * (index - lo), 3)
    return {"mean": round(statistics.mean(values), 3), "median": percentile(.5),
            "p95": percentile(.95), "min": round(min(values), 3),
            "max": round(max(values), 3), "samples": len(values)}


def gpu_sample():
    result = subprocess.run(
        ["nvidia-smi", "--query-gpu=name,memory.total,memory.used,utilization.gpu,power.draw,temperature.gpu",
         "--format=csv,noheader,nounits"], capture_output=True, text=True, check=True)
    fields = [x.strip() for x in result.stdout.strip().splitlines()[0].split(",")]
    return dict(at=time.time(), name=fields[0], total_mib=float(fields[1]),
                used_mib=float(fields[2]), utilization_pct=float(fields[3]),
                power_w=float(fields[4]), temperature_c=float(fields[5]))


def request(base, model, messages, max_tokens, fixed_length=True):
    start, first, chunks, usage, finish = time.perf_counter(), None, [], None, None
    payload = dict(model=model, messages=messages, temperature=0.0, top_p=1.0,
                   seed=42, max_tokens=max_tokens, stream=True,
                   stream_options={"include_usage": True},
                   chat_template_kwargs={"enable_thinking": False})
    if fixed_length:
        payload["ignore_eos"] = True
    with httpx.Client(base_url=base, trust_env=False, timeout=600) as client:
        with client.stream("POST", "/chat/completions", json=payload) as response:
            response.raise_for_status()
            for line in response.iter_lines():
                if not line.startswith("data: ") or line == "data: [DONE]":
                    continue
                event = json.loads(line[6:])
                if event.get("usage"):
                    usage = event["usage"]
                for choice in event.get("choices", []):
                    text = choice.get("delta", {}).get("content") or ""
                    if text:
                        if first is None:
                            first = time.perf_counter()
                        chunks.append(text)
                    if choice.get("finish_reason"):
                        finish = choice["finish_reason"]
    end = time.perf_counter()
    content = "".join(chunks)
    assert content.strip() and usage and usage["completion_tokens"] > 0
    if fixed_length:
        assert usage["completion_tokens"] == max_tokens, usage
    decode = end - first if first is not None else None
    return dict(passed=True, input=messages, content=content, usage=usage, finish_reason=finish,
                latency_ms=round((end-start)*1000, 3),
                ttft_ms=round((first-start)*1000, 3) if first else None,
                output_tokens_per_s=round(usage["completion_tokens"]/(end-start), 3),
                decode_tokens_per_s=round(max(0, usage["completion_tokens"]-1)/decode, 3)
                if decode and decode > 0 else None)


def workload(name, concurrency, output_tokens, repeats, base, model, long=False):
    # 原创材料；批次标签便于区分请求，服务侧同时关闭前缀缓存。
    material = ("先进先出按进入顺序淘汰；最近最少使用按最近访问时间淘汰。" * 80
                if long else "栈采用后进先出，队列采用先进先出。")
    system = "你是408课程助教，请依据材料用中文解释核心概念并给出简单例子。"
    def messages(batch, index):
        return [{"role": "system", "content": system},
                {"role": "user", "content": f"测试批次{batch}样例{index}。材料：{material}\n请解释材料中的机制。"}]
    with concurrent.futures.ThreadPoolExecutor(max_workers=concurrency) as executor:
        warmups = [executor.submit(request, base, model, messages("warmup", i), output_tokens)
                   for i in range(concurrency)]
        for future in warmups:
            future.result()
    results, samples, stopping = [], [], threading.Event()
    def monitor():
        while not stopping.is_set():
            samples.append(gpu_sample())
            stopping.wait(.2)
    observer = threading.Thread(target=monitor, daemon=True)
    observer.start()
    started = time.perf_counter()
    batches = []
    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=concurrency) as executor:
            for batch in range(repeats):
                batch_start = time.perf_counter()
                futures = [executor.submit(request, base, model, messages(batch, i), output_tokens)
                           for i in range(concurrency)]
                outputs = [f.result() for f in futures]
                results.extend(outputs)
                duration = time.perf_counter()-batch_start
                batches.append(dict(batch=batch, seconds=round(duration, 6),
                                    output_tokens=sum(x["usage"]["completion_tokens"] for x in outputs)))
    finally:
        elapsed = time.perf_counter()-started
        stopping.set()
        observer.join(timeout=5)
    tokens = sum(x["usage"]["completion_tokens"] for x in results)
    return dict(name=name, concurrency=concurrency, repeats=repeats, warmup_requests=concurrency,
                fixed_output_tokens=output_tokens, fixed_length=True,
                elapsed_s=round(elapsed, 6), requests=len(results), failed=0,
                output_tokens=tokens, aggregate_output_tokens_per_s=round(tokens/elapsed, 3),
                request_latency_ms=distribution([x["latency_ms"] for x in results]),
                ttft_ms=distribution([x["ttft_ms"] for x in results]),
                batch_seconds=distribution([x["seconds"] for x in batches]),
                per_request_output_tokens_per_s=distribution([x["output_tokens_per_s"] for x in results]),
                gpu_peak_resident_mib=max(x["used_mib"] for x in samples),
                gpu_peak_utilization_pct=max(x["utilization_pct"] for x in samples),
                batches=batches, results=results, gpu_samples=samples)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="http://127.0.0.1:18003/v1")
    parser.add_argument("--model", default="courseloop-default")
    parser.add_argument("--out", required=True)
    parser.add_argument("--repeats", type=int, default=5)
    args = parser.parse_args()
    assert urlparse(args.base).hostname in ("127.0.0.1", "localhost")
    assert args.repeats >= 5
    output = Path(args.out)
    output.parent.mkdir(parents=True, exist_ok=True)
    from backend.core.config import settings
    with httpx.Client(base_url=args.base, trust_env=False, timeout=30) as client:
        response = client.get("/models")
        response.raise_for_status()
        model_registry = response.json()
    assert any(x["id"] == args.model for x in model_registry["data"])
    result = dict(started_at=datetime.now(timezone.utc).isoformat(), complete=False,
                  model=args.model, base=args.base, source_commit=settings.build_commit,
                  benchmark_script_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                  model_registry=model_registry,
                  scope="local loopback serving latency including HTTP, scheduling and GPU execution",
                  parameters=dict(temperature=0, seed=42, thinking=False, prefix_caching=False),
                  initial_gpu=gpu_sample(), stages=[], application_calls=[])
    def save():
        temporary = output.with_suffix(".tmp")
        temporary.write_text(json.dumps(result, ensure_ascii=False, indent=2)+"\n")
        temporary.replace(output)
    save()
    for name, concurrency, tokens, long in (
            ("single-short", 1, 128, False), ("single-long-context", 1, 256, True),
            ("batch-4", 4, 128, False), ("batch-8", 8, 128, False)):
        result["stages"].append(workload(name, concurrency, tokens, args.repeats,
                                             args.base, args.model, long))
        save()
        print(name, "passed", flush=True)
    # 使用项目真实客户端/提示词做自然结束的应用调用，独立于固定长度性能结果。
    from backend.core.llm import ask_task
    from backend.agents.prompts import build_grounded_qa_system
    for course, fact, question in (
            ("ds-algo", "栈采用后进先出。", "栈采用什么访问顺序？"),
            ("os", "FIFO按进入内存的顺序选择最早进入的页淘汰。", "FIFO如何选择淘汰页？"),
            ("co", "程序计数器保存下一条指令的地址。", "程序计数器保存什么？"),
            ("net", "TCP提供可靠的字节流传输。", "TCP提供什么传输服务？")):
        messages = [{"role":"system", "content":build_grounded_qa_system()},
                    {"role":"user", "content":f"source_id: 1\n课程: {course}\n材料：\n{fact}\n\n问题：{question}"}]
        start = time.perf_counter()
        content = ask_task("grounded_qa", messages, max_tokens=512)
        assert content.strip()
        result["application_calls"].append(dict(course=course, input=messages, output=content,
            latency_ms=round((time.perf_counter()-start)*1000,3),
            transport_and_nonempty_passed=True,
            evaluation_scope="real CourseLoop client and prompt integration, not statistical accuracy"))
        save()
    result.update(complete=True, finished_at=datetime.now(timezone.utc).isoformat(),
                  gpu_execution_observed=all(x["gpu_peak_utilization_pct"] > 0 for x in result["stages"]))
    assert result["gpu_execution_observed"]
    save()
    print("GPU benchmark complete", flush=True)


if __name__ == "__main__":
    main()
