"""Reuse the existing OpenAI-compatible SDK for text embeddings."""

from langchain_openai import OpenAIEmbeddings

from agent_harness.config import Settings


def create_embeddings(settings: Settings) -> OpenAIEmbeddings:
    if not all((settings.embedding_model, settings.embedding_base_url,
                settings.embedding_api_key.get_secret_value())):
        raise ValueError("Embedding configuration is incomplete")
    return OpenAIEmbeddings(
        model=settings.embedding_model, base_url=settings.embedding_base_url,
        api_key=settings.embedding_api_key, check_embedding_ctx_length=False,
        dimensions=settings.embedding_dimensions,
        request_timeout=15, max_retries=0,
    )
