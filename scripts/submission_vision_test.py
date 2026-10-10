"""低成本真实视觉验收：原创印刷体图片转写与可选拍照批改SSE，不输出凭据。"""
import argparse
import base64
import io
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--vision-env",type=Path,required=True)
    parser.add_argument("--out",type=Path,required=True)
    parser.add_argument("--font",default="/usr/share/fonts/truetype/droid/DroidSansFallbackFull.ttf")
    parser.add_argument("--latin-font",default="/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf")
    parser.add_argument("--base",help="可选的真实HTTP拍照批改入口（含/api）")
    parser.add_argument("--accounts",type=Path,default=ROOT / ".private/accounts.json")
    args = parser.parse_args()
    from dotenv import dotenv_values
    values = dotenv_values(args.vision_env)
    for name in ("CL_LLM_VISION_BASE_URL","CL_LLM_VISION_API_KEY","CL_LLM_VISION_MODEL"):
        if not values.get(name):
            raise SystemExit(f"缺少配置{name}")
        os.environ[name] = values[name]
    if values.get("CL_LLM_VISION_TEMPERATURE"):
        os.environ["CL_LLM_VISION_TEMPERATURE"] = values["CL_LLM_VISION_TEMPERATURE"]
    from PIL import Image, ImageDraw, ImageFont
    from backend.core import llm
    from backend.agents.photo_grader import build_prompt
    from backend.core.config import settings
    font = ImageFont.truetype(args.font,32)
    latin_font = ImageFont.truetype(args.latin_font,32)
    args.out.parent.mkdir(parents=True,exist_ok=True)
    fixtures = [("arithmetic-error",["题目：计算 1 + 1", "(1) 1 + 1 = 3", "(2) 所以答案是 3"],("1","+","3")),
                ("os-note",["操作系统：页面置换", "FIFO：先进先出", "LRU：最近最少使用", "访问序列：1, 2, 3, 1"],("FIFO","LRU","1","2","3"))]
    records, image_urls = [], []
    for name,lines,expected in fixtures:
        image = Image.new("RGB",(960,360),"white")
        draw = ImageDraw.Draw(image)
        for row,line in enumerate(lines):
            x = 35
            for char in line:
                selected = latin_font if ord(char) < 128 else font
                draw.text((x,65+row*65),char,fill="black",font=selected,anchor="ls")
                x += selected.getlength(char)
        buffer = io.BytesIO()
        image.save(buffer,format="PNG")
        payload = buffer.getvalue()
        (args.out.parent / f"vision-{name}.png").write_bytes(payload)
        data_url = "data:image/png;base64,"+base64.b64encode(payload).decode()
        image_urls.append(data_url)
        started = time.perf_counter()
        try:
            response = llm.get_vision_client().chat.completions.create(
                model=settings.vision_model,temperature=settings.llm_vision_temperature,max_tokens=2048,
                messages=[{"role":"user","content":[{"type":"text","text":build_prompt("os" if name=="os-note" else "ds-algo")},
                            {"type":"image_url","image_url":{"url":data_url}}]}])
            text = response.choices[0].message.content or ""
            usage = response.usage.model_dump() if response.usage else {}
            faithful = (bool(re.search(r"1\s*\+\s*1\s*=\s*3",text.replace("$","")))
                        and "所以答案" in text) if name=="arithmetic-error" else (
                        all(word in text for word in expected) and "先进先出" in text and "最近最少使用" in text
                        and not any(word in text for word in ("第一步","第二步","页面数","[图:")))
            records.append(dict(name=name,input_lines=lines,transcript=text,
                  passed=faithful,usage=usage,
                  elapsed_ms=round((time.perf_counter()-started)*1000,2)))
        except Exception as exc:
            records.append(dict(name=name,passed=False,error_type=type(exc).__name__,
                                status=getattr(exc,"status_code",None)))
            break
    result = dict(captured_at=datetime.now(timezone.utc).isoformat(),
             model=settings.vision_model,provider="zhizengzeng",cases=records,
             fixture_policy="original rendered printed text; no personal data; NOT handwritten OCR accuracy",
             scope="real image API and limited transcript assertions, not a general OCR benchmark")
    if args.base and all(case["passed"] for case in records) and len(records)==2:
        import httpx
        account = next(row for row in json.loads(args.accounts.read_text()) if row["role"]=="student")
        events=[]
        with httpx.Client(base_url=args.base,timeout=180) as client:
            login=client.post("/auth/login",json={"username":account["username"],"password":account["password"]})
            login.raise_for_status()
            with client.stream("POST","/grade/photo/stream",headers={"Authorization":"Bearer "+login.json()["token"]},
                  json={"image":image_urls[0],"course":"ds-algo","problem":"计算1+1。学生写1+1=3。", "reference":"1+1=2"}) as response:
                response.raise_for_status()
                for line in response.iter_lines():
                    if line.startswith("data:"):
                        event=json.loads(line[5:])
                        if isinstance(event.get("citations"),list):
                            event["citations"]=[{key:value for key,value in citation.items()
                                if key not in ("excerpt","snippet","text")} for citation in event["citations"]]
                        events.append(event)
        finals=[event for event in events if event.get("type")=="result"]
        result["photo_stream"] = dict(events=events,passed=bool(finals) and
             any(event.get("type")=="transcript" and "3" in event.get("text","") for event in events) and
             finals[-1].get("correct") is False and not any(event.get("type")=="error" for event in events))
    result["passed"] = len(records)==2 and all(case["passed"] for case in records) and result.get("photo_stream",{}).get("passed",True)
    args.out.write_text(json.dumps(result,ensure_ascii=False,indent=2))
    print(json.dumps(dict(model=result["model"],cases=len(records),passed=result["passed"],
                         photo_stream=result.get("photo_stream",{}).get("passed")),ensure_ascii=False))
    raise SystemExit(0 if result["passed"] else 1)

if __name__=="__main__":
    main()
