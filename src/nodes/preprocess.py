"""MD 解析与章节分类：按标题文本匹配词库，不依赖标题层级。"""

import re
from typing import Any, Dict, List, Optional

from ..state import AgentState, Section
# 行首可有空白 1-6# 至少一个空白 非空白结尾的字符 结尾可有空白
_HEADING_RE = re.compile(r"^\s*(#{1,6})\s+(.*\S)\s*$")
_IMAGE_RE = re.compile(r"!\[([^\]]*)\]\([^)]*\)")


def _parse_headings(lines: List[str]) -> List[Dict[str, Any]]:
    headings = []
    for idx, line in enumerate(lines):
        m = _HEADING_RE.match(line)
        if m:
            headings.append({"line": idx, "level": len(m.group(1)), "text": m.group(2).strip()})
    return headings


def _build_sections(lines: List[str]) -> List[Section]:
    """每个标题的章节范围 = 标题行到下一个同级或更高级标题之前。"""
    headings = _parse_headings(lines)
    sections: List[Section] = []
    for i, h in enumerate(headings):
        end = len(lines)
        for nxt in headings[i + 1:]:
            if nxt["level"] <= h["level"]:
                end = nxt["line"]
                break
        content = "\n".join(lines[h["line"]:end]).strip()
        sections.append(Section(
            id=f"S{i + 1}",
            heading=h["text"],
            level=h["level"],
            start_line=h["line"] + 1,
            end_line=end,
            content=content,
            category="",
        ))
    return sections


def _match_category(heading: str, core_kw: List[str], ref_kw: List[str]) -> Optional[str]:
    for kw in core_kw:
        if kw in heading:
            return "core"
    for kw in ref_kw:
        if kw in heading:
            return "reference"
    return None


def _encloses(a: Section, b: Section) -> bool:
    return a["level"] < b["level"] and a["start_line"] < b["start_line"] and a["end_line"] >= b["end_line"]


def parse_md(state: AgentState) -> Dict[str, Any]:
    with open(state["prd_path"], encoding="utf-8") as f:
        raw = f.read()
    lines = raw.splitlines()
    sections = _build_sections(lines)

    doc_title = ""
    for s in sections:
        if s["level"] == 1:
            s["category"] = "title"
            doc_title = s["heading"]
            break

    return {"raw_md": raw, "sections": sections, "doc_title": doc_title}


def classify_sections(state: AgentState, core_kw: List[str], ref_kw: List[str]) -> Dict[str, Any]:
    sections = [dict(s) for s in state["sections"]]

    for s in sections:
        if s["category"] == "title":
            s["direct"] = False
            continue
        s["category"] = _match_category(s["heading"], core_kw, ref_kw) or ""
        s["direct"] = bool(s["category"])

    # 未直接命中的章节：若被已命中章节包含则继承其类别，否则为噪声
    for s in sections:
        if s["category"]:
            continue
        parent = None
        for other in sections:
            if other["category"] in ("core", "reference") and _encloses(other, s):
                if parent is None or other["level"] > parent["level"]:
                    parent = other

    # 只取直接命中的顶层章节（子章节内容已包含在父章节范围内，避免重复拼接）
    core = [s for s in sections if s["category"] == "core" and s["direct"]]
    reference = [s for s in sections if s["category"] == "reference" and s["direct"]]

    # 清洗日志：只记录顶层噪声章节（其父章节不是噪声的）
    removed_log = []
    for s in sections:
        if s["category"] != "noise":
            continue
        has_noise_parent = any(
            o["category"] == "noise" and _encloses(o, s) for o in sections
        )
        if not has_noise_parent:
            removed_log.append({
                "heading": s["heading"],
                "lines": f"{s['start_line']}-{s['end_line']}",
                "reason": "未命中保留词库，按规则去噪",
                "excerpt": s["content"][:200],
            })

    # 核心文本预处理：图片替换为占位符，链接/埋点地址等原文保留（不参与抽点由 prompt 约束）
    core_text = "\n\n".join(s["content"] for s in core)
    core_text = _IMAGE_RE.sub(lambda m: f"[图片: {m.group(1) or '截图'}]", core_text)
    reference_text = "\n\n".join(s["content"] for s in reference)

    def _clean(s: Dict[str, Any]) -> Section:
        return Section(**{k: s[k] for k in Section.__annotations__})

    return {
        "sections": [_clean(s) for s in sections],
        "core_sections": [_clean(s) for s in core],
        "reference_sections": [_clean(s) for s in reference],
        "removed_log": removed_log,
        "core_text": core_text,
        "reference_text": reference_text,
    }
