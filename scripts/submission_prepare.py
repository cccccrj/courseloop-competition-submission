"""比赛演示资料：从零构建，无生产快照、真实身份、课件或第三方题库复制。

bootstrap 只允许在当前提交仓库 data/processed 新建空库；已有库拒绝覆写。
provision-live 只插入一个新的 student 评委账号，不播种、不修改已有账号。
"""
from __future__ import annotations
import argparse
import json
import os
import secrets
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# 原创微型演示集：覆盖四课程；并非 4722/4808 道生产题库的缩小统计样本。
QUESTIONS = [
    ("ds-algo", "栈的应用", "以下哪种数据结构遵循后进先出？", ["栈", "队列", "数组的任意访问", "哈希表"], "A"),
    ("ds-algo", "二叉树遍历", "二叉搜索树的中序遍历结果通常满足什么顺序？", ["降序", "层序", "升序", "随机"], "C"),
    ("ds-algo", "快速排序", "采用合理基准划分时，快速排序的平均时间复杂度是？", ["O(n)", "O(n log n)", "O(n²)", "O(1)"], "B"),
    ("ds-algo", "复杂度分析", "对长度 n 的有序数组做二分查找，时间复杂度是？", ["O(n²)", "O(n)", "O(log n)", "O(1)"], "C"),
    ("ds-algo", "队列", "广度优先搜索管理待访问节点，通常使用哪种结构？", ["栈", "队列", "集合替代访问顺序", "仅用递归栈"], "B"),
    ("co", "Cache", "Cache 的主要作用是？", ["减少处理器与主存速度差造成的等待", "替代所有主存", "提高磁盘容量", "改变指令语义"], "A"),
    ("co", "补码", "8 位补码表示有符号整数的范围是？", ["0 到 255", "−127 到 127", "−128 到 127", "−128 到 128"], "C"),
    ("co", "流水线", "理想流水线主要改善哪项指标？", ["单条指令语义", "指令吞吐率", "寄存器位数", "磁盘寻道时间"], "B"),
    ("co", "寻址方式", "立即寻址中的操作数位于哪里？", ["指令中", "只能在主存", "只能在磁盘", "只能在栈顶"], "A"),
    ("co", "中断", "外部中断通常由什么触发？", ["源代码注释", "外部设备事件", "编译器优化", "函数名称"], "B"),
    ("os", "进程与线程", "同一进程的线程通常共享什么？", ["程序计数器的当前值", "各自的栈", "进程地址空间", "所有局部变量的当前值"], "C"),
    ("os", "死锁", "哪项不是死锁发生的必要条件？", ["互斥", "请求并保持", "不剥夺", "资源数量无限"], "D"),
    ("os", "页面置换算法", "LRU 淘汰哪一个页面？", ["最早装入", "最近最少使用", "编号最大", "编号最小"], "B"),
    ("os", "虚拟内存", "缺页异常表示什么？", ["所需页不在物理内存中", "程序一定结束", "所有页都丢失", "CPU 已损坏"], "A"),
    ("os", "进程调度", "时间片轮转调度主要用于改善什么？", ["交互响应与公平性", "磁盘容量", "代码行数", "文件压缩率"], "A"),
    ("net", "TCP", "TCP 建立连接通常采用几次握手？", ["一次", "两次", "三次", "四次"], "C"),
    ("net", "IP 地址", "IPv4 地址长度是多少位？", ["16", "32", "64", "128"], "B"),
    ("net", "DNS", "DNS 的主要作用是什么？", ["域名解析", "加密所有数据", "替代 TCP", "分配 CPU 时间片"], "A"),
    ("net", "流量控制", "TCP 流量控制主要避免哪种问题？", ["接收方处理能力被发送速度淹没", "域名重复", "路由器名称过长", "文件名冲突"], "A"),
    ("net", "子网划分", "IPv4 /24 子网掩码对应多少个主机地址位？", ["4", "8", "16", "24"], "B"),
]
MATERIALS = {
    "ds-algo": "原创演示讲义：栈与队列\n栈遵循后进先出（LIFO），队列遵循先进先出（FIFO）。括号匹配可以用栈；广度优先搜索使用队列。二叉搜索树中序遍历为升序。快速排序平均 O(n log n)，最坏 O(n²)。二分查找复杂度 O(log n)。",
    "co": "原创演示讲义：计算机组成\nCache 缓存缓解处理器与主存的速度差。8 位补码范围是 −128 到 127。流水线在理想情况下提高吞吐率，不保证单条指令时延缩短。立即寻址的操作数位于指令中。外部设备事件可以触发外部中断。",
    "os": "原创演示讲义：操作系统\n同一进程的线程共享进程地址空间，但有独立的栈和程序计数器。死锁必要条件：互斥、请求并保持、不剥夺、循环等待。LRU 页面置换淘汰最近最少使用页面；FIFO 淘汰最早装入页面。缺页表示所需页不在物理内存中。时间片轮转兼顾交互响应和公平性。",
    "net": "原创演示讲义：计算机网络\nTCP 建立连接采用三次握手，协调两端序列号并确认收发能力。IPv4 地址为 32 位，/24 有 8 个主机地址位。DNS 负责域名解析。TCP 流量控制防止发送方超过接收能力，拥塞控制则关注网络负载。",
}


def accounts():
    private = ROOT / ".private"
    private.mkdir(exist_ok=True, mode=0o700)
    path = private / "accounts.json"
    if path.exists() and not (ROOT / "ACCESS.md").exists():
        return json.loads(path.read_text())
    access = ROOT / "ACCESS.md"
    if access.exists():
        rows = []
        for line in access.read_text().splitlines():
            parts = [x.strip() for x in line.strip("|").split("|")]
            if len(parts) == 4 and parts[0].startswith("competition-"):
                rows.append(dict(username=parts[0], role=parts[1], password=parts[2]))
        if len(rows) != 5:
            raise SystemExit("ACCESS.md 账号表不完整，拒绝静默重置")
    else:
        rows = [dict(username=f"competition-{name}", role=role, password=secrets.token_urlsafe(14))
                for name, role in [("student", "student"), ("student2", "student"),
                                   ("ta", "ta"), ("teacher", "teacher"), ("admin", "admin")]]
        lines = ["# 本地演示账号", "", "以下为本地虚构身份；线上评审凭据通过比赛指定渠道单独提供。",
                 "", "本地：http://127.0.0.1:8000/；线上地址通过比赛指定渠道单独提供。", "",
                 "全部账号只用于本地演示库和独立测试环境，不可作为线上登录凭据。", "",
                 "| 账号 | 角色 | 密码 | 可用环境 |", "|---|---|---|---|"]
        for row in rows:
            where = "本地演示"
            lines.append(f"| {row['username']} | {row['role']} | {row['password']} | {where} |")
        lines += ["", "演示使用虚构身份与学习记录。",
                  "默认 demo_student / demo_ta 的 123456 是历史本地演示约定，不作为线上访问凭据。",
                  "本地演示仅监听回环地址；若部署到公网，请停用弱密码演示账号、配置反向代理和速率限制。",
                  "对外部署前请修改或停用全部演示密码，生成独立签名密钥。"]
        access.write_text("\n".join(lines) + "\n")
    path.write_text(json.dumps(rows, indent=2))
    path.chmod(0o600)
    return rows


def bootstrap():
    target = ROOT / "data/processed/courseloop.db"
    if target.exists():
        raise SystemExit("演示库已存在，拒绝覆写。请换用全新仓库目录。")
    os.environ["CL_DB_PATH"] = str(target)
    os.environ["CL_RAG_MODE"] = "fts5"
    os.environ["CL_RAG_FTS_PATH"] = str(ROOT / "data/processed/rag_fts.db")
    os.environ["CL_LOGS_DIR"] = str(ROOT / "data/logs")
    from backend.core import db
    from backend.core.config import settings
    from backend.rag import indexer
    from backend.rag.chunking import Chunk
    from scripts.seed_knowledge import seed_course
    from scripts.seed_problems import seed
    db.init_db()
    seed()  # 只针对这个尚无任何数据的新库；该旧脚本本身会清空 ds-algo 题库。
    for course in MATERIALS:
        seed_course(course)
    for row in accounts():
        db.register_user(row["username"], row["password"], "虚构" + row["role"],
                         role=row["role"], class_name="比赛演示班" if row["role"] == "student" else "")
    for course, knowledge, stem, options, answer in QUESTIONS:
        text = stem + "\n" + "\n".join(f"{chr(65+i)}. {v}" for i, v in enumerate(options))
        spec = json.dumps({"kind": "choice", "value": answer}, ensure_ascii=False)
        pid = db.insert_problem(course, "选择题", text, 2, spec, "numeric", knowledge=knowledge,
                                source="submission_original", hint="先核对概念与适用条件")
        kid = db.get_or_create_knowledge_id(knowledge, course)
        db.link_problem_knowledge(pid, kid)
    conn = indexer.connect(settings.rag_fts_path)
    for course, text in MATERIALS.items():
        # TXT 无真实页码；不制造书名/页号，引用定位采用标题。
        raw = ROOT / "data/raw" / course / "courseware"
        raw.mkdir(parents=True, exist_ok=True)
        (raw / "原创演示讲义.txt").write_text(text)
        indexer.write_chunks(conn, [Chunk(text=text, source="原创演示讲义", doc_type="courseware",
                                          course=course, chapter="原创演示讲义", page_start=0,
                                          page_end=0, chunk_index=0)])
    conn.close()
    # 为助教工作台提供明确标注的合成示例，不冒充真实学习效果。
    pid = db.problem_id_by_stem("os", next(p["stem"] for p in db.list_problems("os")
                                              if "LRU" in p["stem"]))
    p = db.get_problem(pid, with_solution=True)
    kid = db.get_or_create_knowledge_id("页面置换算法", "os")
    db.save_error_record("competition-student", "concept_confuse", "合成示例：把 LRU 与 FIFO 混淆",
                         problem_id=pid, knowledge_id=kid, problem_stem=p["stem"],
                         student_answer="A", review_reason="student_request")
    db.upsert_mastery("competition-student", kid, 0.35, 0.28, "learning")
    with db.get_conn() as c:
        c.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    env = ROOT / ".env"
    if not env.exists():
        env.write_text("CL_RAG_MODE=fts5\nCL_LLM_BASE_URL=\nCL_LLM_API_KEY=\n"
                       "CL_LLM_WORKHORSE_MODEL=\nCL_LLM_VISION_MODEL=\n")
        env.chmod(0o600)
    print("原创演示库已生成；密码不输出，本地演示账号见 ACCESS.md。")


def provision_live():
    row = json.load(sys.stdin)
    if row.get("username") != "competition-student" or row.get("role") != "student":
        raise SystemExit("只允许新增 student 评委账号")
    # 不调用 init_db，不触碰服务 .env/令牌密钥或其他表。
    from backend.core import db
    with db.get_conn() as conn:
        if conn.execute("SELECT 1 FROM users WHERE username=?", (row["username"],)).fetchone():
            raise SystemExit("账号已存在，拒绝覆盖密码")
    ok, msg = db.register_user(row["username"], row["password"], "比赛评委演示", role="student",
                               class_name="比赛测试班")
    if not ok:
        raise SystemExit(msg)
    print("评委 student 账号已新增；现有生产账号未修改。")


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("mode", choices=["credentials", "bootstrap", "provision-live"])
    a = p.parse_args()
    if a.mode == "credentials":
        accounts()
        print("5 个独立比赛账号已准备，密码不输出。")
    elif a.mode == "bootstrap":
        bootstrap()
    else:
        provision_live()
