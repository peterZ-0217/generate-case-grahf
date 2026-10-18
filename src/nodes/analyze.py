"""分析节点：歧义检测、功能单元切分、测试点抽取。"""

import re
from typing import Any, Dict, List

from .. import prompts
from ..llm import LLMClient
from ..state import AgentState, TestPoint, Unit

_UNIT_TYPES = ("触发规则", "频控", "交互", "数据填充", "状态流转", "实验分组", "其他")
_ORIGINS = ("prd_explicit", "derived")


def _mock_units(core_text: str) -> List[Dict[str, str]]:
    """mock 模式：按 ###/#### 子标题与 ● 条目粗切功能单元。"""
    units = []
    for m in re.finditer(r"^\s*#{2,6}\s+(.+)$", core_text, re.M):
        units.append({"title": m.group(1).strip(), "type": "其他", "source": m.group(0)})
    if not units:
        units.append({"title": "整体需求", "type": "其他", "source": core_text[:200]})
    return units


def detect_ambiguity(state: AgentState, llm: LLMClient) -> Dict[str, Any]:
    result = llm.chat_json(
        prompts.DETECT_AMBIGUITY_SYSTEM,
        prompts.DETECT_AMBIGUITY_USER.format(
            core_text=state["core_text"], reference_text=state["reference_text"]
        ),
        mock=[],
    )
    ambiguities = [
        {"question": str(item.get("question", "")), "location": str(item.get("location", "")),
         "impact": str(item.get("impact", ""))}
        for item in result if isinstance(item, dict)
    ]
    return {"ambiguities": ambiguities}


def split_units(state: AgentState, llm: LLMClient) -> Dict[str, Any]:
    result = llm.chat_json(
        prompts.SPLIT_UNITS_SYSTEM,
        prompts.SPLIT_UNITS_USER.format(core_text=state["core_text"]),
        mock=_mock_units(state["core_text"]),
    )
    units: List[Unit] = []
    for i, item in enumerate(result):
        if not isinstance(item, dict):
            continue
        units.append(Unit(
            id=f"U{i + 1:02d}",
            title=str(item.get("title", "")),
            type=str(item.get("type", "其他")) if item.get("type") in _UNIT_TYPES else "其他",
            source=str(item.get("source", "")),
        ))
    # 优化点：添加pydantic数据格式校验
    return {"units": units}


def _mock_points(unit: Dict[str, Any]) -> List[Dict[str, str]]:
    """mock 模式占位测试点：统一标 derived，避免验收时虚增 prd_explicit 覆盖率。"""
    return [{"title": f"【mock】验证「{unit['title']}」", "origin": "derived"}]


def _semantic_dedup(llm: LLMClient, points: List[TestPoint]) -> List[TestPoint]:
    """extended 模式：用一次 LLM 调用合并"换词不换意"的测试点，程序侧精确去重为主。"""
    if len(points) < 2:
        return points
    payload = [{"id": p["id"], "unit_id": p["unit_id"], "title": p["title"]} for p in points]
    try:
        groups = llm.chat_json(
            prompts.DEDUP_SYSTEM,
            prompts.DEDUP_USER.format(points=payload),
            mock=[],
        )
    except Exception:  # 去重失败不能影响主流程
        return points
    drop = set()
    for group in groups or []:
        if not isinstance(group, list):
            continue
        ids = [g for g in group if isinstance(g, str)]
        drop.update(ids[1:])
    return [p for p in points if p["id"] not in drop]


def _renumber(points: List[TestPoint]) -> List[TestPoint]:
    for i, p in enumerate(points, start=1):
        p["id"] = f"TP{i:03d}"
    return points


def extract_points(state: AgentState, llm: LLMClient, mode: str = "prd_focused") -> Dict[str, Any]:
    units = state["units"]
    existing: List[TestPoint] = list(state.get("test_points", []))
    hints = list(state.get("retry_hints", []))
    retry_unit_ids = set(state.get("retry_unit_ids", []))

    target_units = units
    if hints and retry_unit_ids:
        selected = [unit for unit in units if unit["id"] in retry_unit_ids]
        if selected:
            target_units = selected

    retry_block = ""
    if hints:
        retry_block = prompts.RETRY_BLOCK.format(
            hints="\n".join(f"- {h}" for h in hints),
            existing_titles="、".join(p["title"] for p in existing),
        )

    existing_titles = {p["title"] for p in existing}
    points: List[TestPoint] = existing
    seq = len(existing)
    system = prompts.extract_points_system(mode)

    for unit in target_units:
        result = llm.chat_json(
            system,
            prompts.EXTRACT_POINTS_USER.format(
                reference_text=state["reference_text"][:2000],
                unit_title=unit["title"],
                unit_type=unit["type"],
                unit_source=unit["source"] or unit["title"],
                retry_block=retry_block,
            ),
            mock=_mock_points(unit),
        )
        for item in result:
            if not isinstance(item, dict):
                continue
            title = str(item.get("title", "")).strip()
            if not title or title in existing_titles:
                continue
            origin = item.get("origin") if item.get("origin") in _ORIGINS else "derived"
            existing_titles.add(title)
            seq += 1
            points.append(TestPoint(
                id=f"TP{seq:03d}",
                unit_id=unit["id"],
                title=title,
                origin=origin,
            ))

    if mode == "extended" and not llm.mock:
        points = _semantic_dedup(llm, points)

    return {"test_points": _renumber(points), "retry_hints": [], "retry_unit_ids": []}
