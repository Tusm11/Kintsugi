"""Test provider factory configuration"""

import os
import pytest
from src.model_provider import get_provider, GroqProvider, OpenAIProvider, AnthropicProvider, OpenRouterProvider


class TestProviderFactory:
    """Test environment-driven provider configuration"""
    
    def test_missing_provider_env_var_raises_error(self):
        """Test that missing SEMANTIC_PROVIDER raises clear error"""
        # Clear environment
        old_val = os.environ.pop("SEMANTIC_PROVIDER", None)
        
        try:
            with pytest.raises(ValueError, match="Missing required environment variable: SEMANTIC_PROVIDER"):
                get_provider("semantic")
        finally:
            if old_val:
                os.environ["SEMANTIC_PROVIDER"] = old_val
    
    def test_invalid_provider_type_raises_error(self):
        """Test that invalid provider type raises clear error"""
        os.environ["SEMANTIC_PROVIDER"] = "invalid_provider"
        
        try:
            with pytest.raises(ValueError, match="Invalid provider 'invalid_provider'"):
                get_provider("semantic")
        finally:
            os.environ.pop("SEMANTIC_PROVIDER", None)
    
    def test_missing_api_key_raises_error(self):
        """Test that missing API key raises clear error naming the variable"""
        os.environ["SEMANTIC_PROVIDER"] = "groq"
        old_key = os.environ.pop("GROQ_API_KEY", None)
        
        try:
            with pytest.raises(ValueError, match="GROQ_API_KEY environment variable is required"):
                get_provider("semantic")
        finally:
            os.environ.pop("SEMANTIC_PROVIDER", None)
            if old_key:
                os.environ["GROQ_API_KEY"] = old_key
    
    def test_provider_switching_returns_correct_class(self):
        """Test that switching provider actually returns different provider classes"""
        # Test groq
        os.environ["SEMANTIC_PROVIDER"] = "groq"
        os.environ["GROQ_API_KEY"] = "test_key"
        
        try:
            provider = get_provider("semantic")
            assert isinstance(provider, GroqProvider)
        finally:
            os.environ.pop("SEMANTIC_PROVIDER", None)
            os.environ.pop("GROQ_API_KEY", None)
        
        # Test openai 
        os.environ["SEMANTIC_PROVIDER"] = "openai"
        os.environ["OPENAI_API_KEY"] = "test_key"
        
        try:
            provider = get_provider("semantic")
            assert isinstance(provider, OpenAIProvider)
        finally:
            os.environ.pop("SEMANTIC_PROVIDER", None)
            os.environ.pop("OPENAI_API_KEY", None)
    
    def test_semantic_vs_structural_provider_independence(self):
        """Test that semantic and structural providers are configured independently"""
        os.environ["SEMANTIC_PROVIDER"] = "openai"
        os.environ["STRUCTURAL_PROVIDER"] = "groq"
        os.environ["OPENAI_API_KEY"] = "test_key"
        os.environ["GROQ_API_KEY"] = "test_key"
        
        try:
            semantic_provider = get_provider("semantic")
            structural_provider = get_provider("structural")
            
            assert isinstance(semantic_provider, OpenAIProvider)
            assert isinstance(structural_provider, GroqProvider)
        finally:
            for var in ["SEMANTIC_PROVIDER", "STRUCTURAL_PROVIDER", "OPENAI_API_KEY", "GROQ_API_KEY"]:
                os.environ.pop(var, None)
    
    def test_all_supported_providers(self):
        """Test that all provider types can be instantiated"""
        providers_config = [
            ("groq", "GROQ_API_KEY", GroqProvider),
            ("openai", "OPENAI_API_KEY", OpenAIProvider),
            ("anthropic", "ANTHROPIC_API_KEY", AnthropicProvider), 
            ("openrouter", "OPENROUTER_API_KEY", OpenRouterProvider),
        ]
        
        for provider_type, api_key_var, expected_class in providers_config:
            os.environ["SEMANTIC_PROVIDER"] = provider_type
            os.environ[api_key_var] = "test_key"
            
            try:
                provider = get_provider("semantic")
                assert isinstance(provider, expected_class), f"Expected {expected_class}, got {type(provider)}"
            finally:
                os.environ.pop("SEMANTIC_PROVIDER", None)
                os.environ.pop(api_key_var, None)