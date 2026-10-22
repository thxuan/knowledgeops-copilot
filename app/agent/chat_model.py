"""Single-turn BaseChatModel bridge for the existing compatible provider contract."""
from typing import Any
from pydantic import Field
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, convert_to_openai_messages
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.utils.function_calling import convert_to_openai_tool


class AgentChatModel(BaseChatModel):
    backend: Any = Field(exclude=True, repr=False)

    @property
    def _llm_type(self):
        return "knowledgeops-compatible"

    def bind_tools(self, tools, **kwargs):
        return self.bind(tools=[convert_to_openai_tool(tool) for tool in tools], **kwargs)

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        raise NotImplementedError("KnowledgeOps agents use asynchronous execution")

    async def _agenerate(self, messages, stop=None, run_manager=None, **kwargs):
        turn = await self.backend.complete(convert_to_openai_messages(messages), kwargs.get("tools", []))
        # Keep the unnormalized protocol/usage until policy middleware validates it.
        # The middleware removes this transient field before framework persistence.
        reply = AIMessage(content="", additional_kwargs={"_ko_turn": turn})
        return ChatResult(generations=[ChatGeneration(message=reply)])
