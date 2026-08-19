"""Abstract model provider interface and implementations for LLM/SLM integration"""

from abc import ABC, abstractmethod
from typing import Optional, Tuple
from dataclasses import dataclass
import os
import time
from typing import Callable


@dataclass
class ModelCapabilities:
    """Declared capabilities of a model provider"""
    supports_prompt_caching: bool = False
    max_context_tokens: int = 4096
    supports_structured_output: bool = False
    est_cost_per_1k_input_tokens: float = 0.0  # 0 for self-hosted/local
    est_cost_per_1k_output_tokens: float = 0.0
    latency_class: str = "normal"  # "instant", "normal", "slow"
    reliability: str = "high"  # "high", "medium", "low"


@dataclass
class ModelResponse:
    """Structured response from a model provider"""
    content: str
    input_tokens: int
    output_tokens: int
    stop_reason: str  # "end_turn", "max_tokens", "error"
    estimated_input_cost: float = 0.0
    estimated_output_cost: float = 0.0
    retry_count: int = 0
    
    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens
    
    @property
    def total_cost(self) -> float:
        return self.estimated_input_cost + self.estimated_output_cost


class ModelProvider(ABC):
    """Abstract base class for model providers - user can choose any provider for any task"""
    
    def __init__(self, max_retries: Optional[int] = None, base_wait_seconds: Optional[float] = None):
        """
        Initialize model provider with retry configuration.
        
        Args:
            max_retries: Maximum number of retries on failure (from MAX_RETRIES env or passed explicitly)
            base_wait_seconds: Base wait time for exponential backoff (from BASE_WAIT_SECONDS env or passed explicitly)
        """
        if max_retries is not None:
            self.max_retries = max_retries
        else:
            env_max_retries = os.getenv("MAX_RETRIES")
            self.max_retries = int(env_max_retries) if env_max_retries else None

        self.base_wait_seconds = base_wait_seconds if base_wait_seconds is not None else float(
            os.getenv("BASE_WAIT_SECONDS", "1.0")
        )
    
    @abstractmethod
    def call(
        self,
        prompt: str,
        budget_tokens: int,
        temperature: float = 0.7,
    ) -> Tuple[bool, ModelResponse]:
        """
        Call model with the given prompt.
        
        Args:
            prompt: The prompt to send
            budget_tokens: Maximum tokens to spend on this call
            temperature: Sampling temperature (0.0-1.0)
            
        Returns:
            Tuple of (success, ModelResponse)
        """
        pass
    
    def call_with_retry(
        self,
        prompt: str,
        budget_tokens: int,
        temperature: float = 0.7,
    ) -> Tuple[bool, ModelResponse]:
        """
        Call model with automatic retry on transient failures.
        
        Handles:
        - Rate limit errors (429)
        - Timeout errors
        - Temporary service errors (5xx)
        
        Does NOT retry on:
        - Invalid input (400 errors)
        - Authentication errors (401, 403)
        - Model not found errors
        
        Args:
            prompt: The prompt to send
            budget_tokens: Maximum tokens to spend
            temperature: Sampling temperature
            
        Returns:
            Tuple of (success, ModelResponse) with retry_count set
        """
        if self.max_retries is None:
            raise ValueError(
                "MAX_RETRIES must be set explicitly or passed to the provider before using retry logic"
            )

        last_error = None
        
        for attempt in range(self.max_retries + 1):
            try:
                success, response = self.call(prompt, budget_tokens, temperature)
                response.retry_count = attempt
                
                if success:
                    return True, response
                
                # Check if error is retryable
                if self._is_retryable_error(response.content):
                    if attempt < self.max_retries:
                        wait_time = self._exponential_backoff(attempt)
                        print(f"Retryable error, waiting {wait_time}s before retry {attempt + 1}/{self.max_retries}")
                        time.sleep(wait_time)
                        continue
                
                # Non-retryable or final attempt
                return False, response
            
            except Exception as e:
                last_error = e
                if self._is_retryable_exception(e):
                    if attempt < self.max_retries:
                        wait_time = self._exponential_backoff(attempt)
                        print(f"Transient error: {type(e).__name__}, waiting {wait_time}s before retry {attempt + 1}/{self.max_retries}")
                        time.sleep(wait_time)
                        continue
                
                # Non-retryable exception or final attempt
                error_response = ModelResponse(
                    content=f"Exception after {attempt + 1} attempts: {str(e)}",
                    input_tokens=0,
                    output_tokens=0,
                    stop_reason="error",
                    retry_count=attempt,
                )
                return False, error_response
        
        # All retries exhausted
        error_response = ModelResponse(
            content=f"Failed after {self.max_retries + 1} attempts: {str(last_error)}",
            input_tokens=0,
            output_tokens=0,
            stop_reason="error",
            retry_count=self.max_retries,
        )
        return False, error_response
    
    def _exponential_backoff(self, attempt: int) -> float:
        """
        Calculate exponential backoff with jitter.
        
        Args:
            attempt: Current attempt number (0-indexed)
            
        Returns:
            Wait time in seconds
        """
        import random
        wait_time = self.base_wait_seconds * (2 ** attempt)
        # Add jitter: random value between 0 and wait_time
        jitter = random.uniform(0, wait_time * 0.1)
        return min(wait_time + jitter, 300)  # Cap at 5 minutes
    
    def _is_retryable_error(self, error_content: str) -> bool:
        """Check if error response is retryable"""
        retryable_keywords = [
            "rate limit",
            "429",
            "too many requests",
            "timeout",
            "temporarily unavailable",
            "service unavailable",
            "503",
            "502",
            "internal server error",
            "500",
        ]
        return any(keyword in error_content.lower() for keyword in retryable_keywords)
    
    def _is_retryable_exception(self, exception: Exception) -> bool:
        """Check if exception is retryable"""
        exception_str = str(exception).lower()
        retryable_keywords = [
            "timeout",
            "connection",
            "broken pipe",
            "connection reset",
        ]
        return any(keyword in exception_str for keyword in retryable_keywords)
    
    @abstractmethod
    def get_capabilities(self) -> ModelCapabilities:
        """Get declared capabilities of this provider"""
        pass
    
    def get_name(self) -> str:
        """Get provider name"""
        return self.__class__.__name__


class GroqProvider(ModelProvider):
    """Groq provider - fast, cost-effective, can be used for any task"""
    
    def __init__(
        self,
        api_key: Optional[str] = None,
        model: Optional[str] = None,
        max_retries: Optional[int] = None,
        base_wait_seconds: Optional[float] = None,
    ):
        """
        Initialize Groq provider.
        
        Args:
            api_key: Groq API key (defaults to GROQ_API_KEY env var)
            model: Model to use (defaults to GROQ_MODEL env var)
            max_retries: Maximum retries for call_with_retry
            base_wait_seconds: Base wait time for exponential backoff
        """
        super().__init__(max_retries=max_retries, base_wait_seconds=base_wait_seconds)
        self.api_key = api_key or os.getenv("GROQ_API_KEY")
        if not self.api_key:
            raise ValueError("GROQ_API_KEY not provided and not in environment")
        
        self.model_name = model or os.getenv("GROQ_MODEL")
        
        # Groq capabilities
        self.capabilities = ModelCapabilities(
            supports_prompt_caching=False,
            max_context_tokens=32768,
            supports_structured_output=False,
            est_cost_per_1k_input_tokens=0.00024,
            est_cost_per_1k_output_tokens=0.00024,
            latency_class="instant",
            reliability="high",
        )
    
    def call(
        self,
        prompt: str,
        budget_tokens: int,
        temperature: float = 0.7,
    ) -> Tuple[bool, ModelResponse]:
        """Call Groq API"""
        try:
            from groq import Groq
            
            client = Groq(api_key=self.api_key)
            
            completion = client.chat.completions.create(
                model=self.model_name,
                messages=[{"role": "user", "content": prompt}],
                max_tokens=min(budget_tokens, 2000),
                temperature=temperature,
            )
            
            input_tokens = completion.usage.prompt_tokens
            output_tokens = completion.usage.completion_tokens
            
            response = ModelResponse(
                content=completion.choices[0].message.content,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                stop_reason=completion.choices[0].finish_reason or "end_turn",
                estimated_input_cost=(input_tokens / 1000) * self.capabilities.est_cost_per_1k_input_tokens,
                estimated_output_cost=(output_tokens / 1000) * self.capabilities.est_cost_per_1k_output_tokens,
            )
            
            return True, response
        
        except Exception as e:
            error_response = ModelResponse(
                content=f"Error: {str(e)}",
                input_tokens=0,
                output_tokens=0,
                stop_reason="error",
            )
            return False, error_response
    
    def get_capabilities(self) -> ModelCapabilities:
        return self.capabilities


class OpenAIProvider(ModelProvider):
    """OpenAI provider - high quality, can be used for any task"""
    
    def __init__(
        self,
        api_key: Optional[str] = None,
        model: Optional[str] = None,
        max_retries: Optional[int] = None,
        base_wait_seconds: Optional[float] = None,
    ):
        """
        Initialize OpenAI provider.
        
        Args:
            api_key: OpenAI API key (defaults to OPENAI_API_KEY env var)
            model: Model to use (defaults to OPENAI_MODEL env var)
            max_retries: Maximum retries for call_with_retry
            base_wait_seconds: Base wait time for exponential backoff
        """
        super().__init__(max_retries=max_retries, base_wait_seconds=base_wait_seconds)
        self.api_key = api_key or os.getenv("OPENAI_API_KEY")
        if not self.api_key:
            raise ValueError("OPENAI_API_KEY not provided and not in environment")
        
        self.model_name = model or os.getenv("OPENAI_MODEL")
        
        # Set capabilities based on model
        if "gpt-4" in model.lower():
            cost_input = 0.003
            cost_output = 0.006
            context = 8192 if "turbo" in model.lower() else 128000
        else:  # gpt-3.5-turbo
            cost_input = 0.0005
            cost_output = 0.0015
            context = 4096
        
        self.capabilities = ModelCapabilities(
            supports_prompt_caching=True,
            max_context_tokens=context,
            supports_structured_output=True,
            est_cost_per_1k_input_tokens=cost_input,
            est_cost_per_1k_output_tokens=cost_output,
            latency_class="normal",
            reliability="high",
        )
    
    def call(
        self,
        prompt: str,
        budget_tokens: int,
        temperature: float = 0.7,
    ) -> Tuple[bool, ModelResponse]:
        """Call OpenAI API"""
        try:
            from openai import OpenAI
            
            client = OpenAI(api_key=self.api_key)
            
            completion = client.chat.completions.create(
                model=self.model_name,
                messages=[{"role": "user", "content": prompt}],
                max_tokens=min(budget_tokens, 3000),
                temperature=temperature,
            )
            
            input_tokens = completion.usage.prompt_tokens
            output_tokens = completion.usage.completion_tokens
            
            response = ModelResponse(
                content=completion.choices[0].message.content,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                stop_reason=completion.choices[0].finish_reason or "end_turn",
                estimated_input_cost=(input_tokens / 1000) * self.capabilities.est_cost_per_1k_input_tokens,
                estimated_output_cost=(output_tokens / 1000) * self.capabilities.est_cost_per_1k_output_tokens,
            )
            
            return True, response
        
        except Exception as e:
            error_response = ModelResponse(
                content=f"Error: {str(e)}",
                input_tokens=0,
                output_tokens=0,
                stop_reason="error",
            )
            return False, error_response
    
    def get_capabilities(self) -> ModelCapabilities:
        return self.capabilities


class AnthropicProvider(ModelProvider):
    """Anthropic Claude provider - excellent reasoning, can be used for any task"""
    
    def __init__(
        self,
        api_key: Optional[str] = None,
        model: str = "claude-3-opus-20240229",
        max_retries: Optional[int] = None,
        base_wait_seconds: Optional[float] = None,
    ):
        """
        Initialize Anthropic provider.
        
        Args:
            api_key: Anthropic API key (defaults to ANTHROPIC_API_KEY env var)
            model: Model to use (default: claude-3-opus-20240229)
            max_retries: Maximum retries for call_with_retry
            base_wait_seconds: Base wait time for exponential backoff
        """
        super().__init__(max_retries=max_retries, base_wait_seconds=base_wait_seconds)
        self.api_key = api_key or os.getenv("ANTHROPIC_API_KEY")
        if not self.api_key:
            raise ValueError("ANTHROPIC_API_KEY not provided and not in environment")
        
        self.model_name = model
        
        # Determine capabilities based on model
        if "opus" in model.lower():
            cost_input = 0.015
            cost_output = 0.075
        elif "sonnet" in model.lower():
            cost_input = 0.003
            cost_output = 0.015
        else:  # haiku
            cost_input = 0.00080
            cost_output = 0.0024
        
        self.capabilities = ModelCapabilities(
            supports_prompt_caching=True,
            max_context_tokens=200000,
            supports_structured_output=False,
            est_cost_per_1k_input_tokens=cost_input,
            est_cost_per_1k_output_tokens=cost_output,
            latency_class="normal",
            reliability="high",
        )
    
    def call(
        self,
        prompt: str,
        budget_tokens: int,
        temperature: float = 0.7,
    ) -> Tuple[bool, ModelResponse]:
        """Call Anthropic API"""
        try:
            import anthropic
            
            client = anthropic.Anthropic(api_key=self.api_key)
            
            message = client.messages.create(
                model=self.model_name,
                max_tokens=min(budget_tokens, 3000),
                messages=[{"role": "user", "content": prompt}],
                temperature=temperature,
            )
            
            input_tokens = message.usage.input_tokens
            output_tokens = message.usage.output_tokens
            
            response = ModelResponse(
                content=message.content[0].text,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                stop_reason=message.stop_reason or "end_turn",
                estimated_input_cost=(input_tokens / 1000) * self.capabilities.est_cost_per_1k_input_tokens,
                estimated_output_cost=(output_tokens / 1000) * self.capabilities.est_cost_per_1k_output_tokens,
            )
            
            return True, response
        
        except Exception as e:
            error_response = ModelResponse(
                content=f"Error: {str(e)}",
                input_tokens=0,
                output_tokens=0,
                stop_reason="error",
            )
            return False, error_response
    
    def get_capabilities(self) -> ModelCapabilities:
        return self.capabilities


class OpenRouterProvider(ModelProvider):
    """OpenRouter provider - access to many models (OpenAI, Anthropic, Meta, etc.)"""
    
    def __init__(
        self,
        api_key: Optional[str] = None,
        model: Optional[str] = None,
        max_retries: Optional[int] = None,
        base_wait_seconds: Optional[float] = None,
    ):
        """
        Initialize OpenRouter provider.
        
        Args:
            api_key: OpenRouter API key (defaults to OPENROUTER_API_KEY env var)
            model: Model to use (default: meta-llama/llama-2-70b-chat)
            max_retries: Maximum retries for call_with_retry
            base_wait_seconds: Base wait time for exponential backoff
        """
        super().__init__(max_retries=max_retries, base_wait_seconds=base_wait_seconds)
        self.api_key = api_key or os.getenv("OPENROUTER_API_KEY")
        if not self.api_key:
            raise ValueError("OPENROUTER_API_KEY not provided and not in environment")
        
        self.model_name = model or os.getenv("OPENROUTER_MODEL", "meta-llama/llama-2-70b-chat")
        
        # Generic capabilities - varies by model
        self.capabilities = ModelCapabilities(
            supports_prompt_caching=False,
            max_context_tokens=4096,
            supports_structured_output=False,
            est_cost_per_1k_input_tokens=0.001,  # Estimate
            est_cost_per_1k_output_tokens=0.002,
            latency_class="normal",
            reliability="medium",
        )
    
    def call(
        self,
        prompt: str,
        budget_tokens: int,
        temperature: float = 0.7,
    ) -> Tuple[bool, ModelResponse]:
        """Call OpenRouter API"""
        try:
            import httpx
            
            headers = {
                "Authorization": f"Bearer {self.api_key}",
                "HTTP-Referer": "https://github.com/abhiram-kintsugi/kintsugi",
                "X-Title": "Kintsugi CI Self-Healer",
            }
            
            data = {
                "model": self.model_name,
                "messages": [{"role": "user", "content": prompt}],
                "max_tokens": min(budget_tokens, 3000),
                "temperature": temperature,
            }
            
            response = httpx.post(
                "https://openrouter.ai/api/v1/chat/completions",
                headers=headers,
                json=data,
                timeout=30.0,
            )
            
            if response.status_code != 200:
                error_response = ModelResponse(
                    content=f"OpenRouter API error: {response.status_code}",
                    input_tokens=0,
                    output_tokens=0,
                    stop_reason="error",
                )
                return False, error_response
            
            result = response.json()
            
            input_tokens = result.get("usage", {}).get("prompt_tokens", 0)
            output_tokens = result.get("usage", {}).get("completion_tokens", 0)
            
            model_response = ModelResponse(
                content=result["choices"][0]["message"]["content"],
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                stop_reason=result["choices"][0].get("finish_reason", "end_turn"),
                estimated_input_cost=(input_tokens / 1000) * self.capabilities.est_cost_per_1k_input_tokens,
                estimated_output_cost=(output_tokens / 1000) * self.capabilities.est_cost_per_1k_output_tokens,
            )
            
            return True, model_response
        
        except Exception as e:
            error_response = ModelResponse(
                content=f"Error: {str(e)}",
                input_tokens=0,
                output_tokens=0,
                stop_reason="error",
            )
            return False, error_response
    
    def get_capabilities(self) -> ModelCapabilities:
        return self.capabilities


class MockProvider(ModelProvider):
    """Mock provider for testing without real API calls"""
    
    def __init__(
        self,
        response: Optional[str] = None,
        max_retries: Optional[int] = None,
        base_wait_seconds: Optional[float] = None,
    ):
        """
        Initialize mock provider.
        
        Args:
            response: Fixed response to return (for testing)
            max_retries: Maximum retries for call_with_retry
            base_wait_seconds: Base wait time for exponential backoff
        """
        super().__init__(max_retries=max_retries, base_wait_seconds=base_wait_seconds)
        self.response = response or "Mock response"
        self.call_count = 0
        self.capabilities = ModelCapabilities(
            supports_prompt_caching=True,
            max_context_tokens=4096,
            supports_structured_output=True,
            est_cost_per_1k_input_tokens=0.0,
            est_cost_per_1k_output_tokens=0.0,
            latency_class="instant",
            reliability="high",
        )
    
    def call(
        self,
        prompt: str,
        budget_tokens: int,
        temperature: float = 0.7,
    ) -> Tuple[bool, ModelResponse]:
        """Return mock response"""
        self.call_count += 1
        return True, ModelResponse(
            content=self.response,
            input_tokens=len(prompt.split()) * 1.3,  # Rough estimate
            output_tokens=len(self.response.split()) * 1.3,
            stop_reason="end_turn",
            estimated_input_cost=0.0,
            estimated_output_cost=0.0,
        )
    
    def get_capabilities(self) -> ModelCapabilities:
        return self.capabilities


def get_provider(role: str) -> ModelProvider:
    """
    Create a provider for the specified role based on environment configuration.
    
    Args:
        role: Either 'semantic' or 'structural'
        
    Returns:
        Configured ModelProvider instance
        
    Raises:
        ValueError: If provider type is invalid or required env vars are missing
    """
    import os
    
    # Get provider type from environment
    env_var = f"{role.upper()}_PROVIDER"
    provider_type = os.getenv(env_var)
    
    if not provider_type:
        raise ValueError(f"Missing required environment variable: {env_var}")
    
    provider_type = provider_type.lower()
    
    # Validate provider type
    valid_providers = {"groq", "openai", "anthropic", "openrouter"}
    if provider_type not in valid_providers:
        raise ValueError(f"Invalid provider '{provider_type}' for {env_var}. Must be one of: {', '.join(valid_providers)}")
    
    # Create provider instance
    if provider_type == "groq":
        api_key = os.getenv("GROQ_API_KEY")
        if not api_key:
            raise ValueError("GROQ_API_KEY environment variable is required when using groq provider")
        return GroqProvider()
    elif provider_type == "openai":
        api_key = os.getenv("OPENAI_API_KEY")
        if not api_key:
            raise ValueError("OPENAI_API_KEY environment variable is required when using openai provider")
        return OpenAIProvider()
    elif provider_type == "anthropic":
        api_key = os.getenv("ANTHROPIC_API_KEY")
        if not api_key:
            raise ValueError("ANTHROPIC_API_KEY environment variable is required when using anthropic provider")
        return AnthropicProvider()
    elif provider_type == "openrouter":
        api_key = os.getenv("OPENROUTER_API_KEY")
        if not api_key:
            raise ValueError("OPENROUTER_API_KEY environment variable is required when using openrouter provider")
        return OpenRouterProvider()


class ModelProviderFactory:
    """Factory to create model providers based on configuration"""
    
    _providers = {
        "groq": GroqProvider,
        "openai": OpenAIProvider,
        "anthropic": AnthropicProvider,
        "openrouter": OpenRouterProvider,
    }
    
    @classmethod
    def create(
        cls,
        provider_type: str,
        api_key: Optional[str] = None,
        model: Optional[str] = None,
    ) -> ModelProvider:
        """
        Create a model provider.
        
        Args:
            provider_type: Type of provider ("groq", "openai", "anthropic", "openrouter")
            api_key: API key (optional, uses env var if not provided)
            model: Model name (optional, uses default if not provided)
            
        Returns:
            ModelProvider instance
            
        Raises:
            ValueError: If provider type not recognized or API key missing
        """
        provider_type = provider_type.lower()
        
        if provider_type not in cls._providers:
            raise ValueError(
                f"Unknown provider type: {provider_type}. "
                f"Available: {', '.join(cls._providers.keys())}"
            )
        
        provider_class = cls._providers[provider_type]
        
        # For other providers, pass api_key and model if provided
        kwargs = {}
        if api_key:
            kwargs["api_key"] = api_key
        if model:
            kwargs["model"] = model
        
        return provider_class(**kwargs)
    
    @classmethod
    def register_provider(cls, name: str, provider_class) -> None:
        """Register a custom model provider"""
        cls._providers[name.lower()] = provider_class
    
    @classmethod
    def create_from_env(
        cls,
        provider_type_env: str = "SEMANTIC_PROVIDER",
    ) -> ModelProvider:
        """
        Create provider from environment variables.
        
        Args:
            provider_type_env: Env var name for provider type (SEMANTIC_PROVIDER or STRUCTURAL_PROVIDER)
            
        Returns:
            ModelProvider instance
            
        Raises:
            ValueError: If provider not configured or API key missing
        """
        provider_type = os.getenv(provider_type_env)
        
        if not provider_type:
            raise ValueError(
                f"{provider_type_env} not set in environment. "
                f"Please set it to one of: {', '.join(ModelProviderFactory._providers.keys())}"
            )
        
        return cls.create(provider_type)
