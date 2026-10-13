"""Runtime must never silently substitute a simulated model."""
import asyncio
from dataclasses import replace
import pytest
from app.core.embedding import EmbeddingSettings, EmbeddingError, make_embedding_client, CompatibleEmbeddingClient
from app.generation.provider import GenerationSettings, make_generation_client, CompatibleGenerationClient
from app.agent.contracts import AgentSettings
from app.agent.provider import UnconfiguredAgentModel
from app.retrieval.rerank import RerankSettings
from app.core.errors import AppError

@pytest.mark.parametrize("settings", [EmbeddingSettings(), GenerationSettings(), AgentSettings(), RerankSettings()])
def test_runtime_rejects_fake_provider(settings):
    with pytest.raises(ValueError):
        replace(settings, provider="fake").validate()

def test_unconfigured_models_fail_without_generating_outputs():
    with pytest.raises(EmbeddingError):
        asyncio.run(make_embedding_client(EmbeddingSettings()).embed(["question"]))
    with pytest.raises(AppError) as generation:
        asyncio.run(make_generation_client(GenerationSettings()).generate([], GenerationSettings()))
    assert generation.value.code == "MODEL_NOT_CONFIGURED"
    with pytest.raises(AppError) as agent:
        asyncio.run(UnconfiguredAgentModel().complete([], []))
    assert agent.value.code == "AGENT_MODEL_NOT_CONFIGURED"

def test_configured_factories_select_real_adapters():
    assert isinstance(make_embedding_client(EmbeddingSettings(provider="compatible", api_key="test-only")), CompatibleEmbeddingClient)
    assert isinstance(make_generation_client(GenerationSettings(provider="compatible", api_key="test-only")), CompatibleGenerationClient)
