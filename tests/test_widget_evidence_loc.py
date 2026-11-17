"""引用原文出处的纯逻辑测试（`app.widget_service._loc`，不连库）。

钉住一条口径：出处优先用落库的 `meta.loc`（章节标题 / 工作表行号），
再退到章节号 / 页码。否则没有编号的章节（"一、品牌标识"）会得到空出处，
表格行也会被改写成「§序号」这种看不懂的定位。
"""

from __future__ import annotations

from app.widget_service import _loc


def test_loc_prefers_stored_meta_loc() -> None:
    assert _loc(None, 0, 0, "一、品牌标识") == "一、品牌标识"
    assert _loc("1", 0, 0, "型式试验证书台账 第4行") == "型式试验证书台账 第4行"


def test_loc_falls_back_to_section_no_and_page() -> None:
    assert _loc("3.2", 6, 6, None) == "§3.2 · P6"
    assert _loc(None, 6, 6, "") == "P6", "空字符串的 meta.loc 不能挡掉页码回退"
    assert _loc(None, 0, 0, None) == ""
