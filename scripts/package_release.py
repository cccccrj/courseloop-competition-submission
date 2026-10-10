"""打发布包：代码包 + 数据包，都带版本号与校验值（docs/29 §1、§3）。

为什么要有这个脚本：原先部署靠文档里手写的几条 tar 命令，打出来的包说不清是哪一版代码、
前端是不是按这一版构建的、数据库是不是在写入中途拷的。三件事各出一次错，现场就是
"线上看到的和仓库不是一版"（docs/34 §2.1）。

产物（默认放在 release/<日期>-<提交号>/，该目录不进版本控制）：

    courseloop-<commit>.tar.gz          代码：git archive HEAD（只含已提交内容）
                                        + frontend/dist（已构建）+ BUILD_COMMIT
    courseloop-data-<commit>.tar.gz     数据：业务库在线快照（已 integrity_check）
                                        + .secret_key + rag_fts.db + withdrawn/
                                        [+ raw/，加 --with-raw]
    SHA256SUMS                          两个包的校验值
    MANIFEST.txt                        版本、构建时间、各文件大小、库里的关键计数

用法：
    python scripts/package_release.py                    # 构建前端并打包（不带课件原件）
    python scripts/package_release.py --with-raw         # 连课件原件一起（约 +560MB）
    python scripts/package_release.py --skip-build       # 前端已按当前提交构建过
    python scripts/package_release.py --out /tmp/rel     # 指定输出目录

闸门：工作区有未提交改动、或前端产物的版本戳和 HEAD 对不上，直接拒绝。
确实要打临时包时加 --allow-dirty，包名会带上 -dirty 后缀，免得被当成正式包发出去。
"""
from __future__ import annotations

import argparse
import gzip
import hashlib
import io
import json
import shutil
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DIST = ROOT / "frontend" / "dist"
DATA = ROOT / "data"


def _git(*args: str) -> str:
    return subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True,
                          check=True).stdout.strip()


def _die(msg: str) -> None:
    print(f"✗ {msg}", file=sys.stderr)
    sys.exit(1)


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _snapshot_sqlite(src: Path, dst: Path) -> None:
    """在线 backup API 拍快照，拷完立刻 integrity_check（与 backup_userdb.py 同一做法）。"""
    with sqlite3.connect(src) as s, sqlite3.connect(dst) as d:
        s.backup(d)
    with sqlite3.connect(dst) as d:
        ok = d.execute("PRAGMA integrity_check").fetchone()[0]
    if ok != "ok":
        _die(f"{src.name} 快照完整性检查失败：{ok}")


def build_code_package(out: Path, commit: str, tag: str) -> Path:
    prefix = f"courseloop-{commit}/"
    raw_tar = out / f".code-{commit}.tar"
    subprocess.run(["git", "archive", "--format=tar", f"--prefix={prefix}", "-o", str(raw_tar), "HEAD"],
                   cwd=ROOT, check=True)
    with tarfile.open(raw_tar, "a") as t:
        t.add(DIST, arcname=prefix + "frontend/dist")
        info = tarfile.TarInfo(prefix + "BUILD_COMMIT")
        payload = (commit + "\n").encode()
        info.size = len(payload)
        info.mtime = int(datetime.now().timestamp())
        t.addfile(info, io.BytesIO(payload))
    pkg = out / f"courseloop-{tag}.tar.gz"
    # git archive 出的 tar 追加完 dist 之后整体 gzip，不逐个成员重写
    with raw_tar.open("rb") as fin, gzip.open(pkg, "wb", compresslevel=6) as fout:
        shutil.copyfileobj(fin, fout)
    raw_tar.unlink()
    return pkg


def build_data_package(out: Path, tag: str, with_raw: bool) -> tuple[Path, dict]:
    pkg = out / f"courseloop-data-{tag}.tar.gz"
    stats: dict = {}
    with tempfile.TemporaryDirectory(prefix="cl-pack-") as tmp, tarfile.open(pkg, "w:gz") as t:
        tmp = Path(tmp)
        db = DATA / "processed" / "courseloop.db"
        if not db.is_file():
            _die(f"业务库不存在：{db}")
        snap = tmp / "courseloop.db"
        _snapshot_sqlite(db, snap)
        with sqlite3.connect(snap) as c:
            for table in ("users", "problems", "knowledge_points", "practice_attempts", "error_records"):
                stats[table] = c.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            stats["showcase_users"] = c.execute(
                "SELECT COUNT(*) FROM users WHERE username LIKE 'showcase_%'").fetchone()[0]
        t.add(snap, arcname="data/processed/courseloop.db")

        rag = DATA / "processed" / "rag_fts.db"
        if rag.is_file():
            rsnap = tmp / "rag_fts.db"
            _snapshot_sqlite(rag, rsnap)
            t.add(rsnap, arcname="data/processed/rag_fts.db")
        else:
            print("! 没有 rag_fts.db：教材检索需要在目标机重建（docs/29 §4.3）")

        key = DATA / "processed" / ".secret_key"
        if key.is_file():
            t.add(key, arcname="data/processed/.secret_key")
        for sub in ("withdrawn",) + (("raw",) if with_raw else ()):
            p = DATA / sub
            if p.is_dir():
                t.add(p, arcname=f"data/{sub}")
    return pkg, stats


def main() -> None:
    ap = argparse.ArgumentParser(description="打 CourseLoop 发布包（代码 + 数据）")
    ap.add_argument("--out", default="", help="输出目录，默认 release/<日期>-<提交号>/")
    ap.add_argument("--with-raw", action="store_true", help="数据包带上课件原件 data/raw（约 +560MB）")
    ap.add_argument("--skip-build", action="store_true", help="不重新构建前端（产物须已按当前提交构建）")
    ap.add_argument("--allow-dirty", action="store_true", help="允许工作区有未提交改动（包名带 -dirty）")
    args = ap.parse_args()

    commit = _git("rev-parse", "--short=7", "HEAD")
    dirty = bool(_git("status", "--porcelain", "--untracked-files=no"))
    if dirty and not args.allow_dirty:
        _die("工作区有未提交的改动。先提交，或加 --allow-dirty 打一个临时包。")
    tag = commit + ("-dirty" if dirty else "")

    if not args.skip_build:
        print("… 构建前端（npm run build，先 vue-tsc -b 再 vite build）")
        r = subprocess.run(["npm", "run", "build"], cwd=ROOT / "frontend")
        if r.returncode != 0:
            _die("前端构建失败")
    stamp_file = DIST / "build.json"
    if not stamp_file.is_file():
        _die("frontend/dist/build.json 不存在：前端没按 npm run build 构建（单跑 vite build 不会生成版本戳）")
    stamp = json.loads(stamp_file.read_text(encoding="utf-8"))
    if stamp.get("commit") != commit:
        _die(f"前端产物是按 {stamp.get('commit')} 构建的，当前 HEAD 是 {commit}。去掉 --skip-build 重新打包。")
    if stamp.get("dirty") and not args.allow_dirty:
        _die("前端产物是在有未提交改动时构建的。")

    out = Path(args.out) if args.out else ROOT / "release" / f"{datetime.now():%Y%m%d}-{tag}"
    out.mkdir(parents=True, exist_ok=True)

    print(f"… 打代码包（{commit}）")
    code_pkg = build_code_package(out, commit, tag)
    print("… 打数据包（业务库在线快照 + 完整性检查）")
    data_pkg, stats = build_data_package(out, tag, args.with_raw)

    sums = {p.name: _sha256(p) for p in (code_pkg, data_pkg)}
    (out / "SHA256SUMS").write_text("".join(f"{h}  {n}\n" for n, h in sums.items()), encoding="utf-8")
    manifest = [
        f"commit       {commit}{' (dirty)' if dirty else ''}",
        f"frontend     built_at {stamp.get('built_at')}",
        f"packaged_at  {datetime.now():%Y-%m-%d %H:%M:%S}",
        f"with_raw     {args.with_raw}",
        "",
        *(f"{p.name:<44} {p.stat().st_size / 1e6:8.1f} MB" for p in (code_pkg, data_pkg)),
        "",
        "库内计数：" + "  ".join(f"{k}={v}" for k, v in stats.items()),
    ]
    if stats.get("showcase_users"):
        manifest.append(f"注意：数据包里有 {stats['showcase_users']} 个演示账号（showcase_*）。"
                        "正式上线前按 docs/29 §5 清掉。")
    (out / "MANIFEST.txt").write_text("\n".join(manifest) + "\n", encoding="utf-8")
    print("\n".join(["", f"✓ 已输出到 {out}", *manifest]))


if __name__ == "__main__":
    main()
