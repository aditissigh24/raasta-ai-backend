"""
LLM initialization and configuration utilities.
"""
from langchain_openai import ChatOpenAI
from config.settings import settings


def get_llm(
    model: str | None = None,
    temperature: float = 0.7,
    streaming: bool = True
) -> ChatOpenAI:
    """
    Initialize and return a ChatOpenAI instance.
    
    Args:
        model: The model to use. Defaults to settings.OPENAI_MODEL
        temperature: Sampling temperature (0.0 to 1.0). Higher = more creative.
        streaming: Whether to enable streaming responses.
        
    Returns:
        Configured ChatOpenAI instance
    """
    return ChatOpenAI(
        api_key=settings.OPENAI_API_KEY,
        model=model or settings.OPENAI_MODEL,
        temperature=temperature,
        streaming=streaming
    )


