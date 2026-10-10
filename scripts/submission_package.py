"""从冻结源码及经核验的处理后数据或原创演示库构建下载运行包。"""
from __future__ import annotations
import hashlib
import argparse
import re
import json
import sqlite3
import subprocess
import tarfile
import tempfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", default="v1.0")
    parser.add_argument("--processed-dir", type=Path, help="已脱敏并创建虚构身份的处理后数据库目录")
    parser.add_argument("--media-dir", type=Path, help="已确认的最终PPT和视频；按media-manifest.json核验后原样复制")
    parser.add_argument("--evidence-version", default="v1.0", help="正式交付实测证据目录")
    args = parser.parse_args()
    media = json.loads((ROOT / "docs/submission/media-manifest.json").read_text()) if args.media_dir else None
    if media:
        for item in media["assets"]:
            name = item["filename"]
            assert Path(name).name == name and item["content_unchanged"] and item["owner_confirmed_final"]
            source = args.media_dir / name
            assert source.stat().st_size == item["bytes"]
            assert hashlib.sha256(source.read_bytes()).hexdigest() == item["sha256"]
    if not re.fullmatch(r"v\d+\.\d+(?:\.\d+)?", args.version):
        raise SystemExit("版本格式须为vX.Y或vX.Y.Z")
    output = ROOT / "artifacts/release" / args.version
    if output.exists() and list(output.iterdir()):
        raise SystemExit("该版本已有产物，拒绝覆盖冻结文件")
    commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    build = json.loads((ROOT / "frontend/dist/build.json").read_text())
    if not commit.startswith(build["commit"]) or build["dirty"]:
        raise SystemExit("前端版本与当前源码不一致或工作区有变，拒绝打包")
    if subprocess.check_output(["git", "status", "--porcelain"], cwd=ROOT, text=True).strip():
        raise SystemExit("工作区有未提交变更，拒绝打包")
    evidence = ROOT / "artifacts/evidence" / args.evidence_version
    gpu_file = evidence / "gpu-4090d/benchmark.json"
    gpu = json.loads(gpu_file.read_text()) if gpu_file.exists() else None
    if gpu:
        assert gpu["complete"] and gpu["gpu_execution_observed"]
        assert all(stage["failed"] == 0 for stage in gpu["stages"])
    output.mkdir(parents=True, exist_ok=True)
    data_dir = args.processed_dir.resolve() if args.processed_dir else ROOT / "data/processed"
    data_manifest = json.loads((data_dir / "DATA_MANIFEST.json").read_text()) if args.processed_dir else None
    if data_manifest:
        if data_manifest.get("profile") != "processed-course-content" or data_manifest.get("reviewer_accounts_created") != 7:
            raise SystemExit("处理后数据缺少导出／虚构账号验收清单")
        for name, info in data_manifest["files"].items():
            if Path(name).name != name or hashlib.sha256((data_dir / name).read_bytes()).hexdigest() != info["sha256"]:
                raise SystemExit("处理后数据摘要不匹配")
        if any(data_manifest["empty_personal_and_instance_tables"].values()):
            raise SystemExit("处理后数据包含非内容记录")
    conn = sqlite3.connect(f"file:{data_dir/'courseloop.db'}?mode=ro", uri=True)
    allowed_users = {"demo_student", "demo_ta", "competition-student", "competition-student2",
                     "competition-ta", "competition-teacher", "competition-admin"}
    if set(r[0] for r in conn.execute("SELECT username FROM users")) != allowed_users:
        raise SystemExit("本地库账号不是严格的原创演示白名单")
    problem_count = conn.execute("SELECT COUNT(*) FROM problems").fetchone()[0]
    if problem_count != (data_manifest["content_tables"]["problems"] if data_manifest else 36):
        raise SystemExit("题库数量与数据清单不一致")
    with sqlite3.connect(f"file:{data_dir/'rag_fts.db'}?mode=ro", uri=True) as rag:
        chunk_count = rag.execute("SELECT count(*) FROM chunks").fetchone()[0]
    if data_manifest and chunk_count != data_manifest["retrieval_chunks"]:
        raise SystemExit("检索块数量与数据清单不一致")
    with tempfile.TemporaryDirectory(prefix="courseloop-runtime-") as temp:
        stage = Path(temp) / "courseloop-submission"
        stage.mkdir()
        archive = Path(temp) / "source.tar"
        subprocess.run(["git","archive","--format=tar","-o",str(archive),commit],cwd=ROOT,check=True)
        with tarfile.open(archive) as tar:
            tar.extractall(stage)  # trusted git archive, not user supplied tar
        import shutil
        shutil.copytree(ROOT / "frontend/dist", stage / "frontend/dist")
        (stage / "BUILD_COMMIT").write_text(commit[:12] + "\n")
        processed = stage / "data/processed"
        processed.mkdir(parents=True)
        with sqlite3.connect(processed / "courseloop.db") as target:
            conn.backup(target)
        conn.close()
        with sqlite3.connect(f"file:{data_dir/'rag_fts.db'}?mode=ro",uri=True) as src:
            with sqlite3.connect(processed / "rag_fts.db") as dst:
                src.backup(dst)
        if not data_manifest:
            shutil.copytree(ROOT / "data/raw", stage / "data/raw")
        else:
            shutil.copyfile(data_dir / "DATA_MANIFEST.json", processed / "DATA_MANIFEST.json")
        # 未携带任何 .secret_key；首次运行自动创建新部署身份密钥。
        # 日志／测试运行痕迹不作为初始演示学习记录：只保留显式合成fixture。
        with sqlite3.connect(processed / "courseloop.db") as c:
            for table in ("practice_attempts", "dialog_turns", "learning_sessions", "tutor_snapshots",
                          "review_requests", "review_plans", "review_tasks", "review_task_attempts",
                          "assignments", "study_budgets", "study_budget_exceptions"):
                c.execute(f'DELETE FROM "{table}"')
            c.commit()
            c.execute("VACUUM")
        c.close()
        if data_manifest:
            packaged_data = dict(data_manifest)
            packaged_data["files"] = {
                p.name: {"bytes": p.stat().st_size, "sha256": hashlib.sha256(p.read_bytes()).hexdigest()}
                for p in sorted(processed.glob("*.db"))
            }
            (processed / "DATA_MANIFEST.json").write_text(json.dumps(packaged_data, ensure_ascii=False, indent=2) + "\n")
        manifest = dict(version=args.version, source_commit=commit, historical_application_baseline="c171ac5",
                        frontend_build=build, demo_problems=36 if not data_manifest else 0,
                        demo_materials=4 if not data_manifest else 0,
                        data_profile="processed-course-content" if data_manifest else "original-demo",
                        delivered_problems=problem_count, delivered_retrieval_chunks=chunk_count,
                        data_attribution=data_manifest["attribution"] if data_manifest else "项目原创演示内容",
                        evidence_version=args.evidence_version,
                        production_human_records=0, production_secrets=False,
                        video="owner-approved original demo, 279.175 seconds" if media else "media files not included in this build",
                        presentation="owner-approved original, 33 slides" if media else "not included in this build",
                        media_assets=media["assets"] if media else [],
                        gpu_timing="4090D inference completed" if gpu else "not included",
                        gpu_benchmark_sha256=hashlib.sha256(gpu_file.read_bytes()).hexdigest() if gpu else None,
                        official_reports=sorted(p.name for p in (stage / "docs/submission").glob("[0-9][0-9]-*.pdf")))
        (stage / "SUBMISSION_MANIFEST.json").write_text(json.dumps(manifest,ensure_ascii=False,indent=2))
        (stage / ".env").write_text("CL_RAG_MODE=fts5\nCL_LLM_BASE_URL=\nCL_LLM_API_KEY=\nCL_LLM_WORKHORSE_MODEL=\nCL_BUILD_COMMIT=" + commit[:12] + "\n")
        # 这是新生成的空白示例配置，不是从开发或生产 .env 复制。
        dest = output / f"courseloop-{args.version}-runtime.tar.gz"
        with tarfile.open(dest,"w:gz") as tar:
            tar.add(stage,arcname=stage.name)
        reports = output / f"courseloop-{args.version}-reports.zip"
        with zipfile.ZipFile(reports, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for report in sorted((stage / "docs/submission").glob("[0-9][0-9]-*")):
                if report.suffix in {".md", ".docx", ".pdf"}:
                    archive.write(report, arcname=report.name)
            for name in ("data-dictionary.json", "EVIDENCE_RULES.md", "演示材料.md", "media-manifest.json"):
                archive.write(stage / "docs/submission" / name, arcname=name)
            for name in ("处理后数据交付范围.md", "processed-data-inventory.json"):
                if (stage / "docs/submission" / name).exists():
                    archive.write(stage / "docs/submission" / name, arcname=name)
            for name in ("PUBLICATION.md", "NOTICE.md", "THIRD_PARTY_NOTICES.md"):
                archive.write(stage / name, arcname=name)
    ui = output / f"courseloop-{args.version}-ui-evidence.tar.gz"
    with tarfile.open(ui,"w:gz") as tar:
        for directory in sorted(evidence.glob("ui-*")):
            tar.add(directory,arcname=directory.name)
        if (evidence / "gpu-4090d/ui").exists():
            tar.add(evidence / "gpu-4090d/ui", arcname="gpu-4090d-ui")
    checks = []
    media_paths = []
    if media:
        import shutil
        for item in media["assets"]:
            path = output / item["filename"]
            shutil.copyfile(args.media_dir / path.name, path)
            media_paths.append(path)
    for p in (dest, ui, reports, *media_paths):
        checks.append(hashlib.sha256(p.read_bytes()).hexdigest()+"  "+p.name)
    (output / "SHA256SUMS").write_text("\n".join(checks)+"\n")
    (output / "SUBMISSION_MANIFEST.json").write_text(json.dumps(manifest,ensure_ascii=False,indent=2))
    print(json.dumps(dict(runtime_bytes=dest.stat().st_size,ui_bytes=ui.stat().st_size,source_commit=commit)))


if __name__ == "__main__":
    main()
