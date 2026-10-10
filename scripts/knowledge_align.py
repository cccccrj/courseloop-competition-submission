"""知识点对齐：把合成/真题数据里的自由文本知识点收敛到 courses.py 的大纲。

存在的意义：入库脚本原本直接 ``db.get_or_create_knowledge_id(自由文本)``，模型每换一种
说法就新建一个知识点空壳 —— 库里 knowledge_points 曾涨到大纲之外几十条，掌握度按知识点
聚合时被摊薄到一堆同义空壳上，前端知识树也多出没有章节归属的孤儿节点。

对齐顺序：别名表 → 大纲精确 → 包含 → 字符二元组 Jaccard → 「<章节>·其他」→ 「未归类」。
别名表与大纲外专题都是显式、有界、可审计的；换数据源后请用 --dry-run 复查兜底清单再补。

被 scripts/practice_to_bank.py 与 scripts/ingest_past_papers.py 共用。
"""
from __future__ import annotations

import hashlib
import re
import unicodedata

from backend.core.courses import get_course

UNCLASSIFIED = "未归类"

_PUNCT = re.compile(r"[\s　,，.。、;；:：!！?？()（）\[\]【】{}《》\"'“”‘’/\\|_\-~`*#+=<>%$@^&]+")


def _norm(text: str) -> str:
    """归一化：全角转半角、去标点空白、转小写。用于知识点匹配与题干去重。"""
    s = unicodedata.normalize("NFKC", str(text or "")).lower()
    return _PUNCT.sub("", s)


def stem_key(stem: str) -> str:
    """题干指纹，用于跨文件去重。"""
    return hashlib.sha1(_norm(stem).encode("utf-8")).hexdigest()


def _bigrams(s: str) -> set[str]:
    return {s[i:i + 2] for i in range(len(s) - 1)} if len(s) > 1 else ({s} if s else set())


def _jaccard(a: str, b: str) -> float:
    ba, bb = _bigrams(a), _bigrams(b)
    if not ba or not bb:
        return 0.0
    return len(ba & bb) / len(ba | bb)


# —— 大纲外专题：408 官方大纲没有单列、但题库确实覆盖的算法主题 ——
# 显式、有界、可审计；不是自由文本新建。章节统一挂「<课程章节>（大纲外）」。
EXTRA_TOPICS: dict[str, list[tuple[str, str, int]]] = {
    "ds-algo": [
        ("算法专题（大纲外）", "递归与分治", 3),
        ("算法专题（大纲外）", "回溯法", 4),
        ("算法专题（大纲外）", "贪心算法", 4),
        ("算法专题（大纲外）", "动态规划", 5),
    ],
    "net": [
        ("计算机网络概述（大纲外）", "SDN架构", 3),
    ],
}

# —— 关键词规则：真题标签是长尾（136 种说法 / 191 条），逐条建别名不现实 ——
# 顺序敏感，**先具体后笼统**：命中即停。只在别名/精确/包含/相似都落空后才用，
# 所以不会抢走更准的匹配。每条都能对着大纲核对，出错也看得出来错在哪。
KEYWORD_TOPICS: dict[str, list[tuple[str, str]]] = {
    "co": [
        ("组相联", "Cache映射与替换"), ("映像", "Cache映射与替换"), ("cache", "Cache映射与替换"),
        ("微程序", "控制器"), ("微指令", "控制器"), ("控制信号", "控制器"), ("硬布线", "控制器"),
        ("流水", "指令流水线"), ("冒险", "指令流水线"), ("相关性", "指令流水线"),
        ("中断", "异常与中断"), ("异常", "异常与中断"),
        ("dma", "程序查询/中断/DMA"), ("通道", "程序查询/中断/DMA"),
        ("磁盘", "I/O接口"), ("打印", "I/O接口"), ("i/o", "I/O接口"), ("io", "I/O接口"),
        ("总线", "总线"),
        ("浮点", "浮点数与IEEE754"), ("ieee", "浮点数与IEEE754"), ("阶码", "浮点数与IEEE754"),
        ("补码", "定点数补码"), ("原码", "定点数补码"), ("反码", "定点数补码"),
        ("移码", "定点数补码"), ("机器数", "定点数补码"), ("定点", "定点数补码"),
        ("溢出", "定点数补码"),
        ("乘法", "乘除运算"), ("除法", "乘除运算"),
        ("寻址", "寻址方式"),
        ("指令", "指令格式"), ("mips", "指令格式"), ("riscv", "指令格式"), ("risc", "CISC与RISC"),
        ("寄存器", "CPU数据通路"), ("数据通路", "CPU数据通路"),
        ("运算器", "加法器与ALU"), ("alu", "加法器与ALU"), ("逻辑电路", "加法器与ALU"),
        ("逻辑表达式", "加法器与ALU"),
        ("dram", "SRAM与DRAM"), ("sram", "SRAM与DRAM"), ("刷新", "SRAM与DRAM"),
        ("动态存储", "SRAM与DRAM"),
        ("虚拟", "虚拟存储器"), ("内存管理", "虚拟存储器"), ("页", "虚拟存储器"),
        ("存储", "存储器分类"),
        ("加速比", "性能指标CPI/MIPS"), ("cpi", "性能指标CPI/MIPS"), ("性能", "性能指标CPI/MIPS"),
        ("字长", "计算机系统层次结构"), ("诺依曼", "计算机系统层次结构"),
        ("neumann", "计算机系统层次结构"), ("体系结构", "计算机系统层次结构"),
        ("组织结构", "计算机系统层次结构"), ("计算机组成原理", "计算机系统层次结构"),
        ("计算机系统", "计算机系统层次结构"), ("大小端", "大小端与数据对齐"),
        ("对齐", "大小端与数据对齐"),
    ],
    "os": [
        ("死锁", "死锁"), ("银行家", "死锁"),
        ("调度", "进程调度"), ("轮转", "进程调度"),
        ("信号量", "信号量"), ("同步", "同步与互斥"), ("互斥", "同步与互斥"),
        ("线程", "进程与线程"), ("进程", "进程与线程"),
        ("置换", "页面置换算法"), ("分页", "分页"), ("分段", "分段"),
        ("虚拟", "虚拟内存"), ("内存", "连续分配"),
        ("文件", "文件与目录"), ("目录", "文件与目录"), ("索引", "文件物理结构"),
        ("磁盘", "磁盘调度"), ("缓冲", "缓冲与假脱机"), ("spooling", "缓冲与假脱机"),
        ("内核", "内核结构"), ("系统调用", "系统调用"),
    ],
    "net": [
        ("拥塞", "TCP拥塞控制"), ("流量控制", "TCP流量控制"), ("滑动窗口", "流量控制与滑动窗口"),
        ("握手", "TCP连接管理"), ("tcp", "TCP可靠传输"), ("udp", "UDP"),
        ("crc", "差错控制CRC"), ("差错", "差错控制CRC"), ("组帧", "组帧"),
        ("以太", "以太网"), ("csma", "介质访问控制"), ("vlan", "VLAN"),
        ("子网", "IPv4与子网划分"), ("ip地址", "IPv4与子网划分"), ("cidr", "CIDR"),
        ("路由", "路由算法"), ("arp", "ARP"), ("dhcp", "DHCP"), ("icmp", "ICMP"),
        ("dns", "DNS"), ("http", "HTTP"), ("邮件", "电子邮件SMTP/POP3"), ("ftp", "FTP"),
        ("osi", "OSI与TCP/IP模型"), ("体系结构", "网络体系结构"), ("协议", "网络体系结构"),
    ],
    "ds-algo": [
        ("二分", "顺序与折半查找"), ("折半", "顺序与折半查找"),
        ("散列", "散列表"), ("哈希", "散列表"),
        ("排序", "快速排序"), ("堆", "堆"), ("并查集", "并查集"),
        ("图", "图的存储"), ("最短路", "最短路径"), ("生成树", "最小生成树"),
        ("拓扑", "拓扑排序与关键路径"),
        ("二叉树", "二叉树遍历"), ("树", "二叉树遍历"),
        ("链表", "链表"), ("栈", "栈"), ("队列", "队列"), ("串", "串与模式匹配"),
        ("复杂度", "时间复杂度分析"), ("动态规划", "动态规划"), ("贪心", "贪心算法"),
        ("回溯", "回溯法"), ("分治", "递归与分治"), ("递归", "递归与分治"),
    ],
}

# —— 别名：合成数据里的说法 → 大纲（或上面 EXTRA_TOPICS）里的标准知识点 ——
# 只收在实际数据里出现过的说法，改数据源后请重跑 --dry-run 复查兜底清单再补。
ALIASES: dict[str, dict[str, str]] = {
    "ds-algo": {
        "二分查找": "顺序与折半查找", "折半查找": "顺序与折半查找",
        "字符串": "串与模式匹配", "哈希表": "散列表",
        "递归": "递归与分治", "递归与分治": "递归与分治",
        "回溯": "回溯法", "贪心": "贪心算法", "贪心算法": "贪心算法",
        "动态规划": "动态规划",
    },
    "co": {
        "大小端存储": "大小端与数据对齐",
        "原码乘法运算": "乘除运算", "原码乘法": "乘除运算",
        "原码一位乘法": "乘除运算", "补码乘法运算": "乘除运算",
        "补码加减运算": "定点数补码", "定点补码加减运算": "定点数补码",
        "补码与原码转换": "定点数补码", "移码表示": "定点数补码", "移码表示法": "定点数补码",
        "立即寻址": "寻址方式", "直接寻址": "寻址方式",
        "浮点数表示": "浮点数与IEEE754",
        "中断处理": "异常与中断", "中断处理过程": "异常与中断", "中断处理时间计算": "异常与中断",
        "直接映射Cache": "Cache映射与替换",
        "CPI计算": "性能指标CPI/MIPS",
        "DMA控制方式": "程序查询/中断/DMA",
        "I/O端口编址方式": "I/O接口",
        # —— 以下来自真题（组原2023）的自由文本标签 ——
        "存储程序概念": "计算机系统层次结构", "计算机系统兼容性": "计算机系统层次结构",
        "Amdahl定律": "性能指标CPI/MIPS", "性能优化策略": "性能指标CPI/MIPS",
        "整数边界存储": "大小端与数据对齐", "相对寻址": "寻址方式",
        "总线仲裁": "总线", "磁盘性能计算": "I/O接口", "I/O控制方式": "程序查询/中断/DMA",
        "中断隐指令": "异常与中断", "中断处理与I/O传输速率计算": "异常与中断",
        "中断处理时钟周期计算": "异常与中断", "中断处理开销计算": "异常与中断",
        "动态存储器刷新": "SRAM与DRAM", "存储器扩展设计": "存储器分类",
        "存储器体系结构": "存储器分类", "内存写入": "Cache映射与替换",
        "Cache性能影响因素": "Cache映射与替换", "Cache地址映射": "Cache映射与替换",
        "Cache缺失计算": "Cache映射与替换", "Cache访问流程": "Cache映射与替换",
        "变形补码溢出判断": "定点数补码", "补码运算": "定点数补码", "补码表示范围": "定点数补码",
        "RISC-V浮点格式": "浮点数与IEEE754",
        "IEEE754浮点数表示、补码表示、小端存储": "浮点数与IEEE754",
        "IEEE754浮点数加减运算": "浮点数与IEEE754",
        "硬布线控制器特点": "控制器", "字段直接编码法位数计算": "控制器",
        "程序员可见寄存器": "CPU数据通路", "指令执行流程": "CPU数据通路", "指令执行周期": "CPU数据通路",
        "操作码扩展": "指令格式", "操作码平均长度计算": "指令格式",
        "流水线时钟周期": "指令流水线", "流水线冲突": "指令流水线",
        "流水线冲突与性能分析": "指令流水线", "流水线时空图": "指令流水线",
    },
    "os": {
        "微内核OS结构": "内核结构", "分层结构OS": "内核结构", "操作系统结构": "内核结构",
        "线程与进程的比较": "进程与线程", "线程模型": "进程与线程",
        "多对多线程模型": "进程与线程", "多对一线程模型": "进程与线程",
        "虚拟存储器": "虚拟内存",
        "索引分配": "文件物理结构", "索引结构": "文件物理结构",
        "成组链接法": "空闲空间管理",
    },
    "net": {
        "循环冗余检测CRC": "差错控制CRC",
        "滑动窗口协议": "流量控制与滑动窗口",
        "选择重传协议": "GBN与SR", "GBN协议": "GBN与SR",
        "RIP协议": "RIP/OSPF/BGP",
        "OSI模型": "OSI与TCP/IP模型", "OSI参考模型": "OSI与TCP/IP模型",
        "POP3协议": "电子邮件SMTP/POP3",
        "以太帧结构": "以太网",
        "802.11帧地址字段": "介质访问控制", "802.11帧结构": "介质访问控制",
        "调制解调": "编码与调制",
        "IPv4地址分类": "IPv4与子网划分",
        "SDN架构": "SDN架构",
    },
}


def _norm(text: str) -> str:
    """归一化：全角转半角、去标点空白、转小写。用于知识点匹配与题干去重。"""
    s = unicodedata.normalize("NFKC", str(text or "")).lower()
    return _PUNCT.sub("", s)


def _stem_key(stem: str) -> str:
    return hashlib.sha1(_norm(stem).encode("utf-8")).hexdigest()


def _bigrams(s: str) -> set[str]:
    return {s[i:i + 2] for i in range(len(s) - 1)} if len(s) > 1 else ({s} if s else set())


def _jaccard(a: str, b: str) -> float:
    ba, bb = _bigrams(a), _bigrams(b)
    if not ba or not bb:
        return 0.0
    return len(ba & bb) / len(ba | bb)


class KnowledgeAligner:
    """把合成数据里的自由文本知识点对齐到 courses.py 大纲。

    存在的意义：原实现直接 get_or_create_knowledge_id(自由文本)，模型每换一种说法就新建一个
    知识点空壳 —— 库里 knowledge_points 曾涨到 120 条而大纲只有 116 条，掌握度按知识点聚合时
    被摊薄到一堆同义空壳上。
    """

    SIM_THRESHOLD = 0.5

    def __init__(self, course_id: str):
        c = get_course(course_id)
        self.course_id = course_id
        self.topic_chapter: dict[str, str] = {}
        self.topic_difficulty: dict[str, int] = {}
        self.chapters: list[str] = []
        for idx, ch in enumerate(c.knowledge_taxonomy):
            chapter = ch.get("chapter", "")
            self.chapters.append(chapter)
            base = min(5, 1 + idx // 2)  # 与 seed_knowledge 的难度基线保持一致
            for t in ch.get("topics", []):
                self.topic_chapter[t] = chapter
                self.topic_difficulty[t] = base
        for chapter, topic, diff in EXTRA_TOPICS.get(course_id, []):
            self.topic_chapter[topic] = chapter
            self.topic_difficulty[topic] = diff
            if chapter not in self.chapters:
                self.chapters.append(chapter)
        self._by_norm = {_norm(t): t for t in self.topic_chapter}
        self._alias = {_norm(k): v for k, v in ALIASES.get(course_id, {}).items()}
        self._keywords = [(_norm(k), v) for k, v in KEYWORD_TOPICS.get(course_id, [])
                          if v in self.topic_chapter]
        self.stats: dict[str, int] = {"alias": 0, "exact": 0, "contain": 0, "similar": 0,
                                      "keyword": 0, "chapter": 0, "unclassified": 0}
        self.unmatched: dict[str, str] = {}   # 原始自由文本 -> 落到哪个兜底节点

    def _match_topic(self, name: str) -> tuple[str, str] | None:
        n = _norm(name)
        if not n:
            return None
        if n in self._alias:
            self.stats["alias"] += 1
            return self._alias[n], "alias"
        if n in self._by_norm:
            self.stats["exact"] += 1
            return self._by_norm[n], "exact"
        # 包含关系：取最长的被包含项，避免「树」匹配到「二叉树的遍历」这种过宽的命中
        cands = [t for k, t in self._by_norm.items() if k and (k in n or n in k)]
        if cands:
            self.stats["contain"] += 1
            return max(cands, key=len), "contain"
        best, score = None, 0.0
        for k, t in self._by_norm.items():
            s = _jaccard(n, k)
            if s > score:
                best, score = t, s
        if best and score >= self.SIM_THRESHOLD:
            self.stats["similar"] += 1
            return best, "similar"
        for kw, topic in self._keywords:
            if kw and kw in n:
                self.stats["keyword"] += 1
                return topic, "keyword"
        return None

    def align(self, name: str) -> tuple[str, str, int]:
        """返回 (入库知识点名, 章节, 难度基线)。"""
        hit = self._match_topic(name)
        if hit:
            topic = hit[0]
            return topic, self.topic_chapter[topic], self.topic_difficulty[topic]
        # 兜底一：能认出章节就挂到「<章节>·其他」
        n = _norm(name)
        for idx, ch in enumerate(self.chapters):
            cn = _norm(ch)
            if cn and (cn in n or n in cn or _jaccard(n, cn) >= self.SIM_THRESHOLD):
                self.stats["chapter"] += 1
                self.unmatched[name] = f"{ch}·其他"
                return f"{ch}·其他", ch, min(5, 1 + idx // 2)
        # 兜底二：全课一个「未归类」，数量有界
        self.stats["unclassified"] += 1
        self.unmatched[name] = UNCLASSIFIED
        return UNCLASSIFIED, UNCLASSIFIED, 2
