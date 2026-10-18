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
    source_lines: str        # 如 "48-61"


class TestPoint(TypedDict):
    id: str
    unit_id: str
    title: str
    precondition: str
    scenario: str
    expected: str
    type: str                # 正向 / 反向 / 边界 / 异常 / 状态流转 / 频控
    source_lines: str


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
