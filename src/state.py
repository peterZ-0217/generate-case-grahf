from typing import Any, Dict, List, Optional, TypedDict


class Section(TypedDict):
    id: str
    heading: str
    level: int
    start_line: int          # 1-based，含标题行
    end_line: int            # 1-based，含
    content: str
    category: str            # title / core / reference / noise


class Unit(TypedDict):
    id: str
    title: str
    type: str                # 触发规则 / 频控 / 交互 / 数据填充 / 状态流转 / 实验分组 / 其他
    source: str              # 原文引用片段


class TestPoint(TypedDict):
    """覆盖点级测试点：字段只保留"验证什么 + 从哪来"，操作细节/测试数据下放下游。"""
    id: str
    unit_id: str
    title: str               # 一句话验证断言：前半句条件、后半句预期行为
    origin: str              # prd_explicit（PRD 明确规则）/ derived（测试经验推导）


class AgentState(TypedDict, total=False):
    prd_path: str
    raw_md: str
    doc_title: str

    sections: List[Section]
    core_sections: List[Section]
    reference_sections: List[Section]
    removed_log: List[Dict[str, Any]]

    core_text: str           # 核心需求文本（图片已替换为占位符）
    reference_text: str      # 背景参考原文

    ambiguities: List[Dict[str, str]]
    units: List[Unit]
    test_points: List[TestPoint]

    retry_hints: List[str]   # 校验回灌给抽取的未覆盖规则句 / 人工批注
    retry_unit_ids: List[str]  # 本轮需要补抽的功能单元；为空时全量补抽
    coverage_report: Dict[str, Any]
    validate_retry: int

    review_status: str       # approved / rejected
    human_feedback: str

    next_route: str          # 节点内部路由标记（validate/human_review 写入）

    output_files: Dict[str, str]
