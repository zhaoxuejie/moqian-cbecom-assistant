"""统计后台还有多少中文文案没被 TEXT_MAP/DICT 覆盖（只读审计）。"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path("frontend/admin")
I18N = (ROOT / "controllers" / "i18n.js").read_text(encoding="utf-8")

# TEXT_MAP 的键（"中文": "English",）与 DICT 值（"key": "中文"）
text_map_block = I18N.split("var TEXT_MAP = {", 1)[1].split("};", 1)[0]
covered = set(re.findall(r'"((?:[^"\\]|\\.)+)"\s*:', text_map_block))
# DICT zh 里的中文值也算已覆盖（通过 data-i18n/导航翻译）
zh_block = I18N.split('"zh": {', 1)[1].split("},", 1)[0] if '"zh": {' in I18N else ""
covered |= set(re.findall(r'"((?:[^"\\]|\\.)+)"\s*:', zh_block))
covered = {item for item in covered if re.search(r"[\u4e00-\u9fff]", item)}

CJK = re.compile(r"[\u4e00-\u9fff]")


def html_segments(text: str) -> set[str]:
    text = re.sub(r"<script.*?</script>", " ", text, flags=re.S | re.I)
    text = re.sub(r"<style.*?</style>", " ", text, flags=re.S | re.I)
    text = re.sub(r"<!--.*?-->", " ", text, flags=re.S)
    segs = set(re.findall(r">([^<>]+)<", text))
    attrs = set(re.findall(r'(?:placeholder|title|aria-label)="([^"]+)"', text))
    out = set()
    for seg in segs | attrs:
        seg = re.sub(r"\s+", " ", seg).strip()
        if seg and CJK.search(seg) and len(seg) <= 30:
            out.add(seg)
    return out


def js_segments(text: str) -> set[str]:
    # 去掉整行注释（粗略）
    lines = text.splitlines()
    text = "\n".join(
        line for line in lines if not line.strip().startswith(("*", "//", "/*"))
    )
    out = set()
    pattern = (
        r'"((?:[^"\\]|\\.)+)"|\'((?:[^\'\\]|\\.)+)\'|`((?:[^`\\]|\\.)+)`'
    )
    for match in re.finditer(pattern, text):
        seg = next(group for group in match.groups() if group is not None)
        seg = re.sub(r"\s+", " ", seg).strip()
        if seg and CJK.search(seg) and len(seg) <= 30 and not seg.startswith("http"):
            out.add(seg)
    return out


total_remaining = 0
rows = []
js_files = list((ROOT / "controllers").glob("*.js")) + list(
    (ROOT / "adapters").glob("*.js")
)
for path in sorted(list((ROOT / "pages").glob("*.html")) + js_files):
    if path.name == "i18n.js":
        continue
    text = path.read_text(encoding="utf-8")
    segs = html_segments(text) if path.suffix == ".html" else js_segments(text)
    remain = {seg for seg in segs if seg not in covered}
    if remain:
        rows.append((path.name, len(remain), sorted(remain)[:6]))
        total_remaining += len(remain)

rows.sort(key=lambda row: row[1], reverse=True)
print(f"covered entries: {len(covered)}")
print(f"files with remaining Chinese strings: {len(rows)}")
print(f"total remaining unique segments: {total_remaining}\n")
for name, count, sample in rows:
    print(f"{name:22} {count:4}  e.g. " + " / ".join(sample))
