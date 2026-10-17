"""LLM 调用封装：OpenAI 兼容接口；无 API Key 时进入 mock 模式保证流程可跑通。

多 provider 支持：
  - settings.yaml 中每个 provider 对应一个顶层配置块，命名规则为 ``llm``（默认块）
    或 ``llm-<provider>``（如 ``llm-deepseek``）；
  - 生效的块由优先级决定：显式传参 > settings 的 ``llm_provider`` > ``llm`` 块；
  - 密钥优先级：配置块内的 ``api_key`` > 环境变量（``api_key_env`` 指定）> mock；
  - 配置值支持 ``${ENV_NAME}`` 展开，便于密钥只存在环境变量里。
"""

import json
import os
import re
from typing import Any, Dict, List, Optional, Tuple

from .logger import get_logger

logger = get_logger("llm")

# 约定：默认 provider 的配置块名为 "llm"，其余为 "llm-<name>"
_DEFAULT_PROVIDER = "llm"
_PROVIDER_PREFIX = "llm-"

_ENV_REF_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


def _coalesce(*values: Any) -> Any:
    """返回第一个非 None 的值（注意与 ``or`` 的区别：0 / "" / False 会被保留）。"""
    for value in values:
        if value is not None:
            return value
    return None


def expand_env(value: Any) -> Any:
    """递归展开字符串中的 ``${VAR}``；变量不存在时替换为空串。"""
    if isinstance(value, str):
        return _ENV_REF_RE.sub(lambda m: os.environ.get(m.group(1), ""), value)
    if isinstance(value, dict):
        return {k: expand_env(v) for k, v in value.items()}
    if isinstance(value, list):
        return [expand_env(v) for v in value]
    return value


def _mask(secret: str) -> str:
    """日志用：只暴露首尾少量字符。"""
    if not secret:
        return ""
    if len(secret) <= 10:
        return "*" * len(secret)
    return f"{secret[:5]}...{secret[-4:]}"


def resolve_llm_config(
    settings: Dict[str, Any], provider: Optional[str] = None
) -> Tuple[str, Dict[str, Any]]:
    """定位生效的 provider 配置块。

    返回 (provider_name, config_block)。provider_name 为日志友好名：
    默认块返回 "default"，``llm-deepseek`` 返回 "deepseek"。
    """
    name = provider or settings.get("llm_provider") or _DEFAULT_PROVIDER
    name = str(name).strip()
    if name in (_DEFAULT_PROVIDER, "default"):
        block_name = _DEFAULT_PROVIDER
    elif name.startswith(_PROVIDER_PREFIX):
        block_name = name
    else:
        block_name = f"{_PROVIDER_PREFIX}{name}"
    cfg = settings.get(block_name)
    if not isinstance(cfg, dict):
        available = sorted(
            k for k in settings
            if k == _DEFAULT_PROVIDER or k.startswith(_PROVIDER_PREFIX)
        )
        # raise ValueError(
        #     f"未找到 provider 配置块 '{block_name}'；settings 中可用的块：{available}。"
        #     f"请检查 llm_provider 或 --llm-provider 的取值。"
        # )

    display = "default" if block_name == _DEFAULT_PROVIDER else block_name[len(_PROVIDER_PREFIX):]
    return display, cfg


class LLMClient:
    def __init__(self, settings: Dict[str, Any], provider: Optional[str] = None):
        settings = expand_env(settings)
        self.provider, cfg = resolve_llm_config(settings, provider)

        if not cfg.get("model"):
            raise ValueError(f"provider '{self.provider}' 的配置缺少 model 字段。")
        self.model = str(cfg["model"])
        self.base_url = cfg.get("base_url") or os.environ.get("OPENAI_BASE_URL") or None

        # temperature: 不写/null → 默认 0.2；显式写 false → 不向厂商传该参数
        # （部分模型温度固定，传了会报错，例如 Kimi K3）
        temperature = cfg.get("temperature")
        self.temperature: Optional[float] = None if temperature is False else _coalesce(temperature, 0.2)

        # 厂商特有参数，如 max_tokens / top_p，原样透传
        self.extra_params = dict(cfg.get("request") or {})

        self.api_key = self._resolve_api_key(cfg)
        self.mock = not self.api_key and settings.get("mock_when_no_api_key", True)
        self.client = None
        if not self.mock:
            from openai import OpenAI

            self.client = OpenAI(api_key=self.api_key, base_url=self.base_url)

        logger.info(
            "LLM 初始化 | provider=%s | model=%s | base_url=%s | api_key=%s | mock=%s",
            self.provider, self.model, self.base_url or "(官方默认)",
            _mask(self.api_key), self.mock,
        )

    @staticmethod
    def _resolve_api_key(cfg: Dict[str, Any]) -> str:
        """配置块内 api_key 直填优先；否则读 api_key_env 指定的环境变量。"""
        inline = str(cfg.get("api_key") or "").strip()
        if inline:
            return inline
        env_name = cfg.get("api_key_env") or "OPENAI_API_KEY"
        return os.environ.get(str(env_name), "").strip()

    def _build_messages(self, system: str, user: str) -> List[Dict[str, str]]:
        return [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ]

    def chat_text(self, system: str, user: str, mock: str = "") -> str:
        if self.mock:
            logger.warning("LLM mock 模式返回占位内容 | provider=%s | model=%s",
                           self.provider, self.model)
            return mock

        request: Dict[str, Any] = {
            "model": self.model,
            "messages": self._build_messages(system, user),
        }
        if self.temperature is not None:
            request["temperature"] = self.temperature
        request.update(self.extra_params)

        logger.info(
            "LLM 请求开始 | provider=%s | model=%s | prompt_chars=%d",
            self.provider, self.model, len(system) + len(user),
        )
        try:
            resp = self.client.chat.completions.create(**request)
        except Exception:
            logger.exception("LLM 请求失败 | provider=%s | model=%s", self.provider, self.model)
            raise

        content = resp.choices[0].message.content or ""
        usage = getattr(resp, "usage", None)
        logger.info(
            "LLM 请求结束 | provider=%s | model=%s | response_chars=%d | tokens=%s",
            self.provider, self.model, len(content),
            f"{usage.prompt_tokens}/{usage.completion_tokens}" if usage else "unknown",
        )
        return content

    def chat_json(self, system: str, user: str, mock: Any) -> Any:
        """要求模型返回 JSON；容错提取首个 JSON 数组/对象。"""
        text = self.chat_text(
            system + "\n只输出 JSON，不要输出任何其他内容。",
            user,
            mock=json.dumps(mock, ensure_ascii=False),
        )
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            m = re.search(r"(\[.*\]|\{.*\})", text, re.S)
            if m:
                return json.loads(m.group(1))
            logger.error("LLM 返回内容无法解析为 JSON | provider=%s | head=%s",
                         self.provider, text[:200])
            raise
