"""分析节点：歧义检测、功能单元切分、测试点抽取。"""

import re
from typing import Any, Dict, List

from .. import prompts
from ..llm import LLMClient
from ..state import AgentState, TestPoint, Unit

_UNIT_TYPES = ("触发规则", "频控", "交互", "数据填充", "状态流转", "实验分组", "其他")
_POINT_TYPES = ("正向", "反向", "边界", "异常", "状态流转", "频控")


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
            source_lines="",
        ))
    # 优化点：添加pydantic数据格式校验
    return {"units": units}


def extract_points(state: AgentState, llm: LLMClient) -> Dict[str, Any]:
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

    for unit in target_units:
        result = llm.chat_json(
            prompts.EXTRACT_POINTS_SYSTEM,
            prompts.EXTRACT_POINTS_USER.format(
                reference_text=state["reference_text"][:2000],
                unit_title=unit["title"],
                unit_type=unit["type"],
                unit_source=unit["source"] or unit["title"],
                retry_block=retry_block,
            ),
            mock=[{
                "title": f"【mock】验证「{unit['title']}」",
                "precondition": "待 LLM 生成",
                "scenario": "待 LLM 生成",
                "expected": "待 LLM 生成",
                "type": "正向",
            }],
        )
        for item in result:
            if not isinstance(item, dict):
                continue
            title = str(item.get("title", "")).strip()
            if not title or title in existing_titles:
                continue
            existing_titles.add(title)
            seq += 1
            ptype = item.get("type") if item.get("type") in _POINT_TYPES else "正向"
            points.append(TestPoint(
                id=f"TP{seq:03d}",
                unit_id=unit["id"],
                title=title,
                precondition=str(item.get("precondition", "")),
                scenario=str(item.get("scenario", "")),
                expected=str(item.get("expected", "")),
                type=ptype,
                source_lines=unit.get("source_lines", ""),
            ))

    return {"test_points": points, "retry_hints": [], "retry_unit_ids": []}
