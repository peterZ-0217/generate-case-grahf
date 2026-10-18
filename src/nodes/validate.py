"""校验节点：回映射覆盖校验 + 交叉校验 + 单元×维度检查单。"""

import re
from typing import Any, Dict, List

from .. import prompts
from ..llm import LLMClient
from ..state import AgentState

_POINT_TYPES = ("正向", "反向", "边界", "异常", "状态流转", "频控")
# 无需测试的原子句：图片占位符、外链、埋点
_SKIP_RE = re.compile(r"^\[图片|^\S*地址[:：]|https?://")


def split_rule_sentences(core_text: str) -> List[str]:
    """把核心需求文本切成原子规则句：列表项、编号项、表格行、独立行。"""
    sentences = []
    for line in core_text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        line = re.sub(r"^[-*●|>]\s*", "", line).strip()
        line = re.sub(r"^\d+[.、)]\s*", "", line).strip()
        if line and line not in ("---",):
            sentences.append(line)
    # 去重保序
    seen, result = set(), []
    for s in sentences:
        if s not in seen:
            seen.add(s)
            result.append(s)
    return result


def _validate_coverage(llm: LLMClient, sentences: List[str], points: List[Dict[str, Any]]) -> Dict[str, Any]:
    if llm.mock:
        return {
            "total": len(sentences), "covered": len(sentences),
            "coverage_rate": 1.0, "uncovered": [],
            "note": "mock 模式未执行真实覆盖判定",
        }
    points_brief = [{"id": p["id"], "title": p["title"], "scenario": p["scenario"]} for p in points]
    result = llm.chat_json(
        prompts.VALIDATE_COVERAGE_SYSTEM,
        prompts.VALIDATE_COVERAGE_USER.format(sentences=sentences, points=points_brief),
        mock=[],
    )
    uncovered = []
    covered = 0
    for item in result:
        if not isinstance(item, dict):
            continue
        if item.get("covered"):
            covered += 1
        else:
            uncovered.append({"sentence": item.get("sentence", ""), "reason": item.get("reason", "")})
    total = len(result) or len(sentences)
    return {
        "total": total,
        "covered": covered,
        "coverage_rate": round(covered / total, 4) if total else 1.0,
        "uncovered": uncovered,
    }


def _cross_check(llm: LLMClient, state: AgentState) -> List[Dict[str, str]]:
    if llm.mock:
        return []
    points_a = [{"title": p["title"], "scenario": p["scenario"]} for p in state["test_points"]]
    points_b = llm.chat_json(
        prompts.EXTRACT_POINTS_SYSTEM,
        prompts.EXTRACT_POINTS_USER.format(
            reference_text=state["reference_text"][:2000],
            unit_title="全部功能单元",
            unit_type="其他",
            unit_source=state["core_text"],
            retry_block="",
        ),
        mock=[],
    )
    units = [
        {"id": u["id"], "title": u["title"], "source": u["source"]}
        for u in state["units"]
    ]
    diff = llm.chat_json(
        prompts.CROSS_CHECK_SYSTEM,
        prompts.CROSS_CHECK_USER.format(
            core_text=state["core_text"], points_a=points_a, points_b=points_b, units=units
        ),
        mock=[],
    )
    return [d for d in diff if isinstance(d, dict)]


def _checklist_gaps(state: AgentState) -> List[Dict[str, str]]:
    """单元×维度矩阵：交互/触发类单元缺异常或反向维度时提示。"""
    expected_dims = {
        "触发规则": {"正向", "反向"},
        "频控": {"频控"},
        "交互": {"正向", "异常", "状态流转"},
        "数据填充": {"正向", "边界"},
        "状态流转": {"状态流转"},
        "实验分组": {"正向", "反向"},
    }
    gaps = []
    unit_types = {}
    for p in state["test_points"]:
        unit_types.setdefault(p["unit_id"], set()).add(p["type"])
    for unit in state["units"]:
        want = expected_dims.get(unit["type"], set())
        have = unit_types.get(unit["id"], set())
        missing = want - have
        if missing:
            gaps.append({
                "unit": unit["title"],
                "missing_dimensions": "、".join(sorted(missing)),
            })
    return gaps


def _normalize(text: str) -> str:
    return re.sub(r"[^\w]+", "", text).casefold()


def _locate_retry_units(
    uncovered: List[str], b_diff: List[Dict[str, str]], units: List[Dict[str, Any]]
) -> List[str]:
    all_ids = [u["id"] for u in units]
    targets = set()
    for sentence in uncovered:
        normalized = _normalize(sentence)
        matches = [
            u["id"] for u in units
            if normalized and normalized in _normalize(u["source"])
        ]
        if not matches:
            return all_ids
        targets.update(matches)
    for diff in b_diff:
        unit_id = str(diff.get("unit_id", "")).strip()
        if unit_id not in all_ids:
            return all_ids
        targets.add(unit_id)
    return [unit_id for unit_id in all_ids if unit_id in targets]


def validate(state: AgentState, llm: LLMClient, max_retry: int) -> Dict[str, Any]:
    sentences = split_rule_sentences(state["core_text"])
    sentences = [s for s in sentences if not _SKIP_RE.match(s)]

    coverage = _validate_coverage(llm, sentences, state["test_points"])
    cross_diff = _cross_check(llm, state)
    gaps = _checklist_gaps(state)

    report = {
        "coverage": coverage,
        "cross_check_diff": cross_diff,
        "checklist_gaps": gaps,
    }

    retry = state.get("validate_retry", 0)
    uncovered = [u["sentence"] for u in coverage["uncovered"]]
    b_diff = [
        d for d in cross_diff
        if str(d.get("from", "")).strip().upper() == "B" and str(d.get("title", "")).strip()
    ]
    hints = uncovered + [str(d["title"]).strip() for d in b_diff]
    if hints and retry < max_retry:
        return {
            "coverage_report": report,
            "retry_hints": hints,
            "retry_unit_ids": _locate_retry_units(uncovered, b_diff, state["units"]),
            "validate_retry": retry + 1,
            "next_route": "extract_points",
        }
    return {
        "coverage_report": report,
        "validate_retry": retry,
        "retry_unit_ids": [],
        "next_route": "human_review",
    }
