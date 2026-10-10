"""Quarantine failed bulk blind-audit rows and prepare one disjoint retry round."""
from __future__ import annotations

import json
import time
from pathlib import Path


HERE = Path(__file__).resolve().parents[1]
BULK = HERE / "data" / "synthetic_bulk_3000"


def _read_json(path: Path, default):
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else default


def repair(bulk_dir: Path = BULK) -> dict:
    audit_path = bulk_dir / "blind_audit.jsonl"
    report_path = bulk_dir / "blind_audit_report.json"
    if not audit_path.exists() or not report_path.exists():
        raise FileNotFoundError("blind audit artifacts are incomplete")
    report = _read_json(report_path, {})
    if report.get("passed"):
        raise RuntimeError("blind audit already passed; repair is not allowed")
    audited = [json.loads(line) for line in audit_path.read_text(encoding="utf-8").splitlines()
               if line.strip()]
    rejected = [row for row in audited if row.get("accepted") is not True]
    if not rejected:
        raise RuntimeError("audit failed without rejected rows; cannot make a targeted repair")

    history_path = bulk_dir / "blind_audit_history.json"
    history = _read_json(history_path, {"rounds": []})
    round_no = len(history.get("rounds", [])) + 1
    accepted = sum(row.get("accepted") is True for row in audited)
    history.setdefault("rounds", []).append({
        "round": round_no,
        "created_at": report.get("created_at"),
        "n": len(audited),
        "accepted": accepted,
        "accept_rate": report.get("accept_rate"),
        "severe_errors": report.get("severe_errors"),
        "rejected_candidate_ids": [str(row.get("candidate_id")) for row in rejected],
    })
    history_path.write_text(json.dumps(history, ensure_ascii=False, indent=2) + "\n",
                            encoding="utf-8")

    rejected_path = bulk_dir / "blind_audit_rejected_ids.json"
    rejected_payload = _read_json(rejected_path, {"candidate_ids": []})
    rejected_ids = set(map(str, rejected_payload.get("candidate_ids", [])))
    rejected_ids.update(str(row.get("candidate_id")) for row in rejected)
    rejected_path.write_text(json.dumps({
        "updated_at": time.strftime("%F %T"),
        "candidate_ids": sorted(rejected_ids),
    }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    excluded_path = bulk_dir / "blind_audit_excluded_ids.json"
    excluded_payload = _read_json(excluded_path, {"candidate_ids": []})
    excluded_ids = set(map(str, excluded_payload.get("candidate_ids", [])))
    excluded_ids.update(str(row.get("candidate_id")) for row in audited)
    excluded_path.write_text(json.dumps({
        "updated_at": time.strftime("%F %T"),
        "candidate_ids": sorted(excluded_ids),
    }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    archive_audit = bulk_dir / f"blind_audit_round_{round_no:02d}.jsonl"
    archive_report = bulk_dir / f"blind_audit_report_round_{round_no:02d}.json"
    audit_path.replace(archive_audit)
    report_path.replace(archive_report)
    result = {
        "round": round_no,
        "audited": len(audited),
        "accepted": accepted,
        "newly_rejected": len(rejected),
        "total_rejected": len(rejected_ids),
        "excluded_from_next_audit": len(excluded_ids),
        "archived_audit": str(archive_audit),
        "archived_report": str(archive_report),
    }
    (bulk_dir / f"blind_audit_repair_round_{round_no:02d}.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return result


if __name__ == "__main__":
    print(json.dumps(repair(), ensure_ascii=False, indent=2))
