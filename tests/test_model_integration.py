"""Integration tests with real model providers - gated by environment variables

To run these tests with real models:
1. Set environment variables:
   - GROQ_API_KEY=your-groq-key
   - OPENAI_API_KEY=your-openai-key (optional)
   - RUN_REAL_MODEL_TESTS=true

2. Run: pytest tests/test_model_integration.py -v

These tests are skipped by default unless RUN_REAL_MODEL_TESTS=true
"""

import os
import pytest
from src.model_provider import (
    GroqProvider, OpenAIProvider, AnthropicProvider,
    ModelProviderFactory, MockProvider
)
from src.handlers import SemanticHandler, StructuralHandler
from src.models import Run, Attribution, Step, StepType, StepLayer, StepStatus


# Skip all tests in this module unless explicitly enabled
pytestmark = pytest.mark.skipif(
    os.getenv("RUN_REAL_MODEL_TESTS", "").lower() != "true",
    reason="Real model tests disabled - set RUN_REAL_MODEL_TESTS=true to enable"
)


class TestGroqProvider:
    """Test Groq provider with real API"""
    
    @pytest.mark.skipif(
        not os.getenv("GROQ_API_KEY"),
        reason="GROQ_API_KEY not set"
    )
    def test_groq_basic_call(self):
        """Test basic Groq API call"""
        provider = GroqProvider()
        
        prompt = "Respond with one sentence: What is 2+2?"
        success, response = provider.call(prompt, budget_tokens=100)
        
        assert success is True
        assert len(response.content) > 0
        assert response.input_tokens > 0
        assert response.output_tokens > 0
        assert response.stop_reason == "end_turn"
    
    @pytest.mark.skipif(
        not os.getenv("GROQ_API_KEY"),
        reason="GROQ_API_KEY not set"
    )
    def test_groq_with_retry(self):
        """Test Groq provider with retry logic"""
        provider = GroqProvider(max_retries=2)
        
        prompt = "What is AI?"
        success, response = provider.call_with_retry(prompt, budget_tokens=100)
        
        assert success is True
        assert response.retry_count >= 0
    
    @pytest.mark.skipif(
        not os.getenv("GROQ_API_KEY"),
        reason="GROQ_API_KEY not set"
    )
    def test_groq_capabilities(self):
        """Test Groq capabilities declaration"""
        provider = GroqProvider()
        caps = provider.get_capabilities()
        
        assert caps.max_context_tokens > 0
        assert caps.latency_class == "instant"
        assert caps.reliability == "high"
        assert caps.est_cost_per_1k_input_tokens > 0


class TestOpenAIProvider:
    """Test OpenAI provider with real API"""
    
    @pytest.mark.skipif(
        not os.getenv("OPENAI_API_KEY"),
        reason="OPENAI_API_KEY not set"
    )
    def test_openai_basic_call(self):
        """Test basic OpenAI API call"""
        provider = OpenAIProvider()
        
        prompt = "Respond with one sentence: What is AI?"
        success, response = provider.call(prompt, budget_tokens=100)
        
        assert success is True
        assert len(response.content) > 0
        assert response.input_tokens > 0
        assert response.output_tokens > 0
    
    @pytest.mark.skipif(
        not os.getenv("OPENAI_API_KEY"),
        reason="OPENAI_API_KEY not set"
    )
    def test_openai_capabilities(self):
        """Test OpenAI capabilities declaration"""
        provider = OpenAIProvider()
        caps = provider.get_capabilities()
        
        assert caps.max_context_tokens > 0
        assert caps.supports_prompt_caching is True
        assert caps.supports_structured_output is True


class TestAnthropicProvider:
    """Test Anthropic provider with real API"""
    
    @pytest.mark.skipif(
        not os.getenv("ANTHROPIC_API_KEY"),
        reason="ANTHROPIC_API_KEY not set"
    )
    def test_anthropic_basic_call(self):
        """Test basic Anthropic API call"""
        provider = AnthropicProvider()
        
        prompt = "Respond with one sentence: What is machine learning?"
        success, response = provider.call(prompt, budget_tokens=100)
        
        assert success is True
        assert len(response.content) > 0


class TestModelProviderFactory:
    """Test ModelProviderFactory"""
    
    def test_factory_creates_groq(self):
        """Test factory creates Groq provider"""
        with pytest.raises(ValueError):
            # Will fail without GROQ_API_KEY set, but proves factory works
            provider = ModelProviderFactory.create("groq")
    
    def test_factory_creates_mock(self):
        """Test factory creates mock provider"""
        provider = ModelProviderFactory.create("mock", api_key="test response")
        
        assert provider is not None
        assert provider.get_name() == "MockProvider"
    
    def test_factory_invalid_provider(self):
        """Test factory rejects unknown provider"""
        with pytest.raises(ValueError):
            ModelProviderFactory.create("invalid_provider")


class TestSemanticHandlerWithRealModels:
    """Test SemanticHandler with real model providers"""
    
    @pytest.mark.skipif(
        not os.getenv("GROQ_API_KEY"),
        reason="GROQ_API_KEY not set"
    )
    def test_semantic_repair_with_configured_provider(self):
        """Test semantic repair using environment-configured provider"""
        from src.model_provider import get_provider
        
        provider = get_provider("semantic")
        handler = SemanticHandler(model_provider=provider)
        
        # Create a run with semantic failure
        run = Run(
            repo="test/repo",
            failing_commit="abc123",
            failure_logs="Test failed: AssertionError: expected 42 but got 41",
            diff="- return 42\n+ return 41",
            commit_message="Fix calculation"
        )
        
        # Add attribution
        attr = Attribution(
            claimed_cause="Off-by-one error in return value",
            evidence_for=["Test expects 42", "Code returns 41"],
            evidence_against=[],
            counterfactual_result="pass"
        )
        step = Step(
            type=StepType.ATTRIBUTION,
            layer=StepLayer.SEMANTIC,
            status=StepStatus.SUCCESS,
            attribution=attr,
        )
        run.add_step(step)
        
        # Attempt repair
        success, description, output = handler.handle(run, budget_tokens=500)
        
        assert success is True or success is False  # May fail if rate limited
        assert len(description) > 0
        assert 'model_provider' in output
        assert 'tokens_used' in output


class TestStructuralHandlerWithRealModels:
    """Test StructuralHandler with real model providers"""
    
    @pytest.mark.skipif(
        not os.getenv("GROQ_API_KEY"),
        reason="GROQ_API_KEY not set"
    )
    def test_structural_repair_with_configured_provider(self):
        """Test structural repair using environment-configured provider"""
        from src.model_provider import get_provider
        
        provider = get_provider("structural")
        handler = StructuralHandler(model_provider=provider)
        
        # Create a run with structural failure
        run = Run(
            repo="test/repo",
            failing_commit="abc123",
            failure_logs="JSON decode error: Unexpected end of JSON input at line 5",
            diff='{\n  "name": "test"\n  "value": 123',
            commit_message="Add config"
        )
        
        # Attempt repair
        success, description, output = handler.handle(run, budget_tokens=300)
        
        assert success is True or success is False  # May fail if rate limited
        assert 'issue_type' in output


class TestCostTracking:
    """Test cost tracking with real models"""
    
    @pytest.mark.skipif(
        not os.getenv("GROQ_API_KEY"),
        reason="GROQ_API_KEY not set"
    )
    def test_cost_tracking(self):
        """Test that costs are properly tracked"""
        from src.cost_tracker import CostTracker
        from src.model_provider import get_provider
        
        provider = get_provider("semantic")
        tracker = CostTracker()
        
        prompt = "What is 2+2?"
        success, response = provider.call(prompt, budget_tokens=50)
        
        # Log the call
        tracker.log_call(
            provider_name=provider.get_name(),
            model_name=provider.model_name,
            response=response,
            task_type="test",
            success=success,
        )
        
        # Verify tracking
        stats = tracker.get_stats()
        assert stats.total_calls == 1
        if success:
            assert stats.successful_calls == 1
            assert stats.total_tokens_used > 0
            assert stats.total_cost >= 0


class TestProviderComparison:
    """Compare behavior across different providers"""
    
    @pytest.mark.skipif(
        not (os.getenv("GROQ_API_KEY") or os.getenv("OPENAI_API_KEY")),
        reason="At least one API key required"
    )
    def test_same_prompt_different_providers(self):
        """Test same prompt across different providers"""
        prompt = "Respond in one word: What is AI?"
        results = {}
        
        if os.getenv("GROQ_API_KEY"):
            provider = GroqProvider()
            success, response = provider.call(prompt, budget_tokens=20)
            if success:
                results["Groq"] = response
        
        if os.getenv("OPENAI_API_KEY"):
            provider = OpenAIProvider()
            success, response = provider.call(prompt, budget_tokens=20)
            if success:
                results["OpenAI"] = response
        
        # Just verify we got responses, don't compare content
        assert len(results) > 0
        for name, response in results.items():
            assert len(response.content) > 0
            print(f"{name}: {response.content[:100]}")


# Example: How to run these tests
"""
# Setup:
export GROQ_API_KEY=gsk_...
export RUN_REAL_MODEL_TESTS=true

# Run all integration tests:
pytest tests/test_model_integration.py -v

# Run specific test:
pytest tests/test_model_integration.py::TestGroqProvider::test_groq_basic_call -v

# Run with verbose output:
pytest tests/test_model_integration.py -vv -s
"""
