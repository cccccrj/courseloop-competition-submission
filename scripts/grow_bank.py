"""按知识点缺口补充经过确定性验证的题目。

默认只报告缺口；显式传 ``--apply`` 才会调用模型并写库。生成题通过代码样例、
canonical 数值或 rubric 自检后才以 source=generated 入库，题干相同时幂等复用。

``--kind choice`` 定向补单项选择题，并且**缺口也按选择题数来算**。模考的单选段
（`mock_exam` 每卷 40 道）和入学摸底（`db.placement_pool` 只收 canonical 可判的题）
都只吃这一种；不定向的话，代码门几乎必然出成 unit_test，跑完一轮那两处的缺口
一动不动 —— 而总题量确实涨了，很容易误判成"补过了但功能坏了"。

与 ``practice_to_bank.py`` 的分工：那个脚本靠先 `clear_problem_bank` 实现幂等，
只能在真题入库前跑一次；本脚本按题干幂等追加，不清库，是后续补题的唯一入口。

用法：
    python -m scripts.grow_bank --course ds-algo
    python -m scripts.grow_bank --course ds-algo --target 3 --limit-points 5 --apply
    python -m scripts.grow_bank --course ds-algo --kind choice --target 2 --apply
"""
from __future__ import annotations

import argparse
import json
import sys
from itertools import cycle
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend.agents import practice_generator  # noqa: E402
from backend.agents.prompts import SHAPE_CHOICE  # noqa: E402
from backend.api.core_routes import rag_context  # noqa: E402
from backend.core import db  # noqa: E402
from backend.core.config import settings  # noqa: E402
from backend.core.courses import get_course  # noqa: E402
from backend.learning import profile  # noqa: E402
from backend.learning.mock_exam import is_choice  # noqa: E402
from backend.verification.cross_check import DEFAULT_JUDGES, review  # noqa: E402


def choice_counts(course: str) -> dict[int, int]:
    """每个知识点已有多少道**选择题**。

    判据直接用组卷那边的 `is_choice`，不另写一个 —— 补题脚本和组卷器对"什么算选择题"
    一旦有两套说法，就会出现"脚本报补够了、组卷仍报缺口"，且看不出是谁错。
    取数走 `db.bank_pool`（同样是组卷的取数口），于是没有 problem_knowledge 关联的题
    在这里自动不算数：那种题组卷也用不了，算进来只会把缺口盖住。
    """
    counts: dict[int, int] = {}
    for p in db.bank_pool("__bank_builder__", course):
        if is_choice(p):
            counts[p["knowledge_id"]] = counts.get(p["knowledge_id"], 0) + 1
    return counts


# 出题角度。同一个知识点连出多道时轮换，既换检索到的教材片段，也换考查方向。
# 不换的话三次调用喂的是**完全相同**的上下文，模型被同一段材料锚住 ——
# 实测三道题干相似度 0.97/0.93/0.90，而 avoid 排除项说得再重也绕不开这个锚。
FACETS = ("定义与性质", "算法步骤与实现", "时间与空间复杂度", "典型应用与选型", "易错点与反例")


def point_difficulties(course: str) -> dict[int, int]:
    """知识点自身的难度（knowledge_points.difficulty）。

    **不能用 `build_profile` 的 `target_difficulty`** —— 那是按某个学生的掌握度算的
    自适应难度，而 grow_bank 用的是 `__bank_builder__` 这个没有任何作答记录的伪用户，
    于是 40 个缺口知识点的 target_difficulty 全是 1，补出来的题也全是难度 1。
    建库是给全体建的，该用知识点本身的难度。
    """
    with db.get_conn() as conn:
        return {int(r["id"]): int(r["difficulty"] or 2) for r in conn.execute(
            "SELECT id, difficulty FROM knowledge_points WHERE course_id=?", (course,))}


def existing_stems(course: str, knowledge_id: int, limit: int = 6) -> list[str]:
    """该知识点库里已有的题干，供出题时避开。取数走组卷同一个口。"""
    return [p["stem"] for p in db.bank_pool("__bank_builder__", course)
            if p.get("knowledge_id") == knowledge_id][-limit:]


def difficulty_ladder(target: int) -> list[int]:
    """绕着目标难度的取值阶梯：target、target-1、target+1，夹在 1~5 内去重。

    以 target 打头，所以只补一道时拿到的仍是这个知识点该有的难度；补多道才铺开。
    """
    out: list[int] = []
    for d in (target, target - 1, target + 1):
        d = max(1, min(5, d))
        if d not in out:
            out.append(d)
    return out


def answer_spread(course: str) -> dict[str, int]:
    """全门选择题的正确选项分布。跑完打出来，是"轮转有没有真的生效"的唯一凭据。

    不打的话，答案全压在一个字母上这种事只有在演示当天被学生发现。
    """
    spread: dict[str, int] = {}
    for p in db.bank_pool("__bank_builder__", course):
        if not is_choice(p):
            continue
        value = json.loads(p["answer"])["value"]
        for letter in (value if isinstance(value, list) else [value]):
            spread[str(letter).upper()] = spread.get(str(letter).upper(), 0) + 1
    return dict(sorted(spread.items()))


def main() -> int:
    ap = argparse.ArgumentParser(description="补齐知识点题库（默认 dry-run）")
    ap.add_argument("--course", default="ds-algo")
    ap.add_argument("--target", type=int, default=3, help="每个知识点目标题量")
    ap.add_argument("--limit-points", type=int, default=0, help="最多处理多少个缺口知识点")
    ap.add_argument("--max-attempts", type=int, default=3, help="每个知识点最多生成次数")
    ap.add_argument("--kind", choices=["any", "choice"], default="any",
                    help="定向题型；choice 同时把缺口口径换成「选择题数」")
    ap.add_argument("--apply", action="store_true", help="实际调用模型并写入数据库")
    ap.add_argument("--judges", default=",".join(DEFAULT_JUDGES),
                    help="选择题交叉复核阵容，逗号分隔；传空串关闭复核")
    args = ap.parse_args()

    get_course(args.course)  # 尽早拒绝未知课程
    db.init_db()
    target = max(1, args.target)
    shape = SHAPE_CHOICE if args.kind == "choice" else ""
    rows = profile.build_profile("__bank_builder__", args.course)
    # 缺口口径跟着 --kind 走。用 bank_count（全部题）去数选择题缺口的话，一个已经有
    # 4 道代码题的知识点会被判成"不缺"，而它的选择题其实是 0 道。
    counts = choice_counts(args.course) if shape else {}
    kp_difficulty = point_difficulties(args.course)
    have = ((r, counts.get(r["knowledge_id"], 0) if shape else int(r["bank_count"]))
            for r in rows)
    gaps = [(r, n) for r, n in have if n < target]
    gaps.sort(key=lambda t: (t[1], t[0]["chapter_index"], t[0]["knowledge_id"]))
    if args.limit_points > 0:
        gaps = gaps[:args.limit_points]

    label = "选择题" if shape else "题"
    print(f"▶ {args.course}：{len(gaps)} 个知识点低于 {target} {label}")
    for row, n in gaps:
        print(f"  #{row['knowledge_id']} {row['name']}：{n} → {target}")
    if not args.apply:
        print("\n未写库；确认范围后加 --apply。")
        return 0
    if not settings.llm_ready:
        print("✗ 未配置可用 LLM，无法生成。", file=sys.stderr)
        return 2

    added = reused = rejected = off_shape = api_failed = no_context = 0
    avoid_dropped = 0
    rejected_by_review = review_unusable = 0
    judges = tuple(j.strip() for j in args.judges.split(",") if j.strip())
    if shape and judges:
        print(f"  交叉复核阵容：{', '.join(judges)}（任一不同意即否）")
    # 正确选项在 A/B/C/D 间轮转（见 build_user_prompt 的说明：不指定的话模型会照抄
    # schema 示例里的 B）。跨知识点连续轮转而不是每题重置，整门的分布才会均匀。
    letters = cycle("ABCD")
    for row, n in gaps:
        need = max(0, target - n)
        attempts = 0
        # 该知识点已经出过什么，喂回提示词里当排除项。不传的话同一个知识点连出三道
        # 会得到同一道题换选项顺序（实测题干相似度 0.97），而入库去重按题干精确匹配，
        # 一道都拦不住。库里已有的也要算进去 —— 否则新题会去重复上一轮补的。
        avoid = existing_stems(args.course, int(row["knowledge_id"]))
        # 难度绕着知识点自身的难度铺开，而不是每道都出同一档。一个知识点补 15 道
        # 全是同一难度，练习推荐的分层和间隔复习的进阶就都无从谈起。
        ladder = difficulty_ladder(kp_difficulty.get(int(row["knowledge_id"]), 2))
        # 哪些角度在这门课的语料里检索不到东西。集齐全部角度就说明这个知识点根本
        # 没有对应材料，再试也是白试（见下面 blank 分支的说明）。
        blank_facets: set[str] = set()
        while need and attempts < max(1, args.max_attempts):
            attempts += 1
            # 查询里带上本轮的角度：换角度就换检索片段，是打破"同一段材料出三道
            # 同样的题"最有效的一手 —— 比在提示词里再加一句"请勿重复"管用得多。
            facet = FACETS[(attempts - 1) % len(FACETS)]
            want = ladder[(attempts - 1) % len(ladder)]
            # 出题这一路的异常必须就地接住。上一轮全量跑到第 826 道时，网关对某段
            # 教材片段返 402「检测到有敏感词」，异常一路冒到 main 外面把进程带走，
            # 那门课剩下的 39 个知识点一道没出 —— 而这一批已经跑了两小时。
            # 一次调用失败只该让这一次尝试作废：换个角度就换检索片段，下一轮多半就过。
            # 复核那边早就是这个规矩（`cross_check._ask_one` 失败返 '?' 不抛），
            # 出题这边漏了。
            try:
                context, cites = rag_context(f"{row['name']} {facet}", course=args.course)
            except Exception as exc:  # noqa: BLE001
                api_failed += 1
                print(f"  ✗ {row['name']} 检索失败（{type(exc).__name__}）：{str(exc)[:70]}")
                continue
            # 检索空了就别出题。上一轮全量补题有 110 道（占新增 7.4%）是在 context
            # 为空的情况下生成的 —— 模型凭自己的先验编了一道，照样过了门禁进了库，
            # 而这条管线的全部意义就是"题有教材依据"。os 中招最重（60 道、占该门新增
            # 的 20%）：它只有 104 块语料、一本教材都没有，
            # `rag_context('分页 算法步骤与实现', 'os')` 返回 0 字符。
            # 空上下文比调用失败更该拦 —— 失败至少在日志里留了痕，它连痕都不留。
            if not (context or "").strip():
                no_context += 1
                blank_facets.add(facet)
                # 换个角度就换检索片段，所以一个角度空不代表这个知识点没材料；
                # 但全部角度都空就是真没有，剩下的尝试全留给还有救的知识点。
                if len(blank_facets) >= len(FACETS):
                    print(f"  ⃠ {row['name']}：{len(FACETS)} 个角度都检索不到教材，跳过")
                    break
                continue
            kw = dict(context=context, difficulty=want, max_retries=1,
                      course=args.course, shape=shape, facet=facet)
            try:
                data = practice_generator.generate(
                    row["name"], choice_letter=next(letters) if shape else "",
                    avoid=avoid, **kw)
            except Exception as exc:  # noqa: BLE001
                # 库里某一道**旧题干本身**会被网关判成敏感词，而它每轮都被当排除项
                # 送进提示词 —— 于是这个知识点的每一次生成都返 402，题一道也补不上。
                # ds-algo「队列」就是这样连着两轮 30 次全灭：6 条排除项里第 4 条
                # （一道卡片出队的模拟题）单独送都必翻，其余五条单独送都正常。
                # 排除项是"锦上添花"不是"必须"：丢掉它重试一次，至少能出题；
                # 题干精确去重仍在 persist 那一层拦着，不会因此灌进重复题。
                try:
                    data = practice_generator.generate(
                        row["name"], choice_letter=next(letters) if shape else "",
                        avoid=[], **kw)
                except Exception as exc2:  # noqa: BLE001
                    api_failed += 1
                    print(f"  ✗ {row['name']} 生成失败（{type(exc2).__name__}）：{str(exc2)[:70]}")
                    continue
                avoid_dropped += 1
                print(f"  ! {row['name']} 带排除项被网关拦下（{type(exc).__name__}），"
                      f"改为不带排除项重试")
            # 交叉复核：既有门禁只验"模型与自己一致"，验不了"答案对不对"。
            # 实测 30 道全部通过既有门禁，其中 9 道标答是错的或题目本身是坏的。
            # 放在入库之前 —— 错题一旦进库，下一次组卷就可能把它抽到卷面上。
            if shape and judges:
                canon = data.get("final_answer_canonical") or {}
                verdict = review(data.get("stem", ""), canon, judges)
                if verdict.passed is not True:
                    rejected_by_review += 1
                    if verdict.passed is None:
                        review_unusable += 1
                    print(f"  ✗ {row['name']} 未过复核：{verdict.details[:90]}")
                    continue

            before = db.problem_id_by_stem(args.course, data.get("stem", ""))
            # 出处随题入库：出题时本来就检索了教材喂进提示词，此前把 citations 丢掉了。
            # difficulty 传的是本轮想要的那一档，不用模型自报的（它恒回 1）。
            pid = practice_generator.persist(data, args.course, row["name"],
                                             citations=cites, difficulty=want)
            stem = (data.get("stem") or "").strip()
            if stem:
                avoid.append(stem)
            if not pid:
                rejected += 1
                continue
            if before:
                reused += 1
                continue
            added += 1
            print(f"  ✓ {row['name']} 新增题 #{pid}")
            # 入库了不等于补上了缺口：模型可能无视定向，出成代码题或非选择的 canonical。
            # 那种题对模考单选段没用，不能拿它抵扣 need，否则脚本会报"补齐"而组卷照旧喊缺。
            # with_solution=True 是必须的：get_problem 默认藏 answer（防前端泄题），
            # 而 is_choice 的判据正是 answer 里的 canonical.kind —— 不带这个参数，
            # 每道题都会被判成"偏离题型"，need 永远减不下去。
            if shape and not is_choice(db.get_problem(pid, with_solution=True) or {}):
                off_shape += 1
                continue
            need -= 1
    if rejected_by_review:
        # 单独报出来：这个数掉下去说明出题模型在退步，混进"未通过验证"里就看不见了
        tail_review = (f"，交叉复核否掉 {rejected_by_review}"
                       + (f"（其中 {review_unusable} 条是复核员没能作答，非确定错误）"
                          if review_unusable else ""))
    else:
        tail_review = ""
    tail = (f"，其中偏离题型 {off_shape}（未计入缺口）" if shape else "") + tail_review
    # 生成失败与"被门禁否掉"分开报：前者是网关/网络的事，后者是题的质量问题。
    # 混成一个数的话，一次网关抽风会被读成模型质量下降。
    if api_failed:
        tail += f"，生成调用失败 {api_failed}"
    # 检索为空单独报：这个数大，说明缺的是语料不是题 —— 该去补教材，不是继续烧钱出题。
    if no_context:
        tail += f"，检索为空跳过 {no_context}"
    # 退化出的题（没带排除项）也要报：它们更可能与已有题撞车，值得抽查。
    if avoid_dropped:
        tail += f"，去掉排除项才出的 {avoid_dropped}"
    print(f"\n完成：新增 {added}，复用 {reused}，未通过验证 {rejected}{tail}。")
    if shape:
        print(f"该门选择题正确选项分布：{answer_spread(args.course)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
