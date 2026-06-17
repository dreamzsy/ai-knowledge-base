"""统一的 LLM 调用客户端.

为知识库流水线（采集 / 分析 / 整理）提供一个与具体模型提供商解耦的
调用入口，屏蔽各家 OpenAI 兼容 API 的差异。

用途：
    让上层 Agent 只需关心 ``chat(messages)`` 与返回的 :class:`LLMResponse`，
    通过环境变量切换 DeepSeek / Claude Code / OpenAI，无需改动业务代码。
输入：
    - 环境变量 ``LLM_PROVIDER``（默认 ``deepseek``）选择提供商。
    - 各提供商对应的 ``*_API_KEY`` 环境变量提供凭据。
    - 调用方传入的 OpenAI 风格 ``messages`` 列表。
输出：
    统一的 :class:`LLMResponse`，含生成文本与 :class:`Usage` 用量统计。

依赖：``httpx``（直接调用 OpenAI 兼容 HTTP 接口，不使用 openai SDK）。
"""

from __future__ import annotations

import logging
import os
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import httpx

try:  # python-dotenv 为可选依赖：装了就自动加载 .env，没装也不影响纯环境变量用法。
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:  # pragma: no cover - 仅在未安装 dotenv 时触发
    pass

logger = logging.getLogger(__name__)

DEFAULT_PROVIDER = "deepseek"
DEFAULT_TIMEOUT_SECONDS = 60.0
MAX_RETRIES = 3
RETRY_BACKOFF_BASE = 2.0

Message = Dict[str, str]

# 各提供商默认配置：base_url / 默认模型 / 读取凭据的环境变量名。
PROVIDER_CONFIGS: Dict[str, Dict[str, str]] = {
    "deepseek": {
        "base_url": "https://api.deepseek.com/v1",
        "default_model": "deepseek-chat",
        "api_key_env": "DEEPSEEK_API_KEY",
    },
    "claude_code": {
        "base_url": "https://api.anthropic.com/v1",
        "default_model": "claude-sonnet-4-6",
        "api_key_env": "ANTHROPIC_API_KEY",
    },
    "openai": {
        "base_url": "https://api.openai.com/v1",
        "default_model": "gpt-4o-mini",
        "api_key_env": "OPENAI_API_KEY",
    },
}

# 计价表：USD / 1K tokens，区分输入与输出。用于成本估算，非实时计费。
PRICING_USD_PER_1K: Dict[str, Dict[str, float]] = {
    "deepseek-chat": {"input": 0.00027, "output": 0.0011},
    "claude-sonnet-4-6": {"input": 0.003, "output": 0.015},
    "gpt-4o-mini": {"input": 0.00015, "output": 0.0006},
}

# 人民币计价表：元 / 百万 tokens，按「提供商」维度区分输入与输出。
# 供 CostTracker 估算累计成本，价格随官方调整，仅作预算参考。
PRICING_CNY_PER_MILLION: Dict[str, Dict[str, float]] = {
    "deepseek": {"input": 1.0, "output": 2.0},
    "qwen": {"input": 4.0, "output": 12.0},
    "openai": {"input": 150.0, "output": 600.0},  # gpt-4o-mini
}


@dataclass
class Usage:
    """单次调用的 Token 用量统计。

    Attributes:
        prompt_tokens: 输入（提示）消耗的 token 数。
        completion_tokens: 输出（生成）消耗的 token 数。
        total_tokens: 输入与输出之和。
    """

    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0


@dataclass
class LLMResponse:
    """统一的 LLM 响应结构。

    Attributes:
        content: 模型生成的文本内容。
        model: 实际应答的模型名。
        provider: 产生该响应的提供商标识。
        usage: 本次调用的 :class:`Usage` 用量统计。
        raw: 原始响应 JSON，便于调试与扩展字段读取。
    """

    content: str
    model: str
    provider: str
    usage: Usage = field(default_factory=Usage)
    raw: Dict[str, Any] = field(default_factory=dict)


class LLMProvider(ABC):
    """LLM 提供商抽象接口。

    子类负责把统一的 ``messages`` 调用映射到具体提供商的 HTTP 协议，
    并把响应规整为 :class:`LLMResponse`。
    """

    name: str = "base"

    @abstractmethod
    def chat(
        self,
        messages: List[Message],
        *,
        model: Optional[str] = None,
        temperature: float = 0.7,
        max_tokens: Optional[int] = None,
    ) -> LLMResponse:
        """发起一次对话补全请求。

        Args:
            messages: OpenAI 风格的消息列表，元素含 ``role`` 与 ``content``。
            model: 覆盖默认模型；为 ``None`` 时使用提供商默认模型。
            temperature: 采样温度，越高输出越随机。
            max_tokens: 生成的最大 token 数；``None`` 表示由服务端决定。

        Returns:
            统一的 :class:`LLMResponse`。

        Raises:
            httpx.HTTPError: 网络层或非 2xx 响应导致的请求失败。
        """
        raise NotImplementedError


class OpenAICompatibleProvider(LLMProvider):
    """基于 OpenAI 兼容 ``/chat/completions`` 接口的通用实现。

    DeepSeek、OpenAI 以及以 OpenAI 协议暴露的 Claude 网关均可复用本类，
    差异仅在 ``base_url`` / 默认模型 / 凭据来源。

    Attributes:
        name: 提供商标识（如 ``deepseek``）。
        base_url: API 根地址，末尾不含斜杠。
        default_model: 调用未显式指定 ``model`` 时使用的模型名。
        api_key: 鉴权用的 API Key（仅来自环境变量）。
        timeout: 单次请求超时秒数。
    """

    def __init__(
        self,
        name: str,
        base_url: str,
        default_model: str,
        api_key: str,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        """初始化提供商客户端。

        Args:
            name: 提供商标识。
            base_url: API 根地址。
            default_model: 默认模型名。
            api_key: API 凭据，由调用方从环境变量注入。
            timeout: 请求超时秒数，默认 60 秒。
        """
        self.name = name
        self.base_url = base_url.rstrip("/")
        self.default_model = default_model
        self.api_key = api_key
        self.timeout = timeout

    def chat(
        self,
        messages: List[Message],
        *,
        model: Optional[str] = None,
        temperature: float = 0.7,
        max_tokens: Optional[int] = None,
    ) -> LLMResponse:
        """调用 OpenAI 兼容接口并规整响应。详见基类 :meth:`LLMProvider.chat`。"""
        used_model = model or self.default_model
        payload: Dict[str, Any] = {
            "model": used_model,
            "messages": messages,
            "temperature": temperature,
        }
        if max_tokens is not None:
            payload["max_tokens"] = max_tokens

        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        url = f"{self.base_url}/chat/completions"
        logger.debug("POST %s model=%s", url, used_model)

        response = httpx.post(url, json=payload, headers=headers, timeout=self.timeout)
        response.raise_for_status()
        data = response.json()
        result = self._parse_response(data, used_model)
        # 调用成功后自动累计用量到全局成本追踪器（覆盖所有上层调用路径）。
        tracker.record(result.usage, self.name)
        return result

    def _parse_response(self, data: Dict[str, Any], used_model: str) -> LLMResponse:
        """把原始 JSON 解析为 :class:`LLMResponse`。

        Args:
            data: 接口返回的原始 JSON 字典。
            used_model: 本次请求实际使用的模型名。

        Returns:
            规整后的 :class:`LLMResponse`。
        """
        choices = data.get("choices") or [{}]
        content = choices[0].get("message", {}).get("content", "")
        raw_usage = data.get("usage") or {}
        usage = Usage(
            prompt_tokens=raw_usage.get("prompt_tokens", 0),
            completion_tokens=raw_usage.get("completion_tokens", 0),
            total_tokens=raw_usage.get("total_tokens", 0),
        )
        return LLMResponse(
            content=content,
            model=data.get("model", used_model),
            provider=self.name,
            usage=usage,
            raw=data,
        )


def build_provider(
    provider_name: Optional[str] = None,
    *,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
) -> LLMProvider:
    """根据名称（或环境变量）构建提供商实例。

    Args:
        provider_name: 提供商标识；为 ``None`` 时读取环境变量
            ``LLM_PROVIDER``，仍缺省则回退到 :data:`DEFAULT_PROVIDER`。
        timeout: 请求超时秒数。

    Returns:
        已注入凭据的 :class:`LLMProvider` 实例。

    Raises:
        ValueError: 提供商不受支持，或对应的 API Key 环境变量未设置。
    """
    name = (provider_name or os.getenv("LLM_PROVIDER") or DEFAULT_PROVIDER).lower()
    config = PROVIDER_CONFIGS.get(name)
    if config is None:
        supported = ", ".join(sorted(PROVIDER_CONFIGS))
        raise ValueError(f"不支持的 LLM_PROVIDER: {name!r}（可选：{supported}）")

    api_key = os.getenv(config["api_key_env"], "").strip()
    if not api_key:
        raise ValueError(
            f"提供商 {name!r} 缺少凭据，请设置环境变量 {config['api_key_env']}。"
        )

    return OpenAICompatibleProvider(
        name=name,
        base_url=config["base_url"],
        default_model=config["default_model"],
        api_key=api_key,
        timeout=timeout,
    )


# 兼容别名：流水线等调用方使用 create_provider 这一命名。
create_provider = build_provider


def chat_with_retry(
    provider: LLMProvider,
    messages: List[Message],
    *,
    model: Optional[str] = None,
    temperature: float = 0.7,
    max_tokens: Optional[int] = None,
    max_retries: int = MAX_RETRIES,
) -> LLMResponse:
    """带指数退避重试的对话调用。

    对网络错误与 5xx 服务端错误重试；4xx 客户端错误（如鉴权失败）直接抛出，
    重试无意义。退避时长为 ``RETRY_BACKOFF_BASE ** attempt`` 秒。

    Args:
        provider: 已构建的提供商实例。
        messages: OpenAI 风格消息列表。
        model: 覆盖默认模型。
        temperature: 采样温度。
        max_tokens: 生成的最大 token 数。
        max_retries: 最大尝试次数，默认 :data:`MAX_RETRIES`。

    Returns:
        成功时的 :class:`LLMResponse`。

    Raises:
        httpx.HTTPError: 重试耗尽后仍失败，抛出最后一次异常。
    """
    last_exc: Optional[Exception] = None
    for attempt in range(max_retries):
        try:
            return provider.chat(
                messages, model=model, temperature=temperature, max_tokens=max_tokens
            )
        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code
            if 400 <= status < 500:
                logger.error("客户端错误 %s，不重试：%s", status, exc)
                raise
            last_exc = exc
            logger.warning("第 %d/%d 次失败（HTTP %s）", attempt + 1, max_retries, status)
        except httpx.HTTPError as exc:
            last_exc = exc
            logger.warning("第 %d/%d 次失败（%s）", attempt + 1, max_retries, exc)

        if attempt < max_retries - 1:
            backoff = RETRY_BACKOFF_BASE ** attempt
            logger.info("等待 %.1fs 后重试", backoff)
            time.sleep(backoff)

    assert last_exc is not None  # 循环至少执行一次，必有异常被捕获
    raise last_exc


def estimate_tokens(text: str) -> int:
    """粗略估算文本的 token 数。

    采用「英文约 4 字符/token、CJK 约 1.5 字符/token」的经验混合估算，
    仅用于调用前的预算与成本预估，不替代服务端的精确计数。

    Args:
        text: 待估算的文本。

    Returns:
        估算的 token 数（向上取整，至少为 1，空串为 0）。
    """
    if not text:
        return 0
    cjk = sum(1 for ch in text if "一" <= ch <= "鿿")
    ascii_like = len(text) - cjk
    estimated = ascii_like / 4.0 + cjk / 1.5
    return max(1, int(estimated + 0.999))


def estimate_cost_usd(model: str, prompt_tokens: int, completion_tokens: int) -> float:
    """按计价表估算单次调用成本（USD）。

    Args:
        model: 模型名，用于查 :data:`PRICING_USD_PER_1K`。
        prompt_tokens: 输入 token 数。
        completion_tokens: 输出 token 数。

    Returns:
        估算成本（美元）；模型未在计价表中时返回 ``0.0`` 并告警。
    """
    pricing = PRICING_USD_PER_1K.get(model)
    if pricing is None:
        logger.warning("模型 %s 无计价数据，成本按 0 计", model)
        return 0.0
    cost = (
        prompt_tokens / 1000.0 * pricing["input"]
        + completion_tokens / 1000.0 * pricing["output"]
    )
    return round(cost, 6)


@dataclass
class _ProviderCost:
    """单个提供商的累计用量与调用次数（CostTracker 内部使用）。

    Attributes:
        calls: 已记录的成功调用次数。
        prompt_tokens: 累计输入 token 数。
        completion_tokens: 累计输出 token 数。
    """

    calls: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0


class CostTracker:
    """累计 LLM 调用的 token 用量并按人民币估算成本。

    以「提供商」为维度聚合，价格取自 :data:`PRICING_CNY_PER_MILLION`
    （元 / 百万 tokens）。线程不安全，适用于单进程串行的流水线场景。

    Attributes:
        costs: 提供商标识到其累计用量 :class:`_ProviderCost` 的映射。
    """

    def __init__(self) -> None:
        """初始化空的成本追踪器。"""
        self.costs: Dict[str, _ProviderCost] = {}

    def record(self, usage: Usage, provider: str) -> None:
        """记录一次成功调用的 token 用量。

        Args:
            usage: 本次调用的 :class:`Usage` 用量统计。
            provider: 提供商标识（如 ``deepseek`` / ``qwen`` / ``openai``）。
        """
        entry = self.costs.setdefault(provider, _ProviderCost())
        entry.calls += 1
        entry.prompt_tokens += usage.prompt_tokens
        entry.completion_tokens += usage.completion_tokens

    def estimated_cost(self, provider: str) -> float:
        """估算某提供商的累计成本（人民币元）。

        Args:
            provider: 提供商标识。

        Returns:
            估算成本（元，保留 4 位小数）；无记录或无计价数据时返回 ``0.0``。
        """
        entry = self.costs.get(provider)
        if entry is None:
            return 0.0
        pricing = PRICING_CNY_PER_MILLION.get(provider)
        if pricing is None:
            logger.warning("提供商 %s 无人民币计价数据，成本按 0 计", provider)
            return 0.0
        cost = (
            entry.prompt_tokens / 1_000_000 * pricing["input"]
            + entry.completion_tokens / 1_000_000 * pricing["output"]
        )
        return round(cost, 4)

    def total_cost(self) -> float:
        """汇总所有提供商的估算成本（元）。

        Returns:
            各提供商成本之和，保留 4 位小数。
        """
        return round(sum(self.estimated_cost(p) for p in self.costs), 4)

    def report(self, provider: Optional[str] = None) -> None:
        """以日志形式打印成本报告。

        Args:
            provider: 指定提供商则只报告该项；为 ``None`` 时报告全部并汇总。
        """
        targets = [provider] if provider else sorted(self.costs)
        if not targets:
            logger.info("成本报告：暂无 LLM 调用记录。")
            return
        logger.info("===== LLM 成本报告（估算，单位：元）=====")
        for name in targets:
            entry = self.costs.get(name)
            if entry is None:
                logger.info("  %s：无调用记录", name)
                continue
            logger.info(
                "  %s：调用 %d 次，输入 %d / 输出 %d tokens，约 ¥%.4f",
                name, entry.calls, entry.prompt_tokens,
                entry.completion_tokens, self.estimated_cost(name),
            )
        if provider is None and len(targets) > 1:
            logger.info("  合计：约 ¥%.4f", self.total_cost())


# 全局成本追踪器：chat 成功后自动记录，流水线结束时调 report()。
tracker = CostTracker()


def quick_chat(
    prompt: str,
    *,
    system: Optional[str] = None,
    provider_name: Optional[str] = None,
    model: Optional[str] = None,
    temperature: float = 0.7,
) -> str:
    """一句话便捷调用：传入用户提示，返回模型文本。

    内部完成「构建提供商 → 组装 messages → 带重试调用」全流程，适合脚本与
    交互式探索。需要用量统计或多轮对话时请改用 :func:`chat_with_retry`。

    Args:
        prompt: 用户提示文本。
        system: 可选的 system 提示。
        provider_name: 覆盖提供商；``None`` 时走环境变量。
        model: 覆盖默认模型。
        temperature: 采样温度。

    Returns:
        模型生成的文本内容。

    Raises:
        ValueError: 提供商不受支持或缺少凭据。
        httpx.HTTPError: 重试耗尽后仍请求失败。
    """
    provider = build_provider(provider_name)
    messages: List[Message] = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})
    response = chat_with_retry(provider, messages, model=model, temperature=temperature)
    return response.content


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    # 1) 离线自测：不触发任何网络请求，验证纯函数逻辑。
    sample = "LangGraph 引入持久化状态图，支持 long-running agents。"
    tokens = estimate_tokens(sample)
    cost = estimate_cost_usd("deepseek-chat", prompt_tokens=tokens, completion_tokens=200)
    logger.info("样本估算：tokens=%d，假设输出 200 tokens 成本≈$%.6f", tokens, cost)

    # 2) 在线冒烟：仅当对应 provider 的 API Key 已配置时才真正发起调用。
    provider_name = os.getenv("LLM_PROVIDER", DEFAULT_PROVIDER).lower()
    key_env = PROVIDER_CONFIGS.get(provider_name, {}).get("api_key_env", "")
    if key_env and os.getenv(key_env):
        try:
            reply = quick_chat("用一句话解释什么是 RAG。", system="你是简洁的中文技术助手。")
            logger.info("在线调用成功，回复：%s", reply)
        except (ValueError, httpx.HTTPError) as exc:
            logger.error("在线调用失败：%s", exc)
    else:
        logger.info("未检测到 %s，跳过在线冒烟测试。", key_env or "API Key")
