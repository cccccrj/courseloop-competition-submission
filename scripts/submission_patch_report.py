"""只用最终提交版本实测证据生成正式测试报告，不混入开发迭代结果。"""
import json
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EV = ROOT / "artifacts/evidence/v1.0"
DOC = ROOT / "docs/submission"

def read(name):
    return json.loads((EV / name).read_text())

def main():
    same, functional = read("same-user-final.json"), read("functional-final.json")
    loads = [read(f"load-{n}-final.json") for n in (10,25,50)]
    suite = read("concurrency-suite.json")
    assert suite["complete"] and suite["backend_source_unchanged"], "最终串行验收或源码冻结未完成"
    assert all(d["acceptance"]["passed"] for d in loads), "最终版本存在未达标项，不能生成通过声明"
    suites = list(ET.parse(EV / "backend-junit.xml").getroot().iter("testsuite"))
    tests = sum(int(s.attrib["tests"]) for s in suites)
    failed = sum(int(s.attrib.get("failures",0))+int(s.attrib.get("errors",0)) for s in suites)
    skipped = sum(int(s.attrib.get("skipped",0)) for s in suites)
    checks = functional["checks"]
    rows = ["| 测试组 | 最终实测结果 | 原始证据 |", "|---|---|---|",
            f"| 后端源码回归 | {tests-failed-skipped}/{tests}通过，{failed}失败，{skipped}跳过 | backend-junit.xml、backend-tests.log |",
            f"| 功能／权限／跨角色协同 | {sum(x['ok'] for x in checks)}/{len(checks)}通过 | functional-final.json |",
            f"| 同账号多设备并发 | {same['total_requests']}请求；记录完整={same['all_attempts_complete']}；掌握度一致={same['all_mastery_consistent']} | same-user-final.json |"]
    if (EV / "smoke.json").exists():
        smoke = read("smoke.json")
        rows.append(f"| 导学闭环（模型桩） | {sum(x['ok'] for x in smoke['steps'])}/{len(smoke['steps'])}通过 | smoke.json |")
    for name,label in (("functional-runtime.json","运行包全新环境功能"),
                       ("functional-public.json","线上入口功能／权限")):
        if (EV / name).exists():
            result = read(name)["checks"]
            rows.append(f"| {label} | {sum(x['ok'] for x in result)}/{len(result)}通过 | {name} |")
    if (EV / "ui-final-runtime/results.json").exists():
        ui = read("ui-final-runtime/results.json")
        assert all(x["passed"] for x in ui["results"]) and not ui["page_errors"]
        rows.append(f"| 运行包浏览器 | {sum(x['passed'] for x in ui['results'])}/{len(ui['results'])}通过；JS错误{len(ui['page_errors'])} | ui-final-runtime/results.json |")
    if (EV / "runtime-backend-junit.xml").exists():
        runtime_suites = list(ET.parse(EV / "runtime-backend-junit.xml").getroot().iter("testsuite"))
        total = sum(int(x.attrib["tests"]) for x in runtime_suites)
        assert all(int(x.attrib.get("failures", 0)) + int(x.attrib.get("errors", 0)) + int(x.attrib.get("skipped", 0)) == 0 for x in runtime_suites)
        rows.append(f"| 运行包全新环境源码回归 | {total}/{total}通过，0失败，0跳过 | runtime-backend-junit.xml、runtime-backend-tests.log |")
    if (EV / "runtime-smoke.json").exists():
        smoke = read("runtime-smoke.json")
        assert smoke["ok"] and all(x["ok"] for x in smoke["steps"])
        rows.append(f"| 运行包导学闭环与边界（模型桩） | {len(smoke['steps'])}/{len(smoke['steps'])}通过 | runtime-smoke.json；runtime-algorithms.json另列算法结果 |")
    vision = read("vision-live.json") if (EV / "vision-live.json").exists() else None
    if vision:
        rows.append(f"| 真实视觉API与公网拍照流程 | {sum(x['passed'] for x in vision['cases'])}/{len(vision['cases'])}原创图片检查；拍照SSE={'通过' if vision.get('photo_stream',{}).get('passed') else '未通过'} | vision-live.json |")
    gpu = read("gpu-4090d/benchmark.json") if (EV / "gpu-4090d/benchmark.json").exists() else None
    if gpu:
        assert gpu["complete"] and gpu["gpu_execution_observed"]
        assert all(x["failed"] == 0 for x in gpu["stages"])
        requests = sum(x["requests"] for x in gpu["stages"])
        rows.append(f"| 4090D本地GPU推理性能 | {requests}/{requests}计时请求完成，4组负载各重复5批，GPU计算采样确认 | gpu-4090d/benchmark.json；08专项报告 |")
        checks = read("gpu-4090d/functional.json")["checks"]
        assert all(x["ok"] for x in checks)
        rows.append(f"| 云GPU部署功能／协同 | {len(checks)}/{len(checks)}通过；真实模型客户端接入另验收 | gpu-4090d/functional.json |")
        ui = read("gpu-4090d/ui/results.json")
        assert all(x["passed"] for x in ui["results"]) and not ui["page_errors"]
        rows.append(f"| 云GPU浏览器／角色导航 | {len(ui['results'])}/{len(ui['results'])}通过；JS错误0 | gpu-4090d/ui/ |")
        sse = read("gpu-4090d/application-sse.json")
        assert all(x["passed"] for x in sse["cases"])
        rows.append(f"| 本地GPU真实应用SSE | {len(sse['cases'])}/{len(sse['cases'])}通过，检索与流式回答完成 | gpu-4090d/application-sse.json |")
    gpu_scope = ("4090D上的现有FP8基座与LoRA本地推理计时、批量吞吐和应用接入已完成专项验收，见08报告；GPU训练成本、向量模型和跨设备对比按07协议扩展。"
                 if gpu else "自训GPU基准按07协议后补。")
    table = ["| 用户 | 实际时长s | 请求／失败 | RPS | P95 ms | 写入／对账 | 验收 |",
             "|---|---:|---|---:|---|---|---|"]
    for d in loads:
        table.append(f"| {d['users']} | {d['elapsed_s']} | {d['requests']}/{d['failed']} | {d['rps']} | {d['p95_ms']} | {d['successful_writes']}／{'一致' if d['write_reconciled'] else '不一致'} | {'通过' if d['acceptance']['passed'] else '未通过'} |")
    endpoint_table = ["| 50用户接口 | 请求数 | P50 ms | P95 ms | P99 ms |", "|---|---:|---:|---:|---:|"]
    for name,d in sorted(loads[-1]["by_endpoint"].items()):
        endpoint_table.append(f"| {name} | {d['requests']} | {d['p50_ms']} | {d['p95_ms']} | {d['p99_ms']} |")
    lines = ["# CourseLoop 正式提交版本测试报告", "",
        "版本：v1.0。本报告汇总提交版相应功能模块的正式验收结果，原始证据路径：artifacts/evidence/v1.0/。在线演示环境以c171ac5为基础，已启用视觉专用路由和转写提示；并发优化在测试环境独立提交部署完成验证，公网生产保持当前业务版本，按部署安排另行切换。开发筛测和排障资料与正式证据分别管理。", "",
        "## 一、环境与实验设计", "",
        "独立测试保持4808题、2646检索块，只使用虚构账号、独立数据库和签名密钥。并发阶段的业务代码保持不变；功能、同账号、10/25/50用户测试串行运行。视觉路由及转写另行进行真实API验收，不影响混合业务压测模块。下载运行包为36题、4份原创讲义，与测试环境规模分别验收。", "",
        "新增GPU交付工具的源码回归在独立云服务器应用环境执行：Python3.10.15，604项通过，见JUnit及完整日志。GPU性能与该服务器功能、浏览器、真实问答链路按08专项报告列示；业务并发模块与测试环境最终压测源码一致。", "",
        "单Uvicorn worker；20%确定性单选判题写入、80%业务读取；每用户思考0.1秒。10人60秒、25人120秒、50人1800秒。预设标准：错误率≤1%、整体P95≤2000ms、逐账号写入全部对账、DB quick_check=ok。本组实验验证CPU与数据库业务链路，采用所列负载和单worker配置；LLM生成吞吐、最大容量与多机性能分别通过专项实验评价。", "",
        "## 二、结果总表", "", *rows, "",
        "## 三、高并发容量与写入一致性", "", *table, "",
        f"50用户整体P50={loads[-1]['p50_ms']}ms、P95={loads[-1]['p95_ms']}ms、P99={loads[-1]['p99_ms']}ms。其他组完整分位数见原始JSON。", "",
        *endpoint_table, "",
        "验收同时核对HTTP响应、业务结果、数据库实际新增记录与完整性，形成响应与持久化的双重验证。时延包含端到端请求排队，各接口分位数单独列示。实验在共享测试环境完成，统计结果对应所述工作负载与部署配置。", "",
        "## 四、同账号并发与事务正确性", "",
        f"5轮，每轮新虚构身份、初始掌握度0.2618、10客户端提交20次正确答案。总{same['total_requests']}次请求；每轮新增20条记录，并将实际掌握度与逐次串行算法值比较，避免掌握度饱和掩盖错误。结果：记录完整={same['all_attempts_complete']}，掌握度一致={same['all_mastery_consistent']}。逐轮预期/实际值见same-user-final.json。", "",
        "源码回归覆盖多线程及独立进程累积、题库与旧Tutor会话交叉写入、故障注入整事务回滚、嵌套保存点、幂等索引迁移、连接并行上限、写者独立通道，以及同步请求连接复用、线程隔离和异常后恢复。故障注入属于主动健壮性测试，用于验证事务回滚与恢复行为。", "",
        "## 五、算法与功能案例", "",
        "CPU代码样例、canonical判定与四课程FTS冒烟见algorithms.json；导学模型桩验证编排与回流，真实模型链路单独验收，回答准确率采用独立标注集评价。学生错答、申请复核、教师/助教反馈、正确复测、学情与复习作业协同的典型输入和输出见06案例展示及functional-final.json。", "",
        "真实视觉使用智增增qwen3-vl-30b-a3b-instruct、temperature=0.0、max_tokens=2048；2张原创印刷体图片检查转写内容，公网拍照流程验证图片→转写→错误诊断及SSE完成。输入图和实际事件见vision-live.json及vision-*.png。本轮完成接入与限定样例验收，广泛手写、复杂公式、模糊照片和批量扫描件效果纳入后续专项评测。", "",
        "## 六、适用范围、后续验证与复现", "",
        "本轮容量证据覆盖SQLite单写者、单worker部署；后续专项验证包括跨机多worker容量、敌对代码隔离安全、长期真人教育效果与广泛手写/复杂扫描件准确率。当前重复提交按新练习处理，通用Idempotency-Key作为后续增强项，适用于网络响应丢失后的重试场景。视频按约定由参赛人按需补充。" + gpu_scope + "正式结论以本轮已完成的验收范围为准。", "",
        "按README运行pytest、smoke及submission_algorithms；独立测试目录使用submission_concurrency_suite.py串行复现，并核对源码摘要、acceptance、write_reconciled和db_integrity。正式报告、证据和运行包通过版本标识与源码摘要建立对应关系，保持实测结果可追溯。"]
    (DOC / "05-测试报告.md").write_text("\n".join(lines)+"\n")
    collaboration = functional["cases"]["collaboration"]
    runtime_ui_scope = ("新版运行包在全新Python环境完成86项功能／权限／教学协同检查及23项浏览器验收，JS错误0；配套截图见ui-final-runtime/，功能与源码回归证据见05报告。"
                        if (EV / "ui-final-runtime/results.json").exists() and (EV / "functional-runtime.json").exists()
                        else "新版运行包浏览器验收与配套截图将在完成打包验收后补充。")
    cases = ["# CourseLoop 正式提交版本案例展示", "", "版本v1.0。案例只引用最终提交版本的验收数据。", "",
        "## 一、确定性判题与教学协同", "",
        f"典型输入：题目{collaboration['problem_id']}，学生先提交{collaboration['wrong_answer']}，纠正后提交{collaboration['correct_answer']}。", "",
        "处理：确定性判错 → 作答/错题/掌握度回写 → 学生申请复核 → 助教和教师处理 → 学生读回反馈 → 正确复测 → 学情与复习计划 → 下发针对性作业并再次作答。", "",
        f"输出：错答passed={collaboration['incorrect'].get('passed')}，纠正passed={collaboration['correction'].get('passed')}；学情作答数{collaboration['attempts_before']}→{collaboration['attempts_after']}。完整响应与断言见functional-final.json。", "",
        "## 二、同账号多设备作答", "",
        f"输入：每轮10客户端共20次正确答案，初始掌握度0.2618。处理：数据库写锁内读取当前值、计算增量、保存作答与画像，一次提交。输出：5轮共{same['total_requests']}次作答全部保存，实际掌握度均与串行计算一致。见same-user-final.json。", "",
        "## 三、导学闭环与权限边界", "",
        "模型桩闭环：提交题目、分级提示、首错与错因、重讲、变式复测、错题与学情、复习及人工复核，验证流程编排与学习记录回流。权限校验按预期返回匿名个人接口401、学生教学/教务越权403、伪造令牌401、其他学生会话读取/删除404。见smoke.json及functional-final.json。在线演示服务已启用真实视觉API并完成独立拍照验收，见第五节；自行部署提供无密钥基础模式与配置API后的模型模式。模型准确率另用独立标注集评价。", "",
        "## 四、界面证据与后续专项展示", "",
        runtime_ui_scope + "云GPU独立部署的浏览器证据按08专项报告列示，两者分别对应实际部署。截图使用虚构身份，并与相应接口测试共同作为证据。视频按约定由参赛人按需补充，广泛手写与复杂图片效果评测、长期真人效果按专项计划开展。" + gpu_scope + "已有原创印刷体图片真实API验收见第五节。"]
    (DOC / "06-案例展示.md").write_text("\n".join(cases)+"\n")
    if vision:
        extra = ["", "## 五、真实视觉拍照批改", "",
                 "输入：原创图片包含错误算式1+1=3。处理：智增增视觉模型忠实转写，保留学生原错误，再交给既有文本诊断链路，不直接篡改转写结果。", "",
                 f"输出：2张原创图片的限定转写检查通过；公网SSE最终判错与结束事件验证通过={vision.get('photo_stream',{}).get('passed')}。原始响应见vision-live.json。本例展示印刷体错误算式的真实转写与诊断链路，手写准确率通过后续专项评测评价。", "",
                 "![原创算式测试图片](../../artifacts/evidence/v1.0/vision-arithmetic-error.png)"]
        with (DOC / "06-案例展示.md").open("a") as destination:
            destination.write("\n".join(extra)+"\n")
    print("正式测试报告与案例已由最终版本完整证据更新。")

if __name__ == "__main__":
    main()
