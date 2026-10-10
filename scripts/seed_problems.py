"""种子题库：为 3 个重点章节（栈与队列 / 树 / 排序）灌入固定题。

每道代码题的参考实现会先经沙箱 + 测试样例验证，仅通过者入库——保证题库「答案可靠」。
可作为固定评测集（对齐评审：解析可解释性、答案一致率）。

运行： /opt/miniconda3/envs/rag_env/bin/python -m scripts.seed_problems
"""
from __future__ import annotations

import json

from backend.core import db
from backend.verification.unit_test_runner import run_python_tests

# 树的层序构建（供树类题目 ref_code 复用）
_TREE = (
    "def _build(a):\n"
    "    if not a or a[0] is None: return None\n"
    "    nodes=[None if x is None else {'v':x,'l':None,'r':None} for x in a]\n"
    "    kids=nodes[1:][::-1]; root=nodes[0]\n"
    "    for nd in nodes:\n"
    "        if nd:\n"
    "            if kids: nd['l']=kids.pop()\n"
    "            if kids: nd['r']=kids.pop()\n"
    "    return root\n"
)

PROBLEMS: list[dict] = [
    # ---------------- 栈与队列 ----------------
    {"knowledge": "栈的应用", "type": "代码实现", "difficulty": 2,
     "stem": "实现 `valid_parentheses(s)`：s 仅含 ()[]{}，判断括号是否合法匹配，返回 bool。",
     "answer": "用栈：左括号入栈，右括号与栈顶配对；结束时栈空为合法。",
     "func": "valid_parentheses",
     "code": ("def valid_parentheses(s):\n"
              "    m={')':'(',']':'[','}':'{'}; st=[]\n"
              "    for c in s:\n"
              "        if c in '([{': st.append(c)\n"
              "        elif not st or st.pop()!=m[c]: return False\n"
              "    return not st\n"),
     "tests": [{"args": ["()[]{}"], "expected": True}, {"args": ["([)]"], "expected": False},
               {"args": ["("], "expected": False}, {"args": [""], "expected": True, "hidden": True}]},

    {"knowledge": "栈的应用", "type": "代码实现", "difficulty": 3,
     "stem": "实现 `eval_rpn(tokens)`：计算逆波兰表达式（tokens 为字符串列表，运算符 + - * /，除法向零取整），返回整数。",
     "answer": "用栈：数字入栈，运算符弹两操作数计算后压回。",
     "func": "eval_rpn",
     "code": ("def eval_rpn(tokens):\n"
              "    st=[]\n"
              "    for t in tokens:\n"
              "        if t in '+-*/':\n"
              "            b=st.pop(); a=st.pop()\n"
              "            st.append(a+b if t=='+' else a-b if t=='-' else a*b if t=='*' else int(a/b))\n"
              "        else: st.append(int(t))\n"
              "    return st[-1]\n"),
     "tests": [{"args": [["2", "1", "+", "3", "*"]], "expected": 9},
               {"args": [["4", "13", "5", "/", "+"]], "expected": 6}]},

    {"knowledge": "栈的应用", "type": "代码实现", "difficulty": 3,
     "stem": "实现 `daily_temperatures(T)`：返回列表，每个元素表示要等几天才有更高温度，没有则为 0。",
     "answer": "单调递减栈，存下标；遇更高温出栈并计算天数差。",
     "func": "daily_temperatures",
     "code": ("def daily_temperatures(T):\n"
              "    res=[0]*len(T); st=[]\n"
              "    for i,t in enumerate(T):\n"
              "        while st and T[st[-1]]<t:\n"
              "            j=st.pop(); res[j]=i-j\n"
              "        st.append(i)\n"
              "    return res\n"),
     "tests": [{"args": [[73, 74, 75, 71, 69, 72, 76, 73]], "expected": [1, 1, 4, 2, 1, 1, 0, 0]},
               {"args": [[30, 40, 50, 60]], "expected": [1, 1, 1, 0]}]},

    {"knowledge": "队列", "type": "代码实现", "difficulty": 3,
     "stem": "实现 `validate_stack(pushed, popped)`：给定入栈序列与出栈序列，判断出栈序列是否可能，返回 bool。",
     "answer": "模拟栈：按 pushed 压栈，栈顶等于 popped 当前元素则弹出。最后栈空即合法。",
     "func": "validate_stack",
     "code": ("def validate_stack(pushed, popped):\n"
              "    st=[]; i=0\n"
              "    for x in pushed:\n"
              "        st.append(x)\n"
              "        while st and i<len(popped) and st[-1]==popped[i]:\n"
              "            st.pop(); i+=1\n"
              "    return not st\n"),
     "tests": [{"args": [[1, 2, 3, 4, 5], [4, 5, 3, 2, 1]], "expected": True},
               {"args": [[1, 2, 3, 4, 5], [4, 3, 5, 1, 2]], "expected": False}]},

    {"knowledge": "队列", "type": "概念判断", "difficulty": 1, "verification": "rule",
     "stem": "简述栈和队列的区别，并各举一个典型应用场景。",
     "answer": "栈 LIFO（后进先出），如函数调用/括号匹配；队列 FIFO（先进先出），如 BFS/任务调度。",
     "rubric": [{"desc": "指出栈是 LIFO", "score": 25, "evidence": "后进先出"},
                {"desc": "指出队列是 FIFO", "score": 25, "evidence": "先进先出"},
                {"desc": "栈的应用举例", "score": 25, "evidence": "括号匹配/递归"},
                {"desc": "队列的应用举例", "score": 25, "evidence": "BFS/调度"}]},

    # ---------------- 树 ----------------
    {"knowledge": "二叉树遍历", "type": "代码实现", "difficulty": 2,
     "stem": "实现 `tree_max_depth(a)`：a 为二叉树的层序数组（null 用 None 表示），返回最大深度。",
     "answer": "递归：depth = 1 + max(左子树, 右子树)。",
     "func": "tree_max_depth",
     "code": (_TREE +
              "def tree_max_depth(a):\n"
              "    r=_build(a)\n"
              "    def d(n): return 0 if not n else 1+max(d(n['l']),d(n['r']))\n"
              "    return d(r)\n"),
     "tests": [{"args": [[3, 9, 20, None, None, 15, 7]], "expected": 3},
               {"args": [[]], "expected": 0}, {"args": [[1]], "expected": 1, "hidden": True}]},

    {"knowledge": "二叉树遍历", "type": "代码实现", "difficulty": 2,
     "stem": "实现 `inorder(a)`：a 为二叉树层序数组，返回中序遍历的值列表。",
     "answer": "中序：左 -> 根 -> 右。",
     "func": "inorder",
     "code": (_TREE +
              "def inorder(a):\n"
              "    r=_build(a); out=[]\n"
              "    def go(n):\n"
              "        if not n: return\n"
              "        go(n['l']); out.append(n['v']); go(n['r'])\n"
              "    go(r); return out\n"),
     "tests": [{"args": [[1, None, 2, 3]], "expected": [1, 3, 2]},
               {"args": [[2, 1, 3]], "expected": [1, 2, 3]}]},

    {"knowledge": "树", "type": "代码实现", "difficulty": 3,
     "stem": "实现 `count_leaves(a)`：返回二叉树（层序数组）的叶子结点数量。",
     "answer": "叶子=左右子树都为空的结点。",
     "func": "count_leaves",
     "code": (_TREE +
              "def count_leaves(a):\n"
              "    r=_build(a)\n"
              "    def c(n):\n"
              "        if not n: return 0\n"
              "        if not n['l'] and not n['r']: return 1\n"
              "        return c(n['l'])+c(n['r'])\n"
              "    return c(r)\n"),
     "tests": [{"args": [[1, 2, 3, 4, 5]], "expected": 3},
               {"args": [[1]], "expected": 1}, {"args": [[]], "expected": 0, "hidden": True}]},

    {"knowledge": "树", "type": "代码实现", "difficulty": 4,
     "stem": "实现 `is_balanced(a)`：判断二叉树（层序数组）是否为平衡二叉树（任意结点左右子树高度差 ≤ 1），返回 bool。",
     "answer": "自底向上求高度，若某子树不平衡则剪枝返回 -1。",
     "func": "is_balanced",
     "code": (_TREE +
              "def is_balanced(a):\n"
              "    r=_build(a); ok=[True]\n"
              "    def h(n):\n"
              "        if not n: return 0\n"
              "        lh=h(n['l']); rh=h(n['r'])\n"
              "        if abs(lh-rh)>1: ok[0]=False\n"
              "        return 1+max(lh,rh)\n"
              "    h(r); return ok[0]\n"),
     "tests": [{"args": [[3, 9, 20, None, None, 15, 7]], "expected": True},
               {"args": [[1, 2, 2, 3, 3, None, None, 4, 4]], "expected": False}]},

    {"knowledge": "树", "type": "概念判断", "difficulty": 2, "verification": "rule",
     "stem": "为什么二叉搜索树（BST）的中序遍历结果一定是有序（升序）的？",
     "answer": "BST 性质：左子树所有值 < 根 < 右子树所有值；中序遍历按 左-根-右 访问，恰好从小到大。",
     "rubric": [{"desc": "指出 BST 左<根<右 的性质", "score": 40, "evidence": "左子树<根<右子树"},
                {"desc": "说明中序遍历顺序是 左-根-右", "score": 30, "evidence": "左根右"},
                {"desc": "得出升序结论", "score": 30, "evidence": "有序/升序"}]},

    # ---------------- 排序 ----------------
    {"knowledge": "快速排序", "type": "代码实现", "difficulty": 3,
     "stem": "实现 `quick_sort(a)`：返回升序排序后的新列表（可用任意 partition）。",
     "answer": "分治：选基准，分成小于/大于两部分递归。",
     "func": "quick_sort",
     "code": ("def quick_sort(a):\n"
              "    if len(a)<=1: return list(a)\n"
              "    p=a[len(a)//2]\n"
              "    lo=[x for x in a if x<p]; eq=[x for x in a if x==p]; hi=[x for x in a if x>p]\n"
              "    return quick_sort(lo)+eq+quick_sort(hi)\n"),
     "tests": [{"args": [[3, 1, 2, 5, 4]], "expected": [1, 2, 3, 4, 5]},
               {"args": [[5, 5, 1]], "expected": [1, 5, 5]}, {"args": [[]], "expected": [], "hidden": True}]},

    {"knowledge": "排序", "type": "代码实现", "difficulty": 2,
     "stem": "实现 `merge_two_sorted(a, b)`：合并两个升序列表为一个升序列表。",
     "answer": "双指针依次取较小者。",
     "func": "merge_two_sorted",
     "code": ("def merge_two_sorted(a, b):\n"
              "    i=j=0; out=[]\n"
              "    while i<len(a) and j<len(b):\n"
              "        if a[i]<=b[j]: out.append(a[i]); i+=1\n"
              "        else: out.append(b[j]); j+=1\n"
              "    return out+a[i:]+b[j:]\n"),
     "tests": [{"args": [[1, 3, 5], [2, 4, 6]], "expected": [1, 2, 3, 4, 5, 6]},
               {"args": [[], [1]], "expected": [1]}]},

    {"knowledge": "排序", "type": "代码实现", "difficulty": 3,
     "stem": "实现 `kth_largest(nums, k)`：返回数组中第 k 大的元素。",
     "answer": "排序取倒数第 k，或用堆。",
     "func": "kth_largest",
     "code": ("def kth_largest(nums, k):\n"
              "    return sorted(nums)[-k]\n"),
     "tests": [{"args": [[3, 2, 1, 5, 6, 4], 2], "expected": 5},
               {"args": [[3, 2, 3, 1, 2, 4, 5, 5, 6], 4], "expected": 4}]},

    {"knowledge": "排序", "type": "代码实现", "difficulty": 4,
     "stem": "实现 `count_inversions(a)`：统计数组的逆序对数量（i<j 且 a[i]>a[j]）。",
     "answer": "归并排序过程中统计跨区间逆序对，O(n log n)。",
     "func": "count_inversions",
     "code": ("def count_inversions(a):\n"
              "    def ms(x):\n"
              "        if len(x)<=1: return x,0\n"
              "        m=len(x)//2\n"
              "        l,cl=ms(x[:m]); r,cr=ms(x[m:])\n"
              "        merged=[]; i=j=inv=0\n"
              "        while i<len(l) and j<len(r):\n"
              "            if l[i]<=r[j]: merged.append(l[i]); i+=1\n"
              "            else: merged.append(r[j]); j+=1; inv+=len(l)-i\n"
              "        return merged+l[i:]+r[j:], cl+cr+inv\n"
              "    return ms(a)[1]\n"),
     "tests": [{"args": [[2, 4, 1, 3, 5]], "expected": 3}, {"args": [[5, 4, 3, 2, 1]], "expected": 10}]},

    {"knowledge": "复杂度分析", "type": "复杂度分析", "difficulty": 2, "verification": "rule",
     "stem": "快速排序的平均时间复杂度和最坏时间复杂度分别是多少？最坏情况在什么时候发生？如何缓解？",
     "answer": "平均 O(n log n)，最坏 O(n^2)（每次基准都是最值，如已有序且取端点为基准）；随机化基准或三数取中可缓解。",
     "rubric": [{"desc": "平均 O(n log n)", "score": 30, "evidence": "nlogn"},
                {"desc": "最坏 O(n^2)", "score": 30, "evidence": "n^2/n²/平方"},
                {"desc": "指出最坏发生条件", "score": 20, "evidence": "有序/基准取到最值"},
                {"desc": "给出缓解方法", "score": 20, "evidence": "随机化/三数取中"}]},

    {"knowledge": "排序", "type": "概念判断", "difficulty": 2, "verification": "rule",
     "stem": "哪些常见排序算法是稳定的？请至少举出两个稳定和两个不稳定的例子。",
     "answer": "稳定：冒泡、插入、归并、计数；不稳定：快排、选择、堆排、希尔。",
     "rubric": [{"desc": "举出至少两个稳定排序", "score": 50, "evidence": "冒泡/插入/归并"},
                {"desc": "举出至少两个不稳定排序", "score": 50, "evidence": "快排/选择/堆排"}]},
]


def seed():
    db.init_db()
    db.clear_problem_bank("ds-algo")
    ok = 0; fail = 0; concept = 0
    for p in PROBLEMS:
        vtype = p.get("verification", "unit_test" if p.get("code") else "rule")
        if p.get("code") and p.get("tests"):
            vr = run_python_tests(p["code"], p["func"], p["tests"])
            if not vr.passed:
                fail += 1
                print(f"[跳过·参考答案未通过] {p['stem'][:30]} -> {vr.details}")
                continue
            pid = db.insert_problem("ds-algo", p["type"], p["stem"], p["difficulty"],
                                    p["answer"], vtype, knowledge=p["knowledge"],
                                    func_name=p["func"], ref_code=p["code"])
            for t in p["tests"]:
                db.add_test_case(pid, json.dumps(t["args"]), json.dumps(t["expected"]),
                                 1 if t.get("hidden") else 0)
            db.link_problem_knowledge(pid, db.get_or_create_knowledge_id(p["knowledge"], "ds-algo"))
            ok += 1
            print(f"[入库·已验证 {vr.extra.get('total')} 样例] {p['knowledge']} | {p['stem'][:34]}")
        else:
            pid = db.insert_problem("ds-algo", p["type"], p["stem"], p["difficulty"],
                                    p["answer"], vtype, knowledge=p["knowledge"])
            for r in p.get("rubric", []):
                db.add_rubric_item(pid, r["desc"], r["score"], r.get("evidence", ""))
            db.link_problem_knowledge(pid, db.get_or_create_knowledge_id(p["knowledge"], "ds-algo"))
            concept += 1
            print(f"[入库·概念题] {p['knowledge']} | {p['stem'][:34]}")
    print(f"\n完成：代码题 {ok} 道（含验证）、概念题 {concept} 道、跳过 {fail} 道。")
    print("知识点分布：", db.knowledge_distribution())


if __name__ == "__main__":
    seed()
