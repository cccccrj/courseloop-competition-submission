"""交付安全扫描：已知生产密钥零命中，路径白名单，失败日志中的临时令牌脱敏。"""
from __future__ import annotations
import json
import re
import subprocess
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main():
    evidence = ROOT / "artifacts/evidence"
    # 真实视觉事件保留引用定位，但不分发受限教材摘录正文。
    for p in evidence.rglob("vision-live.json"):
        data=json.loads(p.read_text())
        for event in data.get("photo_stream",{}).get("events",[]):
            if isinstance(event.get("citations"),list):
                event["citations"]=[{key:value for key,value in citation.items()
                     if key not in ("excerpt","snippet","text")} for citation in event["citations"]]
        p.write_text(json.dumps(data,ensure_ascii=False,indent=2))
    for p in evidence.rglob("*.log"):
        # 只处理本次生成的测试日志，保留错误与堆栈，但不分发认证令牌。
        text = p.read_text(errors="replace")
        text = re.sub(r"Bearer\s+[A-Za-z0-9_.-]+", "Bearer [REDACTED TEST TOKEN]", text)
        p.write_text(text)
    files = [ROOT / x for x in subprocess.check_output(["git","ls-files"], cwd=ROOT, text=True).splitlines()]
    files += [p for p in evidence.rglob("*") if p.is_file() and p.suffix != ".png"]
    known = []
    secret_file = ROOT / ".private/production-secrets.env"
    if secret_file.exists():
        for line in secret_file.read_text().splitlines():
            k, sep, v = line.partition("=")
            value = v.strip().strip("\"'")
            if sep and ("API_KEY" in k or "SECRET_KEY" in k) and len(value) >= 12:
                known.append(value.encode())
    signing_file = ROOT / ".private/production-signing-key"
    if signing_file.exists() and len(signing_file.read_bytes().strip()) >= 12:
        known.append(signing_file.read_bytes().strip())
    vision_file = ROOT / ".private/zhizengzeng-vision.env"
    if vision_file.exists():
        for line in vision_file.read_text().splitlines():
            name,sep,value = line.partition("=")
            value = value.strip().strip("\"'")
            if sep and "API_KEY" in name and len(value) >= 12:
                known.append(value.encode())
    forbidden, hits = [], []
    for p in files:
        relative = str(p.relative_to(ROOT))
        if not p.exists():
            continue
        if p.name in {".env", ".secret_key", "accounts.json", "load-accounts.json"} or ".private" in p.parts:
            forbidden.append(relative)
        data = p.read_bytes()
        blobs = [data]
        if p.suffix == ".docx":
            with zipfile.ZipFile(p) as z:
                blobs.extend(z.read(n) for n in z.namelist() if n.endswith(".xml"))
        if any(key in blob for key in known for blob in blobs):
            hits.append(relative)  # 永不打印密钥本身
    result = dict(checked_files=len(files), known_production_values_checked=len(set(known)),
                  known_production_secret_hits=hits, forbidden_tracked_files=forbidden,
                  human_production_records_exported=0,
                  data_policy="fresh original demo only; restricted evaluation data not packaged",
                  passed=not hits and not forbidden)
    (evidence / "security-scan.json").write_text(json.dumps(result,ensure_ascii=False,indent=2))
    print(json.dumps(result,ensure_ascii=False))
    raise SystemExit(0 if result["passed"] else 1)


if __name__ == "__main__":
    main()
