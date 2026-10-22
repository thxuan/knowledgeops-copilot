"""Real generation adapter with an explicit unconfigured state."""
from dataclasses import dataclass
from typing import Protocol
from app.core.errors import AppError
from app.core.langchain_models import LangChainGenerationClient

@dataclass(frozen=True)
class GenerationSettings:
    provider: str = "unconfigured"
    model_id: str = "qwen-plus"
    input_limit: int = 32000
    output_limit: int = 2048
    history_budget: int = 2048
    summary_budget: int = 2048
    timeout_seconds: float = 60
    base_url: str = "https://api.example.invalid/v1"
    api_key: str | None = None
    temperature: float = 0.0
    top_p: float | None = None
    coverage_review: bool = True

    def validate(self):
        import math
        if type(self.coverage_review) is not bool:
            raise ValueError("coverage_review must be boolean")
        if self.provider not in {"unconfigured", "compatible"}:
            raise ValueError("Unsupported generation provider")
        if not 1 <= self.output_limit < self.input_limit or min(self.history_budget, self.summary_budget) < 0:
            raise ValueError("Invalid generation context budget")
        if not math.isfinite(self.timeout_seconds) or self.timeout_seconds <= 0:
            raise ValueError("Invalid generation timeout")
        if not math.isfinite(self.temperature) or not 0 <= self.temperature < 2:
            raise ValueError("Invalid temperature")
        if self.top_p is not None and (not math.isfinite(self.top_p) or not 0 < self.top_p <= 1):
            raise ValueError("Invalid top_p")
        if not self.model_id or (self.provider == "compatible" and not self.api_key):
            raise ValueError("Compatible generation needs model ID and API key")

class GenerationClient(Protocol):
    async def generate(self, messages: list[dict], settings: GenerationSettings) -> str: ...

class GenerationOutput(str):
    """String-compatible result carrying provider usage for offline evaluation."""
    def __new__(cls, value, metadata):
        instance = super().__new__(cls, value)
        instance.metadata = metadata
        return instance

class CompatibleGenerationClient:
    """Preserve the application result contract over the LangChain runtime."""
    def __init__(self, transport=None):
        self.runtime = LangChainGenerationClient(transport)

    async def generate(self, messages, settings):
        result = await self.runtime.generate(messages, settings)
        return GenerationOutput(result.text, result.metadata)

    async def generate_stream(self, messages, settings, on_delta):
        result = await self.runtime.generate_stream(messages, settings, on_delta)
        return GenerationOutput(result.text, result.metadata)


def make_generation_client(settings):
    settings.validate()
    return UnconfiguredGenerationClient() if settings.provider == "unconfigured" else CompatibleGenerationClient()

class UnconfiguredGenerationClient:
    async def generate(self, messages, settings):
        raise AppError('MODEL_NOT_CONFIGURED', '请先配置真实生成模型。', 422)
