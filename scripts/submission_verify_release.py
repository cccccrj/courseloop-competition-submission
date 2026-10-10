"""核对下载资产摘要、归档路径、原创数据和密钥边界；只读解包到新临时目录。"""
from __future__ import annotations
import argparse
import hashlib
import json
import sqlite3
import tarfile
import tempfile
import zipfile
from pathlib import Path

from submission_prepare import MATERIALS


def verify(directory: Path) -> dict:
    release_manifest = json.loads((directory / "SUBMISSION_MANIFEST.json").read_text())
    version = release_manifest["version"]
    for item in release_manifest.get("media_assets", []):
        assert Path(item["filename"]).name == item["filename"]
        source = directory / item["filename"]
        assert source.stat().st_size == item["bytes"]
        assert hashlib.sha256(source.read_bytes()).hexdigest() == item["sha256"]
        assert item["content_unchanged"] and item["owner_confirmed_final"]
    checks = []
    for line in (directory / "SHA256SUMS").read_text().splitlines():
        digest, name = line.split(maxsplit=1)
        name = name.strip()
        assert Path(name).name == name
        assert hashlib.sha256((directory / name).read_bytes()).hexdigest() == digest, name
        checks.append(name)
    with tempfile.TemporaryDirectory(prefix="courseloop-release-check-") as temporary:
        target = Path(temporary)
        with tarfile.open(directory / f"courseloop-{version}-runtime.tar.gz") as archive:
            for member in archive.getmembers():
                p = Path(member.name)
                assert not p.is_absolute() and ".." not in p.parts
                assert not member.issym() and not member.islnk()
                assert ".private" not in p.parts and member.name != "courseloop-submission/.git"
                assert p.name not in {".secret_key", "accounts.json", "load-accounts.json"}
            archive.extractall(target)
        root = target / "courseloop-submission"
        manifest = json.loads((root / "SUBMISSION_MANIFEST.json").read_text())
        frontend = json.loads((root / "frontend/dist/build.json").read_text())
        assert manifest["source_commit"].startswith(frontend["commit"]) and not frontend["dirty"]
        assert manifest == json.loads((directory / "SUBMISSION_MANIFEST.json").read_text())
        report_zip = directory / f"courseloop-{version}-reports.zip"
        if report_zip.exists():
            reports = root / "docs/submission"
            expected = {p.name for p in reports.glob("[0-9][0-9]-*") if p.suffix in {".md", ".docx", ".pdf"}}
            expected.update({"data-dictionary.json", "EVIDENCE_RULES.md", "演示材料.md", "media-manifest.json"})
            expected.update({"PUBLICATION.md", "NOTICE.md", "THIRD_PARTY_NOTICES.md"})
            if manifest.get("data_profile") == "processed-course-content":
                expected.update({p.name for p in reports.glob("*") if p.name in {"处理后数据交付范围.md", "processed-data-inventory.json"}})
            with zipfile.ZipFile(report_zip) as archive:
                assert set(archive.namelist()) == expected
                assert all(archive.read(name) == (root / name if name in {"PUBLICATION.md", "NOTICE.md", "THIRD_PARTY_NOTICES.md"} else reports / name).read_bytes() for name in expected)
        if manifest.get("gpu_timing") == "4090D inference completed":
            gpu_file = root / "artifacts/evidence" / manifest.get("evidence_version", version) / "gpu-4090d/benchmark.json"
            assert hashlib.sha256(gpu_file.read_bytes()).hexdigest() == manifest["gpu_benchmark_sha256"]
            gpu = json.loads(gpu_file.read_text())
            assert gpu["complete"] and gpu["gpu_execution_observed"]
            assert all(stage["failed"] == 0 for stage in gpu["stages"])
            assert len(manifest["official_reports"]) == 9
            assert all((root / "docs/submission" / name).is_file() for name in manifest["official_reports"])
        env = (root / ".env").read_text()
        assert "CL_LLM_API_KEY=\n" in env and "CL_BUILD_COMMIT=" + manifest["source_commit"][:12] in env
        raw = sorted((root / "data/raw").rglob("*"))
        texts = [p.read_text() for p in raw if p.is_file()]
        processed_profile = manifest.get("data_profile") == "processed-course-content"
        if processed_profile:
            data_manifest = json.loads((root / "data/processed/DATA_MANIFEST.json").read_text())
            assert not texts
            for name, info in data_manifest["files"].items():
                assert Path(name).name == name
                assert hashlib.sha256((root / "data/processed" / name).read_bytes()).hexdigest() == info["sha256"]
        else:
            assert sorted(texts) == sorted(MATERIALS.values())
        with sqlite3.connect(root / "data/processed/courseloop.db") as conn:
            assert conn.execute("PRAGMA quick_check").fetchone()[0] == "ok"
            assert conn.execute("SELECT COUNT(*) FROM problems").fetchone()[0] == (manifest["delivered_problems"] if processed_profile else 36)
            names = {r[0] for r in conn.execute("SELECT username FROM users")}
            assert names == {"demo_student", "demo_ta", "competition-student", "competition-student2",
                             "competition-ta", "competition-teacher", "competition-admin"}
            assert conn.execute("SELECT COUNT(*) FROM practice_attempts").fetchone()[0] == 0
            assert conn.execute("SELECT COUNT(*) FROM learning_sessions").fetchone()[0] == 0
            assert conn.execute("SELECT COUNT(*) FROM error_records").fetchone()[0] == (0 if processed_profile else 1)
            if processed_profile:
                for table, count in data_manifest["content_tables"].items():
                    assert conn.execute('SELECT count(*) FROM "' + table + '"').fetchone()[0] == count
                for table in data_manifest["empty_personal_and_instance_tables"]:
                    assert conn.execute('SELECT count(*) FROM "' + table + '"').fetchone()[0] == 0
                assert conn.execute("SELECT count(*) FROM problem_knowledge pk LEFT JOIN problems p ON p.id=pk.problem_id WHERE p.id IS NULL").fetchone()[0] == 0
        if processed_profile:
            with sqlite3.connect(root / "data/processed/rag_fts.db") as conn:
                assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
                assert conn.execute("SELECT count(*) FROM chunks").fetchone()[0] == manifest["delivered_retrieval_chunks"]
        private = Path(__file__).resolve().parents[1] / ".private"
        known = []
        secret_env = private / "production-secrets.env"
        if secret_env.exists():
            for line in secret_env.read_text().splitlines():
                key, sep, value = line.partition("=")
                value = value.strip().strip("\"'")
                if sep and ("API_KEY" in key or "SECRET_KEY" in key) and len(value) >= 12:
                    known.append(value.encode())
        signing = private / "production-signing-key"
        if signing.exists():
            known.append(signing.read_bytes().strip())
        vision_env = private / "zhizengzeng-vision.env"
        if vision_env.exists():
            for line in vision_env.read_text().splitlines():
                key,sep,value = line.partition("=")
                value=value.strip().strip("\"'")
                if sep and "API_KEY" in key and len(value)>=12:
                    known.append(value.encode())
        for p in root.rglob("*"):
            if not p.is_file():
                continue
            blobs = [p.read_bytes()]
            if p.suffix == ".docx":
                with zipfile.ZipFile(p) as z:
                    blobs += [z.read(n) for n in z.namelist() if n.endswith(".xml")]
            assert not any(key in blob for key in known for blob in blobs), str(p.relative_to(root))
        return dict(passed=True, verified_assets=checks, source_commit=manifest["source_commit"],
                    frontend_commit=frontend["commit"], original_materials=len(texts),
                    delivered_problems=manifest.get("delivered_problems", 36),
                    delivered_retrieval_chunks=manifest.get("delivered_retrieval_chunks", 4),
                    data_profile=manifest.get("data_profile", "original-demo"),
                    db_integrity="ok", known_production_secrets_checked=len(set(known)),
                    production_secret_hits=0, production_human_records=0)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    parser.add_argument("--out", type=Path)
    args = parser.parse_args()
    result = verify(args.directory)
    if args.out:
        args.out.write_text(json.dumps(result, ensure_ascii=False, indent=2))
    print(json.dumps(result, ensure_ascii=False))
