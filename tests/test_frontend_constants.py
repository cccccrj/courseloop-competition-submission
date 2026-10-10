"""前端里那几份「抄自后端」的常量，必须真的和后端一样。

这类镜像常量抄的时候都对，改的时候只改一边——而且症状极轻微：页面照常渲染，
只是说的话和系统实际做的事不一致。没有断言就只能靠人眼在某次演示里偶然发现。

前端没有测试基础设施（package.json 里没有 test 脚本），所以这两条先由后端用例
读 .vue 源码来守。等前端建起 vitest，可以把它们搬过去。
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from backend.learning.study_budget import (
    DEFAULT_INTENSITY, DEFAULT_WEEKLY, FILL_RATIO, _MAX_DAILY as MAX_DAILY)

WIDGET = Path(__file__).resolve().parents[1] / "frontend/src/components/ui/CLWeekBudget.vue"


@pytest.fixture(scope="module")
def source() -> str:
    assert WIDGET.is_file(), f"找不到 {WIDGET}"
    return WIDGET.read_text(encoding="utf-8")


def _preset(source: str, name: str) -> tuple[list[int], int]:
    """取一个档位的 (七天分钟数, 休息日)。"""
    m = re.search(rf"^\s*{name}: \{{ minutes: \[([0-9,\s]+)\], buffer: (-?\d+) \}}",
                  source, re.MULTILINE)
    assert m, f"CLWeekBudget.vue 里没找到 PRESETS.{name}"
    return [int(x) for x in m.group(1).split(",") if x.strip()], int(m.group(2))


def test_default_intensity_preset_equals_backend_default(source):
    """默认档位的推荐时间 = 后端给新用户的默认时间。

    对不上的话，一个什么都没动的人打开页面会看到"适中"配着一组不属于该档的数字，
    界面上那句"你已手动调整过"还会平白诬告他一次。
    """
    assert _preset(source, DEFAULT_INTENSITY)[0] == list(DEFAULT_WEEKLY)


def test_every_intensity_has_a_preset_of_seven_days(source):
    for name in FILL_RATIO:
        minutes, buffer = _preset(source, name)
        assert len(minutes) == 7, f"{name} 档不是七天"
        # 上限用后端的 _MAX_DAILY：超了会在保存时被静默钳掉，界面显示的和存下来的
        # 就是两个数。前端 MAX_HOURS 与它必须是同一个值。
        assert all(0 <= m <= MAX_DAILY for m in minutes), f"{name} 档有超出上限的值"
        assert -1 <= buffer <= 6, f"{name} 档的休息日越界"


def test_rest_day_matches_the_zero_day(source):
    """休息日和"那天填 0"必须是同一件事。

    档位同时给时间和休息日，两者对不上就会出现"周日排了 8 小时但标着休息"
    或者"周日是 0 小时却不算休息日"——前者排期会把任务塞进休息日，
    后者连续学习天数会被那天打断（community.learning_streak 只跳过休息日）。
    """
    for name in FILL_RATIO:
        minutes, buffer = _preset(source, name)
        zero_days = {i for i, m in enumerate(minutes) if m == 0}
        assert zero_days == ({buffer} if buffer >= 0 else set()), \
            f"{name} 档：0 小时的是 {sorted(zero_days)}，休息日却是 {buffer}"


def test_presets_are_ordered_by_intensity(source):
    """轻 < 适中 < 冲刺。档位名和实际时长反着来的话，界面就是在骗人。"""
    totals = [sum(_preset(source, n)[0]) for n in ("light", "medium", "sprint")]
    assert totals == sorted(totals) and len(set(totals)) == 3, totals


def test_frontend_daily_cap_matches_backend(source):
    """界面允许填的上限 = 后端接受的上限。低于它就会静默钳值。"""
    m = re.search(r"const MAX_HOURS = (\d+)", source)
    assert m, "没找到 MAX_HOURS"
    assert int(m.group(1)) * 60 == MAX_DAILY


def test_fill_ratio_mirror_matches_backend(source):
    """CLWeekBudget 的 FILL 只用于显示「系统只排 ~70%」，抄错不会让计划算错，
    但会让界面上的百分比和服务端真正用的折扣对不上——比算错更难发现。"""
    m = re.search(r"const FILL: Record<string, number> = \{([^}]*)\}", source)
    assert m, "没找到 FILL 常量"
    front = json.loads("{" + re.sub(r"(\w+):", r'"\1":', m.group(1)).strip().rstrip(",") + "}")
    assert front == FILL_RATIO


# ---------------------------------------------------------------------------
# 资料入库页：后缀白名单、大小上限、类别 —— 三份都抄自 admin_routes
# ---------------------------------------------------------------------------
MATERIALS = Path(__file__).resolve().parents[1] / "frontend/src/views/Materials.vue"


@pytest.fixture(scope="module")
def materials() -> str:
    assert MATERIALS.is_file(), f"找不到 {MATERIALS}"
    return MATERIALS.read_text(encoding="utf-8")


def test_accepted_extensions_match_backend(materials):
    """前端先挡一道后缀，挡的范围必须和后端认的一样。

    前端窄了：用户被拦下，而后端本来收得进去 —— 一份传不上去的 .ppt，
    且页面给的理由是"不支持"，没人会想到去试。
    前端宽了：等一趟往返再拿 400，白等的还是那个人。
    """
    from backend.api.admin_routes import _EXT_PARSER

    m = re.search(r"const ACCEPT = \[([^\]]+)\]", materials)
    assert m, "Materials.vue 里没找到 ACCEPT"
    front = {x.strip().strip("'\"") for x in m.group(1).split(",") if x.strip()}
    assert front == set(_EXT_PARSER), f"前端 {sorted(front)} ≠ 后端 {sorted(_EXT_PARSER)}"


def test_upload_size_cap_matches_backend(materials):
    """大小上限对不上时，页面上写的「80 MB 以内」就是句假话。"""
    from backend.api.admin_routes import MAX_UPLOAD_BYTES

    m = re.search(r"const MAX_BYTES = (\d+) \* 1024 \* 1024", materials)
    assert m, "Materials.vue 里没找到 MAX_BYTES"
    assert int(m.group(1)) * 1024 * 1024 == MAX_UPLOAD_BYTES
    assert f"{MAX_UPLOAD_BYTES // 1024 // 1024} MB" in materials, "页面文案里的数也要一起改"


def test_doc_types_match_backend(materials):
    """少一个类别 = 那类资料永远传不上来，且页面上看不出少了什么。"""
    from backend.api.admin_routes import _DOC_TYPES

    front = set(re.findall(r"value: '(\w+)' \}", materials))
    assert set(_DOC_TYPES) <= front, f"前端缺少类别 {set(_DOC_TYPES) - front}"
