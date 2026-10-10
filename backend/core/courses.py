"""课程注册表（408 四门等深的 linchpin）。

CourseLoop 从单门《数据结构与算法》扩展到计算机考研 408 统考四门：
数据结构、计算机组成原理、操作系统、计算机网络。

## 课程从代码搬进了数据库（2026-08-31）

原先四门课是本模块里的 Python 字面量，于是「开一门新课」＝改代码 + 跑 CLI +
重启服务。一所学校教的课不会永远只有 408 四门，教务该能自己加课。

现在真相在库的 `courses` 表里，字面量降级为 `DEFAULT_SEED`（建库时的默认播种
内容）。**`Course` 数据类与下面三个函数的签名一字未改**，所以 38 处引用里有
34 处零改动；只有把 `COURSES` 当 dict 直接用的 4 处改成 `all_courses()` ——
课程既然可变，一个看起来像常量的模块级 dict 就必然会在某个时刻是过期的，
而它错的方式是安静地给出旧课表，所以那个名字被彻底移除而不是保留。

本模块是所有「按科目参数化」改动的唯一数据源：
- prompts.py 的 5 个 persona 工厂读 display/topics/code_enabled/answer_format 组装系统提示；
- error_taxonomy.taxonomy_prompt(course) 读 error_examples 换二级示例；
- retriever / main / core_routes / tutor_graph 用 id 做 RAG 隔离与题库过滤；
- scripts/build_sft_data.py --course 读 topics/code_enabled/verification_types 决定合成与验证 gate；
- review_planner 读 score/EXAM_STRUCTURE 让复习计划考研化（按分值权重）；
- 前端科目切换器展示 display/short/score 与 sample_questions。

知识点（topics / knowledge_taxonomy）依据 2026 官方 408 考试大纲校准，见 data/raw/408_考试大纲_2026.md。
设计约束：code_enabled 只有数据结构为 True（唯一能跑 Python 沙箱判题的科目）。
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field

from backend.core.config import settings

# 408 试卷结构（四门合卷，总 150 分 / 180 分钟）
EXAM_STRUCTURE = "单项选择题 80 分（40 题×2）+ 综合应用题 70 分，总 150 分 / 180 分钟"


@dataclass(frozen=True)
class Course:
    id: str                              # course_id，贯穿 DB / RAG / API / 前端
    display: str                         # 书名号全称，进 persona，如《数据结构与算法》
    short: str                           # 短名，前端切换器/徽章，如「数据结构」
    exam: str                            # 考试语境，如「408 统考」
    score: int                           # 该门在 408 中的分值（复习计划按此配权重）
    code_enabled: bool                   # 是否有可编程判题（仅数据结构 True）
    verification_types: list[str]        # 该门可用的确定性验证法（base.py 词表子集）
    knowledge_taxonomy: list[dict]       # [{"chapter":.., "topics":[..]}] 依官方大纲，供 knowledge_points 播种
    error_examples: dict[str, list[str]] = field(default_factory=dict)  # code -> 本门二级示例（覆盖通用）
    sample_questions: list[str] = field(default_factory=list)     # 大厅示例问题 POOL
    answer_format: str = ""              # 非代码门：canonical 答案格式说明，注入 SOLVE/PRACTICE persona

    @property
    def topics(self) -> list[str]:
        """知识点扁平表（去重保序）：替代 build_sft KNOWLEDGE 与 Practice 硬编码。"""
        seen, out = set(), []
        for ch in self.knowledge_taxonomy:
            for t in ch.get("topics", []):
                if t not in seen:
                    seen.add(t)
                    out.append(t)
        return out


# ============================ 数据结构与算法 ============================
_DS = Course(
    id="ds-algo",
    display="《数据结构与算法》",
    short="数据结构",
    exam="408 统考",
    score=45,
    code_enabled=True,
    verification_types=["unit_test", "complexity", "rule", "citation"],
    knowledge_taxonomy=[
        {"chapter": "绪论", "topics": ["数据结构基本概念", "时间复杂度分析"]},
        {"chapter": "线性表", "topics": ["线性表", "顺序表", "链表"]},
        {"chapter": "栈、队列和数组", "topics": ["栈", "队列", "数组与特殊矩阵", "串与模式匹配"]},
        {"chapter": "树和二叉树", "topics": ["二叉树遍历", "线索二叉树", "哈夫曼树与编码", "并查集", "堆"]},
        {"chapter": "图", "topics": ["图的存储", "图的遍历", "最小生成树", "最短路径", "拓扑排序与关键路径"]},
        {"chapter": "查找", "topics": ["顺序与折半查找", "二叉搜索树与平衡树", "红黑树", "B树与B+树", "散列表"]},
        {"chapter": "排序", "topics": ["插入排序", "希尔排序", "快速排序", "堆排序", "归并排序", "基数排序", "外部排序"]},
    ],
    error_examples={
        "concept_confuse": ["栈↔队列", "BFS↔DFS", "完全二叉树↔满二叉树", "稳定↔不稳定排序", "BST↔平衡树"],
        "strategy": ["选错数据结构或算法", "该用哈希却线性扫", "该用堆却全排序"],
        "reasoning": ["递归终止条件缺失", "循环不变式错误", "归纳跳步"],
        "complexity": ["忽略最坏情况", "摊还分析错误", "漏算递归栈空间"],
        "boundary": ["空表/单节点", "越界", "重复元素", "自环/负权", "整数溢出"],
    },
    sample_questions=[
        "用栈判断括号是否匹配，思路是什么？",
        "快速排序最坏情况为什么是 O(n^2)？如何避免？",
        "B+ 树相比 B 树的优势是什么？",
    ],
)

# ============================ 计算机组成原理 ============================
_CO = Course(
    id="co",
    display="《计算机组成原理》",
    short="计组",
    exam="408 统考",
    score=45,
    code_enabled=False,
    verification_types=["numeric", "rule", "citation"],
    knowledge_taxonomy=[
        {"chapter": "计算机系统概述", "topics": ["计算机系统层次结构", "性能指标CPI/MIPS"]},
        {"chapter": "数据的表示和运算", "topics": ["进位计数制与转换", "定点数补码", "加法器与ALU", "乘除运算", "浮点数与IEEE754"]},
        {"chapter": "存储器层次结构", "topics": ["存储器分类", "SRAM与DRAM", "Cache映射与替换", "虚拟存储器"]},
        {"chapter": "指令系统", "topics": ["指令格式", "寻址方式", "CISC与RISC", "大小端与数据对齐"]},
        {"chapter": "中央处理器", "topics": ["CPU数据通路", "控制器", "异常与中断", "指令流水线", "多处理器"]},
        {"chapter": "总线和输入输出系统", "topics": ["总线", "I/O接口", "程序查询/中断/DMA"]},
    ],
    error_examples={
        "concept_confuse": ["原码↔补码↔反码", "机器数↔真值", "组合逻辑↔时序逻辑", "同步总线↔异步总线", "程序查询↔中断↔DMA"],
        "calculation": ["补码符号位处理错", "移位溢出判断错", "浮点规格化/舍入错", "Cache 命中率/CPI 计算错"],
        "boundary": ["补码最小负数 -2^(n-1) 无对应正数", "浮点上溢/下溢", "阶码全0/全1 特殊值"],
        "reasoning": ["流水线数据/控制相关判断错", "Cache 映射行号计算错", "虚实地址转换 TLB 缺失流程错"],
        "strategy": ["直接映射↔组相联选错", "该用补码一位乘却用原码"],
    },
    sample_questions=[
        "-13 的 8 位补码是多少？怎么算？",
        "Cache 采用二路组相联，命中率如何计算？",
        "五段流水线的 CPI 和加速比怎么求？",
    ],
    answer_format=(
        '本门为非代码题。"code"/"func_name" 一律空字符串；改为在 "final_answer_canonical" 给出可机器校验的规范答案，'
        '格式为 JSON，kind 取值之一：\n'
        '  {"kind":"scalar","value":1.5,"tol":0.01,"unit":""}          # CPI/加速比/带宽等数值\n'
        '  {"kind":"percent","value":0.375,"tol":0.001}                # 命中率/利用率\n'
        '  {"kind":"bits","value":"11110011","base":2,"width":8}       # 补码/原码/反码/IEEE754\n'
        '  {"kind":"int","value":12}                                    # 位数/行号/周期数\n'
        '  {"kind":"choice","value":"B"}                                # 选择题(可为数组表示多选)'
    ),
)

# ============================ 操作系统 ============================
_OS = Course(
    id="os",
    display="《操作系统》",
    short="操作系统",
    exam="408 统考",
    score=35,
    code_enabled=False,
    verification_types=["numeric", "rule", "citation"],
    knowledge_taxonomy=[
        {"chapter": "操作系统基础", "topics": ["操作系统概念", "系统调用", "内核结构", "操作系统引导", "虚拟机"]},
        {"chapter": "进程管理", "topics": ["进程与线程", "进程状态转换", "进程通信", "进程调度", "同步与互斥", "信号量", "经典同步问题", "死锁"]},
        {"chapter": "内存管理", "topics": ["连续分配", "分页", "分段", "段页式", "虚拟内存", "页面置换算法"]},
        {"chapter": "文件管理", "topics": ["文件与目录", "文件物理结构", "文件系统", "空闲空间管理"]},
        {"chapter": "输入输出管理", "topics": ["I/O控制方式", "缓冲与假脱机", "磁盘结构", "磁盘调度", "SSD"]},
    ],
    error_examples={
        "concept_confuse": ["进程↔线程", "并发↔并行", "死锁↔饥饿↔活锁", "分页↔分段", "逻辑地址↔物理地址", "同步↔互斥"],
        "calculation": ["带权周转时间算错", "缺页次数统计错", "磁头移动总道数算错", "页表项/地址位数划分错"],
        "reasoning": ["页面置换 Belady/命中判断错", "银行家算法安全序列推导错", "PV 操作顺序致死锁"],
        "boundary": ["信号量初值/临界区边界", "抖动(thrashing)临界", "磁盘调度到达端点方向"],
        "strategy": ["FCFS↔SJF↔RR 选错", "先来先服务↔最短寻道时间优先选错"],
    },
    sample_questions=[
        "SCAN 磁盘调度算法的磁头移动总道数怎么算？",
        "进程和线程到底有什么区别？",
        "用信号量解决生产者-消费者问题，PV 怎么放？",
    ],
    answer_format=(
        '本门为非代码题。"code"/"func_name" 一律空字符串；改为在 "final_answer_canonical" 给出可机器校验的规范答案，'
        '格式为 JSON，kind 取值之一：\n'
        '  {"kind":"int","value":12}                                    # 缺页数/移动道数/进程数\n'
        '  {"kind":"scalar","value":8.5,"tol":0.1,"unit":"ms"}         # 周转时间/等待时间\n'
        '  {"kind":"percent","value":0.75,"tol":0.001}                 # 命中率/CPU利用率\n'
        '  {"kind":"choice","value":"B"}                                # 选择题\n'
        '  {"kind":"set","values":["P1","P3","P4","P0","P2"],"order_matters":true}  # 安全序列等有序/无序集\n'
        '多解或需推导证明的题，final_answer_canonical 置 null，交由规则校验+讲解。'
    ),
)

# ============================ 计算机网络 ============================
_NET = Course(
    id="net",
    display="《计算机网络》",
    short="计算机网络",
    exam="408 统考",
    score=25,
    code_enabled=False,
    verification_types=["numeric", "rule", "citation"],
    knowledge_taxonomy=[
        {"chapter": "计算机网络概述", "topics": ["网络体系结构", "OSI与TCP/IP模型", "性能指标"]},
        {"chapter": "物理层", "topics": ["奈奎斯特与香农定理", "编码与调制", "交换方式", "传输介质"]},
        {"chapter": "数据链路层", "topics": ["组帧", "差错控制CRC", "流量控制与滑动窗口", "GBN与SR", "介质访问控制", "以太网", "VLAN", "PPP协议"]},
        {"chapter": "网络层", "topics": ["路由算法", "IPv4与子网划分", "CIDR", "ARP", "DHCP", "ICMP", "IPv6", "RIP/OSPF/BGP", "IP组播", "移动IP"]},
        {"chapter": "传输层", "topics": ["UDP", "TCP连接管理", "TCP可靠传输", "TCP流量控制", "TCP拥塞控制"]},
        {"chapter": "应用层", "topics": ["DNS", "FTP", "电子邮件SMTP/POP3", "HTTP"]},
    ],
    error_examples={
        "concept_confuse": ["TCP↔UDP", "OSI↔TCP/IP 分层", "交换机↔路由器", "MAC地址↔IP地址", "流量控制↔拥塞控制", "网络号↔主机号"],
        "calculation": ["子网掩码/可用主机数算错", "CRC 余数计算错", "TCP 序号/确认号算错", "发送时延/传播时延混算", "滑动窗口利用率算错"],
        "reasoning": ["拥塞控制慢启动↔拥塞避免阶段判断错", "路由表最长前缀匹配错", "三次握手/四次挥手状态错"],
        "boundary": ["子网全0/全1地址", "MTU 分片边界", "窗口大小上限 2^n-1"],
        "strategy": ["定长子网↔变长子网(VLSM)选错", "距离向量↔链路状态选错"],
    },
    sample_questions=[
        "192.168.1.0/24 划分 4 个子网，每个子网可用主机数是多少？",
        "TCP 三次握手为什么不能是两次？",
        "CRC 校验的余数怎么算？",
    ],
    answer_format=(
        '本门为非代码题。"code"/"func_name" 一律空字符串；改为在 "final_answer_canonical" 给出可机器校验的规范答案，'
        '格式为 JSON，kind 取值之一：\n'
        '  {"kind":"int","value":62}                                    # 可用主机数/子网数/时隙数\n'
        '  {"kind":"scalar","value":0.5,"tol":0.01,"unit":"ms"}        # 时延/吞吐量\n'
        '  {"kind":"percent","value":0.8,"tol":0.001}                  # 信道利用率\n'
        '  {"kind":"bits","value":"1010","base":2}                      # CRC 余数/帧检验序列\n'
        '  {"kind":"ipv4net","network":"192.168.1.0","prefix":26}      # 子网号/掩码/广播/网段\n'
        '  {"kind":"choice","value":"C"}                                # 选择题\n'
        '需推导证明的题，final_answer_canonical 置 null，交由规则校验+讲解。'
    ),
)


# 上面四门是**默认播种内容**，不再是运行时的唯一真相 —— 开通新租户时写进它的
# courses 表，之后走 /admin/courses 增删改。保留字面量而不是塞进迁移脚本，
# 是因为它们承载了按 2026 官方大纲校准过的知识点树，值得留在代码里可审阅。
DEFAULT_SEED: tuple[Course, ...] = (_DS, _CO, _OS, _NET)

DEFAULT_COURSE_ID = "ds-algo"

# 表里没有课程时的兜底。三种情况会走到它：库还没建 courses 表（改造前的旧库）、
# 建了但一行没有、以及库根本读不出来。任何一种都不该让讲解、组卷、复习计划直接崩掉。
_FALLBACK: dict[str, Course] = {c.id: c for c in DEFAULT_SEED}

# 进程内缓存，按库路径分桶 —— 测试会 monkeypatch settings.db_path 指向临时库，
# 按路径分才不会让上一个用例的课表泄进下一个。
# TTL 是给多 worker 部署留的安全网：另一个进程改了课程，本进程最多 30 秒后跟上；
# 同进程内的写走 invalidate() 立即生效。
_CACHE: dict[str, tuple[float, dict[str, Course]]] = {}
_CACHE_TTL = 30.0

_JSON_FIELDS = ("verification_types", "knowledge_taxonomy", "error_examples", "sample_questions")


def _to_row(c: Course, sort_order: int = 0) -> tuple:
    return (c.id, c.display, c.short, c.exam, c.score, int(c.code_enabled),
            json.dumps(c.verification_types, ensure_ascii=False),
            json.dumps(c.knowledge_taxonomy, ensure_ascii=False),
            json.dumps(c.error_examples, ensure_ascii=False),
            json.dumps(c.sample_questions, ensure_ascii=False),
            c.answer_format, sort_order, 1)


def _from_row(row) -> Course:
    payload = {f: json.loads(row[f] or ("[]" if f != "error_examples" else "{}")) for f in _JSON_FIELDS}
    return Course(id=row["id"], display=row["display"], short=row["short"], exam=row["exam"],
                  score=int(row["score"] or 0), code_enabled=bool(row["code_enabled"]),
                  answer_format=row["answer_format"] or "", **payload)


def _load() -> dict[str, Course]:
    """从库里读启用中的课程；读不到就回退 DEFAULT_SEED。"""
    from backend.core import db
    path = settings.db_path
    hit = _CACHE.get(path)
    if hit and (time.time() - hit[0]) < _CACHE_TTL:
        return hit[1]
    try:
        with db.get_conn() as conn:
            rows = conn.execute(
                "SELECT * FROM courses WHERE enabled=1 ORDER BY sort_order, id").fetchall()
        loaded = {r["id"]: _from_row(r) for r in rows}
    except Exception:  # noqa: BLE001 —— 缺表 / 库不可读，见 _FALLBACK 注释
        loaded = {}
    result = loaded or dict(_FALLBACK)
    _CACHE[path] = (time.time(), result)
    return result


def invalidate_cache() -> None:
    """课程被增删改之后调用，让本进程立即读到新值。"""
    _CACHE.clear()


def all_courses() -> dict[str, Course]:
    """全部启用课程，id → Course，按 sort_order 保序。

    取代了原来的模块级 `COURSES` 字典。刻意不保留那个名字：课程既然可以改，
    那个 dict 就会在某个时刻是过期的，而它过期的方式是安静地给出旧课表。
    """
    return _load()


def get_course(course_id: str | None) -> Course:
    """按 id 取课程；未知/空回退到课表里的第一门（保持旧行为的宽容语义）。"""
    courses = _load()
    if course_id and course_id in courses:
        return courses[course_id]
    if DEFAULT_COURSE_ID in courses:
        return courses[DEFAULT_COURSE_ID]
    return next(iter(courses.values()), _DS)


def course_ids() -> list[str]:
    return list(_load())


def is_valid_course(course_id: str | None) -> bool:
    return course_id in _load()


def list_all_courses() -> list[dict]:
    """管理页用：连停用的一起列出，并带上 sort_order/enabled 两个管理字段。"""
    from backend.core import db
    with db.get_conn() as conn:
        rows = conn.execute("SELECT * FROM courses ORDER BY sort_order, id").fetchall()
    out = []
    for r in rows:
        c = _from_row(r)
        out.append({"id": c.id, "display": c.display, "short": c.short, "exam": c.exam,
                    "score": c.score, "code_enabled": c.code_enabled,
                    "verification_types": c.verification_types,
                    "knowledge_taxonomy": c.knowledge_taxonomy,
                    "error_examples": c.error_examples,
                    "sample_questions": c.sample_questions,
                    "answer_format": c.answer_format,
                    "sort_order": int(r["sort_order"] or 0), "enabled": bool(r["enabled"])})
    return out


def upsert_course(course: Course, sort_order: int | None = None, enabled: bool = True) -> None:
    """新增或整体覆盖一门课。写完立即失效缓存，同进程内下一次读就是新值。"""
    from backend.core import db
    with db.get_conn() as conn:
        if sort_order is None:
            row = conn.execute("SELECT sort_order FROM courses WHERE id=?", (course.id,)).fetchone()
            if row is not None:
                sort_order = int(row["sort_order"] or 0)
            else:
                mx = conn.execute("SELECT MAX(sort_order) AS m FROM courses").fetchone()["m"]
                sort_order = (int(mx) + 1) if mx is not None else 0
        values = _to_row(course, sort_order)[:-1] + (int(enabled),)
        conn.execute(
            "INSERT INTO courses(id,display,short,exam,score,code_enabled,verification_types,"
            "knowledge_taxonomy,error_examples,sample_questions,answer_format,sort_order,enabled)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)"
            " ON CONFLICT(id) DO UPDATE SET display=excluded.display,short=excluded.short,"
            "exam=excluded.exam,score=excluded.score,code_enabled=excluded.code_enabled,"
            "verification_types=excluded.verification_types,"
            "knowledge_taxonomy=excluded.knowledge_taxonomy,error_examples=excluded.error_examples,"
            "sample_questions=excluded.sample_questions,answer_format=excluded.answer_format,"
            "sort_order=excluded.sort_order,enabled=excluded.enabled",
            values)
    invalidate_cache()


def set_course_enabled(course_id: str, enabled: bool) -> bool:
    """停用/启用一门课。

    刻意**不做物理删除**：problems / knowledge_points / mastery_records 里都有
    course_id 外键式的引用，删掉课程行只会留下一堆指向不存在课程的孤儿数据，
    而它们不会报错，只会在统计里变成谁也解释不了的数字。停用即从课表消失。
    """
    from backend.core import db
    with db.get_conn() as conn:
        cur = conn.execute("UPDATE courses SET enabled=? WHERE id=?", (int(enabled), course_id))
        changed = cur.rowcount > 0
    invalidate_cache()
    return changed


def set_setting(key: str, value: str) -> None:
    from backend.core import db
    with db.get_conn() as conn:
        conn.execute(
            "INSERT INTO app_settings(key,value,updated_at) VALUES(?,?,datetime('now'))"
            " ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at",
            (key, value))


def exam_structure() -> str:
    """试卷结构描述。

    原先是模块常量 `EXAM_STRUCTURE`（写死 408 那句话）。学校开了 408 之外的课
    之后这句话就不对了，所以移到 `app_settings` 表里；读不到时仍回退那句常量，
    行为对既有部署不变。
    """
    from backend.core import db
    try:
        with db.get_conn() as conn:
            row = conn.execute(
                "SELECT value FROM app_settings WHERE key='exam_structure'").fetchone()
        if row and (row["value"] or "").strip():
            return row["value"]
    except Exception:  # noqa: BLE001
        pass
    return EXAM_STRUCTURE
