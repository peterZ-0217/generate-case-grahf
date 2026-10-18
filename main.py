"""CLI 入口：
    python main.py <prd路径> [--thread-id TID]

流程跑到人工审核节点会中断并展示审核材料：
  - 输入 approve 批准，产出文件；
  - 输入 reject: <修改意见> 打回重抽。
"""

import argparse
import json
import os
import sys

_REPO_ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _REPO_ROOT)
# 相对路径（config/、output/、logs/）统一以仓库根为基准，避免在别的目录启动时写错位置
os.chdir(_REPO_ROOT)

from langgraph.types import Command

from src.graph import build_graph


def _print_review_materials(payload: dict) -> None:
    print("\n" + "=" * 60)
    print(f"人工审核：{payload.get('doc_title', '')}")
    print(f"测试点模式：{payload.get('mode', 'prd_focused')}")
    print(f"功能单元 {payload.get('unit_count')} 个，测试点 {payload.get('test_point_count')} 条"
          f"（其中推导点 {payload.get('derived_point_count', 0)} 条）")
    cov = payload.get("coverage_report", {}).get("coverage", {})
    print(f"PRD 规则覆盖率：{cov.get('covered')}/{cov.get('total')}（{cov.get('coverage_rate', 0):.1%}）")
    amb = payload.get("ambiguities") or []
    if amb:
        print("\n待确认歧义：")
        for a in amb:
            print(f"  - {a.get('question')}")
    print("=" * 60)


def main() -> None:
    parser = argparse.ArgumentParser(description="PRD 需求分析拆解智能体")
    parser.add_argument("prd", help="PRD markdown 文件路径")
    parser.add_argument("--thread-id", default="default", help="会话 ID（断点续跑/区分文档）")
    parser.add_argument("--sections-config", default="config/sections.yaml")
    parser.add_argument("--settings-config", default="config/settings.yaml")
    parser.add_argument(
        "--llm-provider",
        default=None,
        help="覆盖 settings 中的 llm_provider，取值对应配置块 llm / llm-<name>（如 deepseek）",
    )
    parser.add_argument(
        "--test-point-mode",
        default=None,
        choices=["prd_focused", "extended"],
        help="测试点模式：prd_focused（PRD 明确规则+少量推导，默认）/ extended（更完整的测试经验扩展）",
    )
    args = parser.parse_args()

    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

    graph = build_graph(args.sections_config, args.settings_config, args.llm_provider,
                        args.test_point_mode)
    print(f"[模型] provider={graph.llm_provider} model={graph.llm_model} mode={graph.test_point_mode}")
    if getattr(graph, "llm_mock", False):
        print("[提示] 未检测到 API Key，当前为 mock 模式：LLM 产出为占位内容，仅用于验证流程。")

    config = {"configurable": {"thread_id": args.thread_id}}
    result = graph.invoke({"prd_path": args.prd}, config=config)

    while "__interrupt__" in result:
        payload = result["__interrupt__"][0].value
        _print_review_materials(payload)
        answer = input("输入 approve 批准，或 reject: <修改意见> 打回重抽：").strip()
        if answer.lower() in ("approve", "a", "y"):
            decision = {"status": "approved"}
        elif answer.lower().startswith("reject"):
            feedback = answer.split(":", 1)[1].strip() if ":" in answer else ""
            decision = {"status": "rejected", "feedback": feedback}
        else:
            print("无法识别的输入，按打回处理。")
            decision = {"status": "rejected", "feedback": answer}
        result = graph.invoke(Command(resume=decision), config=config)

    files = result.get("output_files", {})
    print("\n产出文件：")
    for kind, path in files.items():
        print(f"  [{kind}] {path}")


if __name__ == "__main__":
    main()
