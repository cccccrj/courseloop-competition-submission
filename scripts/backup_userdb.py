"""备份 courseloop.db（用户与学习过程数据）。

为什么这个库值得单独备份：
表里混着两类东西——`knowledge_points` / `problems` / `test_cases` / `rubric_items`
是建库产物，删了重跑脚本能重建；而 `users` / `learning_sessions` / `dialog_turns`
/ `practice_attempts` / `error_records` / `mastery_records` 是学生真实用出来的
**生产状态**，一旦丢失不可重现——错因记录与掌握度更是"学习闭环"这个卖点的证据。
两类同处一个文件，所以整库一起备。

用 sqlite3 的在线 backup API 而不是 cp：cp 一个正在被写入的库可能拷到撕裂的中间
状态，恢复时才发现坏了。backup API 走 SQLite 自己的页面级快照，不加写锁，后端照常
读写不受影响。拷完立刻 integrity_check，坏的备份比没有备份更危险——它让人误以为
有退路。

**不含 data/logs/llm_calls.jsonl**：那是 LLM 调用埋点（SFT/分析底座），只增不改且
体量大（开发机上已 55MB）。每 3 小时给它打一份快照，几十份之后就会把盘吃掉，而它
本身是追加日志、丢失最近一段不致命。需要时用 --with-logs 显式带上。

用法：
    python scripts/backup_userdb.py                  # 备份到 data/processed/backups
    python scripts/backup_userdb.py --keep 40        # 只保留最近 40 份
    python scripts/backup_userdb.py --with-logs      # 一并归档 llm_calls.jsonl
"""
from __future__ import annotations

import argparse
import gzip
import shutil
import sqlite3
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend.core.config import settings  # noqa: E402

# 3 小时一次 ＝ 每天 8 份；80 份约等于 10 天的回溯窗口。单份压缩后仅百 KB 级，
# 留长一点是为了覆盖「周末没人看、周一才发现数据出问题」这种情形。
DEFAULT_KEEP = 80

# 这些表是建库产物，可重跑脚本重建；统计时与生产状态分开显示，便于判断快照是否正常
BUILD_TABLES = {"knowledge_points", "problems", "test_cases", "rubric_items",
                "problem_knowledge", "sqlite_sequence"}


def _online_backup(src: Path, dst_gz: Path) -> tuple[bool, str]:
    """在线快照 src 到 dst_gz（gzip 压缩），返回 (完整性是否通过, 说明)。"""
    with tempfile.TemporaryDirectory() as td:
        raw = Path(td) / "snapshot.db"
        with sqlite3.connect(f"file:{src}?mode=ro", uri=True) as s, sqlite3.connect(raw) as d:
            s.backup(d)

        # 校验的是快照本身，不是源库——要确认的是"这份备份能不能用"
        with sqlite3.connect(raw) as chk:
            verdict = chk.execute("PRAGMA integrity_check").fetchone()[0]
            if verdict != "ok":
                return False, verdict
            tables = [r[0] for r in chk.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")]
            live = sum(chk.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                       for t in tables if t not in BUILD_TABLES)

        dst_gz.parent.mkdir(parents=True, exist_ok=True)
        with open(raw, "rb") as fi, gzip.open(dst_gz, "wb", compresslevel=6) as fo:
            shutil.copyfileobj(fi, fo)

    return True, f"{len(tables)} 张表 · 生产状态 {live} 行"


def _copy_gz(src: Path, dst_gz: Path) -> int | None:
    """压缩归档一个 JSONL 追加日志，返回行数；文件不存在则返回 None。"""
    if not src.exists():
        return None
    dst_gz.parent.mkdir(parents=True, exist_ok=True)
    lines = 0
    with open(src, "rb") as fi, gzip.open(dst_gz, "wb", compresslevel=6) as fo:
        for chunk in fi:
            lines += 1
            fo.write(chunk)
    return lines


def _prune(out_dir: Path, stem: str, keep: int) -> int:
    """只保留最近 keep 份，返回删除数量。文件名带 UTC 时间戳，排序即时序。"""
    files = sorted(out_dir.glob(f"{stem}-*.gz"))
    doomed = files[:-keep] if keep > 0 and len(files) > keep else []
    for f in doomed:
        f.unlink()
    return len(doomed)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out", default=None, help="备份目录（默认 库所在目录/backups）")
    ap.add_argument("--keep", type=int, default=DEFAULT_KEEP, help=f"保留份数（默认 {DEFAULT_KEEP}）")
    ap.add_argument("--with-logs", action="store_true", help="一并归档 llm_calls.jsonl（体量大，默认不带）")
    args = ap.parse_args()

    db = Path(settings.db_path)
    if not db.is_absolute():
        db = Path(__file__).resolve().parents[1] / db
    if not db.exists():
        print(f"✗ 库不存在: {db}", file=sys.stderr)
        return 1

    out_dir = Path(args.out) if args.out else db.parent / "backups"
    # 用 UTC：跨时区/夏令时都能靠文件名排序还原真实先后顺序
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")

    print(f"▶ 备份 {db}")
    ok, note = _online_backup(db, out_dir / f"courseloop-{ts}.db.gz")
    if not ok:
        print(f"✗ 备份完整性校验未通过，已丢弃: {note}", file=sys.stderr)
        return 2
    size = (out_dir / f"courseloop-{ts}.db.gz").stat().st_size
    print(f"  ✓ courseloop-{ts}.db.gz  {size / 1024:.0f} KB  ({note})")

    if args.with_logs:
        n = _copy_gz(Path(settings.logs_dir) / "llm_calls.jsonl", out_dir / f"llm_calls-{ts}.jsonl.gz")
        print(f"  ✓ llm_calls-{ts}.jsonl.gz  {n} 行" if n is not None else "  · llm_calls.jsonl 不存在，跳过")

    removed = sum(_prune(out_dir, s, args.keep) for s in ("courseloop", "llm_calls"))
    total = sum(f.stat().st_size for f in out_dir.glob("*.gz"))
    kept = len(list(out_dir.glob("courseloop-*.db.gz")))
    print(f"▶ 轮转：删除 {removed} 份过期，现存 {kept} 个时间点，共 {total / 1024 / 1024:.1f} MB")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
