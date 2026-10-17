"""LLM 连通性自检（不需要人工审核交互）：

    python scripts/check_llm.py [--llm-provider deepseek] [--settings-config config/settings.yaml]

用途：确认 provider / model / base_url / api_key 是否生效，以及模型名是否被端点接受。
成功时打印模型返回的一句话，失败时打印原始异常（含 HTTP 状态），便于快速定位
是密钥问题、模型名问题还是网络问题。
"""

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import yaml

from src.llm import LLMClient


def main() -> int:
    parser = argparse.ArgumentParser(description="LLM 连通性自检")
    parser.add_argument("--settings-config", default="config/settings.yaml")
    parser.add_argument("--llm-provider", default=None,
                        help="覆盖 settings 中的 llm_provider（如 deepseek）")
    args = parser.parse_args()

    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

    with open(args.settings_config, encoding="utf-8") as f:
        settings = yaml.safe_load(f)

    try:
        llm = LLMClient(settings, provider=args.llm_provider)
    except Exception as exc:
        print(f"[失败] 初始化 LLM 客户端出错：{exc}")
        return 2

    print(f"provider = {llm.provider}")
    print(f"model    = {llm.model}")
    print(f"base_url = {llm.base_url or '(官方默认)'}")
    print(f"mock     = {llm.mock}")
    if llm.mock:
        print("[提示] 没有解析到 API Key，当前会走 mock。请检查配置块 api_key / api_key_env。")
        return 1

    try:
        text = llm.chat_text(
            "你是一个连通性测试助手，只回复一句话。",
            "请回复：连通性正常",
        )
    except Exception as exc:
        print(f"[失败] 调用模型出错：{type(exc).__name__}: {exc}")
        return 3

    print(f"[成功] 模型返回：{text.strip()[:200]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
