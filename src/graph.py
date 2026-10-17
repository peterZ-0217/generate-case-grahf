"""LangGraph 图编排：

parse_md → classify_sections → detect_ambiguity → split_units → extract_points
→ validate ⇄ extract_points（回灌重抽）→ human_review(interrupt) ⇄ extract_points（打回）
→ output
"""

import time
from typing import Any, Dict

import yaml
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.errors import GraphInterrupt
from langgraph.graph import END, START, StateGraph

from .llm import LLMClient
from .logger import build_node_logger, get_logger, setup_logging
from .nodes import analyze, deliver, preprocess, validate as validate_mod
from .state import AgentState

logger = get_logger("graph")


def _load_yaml(path: str) -> Dict[str, Any]:
    # 显式指定 UTF-8：Windows 默认编码非 UTF-8 时，含中文注释的配置文件会解码失败
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def _logged_node(name: str, fn, settings: Dict[str, Any]):
    """节点包装：每个节点用独立 logger（prd_agent.node.<name>），并打印入参/出参结构体。"""
    nl = build_node_logger(name, settings)

    def wrapped(state):
        started_at = time.perf_counter()
        nl.start(state)
        try:
            result = fn(state)
        except GraphInterrupt:
            nl.pause(state, started_at)
            raise
        except Exception:
            nl.error(state, started_at)
            nl.logger.exception("节点异常堆栈 | node=%s", name)
            raise
        nl.end(state, result, started_at)
        return result

    return wrapped


def build_graph(sections_config: str, settings_config: str, llm_provider: str = None):
    sections_cfg = _load_yaml(sections_config)
    settings = _load_yaml(settings_config)
    log_path = setup_logging(
        settings.get("log_dir", "logs"),
        settings.get("log_level", "INFO"),
    )
    llm = LLMClient(settings, provider=llm_provider)
    max_retry = settings.get("validate_max_retry", 1)
    output_dir = settings.get("output_dir", "output")

    core_kw = sections_cfg.get("core_keywords", ["需求描述"])
    ref_kw = sections_cfg.get("reference_keywords", ["需求背景"])

    def route_after(state: AgentState) -> str:
        return state.get("next_route", "human_review")

    builder = StateGraph(AgentState)

    def node(name: str, fn):
        """注册节点时统一套上入参/出参结构体日志。"""
        builder.add_node(name, _logged_node(name, fn, settings))

    # 解析节点
    node("parse_md", preprocess.parse_md)
    # 分类与去噪节点
    node("classify_sections",
         lambda s: preprocess.classify_sections(s, core_kw, ref_kw))
    # 检测歧义节点
    node("detect_ambiguity", lambda s: analyze.detect_ambiguity(s, llm))
    # 拆分功能单元节点
    node("split_units", lambda s: analyze.split_units(s, llm))
    # 测试点抽取节点
    node("extract_points", lambda s: analyze.extract_points(s, llm))
    # 功能点校验节点
    node("validate", lambda s: validate_mod.validate(s, llm, max_retry))
    # 人工审核节点
    node("human_review", deliver.human_review)
    # 输出节点
    node("output", lambda s: deliver.output(s, output_dir))

    builder.add_edge(START, "parse_md")
    builder.add_edge("parse_md", "classify_sections")
    builder.add_edge("classify_sections", "detect_ambiguity")
    builder.add_edge("detect_ambiguity", "split_units")
    builder.add_edge("split_units", "extract_points")
    builder.add_edge("extract_points", "validate")
    builder.add_conditional_edges("validate", route_after,
                                  {"extract_points": "extract_points",
                                   "human_review": "human_review"})
    builder.add_conditional_edges("human_review", route_after,
                                  {"extract_points": "extract_points",
                                   "output": "output"})
    builder.add_edge("output", END)

    graph = builder.compile(checkpointer=InMemorySaver())
    graph.llm_mock = llm.mock  # 供 CLI 提示
    graph.llm_provider = llm.provider  # 供 CLI 展示
    graph.llm_model = llm.model
    logger.info("图构建完成 | provider=%s | model=%s | mock=%s | log_file=%s",
                llm.provider, llm.model, llm.mock, log_path)
    return graph
