"""运行 MD 解析与章节分类节点，并将无截断日志写入独立文件：

    python scripts/demo_preprocess.py [prd路径] [--log-file logs/preprocess_demo.log]
"""

import argparse
import os
import sys

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _REPO_ROOT)
os.chdir(_REPO_ROOT)

import yaml

from src.graph import _logged_node
from src.logger import setup_logging
from src.nodes import preprocess


def main() -> None:
    parser = argparse.ArgumentParser(description="parse_md 与 classify_sections 全量日志 demo")
    parser.add_argument("prd", nargs="?", default="examples/sample_prd.md", help="PRD markdown 文件路径")
    parser.add_argument("--sections-config", default="config/sections.yaml")
    parser.add_argument("--log-file", default="logs/preprocess_demo.log", help="独立日志文件路径")
    args = parser.parse_args()

    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

    with open(args.sections_config, encoding="utf-8") as f:
        sections_cfg = yaml.safe_load(f)

    log_path = setup_logging(level="INFO", log_file=args.log_file)
    settings = {"log_full": True, "log_node_level": "INFO"}
    core_kw = sections_cfg.get("core_keywords", ["需求描述"])
    ref_kw = sections_cfg.get("reference_keywords", ["需求背景"])

    print(f"[日志] {log_path}")
    state = {"prd_path": args.prd}
    nodes = [
        ("parse_md", preprocess.parse_md),
        ("classify_sections", lambda s: preprocess.classify_sections(s, core_kw, ref_kw)),
    ]
    for name, fn in nodes:
        result = _logged_node(name, fn, settings)(state)
        state.update(result)

    print(f"[完成] 文档：{state.get('doc_title', '')}")
    print(
        f"章节 {len(state.get('sections', []))} 个，"
        f"核心章节 {len(state.get('core_sections', []))} 个，"
        f"参考章节 {len(state.get('reference_sections', []))} 个"
    )


if __name__ == "__main__":
    main()
