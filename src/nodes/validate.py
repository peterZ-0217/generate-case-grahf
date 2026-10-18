"""校验节点：回映射覆盖校验 + 交叉校验 + 单元×维度检查单。"""

import re
from typing import Any, Dict, List

from .. import prompts
from ..llm import LLMClient
from ..state import AgentState

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
    points_brief = [{"id": p["id"], "title": p["title"]} for p in points]
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


def _cross_check(llm: LLMClient, state: AgentState, mode: str, points: List[Dict[str, Any]]) -> List[Dict[str, str]]:
    if llm.mock:
        return []
    points_a = [{"title": p["title"]} for p in points]
    points_b = llm.chat_json(
        prompts.extract_points_system(mode),
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
        prompts.cross_check_system(mode),
        prompts.CROSS_CHECK_USER.format(
            core_text=state["core_text"], points_a=points_a, points_b=points_b, units=units
        ),
        mock=[],
    )
    return [d for d in diff if isinstance(d, dict)]


def _apply_origin_guard(points: List[Dict[str, Any]], sentences: List[str]) -> List[Dict[str, str]]:
    """origin 防伪：标了 prd_explicit 但匹配不到 PRD 规则句的，降级为 derived。"""
    norms = [_normalize(s) for s in sentences]
    norms = [n for n in norms if n]

    def matched(title: str) -> bool:
        nt = _normalize(title)
        if not nt:
            return False
        for ns in norms:
            if nt in ns:
                return True
            if len(ns) >= 6 and ns in nt:
                return True
        return False

    corrections = []
    for p in points:
        if p.get("origin") != "prd_explicit":
            continue
        if not matched(p["title"]):
            p["origin"] = "derived"
            corrections.append({
                "id": p["id"],
                "title": p["title"],
                "reason": "未匹配到 PRD 规则句，降级为 derived",
            })
    return corrections


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


def validate(state: AgentState, llm: LLMClient, max_retry: int, mode: str = "prd_focused") -> Dict[str, Any]:
    sentences = split_rule_sentences(state["core_text"])
    sentences = [s for s in sentences if not _SKIP_RE.match(s)]

    points = [dict(p) for p in state["test_points"]]
    corrections = _apply_origin_guard(points, sentences)
    explicit_points = [p for p in points if p.get("origin") == "prd_explicit"]
    derived_count = sum(1 for p in points if p.get("origin") == "derived")

    coverage = _validate_coverage(llm, sentences, explicit_points)
    cross_diff = _cross_check(llm, state, mode, points)

    report = {
        "mode": mode,
        "coverage": coverage,
        "derived_points": {"count": derived_count},
        "cross_check_diff": cross_diff,
        "origin_corrections": corrections,
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
            "test_points": points,
            "coverage_report": report,
            "retry_hints": hints,
            "retry_unit_ids": _locate_retry_units(uncovered, b_diff, state["units"]),
            "validate_retry": retry + 1,
            "next_route": "extract_points",
        }
    return {
        "test_points": points,
        "coverage_report": report,
        "validate_retry": retry,
        "retry_unit_ids": [],
        "next_route": "human_review",
    }
