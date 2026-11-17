"""浮层（右侧客服面板）的可见文案必须是英文（回归护栏）。

背景：这是跨境演示项目。**右侧浮层**面向国外访客，制造业 fork 带过来的默认欢迎语
是中文（「我是售后知识助手，可以按故障现象帮你查手册与历史工单」），线上真的显示过，
访客第一眼看到中文会怀疑这不是给自己准备的产品。

⚠️ 刻意的不对称：**左侧「演示控制台」（`index.html`）保留中文**。
它面向国内的演示操作者，翻译成英文反而让同事看不懂；只有浮层面向国外访客。
所以本测试只覆盖浮层相关文件，不检查 `index.html` 的正文。

规则：浮层文件里**注释之外**的字符串字面量不得包含中文。
注释（含行尾注释）不受限——中文注释是我们的写作规范。
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WIDGET = ROOT / "frontend" / "widget"

# 只覆盖右侧浮层；宿主演示页 index.html 是给国内同事看的控制台，保留中文
WIDGET_CHROME_FILES = [
    WIDGET / "widget.html",
    WIDGET / "controllers" / "app.js",
    WIDGET / "adapters" / "api.js",
    WIDGET / "loader.js",
]

CJK = re.compile(r"[\u4e00-\u9fa5]")
QUOTED_CJK = re.compile(r"'[^'\n]*[\u4e00-\u9fa5][^'\n]*'|\"[^\"\n]*[\u4e00-\u9fa5][^\"\n]*\"")


def _strip_comments(text: str) -> list[tuple[int, str]]:
    """返回 (行号, 去掉注释后的行)。只处理 // 与 /* */ 与 <!-- -->。"""
    rows: list[tuple[int, str]] = []
    in_block = False
    in_html = False
    for lineno, line in enumerate(text.split("\n"), start=1):
        stripped = line.lstrip()
        if in_block:
            if "*/" in line:
                in_block = False
            continue
        if in_html:
            if "-->" in line:
                in_html = False
            continue
        if stripped.startswith("/*"):
            if "*/" not in stripped:
                in_block = True
            continue
        if stripped.startswith("<!--"):
            if "-->" not in stripped:
                in_html = True
            continue
        if stripped.startswith("//") or stripped.startswith("*"):
            continue
        # 行尾注释：找引号之外的 // 或 /*
        in_single = in_double = False
        cut = len(line)
        i = 0
        while i < len(line):
            ch = line[i]
            if ch == "\\":
                i += 2
                continue
            if ch == "'" and not in_double:
                in_single = not in_single
            elif ch == '"' and not in_single:
                in_double = not in_double
            elif not in_single and not in_double and (
                line.startswith("//", i) or line.startswith("/*", i)
            ):
                cut = i
                break
            i += 1
        rows.append((lineno, line[:cut]))
    return rows


def test_widget_visible_copy_has_no_chinese() -> None:
    problems: list[str] = []
    for path in WIDGET_CHROME_FILES:
        for lineno, code in _strip_comments(path.read_text(encoding="utf-8")):
            for match in QUOTED_CJK.finditer(code):
                problems.append(f"{path.relative_to(ROOT)}:{lineno} {match.group(0)[:60]}")
    assert not problems, "浮层可见文案里仍有中文（应改为英文）：\n" + "\n".join(problems)


def test_default_welcome_is_not_chinese() -> None:
    from app.routers.admin_config import _DEFAULT_WELCOME

    assert not CJK.search(_DEFAULT_WELCOME), _DEFAULT_WELCOME
