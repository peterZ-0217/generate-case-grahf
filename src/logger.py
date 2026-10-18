"""统一日志配置：精简事件写入 run.log，全量状态与 LLM 内容写入可选 JSONL Trace。

日志 name 规则：``prd_agent.<模块>``，例如 ``prd_agent.node.parse_md``。
所有普通日志带有 run_id、thread_id、node 和 attempt，便于关联重试与断点续跑。
"""

import contextvars
import json
import logging
import sys
import time
import uuid
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any, Dict, List

_ROOT_NAME = "prd_agent"
_TRACE_NAME = "prd_agent_state_trace"
_LLM_TRACE_NAME = "prd_agent_llm_trace"
_FORMAT = (
    "%(asctime)s | %(levelname)s | %(name)s | run=%(run_id)s "
    "thread=%(thread_id)s node=%(node)s attempt=%(attempt)s | %(message)s"
)
_CONTEXT = {
    "run_id": contextvars.ContextVar("log_run_id", default="-"),
    "thread_id": contextvars.ContextVar("log_thread_id", default="-"),
    "node": contextvars.ContextVar("log_node", default="-"),
    "attempt": contextvars.ContextVar("log_attempt", default="-"),
}

# DEBUG 结构体日志的截断预算；全量 Trace 不受这些配置影响
_DEFAULT_MAX_CHARS = 4000
_DEFAULT_MAX_STR_CHARS = 300
_DEFAULT_MAX_ITEMS = 20


class _ContextFilter(logging.Filter):
    def filter(self, record):
        for key, variable in _CONTEXT.items():
            setattr(record, key, variable.get())
        return True


def set_log_context(**fields: Any) -> None:
    for key, value in fields.items():
        if key in _CONTEXT and value is not None:
            _CONTEXT[key].set(str(value))


def get_log_context() -> Dict[str, str]:
    return {key: variable.get() for key, variable in _CONTEXT.items()}


def _configure_jsonl(logger_name: str, path: Path) -> None:
    logger = logging.getLogger(logger_name)
    logger.setLevel(logging.INFO)
    logger.propagate = False
    if getattr(logger, "_configured", False):
        return
    handler = logging.FileHandler(path, encoding="utf-8", errors="replace")
    handler.setFormatter(logging.Formatter("%(message)s"))
    logger.addHandler(handler)
    logger._configured = True


def setup_logging(
    log_dir: str = "logs",
    level: str = "INFO",
    log_file: str = None,
    trace: bool = False,
    llm_trace: bool = False,
    run_id: str = None,
    thread_id: str = None,
) -> Path:
    logger = logging.getLogger(_ROOT_NAME)
    logger.setLevel(_level_of(level))
    logger.propagate = False
    if getattr(logger, "_configured", False):
        return Path(logger._log_path)

    run_id = run_id or uuid.uuid4().hex[:8]
    set_log_context(run_id=run_id, thread_id=thread_id or "-")
    log_path = Path(log_file) if log_file else Path(log_dir) / run_id / "run.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    formatter = logging.Formatter(_FORMAT)

    # 控制台编码：Windows 默认 GBK，遇到 \xa0/生僻字会抛 UnicodeEncodeError
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(formatter)
    console.addFilter(_ContextFilter())
    logger.addHandler(console)

    file_handler = RotatingFileHandler(
        log_path,
        maxBytes=5 * 1024 * 1024,
        backupCount=3,
        encoding="utf-8",
        errors="replace",
    )
    file_handler.setFormatter(formatter)
    file_handler.addFilter(_ContextFilter())
    logger.addHandler(file_handler)

    logger._log_path = str(log_path)
    logger._trace_enabled = bool(trace)
    logger._llm_trace_enabled = bool(llm_trace)
    if trace:
        trace_path = log_path.with_suffix(".trace.jsonl") if log_file else log_path.parent / "trace.jsonl"
        _configure_jsonl(_TRACE_NAME, trace_path)
        logger._trace_path = str(trace_path)
    if llm_trace:
        llm_path = log_path.with_suffix(".llm.jsonl") if log_file else log_path.parent / "llm.jsonl"
        _configure_jsonl(_LLM_TRACE_NAME, llm_path)
        logger._llm_trace_path = str(llm_path)

    logger._configured = True
    return log_path


def get_log_paths() -> Dict[str, Path]:
    logger = logging.getLogger(_ROOT_NAME)
    if not getattr(logger, "_configured", False):
        return {}
    paths = {"run": Path(logger._log_path)}
    if getattr(logger, "_trace_enabled", False):
        paths["trace"] = Path(logger._trace_path)
    if getattr(logger, "_llm_trace_enabled", False):
        paths["llm_trace"] = Path(logger._llm_trace_path)
    return paths


def _write_jsonl(kind: str, event: Dict[str, Any]) -> None:
    root = logging.getLogger(_ROOT_NAME)
    enabled_attr = "_trace_enabled" if kind == "trace" else "_llm_trace_enabled"
    if not getattr(root, enabled_attr, False):
        return
    logger_name = _TRACE_NAME if kind == "trace" else _LLM_TRACE_NAME
    try:
        payload = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            **get_log_context(),
            **_to_jsonable(event),
        }
        logging.getLogger(logger_name).info(
            json.dumps(payload, ensure_ascii=False, default=str)
        )
    except Exception as exc:
        get_logger("logger").warning("Trace 写入失败 | kind=%s | error=%s", kind, exc)


def write_trace(event: Dict[str, Any]) -> None:
    _write_jsonl("trace", event)


def write_llm_trace(event: Dict[str, Any]) -> None:
    _write_jsonl("llm_trace", event)


def _level_of(level: Any) -> int:
    return getattr(logging, str(level).upper(), logging.INFO)


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(f"{_ROOT_NAME}.{name}")


def node_logger(node: str) -> logging.Logger:
    """取某个节点专属的 logger（name = prd_agent.node.<node>）。"""
    return get_logger(f"node.{node}")


# --------------------------------------------------------------------------- #
# 结构体序列化与截断
# --------------------------------------------------------------------------- #

def _to_jsonable(obj: Any) -> Any:
    """把任意对象转成可 json.dumps 的结构。

    只对已知容器做递归，其它类型优先 ``model_dump`` / ``_asdict``，兜底 ``str()``，
    避免把日志写成内存地址。
    """
    if obj is None or isinstance(obj, (bool, int, float, str)):
        return obj
    if isinstance(obj, dict):
        return {str(k): _to_jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple, set)):
        return [_to_jsonable(v) for v in obj]
    if hasattr(obj, "model_dump"):
        return _to_jsonable(obj.model_dump())
    if hasattr(obj, "_asdict"):
        return _to_jsonable(obj._asdict())
    return str(obj)


def _truncate_str(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return f"{text[:limit]}…(+{len(text) - limit} chars)"


def _summarize(obj: Any, max_str_chars: int, max_items: int) -> Any:
    """按字段截断长字符串，并按上限截断长列表，保留结构形状。"""
    if isinstance(obj, str):
        return _truncate_str(obj, max_str_chars)
    if isinstance(obj, dict):
        return {k: _summarize(v, max_str_chars, max_items) for k, v in obj.items()}
    if isinstance(obj, list):
        shown = [_summarize(v, max_str_chars, max_items) for v in obj[:max_items]]
        if len(obj) > max_items:
            shown.append(f"…(+{len(obj) - max_items} items)")
        return shown
    return obj


def format_payload(
    obj: Any,
    max_chars: int = _DEFAULT_MAX_CHARS,
    max_str_chars: int = _DEFAULT_MAX_STR_CHARS,
    max_items: int = _DEFAULT_MAX_ITEMS,
    full: bool = False,
) -> str:
    """把结构体格式化成可读 JSON；full=True 时不截断，否则按各项预算裁剪。"""
    try:
        jsonable = _to_jsonable(obj)
        summarized = jsonable if full else _summarize(jsonable, max_str_chars, max_items)
        text = json.dumps(summarized, ensure_ascii=False, indent=2, default=str)
    except Exception as exc:  # 日志绝不能因为序列化失败而中断主流程
        return f"<结构体序列化失败: {exc}>"
    if not full and len(text) > max_chars:
        text = f"{text[:max_chars]}\n…（共 {len(text)} 字符，已截断）"
    return text


def keys_of(obj: Any) -> List[str]:
    """快速概览：结构体的顶层键。"""
    if isinstance(obj, dict):
        return list(obj.keys())
    return []


def state_summary(state: Any) -> Dict[str, Any]:
    if not isinstance(state, dict):
        return {}

    summary: Dict[str, Any] = {}
    for key in ("prd_path", "doc_title", "next_route", "review_status"):
        value = state.get(key)
        if value not in (None, ""):
            summary[key] = value
    for key in ("raw_md", "core_text", "reference_text", "human_feedback"):
        if key in state and state[key] is not None:
            summary[f"{key}_chars"] = len(str(state[key]))
    for key in (
        "sections",
        "core_sections",
        "reference_sections",
        "removed_log",
        "ambiguities",
        "units",
        "test_points",
        "retry_hints",
        "retry_unit_ids",
    ):
        if key in state:
            value = state[key]
            summary[key] = len(value) if isinstance(value, (list, tuple, dict, set)) else 0
    if "validate_retry" in state:
        summary["validate_retry"] = state["validate_retry"]
    if state.get("output_files"):
        summary["output_files"] = state["output_files"]

    report = state.get("coverage_report") or {}
    coverage = report.get("coverage") or {}
    if coverage:
        summary["coverage"] = f"{coverage.get('covered', 0)}/{coverage.get('total', 0)}"
        summary["coverage_rate"] = coverage.get("coverage_rate", 0)
        summary["uncovered"] = len(coverage.get("uncovered") or [])
    if report:
        summary["mode"] = report.get("mode")
        summary["cross_check_diff"] = len(report.get("cross_check_diff") or [])
        summary["derived_points"] = (report.get("derived_points") or {}).get("count", 0)
        summary["origin_corrections"] = len(report.get("origin_corrections") or [])
    return summary


def format_state_summary(state: Any) -> str:
    return json.dumps(state_summary(state), ensure_ascii=False, separators=(",", ":"), default=str)


# --------------------------------------------------------------------------- #
# 节点日志器：精简事件与摘要，全量状态写入独立 Trace
# --------------------------------------------------------------------------- #

class NodeLogger:
    """按节点名记录入参与出参结构体。

    用法：
        nl = NodeLogger("parse_md", max_chars=4000)
        nl.start(state)
        result = do_something(state)
        nl.end(state, result, started_at=time.perf_counter())
    """

    def __init__(
        self,
        name: str,
        max_chars: int = _DEFAULT_MAX_CHARS,
        max_str_chars: int = _DEFAULT_MAX_STR_CHARS,
        max_items: int = _DEFAULT_MAX_ITEMS,
        level: Any = "INFO",
        full: bool = False,
    ):
        self.name = name
        self.logger = node_logger(name)
        self.level = _level_of(level)
        self.max_chars = int(max_chars)
        self.max_str_chars = int(max_str_chars)
        self.max_items = int(max_items)
        self.full = full

    # 内部统一出口，便于一处控制级别与格式
    def _emit(self, msg: str, level: int = logging.INFO) -> None:
        self.logger.log(level, msg)

    def _dump(self, payload: Any) -> str:
        return format_payload(
            payload,
            max_chars=self.max_chars,
            max_str_chars=self.max_str_chars,
            max_items=self.max_items,
            full=self.full,
        )

    def start(self, state: Any) -> None:
        self._emit(
            f"节点开始 | node={self.name} | 入参结构 | keys={keys_of(state)}\n"
            f"{self._dump(state)}",
            self.level,
        )

    def end(self, state: Any, result: Any, started_at: float) -> None:
        elapsed_ms = (time.perf_counter() - started_at) * 1000
        updates = keys_of(result)
        self._emit(
            f"节点结束 | node={self.name} | 耗时={elapsed_ms:.0f}ms | updates={updates}\n"
            f"出参结构 |\n{self._dump(result)}",
            self.level,
        )

    def pause(self, state: Any, started_at: float) -> None:
        elapsed_ms = (time.perf_counter() - started_at) * 1000
        self._emit(
            f"节点暂停 | node={self.name} | 耗时={elapsed_ms:.0f}ms | reason=interrupt\n"
            f"入参结构 | keys={keys_of(state)}\n{self._dump(state)}",
            self.level,
        )

    def error(self, state: Any, started_at: float) -> None:
        elapsed_ms = (time.perf_counter() - started_at) * 1000
        # 异常堆栈由 logger.exception 追加，这里补上当时的入参快照
        self._emit(
            f"节点异常 | node={self.name} | 耗时={elapsed_ms:.0f}ms\n"
            f"入参结构 | keys={keys_of(state)}\n{self._dump(state)}",
            logging.ERROR,
        )


def build_node_logger(name: str, settings: Dict[str, Any]) -> NodeLogger:
    """按 settings 里的全量开关或日志预算构造节点日志器。"""
    return NodeLogger(
        name,
        max_chars=settings.get("log_max_chars", _DEFAULT_MAX_CHARS),
        max_str_chars=settings.get("log_max_str_chars", _DEFAULT_MAX_STR_CHARS),
        max_items=settings.get("log_max_items", _DEFAULT_MAX_ITEMS),
        level=settings.get("log_node_level", "INFO"),
        full=settings.get("log_full", False),
    )
