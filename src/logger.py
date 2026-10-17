"""统一日志配置：同时输出到控制台和文件；提供按节点命名的 logger 与结构体日志。

日志 name 规则：``prd_agent.<模块>``，例如 ``prd_agent.node.parse_md``。
控制台与文件的 formatter 里已包含 ``%(name)s``，因此每个节点的日志天然可区分。
"""

import json
import logging
import sys
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any, Dict, List

_ROOT_NAME = "prd_agent"
_FORMAT = "%(asctime)s | %(levelname)s | %(name)s | %(message)s"

# 结构体日志的默认预算，可按需调大（配置项 log_max_chars / log_max_str_chars / log_max_items）
_DEFAULT_MAX_CHARS = 4000
_DEFAULT_MAX_STR_CHARS = 300
_DEFAULT_MAX_ITEMS = 20


def setup_logging(log_dir: str = "logs", level: str = "INFO") -> Path:
    logger = logging.getLogger(_ROOT_NAME)
    logger.setLevel(_level_of(level))
    logger.propagate = False

    log_path = Path(log_dir) / "agent.log"
    if getattr(logger, "_configured", False):
        return log_path

    log_path.parent.mkdir(parents=True, exist_ok=True)
    formatter = logging.Formatter(_FORMAT)

    # 控制台编码：Windows 默认 GBK，遇到 \xa0/生僻字会抛 UnicodeEncodeError，
    # 导致整条结构体日志丢失（甚至看起来像主流程失败）。这里显式切到 UTF-8。
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(formatter)
    logger.addHandler(console)

    # 落盘统一 UTF-8 且不允许因编码问题丢日志
    file_handler = RotatingFileHandler(
        log_path,
        maxBytes=5 * 1024 * 1024,
        backupCount=3,
        encoding="utf-8",
        errors="replace",
    )
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)

    logger._configured = True
    return log_path


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
) -> str:
    """把结构体格式化成可读 JSON：先截断再序列化，最后整体再兜一道长度上限。"""
    try:
        summarized = _summarize(_to_jsonable(obj), max_str_chars, max_items)
        text = json.dumps(summarized, ensure_ascii=False, indent=2, default=str)
    except Exception as exc:  # 日志绝不能因为序列化失败而中断主流程
        return f"<结构体序列化失败: {exc}>"
    if len(text) > max_chars:
        text = f"{text[:max_chars]}\n…（共 {len(text)} 字符，已截断）"
    return text


def keys_of(obj: Any) -> List[str]:
    """快速概览：结构体的顶层键。"""
    if isinstance(obj, dict):
        return list(obj.keys())
    return []


# --------------------------------------------------------------------------- #
# 节点日志器：统一「入参 / 出参 / 耗时 / 中断 / 异常」的记录方式
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
    ):
        self.name = name
        self.logger = node_logger(name)
        self.level = _level_of(level)
        self.max_chars = int(max_chars)
        self.max_str_chars = int(max_str_chars)
        self.max_items = int(max_items)

    # 内部统一出口，便于一处控制级别与格式
    def _emit(self, msg: str, level: int = logging.INFO) -> None:
        self.logger.log(level, msg)

    def _dump(self, payload: Any) -> str:
        return format_payload(
            payload,
            max_chars=self.max_chars,
            max_str_chars=self.max_str_chars,
            max_items=self.max_items,
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
    """按 settings 里的日志预算构造节点日志器。"""
    return NodeLogger(
        name,
        max_chars=settings.get("log_max_chars", _DEFAULT_MAX_CHARS),
        max_str_chars=settings.get("log_max_str_chars", _DEFAULT_MAX_STR_CHARS),
        max_items=settings.get("log_max_items", _DEFAULT_MAX_ITEMS),
        level=settings.get("log_node_level", "INFO"),
    )
