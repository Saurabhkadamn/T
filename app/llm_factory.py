"""
llm_factory — Single source of truth for LLM instantiation.

All LLM-using nodes call ``get_llm(task, temperature)`` instead of
constructing a provider-specific class directly.  Switching providers
across the entire pipeline requires only changing ``LLM_PROVIDER`` in
the environment — no node code changes needed.

Supported providers (set via ``LLM_PROVIDER`` env var):
  google     — ChatGoogleGenerativeAI  (default)
  openai     — ChatOpenAI
  anthropic  — ChatAnthropic

The task name maps to the correct model tier (pro vs flash) via
``settings.models.model_for_provider(task, provider)``.

Usage in a node::

    from app.llm_factory import get_llm

    llm = get_llm("query_analysis", temperature=0.2)
    response = await llm.ainvoke(messages)
"""

from __future__ import annotations

from langchain_core.language_models import BaseChatModel

from app.config import settings


def get_llm(task: str, temperature: float = 0.2) -> BaseChatModel:
    """Return a configured LangChain chat model for the given task.

    Args:
        task: Task name matching a ``use_pro_for_*`` field in ModelConfig
              (e.g. ``"query_analysis"``, ``"compression"``).
        temperature: Sampling temperature passed to the provider.

    Returns:
        A LangChain ``BaseChatModel`` ready for ``await llm.ainvoke(...)``.

    Raises:
        ValueError: If ``settings.llm_provider`` is not a recognised value.
        ImportError: If the required provider package is not installed.
    """
    provider = settings.llm_provider

    if provider == "google":
        from langchain_google_genai import ChatGoogleGenerativeAI

        model_name = settings.models.model_for(task)
        return ChatGoogleGenerativeAI(
            model=model_name,
            google_api_key=settings.gemini_api_key,
            temperature=temperature,
        )

    if provider == "openai":
        from langchain_openai import ChatOpenAI

        model_name = settings.models.model_for_provider(task, "openai")
        return ChatOpenAI(
            model=model_name,
            openai_api_key=settings.openai_api_key,
            base_url=settings.openai_base_url,
            temperature=temperature,
        )

    if provider == "anthropic":
        from langchain_anthropic import ChatAnthropic

        model_name = settings.models.model_for_provider(task, "anthropic")
        return ChatAnthropic(
            model=model_name,
            anthropic_api_key=settings.anthropic_api_key,
            temperature=temperature,
        )

    raise ValueError(
        f"Unknown llm_provider: {provider!r}. "
        "Must be one of: 'google', 'openai', 'anthropic'."
    )



