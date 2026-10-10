"""侧栏导航的长度约束。

侧栏放不下这件事是**加页面加出来的**，而且加的时候完全看不出来：每一次都只是
往路由表里多写一行，界面上照常渲染，只是最底下那两项被推到视口外 ——
助教得往下拖整页才点得到自己每天第一个要点的东西，头像和退出也一起消失。
一直到有人在矮屏幕上用了才会发现。

所以这里守的是"常驻可见的条目数"，而不是某个像素值：像素随字号、间距、
浏览器变，条目数才是加页面的人真正在改的东西。同 test_frontend_constants，
前端没有测试基础设施，这条先由后端用例读 router 源码来守。

高度换算（按 MainLayout.vue 现值）：一个条目 padding 10+10 + 行高约 20 = 40px，
条目间 gap 3px，段标题约 19px，段间 gap 18px。加上侧栏 padding 44、字标 42、
两处 gap 40、用户卡片 54，除导航外固定占掉约 180px。1366×768 这类笔记本的
视口高度约 660px，留给导航的就是 480px 上下。
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

ROUTER = Path(__file__).resolve().parents[1] / "frontend/src/router/index.ts"

# 常驻段最多几项。8 项 ≈ 19 + 8×40 + 7×3 = 360px，加上折叠段那行标题和角色段，
# 在 480px 的预算里还留着余量。要加第 9 项常驻页，先想清楚它凭什么天天占着视线 ——
# 或者给它标上 fold。
MAX_DEFAULT_ITEMS = 8


@pytest.fixture(scope="module")
def routes() -> list[dict]:
    """从路由表里取出会进侧栏的那些页。"""
    source = ROUTER.read_text(encoding="utf-8")
    out = []
    for line in source.splitlines():
        m = re.search(r"name: '([\w-]+)'.*meta: \{(.+?)\}", line)
        if not m:
            continue
        name, meta = m.group(1), m.group(2)
        title = re.search(r"title: '([^']+)'", meta)
        if not title or "hideNav: true" in meta:
            continue
        roles = re.search(r"roles: \[([^\]]*)\]", meta)
        out.append({
            "name": name,
            "title": title.group(1),
            "fold": "fold: true" in meta,
            "roles": [r.strip().strip("'") for r in roles.group(1).split(",")] if roles else [],
            "group": (g.group(1) if (g := re.search(r"group: '([^']+)'", meta)) else "学习"),
        })
    assert out, f"没能从 {ROUTER} 里解析出任何导航页"
    return out


def test_parsed_the_whole_nav(routes):
    """解析本身要能证伪 —— 正则漏掉几行的话，下面几条会全部空过。"""
    names = {r["name"] for r in routes}
    assert {"center", "hall", "solve", "exam", "teaching", "users"} <= names
    assert "profile" not in names, "个人信息是 hideNav 页，不该算进侧栏"
    assert len(routes) == 15, f"导航页数变了（现在 {len(routes)}），确认下面几条的预算还够"


def test_default_visible_nav_stays_short(routes):
    """常驻段（不折叠、不按角色限定）的条目数有上限。

    超了侧栏就会顶掉用户卡片。这条挡的是"随手再加一页"——加页面本身没问题，
    问题是默认所有人天天都看见它。
    """
    default = [r for r in routes if not r["fold"] and not r["roles"]]
    assert len(default) <= MAX_DEFAULT_ITEMS, \
        f"常驻条目 {len(default)} 项超了 {MAX_DEFAULT_ITEMS}：{[r['title'] for r in default]}"


def test_role_pages_are_never_folded(routes):
    """按角色限定的页不能被折叠起来。

    它们本来就只对少数人可见，再收进折叠段就是藏了两层 —— 助教工作台对助教
    是首页级的入口，不是"进阶与回看"。
    """
    hidden_twice = [r["title"] for r in routes if r["fold"] and r["roles"]]
    assert not hidden_twice, f"这些页既限角色又被折叠：{hidden_twice}"


def test_folded_pages_are_the_occasional_ones(routes):
    """折叠段装的是③④两段：阶段性回看 + 辅助旁支。

    这份名单在路由表注释里写着，但注释不会失败。真正的风险是**只改一头**：
    往③④加了页却忘了标 fold（于是它天天占着侧栏），或者把②里天天用的页
    标成了 fold（于是每天都要先展开一次）。
    """
    folded = {r["title"] for r in routes if r["fold"]}
    assert folded == {"知识图谱", "真题模考", "入学摸底", "拍照批改", "学情周报", "学习态势"}, \
        f"折叠段的名单变了：{sorted(folded)}"
