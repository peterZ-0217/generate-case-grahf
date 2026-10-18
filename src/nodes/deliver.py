"""人工审核节点（interrupt）与产出节点。"""

import json
import os
import re
from typing import Any, Dict

from langgraph.types import interrupt

from ..state import AgentState


def human_review(state: AgentState) -> Dict[str, Any]:
    """中断等待人工审核。审核材料由 CLI 展示；resume 值约定：
    {"status": "approved"} 或 {"status": "rejected", "feedback": "..."}（打回重抽）。"""
    decision = interrupt({
        "doc_title": state["doc_title"],
        "unit_count": len(state["units"]),
        "test_point_count": len(state["test_points"]),
        "coverage_report": state["coverage_report"],
        "ambiguities": state["ambiguities"],
        "message": "请审核测试点。批准返回 {'status': 'approved'}；打回返回 {'status': 'rejected', 'feedback': '修改意见'}。",
    })
    if isinstance(decision, dict) and decision.get("status") == "rejected":
        feedback = str(decision.get("feedback", ""))
        return {
            "review_status": "rejected",
            "human_feedback": feedback,
            "retry_hints": [feedback] if feedback else [],
            "retry_unit_ids": [unit["id"] for unit in state["units"]],
            "next_route": "extract_points",
        }
    return {"review_status": "approved", "next_route": "output"}


def _safe_name(title: str) -> str:
    name = re.sub(r'[\\/:*?"<>|【】\s]+', "_", title).strip("_")
    return name[:60] or "prd"


def _points_md(state: AgentState) -> str:
    unit_map = {u["id"]: u for u in state["units"]}
    lines = [f"# {state['doc_title']} — 测试点清单", ""]
    for unit in state["units"]:
        lines.append(f"## [{unit['id']}] {unit['title']}（{unit['type']}）")
        lines.append("")
        lines.append("| ID | 测试点 | 类型 | 前置条件 | 预期结果 |")
        lines.append("| --- | --- | --- | --- | --- |")
        for p in state["test_points"]:
            if p["unit_id"] == unit["id"]:
                lines.append(
                    f"| {p['id']} | {p['title']} | {p['type']} | {p['precondition']} | {p['expected']} |"
                )
        lines.append("")
    orphan = [p for p in state["test_points"] if p["unit_id"] not in unit_map]
    if orphan:
        lines.append("## 未关联单元")
        for p in orphan:
            lines.append(f"- [{p['id']}] {p['title']}")
    return "\n".join(lines)


def _report_md(state: AgentState) -> str:
    r = state["coverage_report"]
    cov = r["coverage"]
    lines = [
        f"# {state['doc_title']} — 校验与审核报告", "",
        f"- 功能单元数：{len(state['units'])}",
        f"- 测试点数：{len(state['test_points'])}",
        f"- 规则句覆盖率：{cov['covered']}/{cov['total']}（{cov['coverage_rate']:.1%}）",
        f"- 审核状态：{state.get('review_status', '未审核')}",
    ]
    if cov.get("note"):
        lines.append(f"- 备注：{cov['note']}")
    lines.append("")
    if cov["uncovered"]:
        lines.append("## 未覆盖规则句")
        lines += [f"- {u['sentence']}（{u['reason']}）" for u in cov["uncovered"]]
        lines.append("")
    if r["cross_check_diff"]:
        lines.append("## 交叉校验差异项")
        lines += [f"- [{d.get('from', '?')}] {d.get('title', '')}：{d.get('reason', '')}"
                  for d in r["cross_check_diff"]]
        lines.append("")
    if r["checklist_gaps"]:
        lines.append("## 维度覆盖缺口")
        lines += [f"- 单元「{g['unit']}」缺少维度：{g['missing_dimensions']}" for g in r["checklist_gaps"]]
        lines.append("")
    if state["ambiguities"]:
        lines.append("## 待确认歧义清单")
        for a in state["ambiguities"]:
            lines.append(f"- {a['question']}（位置：{a['location']}；影响：{a['impact']}）")
        lines.append("")
    if state["removed_log"]:
        lines.append("## 去噪日志")
        for item in state["removed_log"]:
            lines.append(f"- 「{item['heading']}」（行 {item['lines']}）：{item['reason']}")
    return "\n".join(lines)


def output(state: AgentState, output_dir: str) -> Dict[str, Any]:
    os.makedirs(output_dir, exist_ok=True)
    base = _safe_name(state["doc_title"])

    json_path = os.path.join(output_dir, f"{base}_test_points.json")
    payload = {
        "doc_title": state["doc_title"],
        "context": {"reference": state["reference_text"]},
        "units": state["units"],
        "test_points": state["test_points"],
        "coverage_report": state["coverage_report"],
        "ambiguities": state["ambiguities"],
        "removed_log": state["removed_log"],
        "review": {"status": state.get("review_status"), "feedback": state.get("human_feedback", "")},
    }
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)

    md_path = os.path.join(output_dir, f"{base}_test_points.md")
    with open(md_path, "w", encoding="utf-8") as f:
        f.write(_points_md(state))

    report_path = os.path.join(output_dir, f"{base}_report.md")
    with open(report_path, "w", encoding="utf-8") as f:
        f.write(_report_md(state))

    return {"output_files": {"json": json_path, "md": md_path, "report": report_path}}
