"""比较 CourseLoop 增量 SFT；首轮不达标时自动执行一次低学习率抢救。

两轮都必须同时通过旧验证集、外部 targeted 结构与 targeted 语义门禁。抢救
训练只重采样 bulk train/旧 replay，绝不把 targeted 内容送入训练。
"""
from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path


HERE = Path(__file__).resolve().parents[1]
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))
from scripts.eval_targeted_semantic import (  # noqa: E402
    CACHE_VERSION as SEMANTIC_CACHE_VERSION,
    _production_prompt_contract_fingerprint as semantic_prompt_fingerprint,
    _semantic_signature,
)
from scripts.test_lora import (  # noqa: E402
    CACHE_VERSION as STANDALONE_CACHE_VERSION,
    _production_prompt_contract_fingerprint as standalone_prompt_fingerprint,
    _standalone_signature,
)
EVAL = HERE / "data" / "eval"
OUT = EVAL / "synth3000_selection.json"
FINAL_REPORT = HERE / "docs" / "17-CourseLoop增量SFT最终选优-20260822.md"
SFT_PYTHON = "/opt/sft_env/bin/python"
RAG_PYTHON = "/opt/projects/conda_data/envs/rag_env/bin/python"
OLD_ADAPTER = "model/loras/courseloop-408"
PRIMARY_ADAPTER = "model/loras/courseloop-408-synth3000"
RESCUE_ADAPTER = "model/loras/courseloop-408-synth3000-rescue"
QA_SPECIALIST_ADAPTER = "model/loras/courseloop-408-qa-probe-rescue"
QA_GATE = HERE / "data" / "qa_probe" / "gate.json"
QA_REPORT = EVAL / "report_targeted_semantic_qa_probe_rescue-prodprompt.json"
QA_EVAL_DATA = HERE / "data" / "qa_probe" / "targeted_eval.jsonl"
DIAGNOSE_GATE = HERE / "data" / "diagnose_probe" / "gate.json"
DIAGNOSE_FALLBACK_REPORT = EVAL / "report_targeted_semantic_diagnose_taxonomy_rescue.json"
OPTIMIZATION_STATUS = HERE / "data" / "optimization_15h_status.json"


def _read(name: str) -> dict:
    path = EVAL / name
    if not path.exists():
        raise FileNotFoundError(path)
    return json.loads(path.read_text(encoding="utf-8"))


def _read_optional(name: str) -> dict | None:
    path = EVAL / name
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None


def _standalone_report_checks(report: dict, adapter: str, *,
                              intents: set[str] | None = None,
                              expected_n: int = 467) -> dict[str, bool]:
    report_adapter = Path(str(report.get("adapter") or ""))
    report_val = Path(str(report.get("val_path") or ""))
    adapter_path = report_adapter if report_adapter.is_absolute() else HERE / report_adapter
    val_path = report_val if report_val.is_absolute() else HERE / report_val
    intents = set(intents or ())
    expected_signature = _standalone_signature(
        adapter, str(HERE / "data/sft/val.jsonl"),
        max_seq=4096, max_new_tokens=768, intents=intents,
        refresh_production_prompts=True,
    )
    return {
        "adapter_matches": adapter_path.resolve() == (HERE / adapter).resolve(),
        "old_val_matches": val_path.resolve() == (HERE / "data/sft/val.jsonl").resolve(),
        "expected_population": int(report.get("n_total", 0) or 0) == expected_n,
        "intent_filter_matches": set(report.get("intent_filter") or ()) == intents,
        "production_prompts_refreshed": report.get("production_prompts_refreshed") is True,
        "cache_signature_current": (
            int(report.get("cache_version", 0) or 0) == STANDALONE_CACHE_VERSION
        ),
        "production_prompt_contract_matches": (
            report.get("production_prompt_contract_fingerprint")
            == standalone_prompt_fingerprint()
        ),
        "run_signature_matches_current": report.get("run_signature") == expected_signature,
    }


def _ensure_old_diagnose_production_report() -> dict:
    """Refresh the old diagnosis baseline under the exact current contract."""
    name = "report_lora_diagnose-old-prodprompt-regression.json"
    report = _read_optional(name)
    checks = _standalone_report_checks(
        report or {}, OLD_ADAPTER, intents={"diagnose"}, expected_n=153,
    )
    if not report or not all(checks.values()):
        _run([
            SFT_PYTHON, "scripts/test_lora.py", "--adapter", OLD_ADAPTER,
            "--val", "data/sft/val.jsonl",
            "--tag", "diagnose-old-prodprompt-regression",
            "--intent", "diagnose", "--max-new-tokens", "768",
            "--refresh-production-prompts",
        ])
        report = _read(name)
        checks = _standalone_report_checks(
            report, OLD_ADAPTER, intents={"diagnose"}, expected_n=153,
        )
    if not all(checks.values()):
        raise RuntimeError(f"旧诊断生产基线身份不匹配：{checks}")
    return report


def _ensure_old_production_report() -> dict:
    """Evaluate the complete old adapter under the current production contract.

    The historical ``report_lora_best.json`` used one global 768-token cap.
    Production now allows 1024 tokens for solve/practice, so mixing that report
    with a current-contract candidate would make the regression gate an
    apples-to-oranges comparison.  Keep a separately signed full baseline and
    refresh it whenever weights, data, prompts, or task limits change.
    """
    name = "report_lora_408-old-prodprompt-regression.json"
    report = _read_optional(name)
    checks = _standalone_report_checks(report or {}, OLD_ADAPTER)
    if not report or not all(checks.values()):
        _run([
            SFT_PYTHON, "scripts/test_lora.py", "--adapter", OLD_ADAPTER,
            "--val", "data/sft/val.jsonl",
            "--tag", "408-old-prodprompt-regression",
            "--max-new-tokens", "768", "--refresh-production-prompts",
        ])
        report = _read(name)
        checks = _standalone_report_checks(report, OLD_ADAPTER)
    if not all(checks.values()):
        raise RuntimeError(f"旧模型全量生产基线身份不匹配：{checks}")
    return report


def _ensure_semantic(tag: str, adapter: str, exclude_intent: str | None = None) -> dict:
    production_tag = f"{tag}-prodprompt"
    path = EVAL / f"report_targeted_semantic_{production_tag}.json"
    report = json.loads(path.read_text(encoding="utf-8")) if path.exists() else None
    report_adapter = Path(str((report or {}).get("adapter") or ""))
    report_data = Path(str((report or {}).get("data") or ""))
    adapter_path = report_adapter if report_adapter.is_absolute() else HERE / report_adapter
    data_path = report_data if report_data.is_absolute() else HERE / report_data
    expected_excluded = {exclude_intent} if exclude_intent else set()
    expected_n = 104 if expected_excluded == {"grounded_qa"} else 154
    expected_signature = _semantic_signature(
        adapter, HERE / "data/synthetic_targeted_200/accepted.jsonl",
        max_seq=4096, max_new=1024,
        refresh_production_prompts=True,
        excluded_intents=expected_excluded,
    )
    identity_ok = bool(report) and all((
        adapter_path.resolve() == (HERE / adapter).resolve(),
        data_path.resolve() == (HERE / "data/synthetic_targeted_200/accepted.jsonl").resolve(),
        int((report or {}).get("n", 0) or 0) == expected_n,
        (report or {}).get("production_prompts_refreshed") is True,
        set((report or {}).get("excluded_intents") or ()) == expected_excluded,
        int((report or {}).get("cache_version", 0) or 0) == SEMANTIC_CACHE_VERSION,
        (report or {}).get("production_prompt_contract_fingerprint")
        == semantic_prompt_fingerprint(),
        (report or {}).get("run_signature") == expected_signature,
    ))
    needs_run = not identity_ok
    if needs_run:
        command = [
            SFT_PYTHON, "scripts/eval_targeted_semantic.py",
            "--adapter", adapter, "--tag", production_tag, "--max-new-tokens", "1024",
            "--refresh-production-prompts",
        ]
        if exclude_intent:
            command += ["--exclude-intent", exclude_intent]
        subprocess.run(command, cwd=HERE, check=True)
    report = json.loads(path.read_text(encoding="utf-8"))
    if not report.get("production_prompts_refreshed"):
        raise RuntimeError(f"语义评测未使用生产提示词：{path}")
    if not report.get("run_signature"):
        raise RuntimeError(f"语义评测缺少权重/数据运行签名：{path}")
    return report


def _weighted_metrics(groups: dict[str, dict]) -> dict:
    """Aggregate already-disjoint semantic groups without rounding bias."""
    n = sum(int(metrics.get("n", 0)) for metrics in groups.values())
    result = {"n": n}
    for key in ("json_valid_rate", "keys_complete_rate", "task_accuracy"):
        result[key] = round(
            sum(int(metrics.get("n", 0)) * float(metrics.get(key, 0.0))
                for metrics in groups.values()) / max(1, n),
            2,
        )
    return result


def _weighted_structure_metrics(groups: dict[str, dict]) -> dict:
    n = sum(int(metrics.get("n", 0)) for metrics in groups.values())
    return {
        "n": n,
        "json_valid_rate": round(sum(
            int(metrics.get("n", 0)) * float(metrics.get("json_valid_rate", 0.0))
            for metrics in groups.values()) / max(1, n), 2),
        "keys_complete_rate": round(sum(
            int(metrics.get("n", 0)) * float(metrics.get("keys_complete_rate", 0.0))
            for metrics in groups.values()) / max(1, n), 2),
    }


def _weighted_diagnose_metrics(report: dict) -> dict:
    """Aggregate diagnosis regression metrics from a mixed-intent old-val report."""
    groups = {
        group: metrics for group, metrics in (report.get("by_intent") or {}).items()
        if group.rsplit("/", 1)[-1] == "diagnose"
    }
    n = sum(int(metrics.get("n", 0)) for metrics in groups.values())
    result = {"n": n}
    for key in ("json_valid_rate", "keys_complete_rate",
                "first_wrong_step_acc", "error_tag_consistency"):
        result[key] = round(
            sum(int(metrics.get("n", 0)) * float(metrics.get(key, 0.0))
                for metrics in groups.values()) / max(1, n),
            2,
        )
    return result


def _without_intent(groups: dict[str, dict], intent: str) -> dict[str, dict]:
    return {group: metrics for group, metrics in groups.items()
            if group.rsplit("/", 1)[-1] != intent}


def _qa_deployment_checks(report: dict) -> dict[str, bool]:
    overall = report.get("overall") or {}
    adapter = Path(str(report.get("adapter") or ""))
    data = Path(str(report.get("data") or ""))
    adapter_path = adapter if adapter.is_absolute() else HERE / adapter
    data_path = data if data.is_absolute() else HERE / data
    expected_signature = _semantic_signature(
        QA_SPECIALIST_ADAPTER, QA_EVAL_DATA,
        max_seq=4096, max_new=1024,
        refresh_production_prompts=True, excluded_intents=set(),
    )
    return {
        "production_prompts_refreshed": report.get("production_prompts_refreshed") is True,
        "adapter_matches": adapter_path.resolve() == (HERE / QA_SPECIALIST_ADAPTER).resolve(),
        "frozen_eval_data_matches": data_path.resolve() == QA_EVAL_DATA.resolve(),
        "frozen_qa_n_50": int(report.get("n", overall.get("n", 0)) or 0) == 50,
        "run_signature_matches_current": report.get("run_signature") == expected_signature,
        "cache_signature_current": (
            int(report.get("cache_version", 0) or 0) == SEMANTIC_CACHE_VERSION
        ),
        "production_prompt_contract_matches": (
            report.get("production_prompt_contract_fingerprint")
            == semantic_prompt_fingerprint()
        ),
        "target_accuracy_90": float(overall.get("task_accuracy", 0.0)) >= 90.0,
        "json_valid_95": float(overall.get("json_valid_rate", 0.0)) >= 95.0,
        "keys_complete_95": float(overall.get("keys_complete_rate", 0.0)) >= 95.0,
    }


def _ensure_qa_production_report() -> dict | None:
    """Evaluate and gate the specialist with the exact production QA contract."""
    if not QA_GATE.exists() or not QA_EVAL_DATA.exists() or not _adapter_complete(QA_SPECIALIST_ADAPTER):
        return None
    gate = json.loads(QA_GATE.read_text(encoding="utf-8"))
    if not gate.get("passed"):
        return None
    report = json.loads(QA_REPORT.read_text(encoding="utf-8")) if QA_REPORT.exists() else None
    identity_checks = _qa_deployment_checks(report) if report else {}
    if not report or not all(identity_checks.get(key, False) for key in (
        "production_prompts_refreshed", "adapter_matches",
        "frozen_eval_data_matches", "frozen_qa_n_50", "run_signature_matches_current",
        "cache_signature_current", "production_prompt_contract_matches",
    )):
        _run([
            SFT_PYTHON, "scripts/eval_targeted_semantic.py",
            "--adapter", QA_SPECIALIST_ADAPTER,
            "--data", str(QA_EVAL_DATA.relative_to(HERE)),
            "--tag", "qa_probe_rescue-prodprompt",
            "--max-new-tokens", "1024", "--refresh-production-prompts",
        ])
        report = json.loads(QA_REPORT.read_text(encoding="utf-8"))
    checks = _qa_deployment_checks(report)
    overall = report.get("overall") or {}
    gate.update({
        "deployment_passed": all(checks.values()),
        "deployment_checks": checks,
        "production_prompts_refreshed": report.get("production_prompts_refreshed") is True,
        "production_report": str(QA_REPORT.relative_to(HERE)),
        "task_accuracy": float(overall.get("task_accuracy", 0.0)),
        "json_valid_rate": float(overall.get("json_valid_rate", 0.0)),
        "keys_complete_rate": float(overall.get("keys_complete_rate", 0.0)),
        "action": (
            "deploy production-verified grounded_qa specialist"
            if all(checks.values())
            else "withhold grounded_qa specialist; use selected default adapter"
        ),
    })
    QA_GATE.write_text(json.dumps(gate, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    try:
        status = json.loads(OPTIMIZATION_STATUS.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        status = {}
    status.update({
        "qa_specialist_production_revalidation": (
            "passed" if gate["deployment_passed"] else "failed"
        ),
        "qa_specialist_temporarily_withheld": not gate["deployment_passed"],
        "qa_specialist_production_report": str(QA_REPORT.relative_to(HERE)),
        "updated_at": time.strftime("%F %T"),
    })
    OPTIMIZATION_STATUS.write_text(
        json.dumps(status, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return report if gate["deployment_passed"] else None


def _qa_specialist_groups() -> dict[str, dict] | None:
    """Return the evaluated production QA route, never an unverified adapter."""
    report = _ensure_qa_production_report()
    if not report:
        return None
    groups = {
        group: metrics for group, metrics in (report.get("by_group") or {}).items()
        if group.rsplit("/", 1)[-1] == "grounded_qa"
    }
    return groups or None


def _deployed_semantic_groups(default_groups: dict[str, dict],
                              qa_groups: dict[str, dict] | None) -> dict[str, dict]:
    """Mirror build_runtime_routes: replace QA with the passed specialist route."""
    if not qa_groups:
        return dict(default_groups)
    return {**_without_intent(default_groups, "grounded_qa"), **qa_groups}


def _replace_intent(groups: dict[str, dict], intent: str,
                    replacement: dict[str, dict] | None) -> dict[str, dict]:
    if not replacement:
        return dict(groups)
    return {**_without_intent(groups, intent), **replacement}


def _diagnose_fallback_groups() -> dict[str, dict] | None:
    if not DIAGNOSE_GATE.exists() or not DIAGNOSE_FALLBACK_REPORT.exists():
        return None
    gate = json.loads(DIAGNOSE_GATE.read_text(encoding="utf-8"))
    adapter = str(gate.get("fallback_adapter") or "")
    if not (gate.get("fallback_passed") is True and adapter and _adapter_complete(adapter)):
        return None
    report = json.loads(DIAGNOSE_FALLBACK_REPORT.read_text(encoding="utf-8"))
    groups = {
        group: metrics for group, metrics in (report.get("by_group") or {}).items()
        if group.rsplit("/", 1)[-1] == "diagnose"
    }
    return groups or None


def _candidate_result(*, old_report: str, targeted_report: str,
                      semantic_tag: str, adapter: str, attempt: str) -> dict:
    old = _ensure_old_production_report()
    targeted_base = _read("report_lora_targeted_baseline.json")
    candidate_old = _read(old_report)
    candidate_report_checks = _standalone_report_checks(candidate_old, adapter)
    if not all(candidate_report_checks.values()):
        raise RuntimeError(
            f"候选旧集报告身份或生产提示不匹配：{old_report} {candidate_report_checks}"
        )
    candidate_targeted = _read_optional(targeted_report)
    qa_groups = _qa_specialist_groups()
    excluded = "grounded_qa" if qa_groups else None
    semantic_base = _ensure_semantic("targeted_baseline", OLD_ADAPTER, excluded)
    semantic_candidate = _ensure_semantic(semantic_tag, adapter, excluded)
    old_overall = old["overall"]
    candidate_old_overall = candidate_old["overall"]
    old_diagnose = _weighted_diagnose_metrics(old)
    candidate_old_diagnose = _weighted_diagnose_metrics(candidate_old)
    base_targeted = targeted_base["overall"]
    new_targeted = candidate_targeted["overall"] if candidate_targeted else None
    semantic_base_raw_groups = semantic_base.get("by_group", {})
    semantic_new_raw_groups = semantic_candidate.get("by_group", {})
    diagnose_fallback_groups = _diagnose_fallback_groups()
    semantic_base_groups = _replace_intent(
        _deployed_semantic_groups(semantic_base_raw_groups, qa_groups),
        "diagnose", diagnose_fallback_groups,
    )
    semantic_new_groups = _deployed_semantic_groups(semantic_new_raw_groups, qa_groups)
    semantic_base_overall = _weighted_metrics(semantic_base_groups)
    semantic_new_overall = _weighted_metrics(semantic_new_groups)
    checks = {
        "candidate_old_report_identity": all(candidate_report_checks.values()),
        "old_json_drop_le_1pp": candidate_old_overall["json_valid_rate"] >= old_overall["json_valid_rate"] - 1.0,
        "old_keys_drop_le_1pp": candidate_old_overall["keys_complete_rate"] >= old_overall["keys_complete_rate"] - 1.0,
        "old_diagnose_same_population": (
            old_diagnose["n"] == candidate_old_diagnose["n"] == 153
        ),
        "old_diagnose_production_prompts_refreshed": bool(
            old.get("production_prompts_refreshed")
            and candidate_old.get("production_prompts_refreshed")
        ),
        "old_diagnose_step_drop_le_2pp": (
            candidate_old_diagnose["first_wrong_step_acc"]
            >= old_diagnose["first_wrong_step_acc"] - 2.0
        ),
        "old_diagnose_tag_drop_le_2pp": (
            candidate_old_diagnose["error_tag_consistency"]
            >= old_diagnose["error_tag_consistency"] - 2.0
        ),
        # targeted 的结构门禁和语义门禁使用同一次生成，并按实际生产路由
        # 组合；旧 test_lora 报告保留作历史参考，不能混用旧意图识别器。
        "targeted_json_absolute_95": semantic_new_overall["json_valid_rate"] >= 95.0,
        "targeted_keys_absolute_95": semantic_new_overall["keys_complete_rate"] >= 95.0,
        "targeted_json_not_lower": (
            semantic_new_overall["json_valid_rate"] >= semantic_base_overall["json_valid_rate"]
        ),
        "targeted_keys_gain_or_ceiling": (
            semantic_new_overall["keys_complete_rate"]
            >= semantic_base_overall["keys_complete_rate"] + 2.0
            or semantic_new_overall["keys_complete_rate"] >= 98.0
        ),
        "semantic_absolute_90": semantic_new_overall["task_accuracy"] >= 90.0,
        "semantic_gain_or_ceiling": (
            semantic_new_overall["task_accuracy"] >= semantic_base_overall["task_accuracy"] + 2.0
            or semantic_new_overall["task_accuracy"] >= 97.0
        ),
        "semantic_no_group_drop_gt_2pp": all(
            semantic_new_groups.get(group, {}).get("task_accuracy", 0)
            >= metrics.get("task_accuracy", 0) - 2.0
            for group, metrics in semantic_base_groups.items()
        ),
    }
    passed = all(checks.values())
    return {
        "created_at": time.strftime("%F %T"), "attempt": attempt,
        "checks": checks, "passed": passed,
        "old_sft_on_old_val": old_overall,
        "old_sft_old_val_report": "report_lora_408-old-prodprompt-regression.json",
        "candidate_on_old_val": candidate_old_overall,
        "candidate_old_report_checks": candidate_report_checks,
        "old_sft_diagnose_on_old_val": old_diagnose,
        "candidate_diagnose_on_old_val": candidate_old_diagnose,
        "old_sft_on_targeted_val": base_targeted,
        "candidate_on_targeted_val": new_targeted,
        "candidate_on_targeted_val_note": (
            "legacy structural report retained" if new_targeted
            else "omitted: production semantic evaluation already includes structure"
        ),
        "old_sft_targeted_semantic": semantic_base_overall,
        "candidate_targeted_semantic": semantic_new_overall,
        "deployed_targeted_structure": {
            "baseline": {
                "n": semantic_base_overall["n"],
                "json_valid_rate": semantic_base_overall["json_valid_rate"],
                "keys_complete_rate": semantic_base_overall["keys_complete_rate"],
            },
            "candidate": {
                "n": semantic_new_overall["n"],
                "json_valid_rate": semantic_new_overall["json_valid_rate"],
                "keys_complete_rate": semantic_new_overall["keys_complete_rate"],
            },
        },
        "targeted_semantic_by_group": {
            "baseline": semantic_base_groups, "candidate": semantic_new_groups,
        },
        "raw_default_adapter_targeted_semantic_by_group": {
            "baseline": semantic_base_raw_groups, "candidate": semantic_new_raw_groups,
        },
        "deployment_route_evaluated": {
            "baseline_default_adapter": OLD_ADAPTER,
            "candidate_default_adapter": adapter,
            "grounded_qa_adapter": QA_SPECIALIST_ADAPTER if qa_groups else adapter,
            "qa_specialist_applied": bool(qa_groups),
            "baseline_diagnose_adapter": (
                "model/loras/courseloop-408-diagnose-taxonomy-rescue"
                if diagnose_fallback_groups else OLD_ADAPTER
            ),
            "diagnose_fallback_applied_to_baseline": bool(diagnose_fallback_groups),
        },
        "candidate_adapter": adapter,
        "selected_adapter": adapter if passed else OLD_ADAPTER,
        "reason": f"{attempt}_gate_passed" if passed else f"{attempt}_gate_failed",
    }


def _write(result: dict) -> None:
    OUT.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    # 每次选优落盘后同步生成可部署任务路由；若统一 LoRA 未过门，已通过的
    # 专项 diagnose/QA LoRA 仍可在同一基座上按请求切换。
    from scripts.build_runtime_routes import build as build_runtime_routes
    build_runtime_routes(HERE)
    _write_final_report(result)
    print(json.dumps(result, ensure_ascii=False, indent=2), flush=True)


def _metric_line(label: str, metrics: dict | None) -> str:
    metrics = metrics or {}
    fields = []
    for title, key in (
        ("样本", "n"), ("JSON", "json_valid_rate"),
        ("字段完整", "keys_complete_rate"), ("任务准确", "task_accuracy"),
    ):
        if key in metrics:
            value = metrics[key]
            fields.append(f"{title} {value}{'' if key == 'n' else '%'}")
    return f"- {label}：" + ("；".join(fields) if fields else "无数据")


def _write_final_report(result: dict) -> None:
    """Write a human-readable deployment decision beside the JSON evidence."""
    checks = result.get("checks") or {}
    failed = [name for name, passed in checks.items() if not passed]
    attempts = result.get("attempts") or {}
    lines = [
        "# CourseLoop 增量 SFT 最终选优",
        "",
        f"- 生成时间：{result.get('created_at', time.strftime('%F %T'))}",
        f"- 最终选择：`{result.get('selected_adapter', OLD_ADAPTER)}`",
        f"- 选择原因：`{result.get('reason', 'unknown')}`",
        f"- 当前轮次：`{result.get('attempt', 'unknown')}`",
        f"- 门禁结果：{'通过' if result.get('passed') else '未通过，保留已验证旧模型'}",
        "",
        "## 核心指标",
        "",
        _metric_line("旧 SFT / 旧验证集", result.get("old_sft_on_old_val")),
        _metric_line("候选模型 / 旧验证集", result.get("candidate_on_old_val")),
        _metric_line("旧生产路由 / 外部目标集", result.get("old_sft_targeted_semantic")),
        _metric_line("候选生产路由 / 外部目标集", result.get("candidate_targeted_semantic")),
        "",
        "## 门禁",
        "",
    ]
    lines.extend(f"- [{'x' if passed else ' '}] `{name}`" for name, passed in checks.items())
    if failed:
        lines += ["", "未通过项：" + "、".join(f"`{name}`" for name in failed)]
    if attempts:
        lines += ["", "## 自动救援", ""]
        for name, attempt in attempts.items():
            lines.append(
                f"- `{name}`：{'通过' if attempt.get('passed') else '未通过'}；"
                f"选择 `{attempt.get('selected_adapter', OLD_ADAPTER)}`"
            )
    lines += [
        "",
        "## 证据文件",
        "",
        "- `data/eval/synth3000_selection.json`",
        "- `data/eval/runtime_routes.json`",
        "- `data/eval/report_lora_408-old-prodprompt-regression.json`",
        "- `data/eval/report_lora_408-synth3000-old.json`",
        "- `data/eval/report_targeted_semantic_408-synth3000-targeted-prodprompt.json`",
        "",
        "> 本文档由选优流水线自动覆盖生成；实际部署以 JSON 路由文件为准。",
        "",
    ]
    FINAL_REPORT.parent.mkdir(parents=True, exist_ok=True)
    FINAL_REPORT.write_text("\n".join(lines), encoding="utf-8")


def _write_optimization_status(stage: str, result: dict) -> None:
    try:
        status = json.loads(OPTIMIZATION_STATUS.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        status = {}
    status.update({
        "stage": stage,
        "updated_at": time.strftime("%F %T"),
        "selection_report": str(OUT.relative_to(HERE)),
        "selected_adapter": result.get("selected_adapter"),
        "selection_reason": result.get("reason"),
    })
    OPTIMIZATION_STATUS.write_text(
        json.dumps(status, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _run(command: list[str]) -> None:
    print("[incremental-rescue] RUN:", " ".join(command), flush=True)
    subprocess.run(command, cwd=HERE, check=True)


def _adapter_complete(relative: str) -> bool:
    path = HERE / relative
    return (path / "adapter_config.json").exists() and (path / "adapter_model.safetensors").exists()


def _run_rescue() -> dict:
    rescue_data = HERE / "data" / "sft_incremental_3000_rescue"
    if not (rescue_data / "report.json").exists():
        _run([RAG_PYTHON, "scripts/prepare_rescue_sft.py"])
    if not _adapter_complete(RESCUE_ADAPTER):
        _run([
            SFT_PYTHON, "scripts/train_sft.py",
            "--adapter", PRIMARY_ADAPTER,
            "--train", "data/sft_incremental_3000_rescue/train.jsonl",
            "--val", "data/sft_incremental_3000/val_combined.jsonl",
            "--out", RESCUE_ADAPTER,
            "--checkpoint-dir", "model/_sft_synth3000_rescue_ckpt",
            "--epochs", "1", "--lr", "2e-5", "--eval-strategy", "epoch",
            "--resume", "auto", "--no-test",
        ])
    rescue_old = _read_optional("report_lora_408-synth3000-rescue-old.json")
    rescue_old_checks = _standalone_report_checks(
        rescue_old or {}, RESCUE_ADAPTER,
    )
    if not rescue_old or not all(rescue_old_checks.values()):
        _run([
            SFT_PYTHON, "scripts/test_lora.py", "--adapter", RESCUE_ADAPTER,
            "--val", "data/sft/val.jsonl", "--tag", "408-synth3000-rescue-old",
            "--max-new-tokens", "768", "--refresh-production-prompts",
        ])
    return _candidate_result(
        old_report="report_lora_408-synth3000-rescue-old.json",
        targeted_report="report_lora_408-synth3000-rescue-targeted.json",
        semantic_tag="408-synth3000-rescue-targeted",
        adapter=RESCUE_ADAPTER,
        attempt="rescue",
    )


def main() -> None:
    primary = _candidate_result(
        old_report="report_lora_408-synth3000-old.json",
        targeted_report="report_lora_408-synth3000-targeted.json",
        semantic_tag="408-synth3000-targeted",
        adapter=PRIMARY_ADAPTER,
        attempt="primary",
    )
    _write(primary)
    if primary["passed"]:
        _write_optimization_status("incremental_selection_complete", primary)
        return

    _write_optimization_status("incremental_rescue_running", primary)

    # 首轮结果先落盘，prepare_rescue_sft 只读取失败维度，不读取 targeted 样本。
    try:
        rescue = _run_rescue()
    except Exception as exc:  # noqa: BLE001
        result = dict(primary)
        result.update(
            selected_adapter=OLD_ADAPTER,
            reason="primary_failed_rescue_error_keep_old_sft",
            rescue_error=f"{type(exc).__name__}: {str(exc)[:1000]}",
            attempts={"primary": primary},
        )
        _write(result)
        _write_optimization_status("incremental_selection_complete", result)
        return

    final = dict(rescue)
    final["attempts"] = {"primary": primary, "rescue": rescue}
    if not rescue["passed"]:
        final["selected_adapter"] = OLD_ADAPTER
        final["reason"] = "primary_and_rescue_failed_keep_old_sft"
    _write(final)
    _write_optimization_status("incremental_selection_complete", final)


if __name__ == "__main__":
    main()
