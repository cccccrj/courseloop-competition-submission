import json

from scripts import analyze_llm_calls as analyze


def _rec(intent, *, parsed=None, passed=None, raw="{}", model="teacher", ts="2026-08-25 10:00:00"):
    verification = None if passed is None else {"method": "unit_test", "passed": passed}
    return {"ts": ts, "intent": intent, "model": model, "messages": [], "raw_output": raw,
            "parsed": parsed, "verification": verification, "passed": passed, "revised": False}


def _write(tmp_path, records, extra_lines=()):
    path = tmp_path / "llm_calls.jsonl"
    lines = [json.dumps(r, ensure_ascii=False) for r in records] + list(extra_lines)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return str(path)


def test_free_text_intents_have_no_parse_failure_rate(tmp_path):
    """grounded_qa 的 parsed 恒为 None。算进解析失败率会凭空报出 100% 失败。"""
    path = _write(tmp_path, [
        _rec("grounded_qa", raw="链表是……"),
        _rec("grounded_qa", raw="进程是……"),
        _rec("solve", parsed={"steps": []}),
        _rec("solve", parsed={"steps": []}),
        _rec("solve", parsed=None),          # 这条才是真的解析失败
    ])
    rows, stat = analyze.load(path)
    summary = analyze.summarize(rows)

    assert stat["bad_lines"] == 0 and len(rows) == 5
    assert analyze.parse_fail_rate(summary["by_intent"]["grounded_qa"]) is None
    assert analyze.parse_fail_rate(summary["by_intent"]["solve"]) == 33.3
    # 合计的分母同样只含 JSON 任务：3 条 solve，不是 5 条
    assert analyze.parse_fail_rate(summary["totals"]) == 33.3


def test_unjudgeable_verification_is_not_counted_as_failure(tmp_path):
    """三态纪律：None 是证据不足，不折成 False（与 verification/、grpo_rewards 一致）。"""
    path = _write(tmp_path, [
        _rec("diagnose", parsed={"error_tags": []}, passed=True),
        _rec("diagnose", parsed={"error_tags": []}, passed=False),
        _rec("diagnose", parsed={"error_tags": []}, passed=None),
        _rec("diagnose", parsed={"error_tags": []}, passed=None),
    ])
    bucket = analyze.summarize(analyze.load(path)[0])["by_intent"]["diagnose"]

    assert (bucket["passed"], bucket["failed"], bucket["unknown"]) == (1, 1, 2)
    assert analyze.pass_rate(bucket) == 50.0          # 分母是 2，不是 4


def test_pass_rate_is_none_when_nothing_is_judgeable(tmp_path):
    path = _write(tmp_path, [_rec("practice", parsed={"stem": "x"}, passed=None)])
    bucket = analyze.summarize(analyze.load(path)[0])["by_intent"]["practice"]
    assert analyze.pass_rate(bucket) is None
    assert bucket["sft_ready"] == 0


def test_sft_candidates_need_both_parsed_and_passed(tmp_path):
    path = _write(tmp_path, [
        _rec("solve", parsed={"steps": []}, passed=True),      # 计入
        _rec("solve", parsed=None, passed=True),               # 解析失败，不计
        _rec("solve", parsed={"steps": []}, passed=False),     # 验证未过，不计
        _rec("solve", parsed={"steps": []}, passed=None),      # 未判定，不计
    ])
    assert analyze.summarize(analyze.load(path)[0])["totals"]["sft_ready"] == 1


def test_broken_lines_are_skipped_and_counted(tmp_path):
    """埋点写入是尽力而为的，进程被 kill 时最后一行可能只写了一半。"""
    path = _write(tmp_path, [_rec("solve", parsed={"steps": []})],
                  extra_lines=['{"ts": "2026-08-25 10:00:01", "intent": "sol',  # 截断
                               '"不是对象"'])
    rows, stat = analyze.load(path)
    assert len(rows) == 1 and stat["bad_lines"] == 2 and stat["lines"] == 3
    report = "\n".join(analyze.format_report(analyze.summarize(rows), path, stat))
    assert "坏行 2 已跳过" in report


def test_since_filter_excludes_records_without_timestamp(tmp_path):
    path = _write(tmp_path, [
        _rec("solve", parsed={}, ts="2026-08-19 23:59:59"),
        _rec("solve", parsed={}, ts="2026-08-20 00:00:01"),
        _rec("solve", parsed={}, ts=""),          # 证不出在窗口内，就不算进窗口
    ])
    rows, stat = analyze.load(path, since="2026-08-20")
    assert len(rows) == 1 and stat["skip_before_since"] == 1 and stat["skip_no_ts"] == 1


def test_intent_filter_and_model_breakdown(tmp_path):
    path = _write(tmp_path, [
        _rec("solve", parsed={}, passed=True, model="deepseek-v3"),
        _rec("solve", parsed=None, passed=None, model="courseloop-408"),
        _rec("diagnose", parsed={}, passed=True, model="deepseek-v3"),
    ])
    rows, stat = analyze.load(path, intent="solve")
    summary = analyze.summarize(rows)
    assert stat["skip_other_intent"] == 1
    assert set(summary["by_model"]) == {"deepseek-v3", "courseloop-408"}
    assert analyze.parse_fail_rate(summary["by_model"]["courseloop-408"]) == 100.0


def test_new_json_intent_is_picked_up_without_being_registered(tmp_path):
    """后加的 JSON 任务若一次都没解析成功，恰恰最该被看见 —— 不能一直显示留白。"""
    path = _write(tmp_path, [
        _rec("exam_review", parsed={"score": 1}),
        _rec("exam_review", parsed=None),
    ])
    summary = analyze.summarize(analyze.load(path)[0])
    assert analyze.parse_fail_rate(summary["by_intent"]["exam_review"]) == 50.0
    assert summary["unregistered_intents"] == ["exam_review"]


def test_empty_output_is_counted(tmp_path):
    """/chat/stream 调用炸掉时，累计到的 answer 是空字符串，但照样会埋点。"""
    path = _write(tmp_path, [_rec("grounded_qa", raw=""), _rec("grounded_qa", raw="  "),
                             _rec("grounded_qa", raw="正常回答")])
    assert analyze.summarize(analyze.load(path)[0])["by_intent"]["grounded_qa"]["empty_output"] == 2


def test_report_and_json_summary_agree(tmp_path):
    path = _write(tmp_path, [_rec("solve", parsed={}, passed=True),
                             _rec("grounded_qa", raw="文本", passed=True)])
    rows, stat = analyze.load(path)
    summary = analyze.summarize(rows)
    report = "\n".join(analyze.format_report(summary, path, stat))
    assert "耗时未埋点" in report and "蒸馏候选 1 条" in report

    blob = analyze.to_json(summary, path, stat)
    assert blob["counted"] == 2 and blob["totals"]["sft_ready"] == 1
    assert blob["by_intent"]["grounded_qa"]["parse_fail_rate"] is None
    assert blob["by_intent"]["solve"]["models"] == {"teacher": 1}


def test_missing_log_file_reports_instead_of_crashing(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr("sys.argv", ["analyze_llm_calls", "--file", str(tmp_path / "nope.jsonl")])
    assert analyze.main() == 1
    assert "找不到" in capsys.readouterr().out


def test_main_writes_json_summary(tmp_path, monkeypatch, capsys):
    path = _write(tmp_path, [_rec("solve", parsed={}, passed=True)])
    out = tmp_path / "eval" / "summary.json"
    monkeypatch.setattr("sys.argv", ["analyze_llm_calls", "--file", path, "--out", str(out)])
    assert analyze.main() == 0
    capsys.readouterr()
    assert json.loads(out.read_text(encoding="utf-8"))["totals"]["n"] == 1
