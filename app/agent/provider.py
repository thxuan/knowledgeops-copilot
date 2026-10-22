"""LangChain model adapter; business tools and paid retries stay server-owned."""
from app.core.errors import AppError
from app.core.langchain_models import LangChainAgentModel as CompatibleAgentModel, Retryable


class UnconfiguredAgentModel:
    async def complete(self, messages, tools):
        raise AppError('AGENT_MODEL_NOT_CONFIGURED', '请配置真实 Agent 模型和输入、输出单价。', 422)
