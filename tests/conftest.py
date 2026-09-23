"""Shared pytest configuration and fixtures"""

import pytest
import os
from src.model_provider import ModelProviderFactory
from datetime import datetime


@pytest.fixture
def semantic_provider():
    """Provider for semantic repair tests - uses SEMANTIC_PROVIDER from .env"""
    try:
        return ModelProviderFactory.create_from_env("SEMANTIC_PROVIDER")
    except ValueError as e:
        pytest.skip(f"SEMANTIC_PROVIDER not configured: {e}")


@pytest.fixture
def structural_provider():
    """Provider for structural repair tests - uses STRUCTURAL_PROVIDER from .env"""
    try:
        return ModelProviderFactory.create_from_env("STRUCTURAL_PROVIDER")
    except ValueError as e:
        pytest.skip(f"STRUCTURAL_PROVIDER not configured: {e}")


@pytest.fixture
def sample_webhook_event():
    """Sample webhook event for integration tests"""
    from src.ingestion import WebhookEvent
    
    return WebhookEvent(
        source="github",
        repo="user/test-repo",
        commit="abc123def456",
        branch="main",
        build_id="ci-build-123",
        failure_logs="Test failure: AssertionError: expected 42 but got 41",
        diff="- return 42\n+ return 41",
        commit_message="Fix calculation",
        webhook_id="gh-delivery-001",
        timestamp=datetime.utcnow(),
        metadata={"pr": 456}
    )


@pytest.fixture
def sample_run():
    """Sample Run object for testing"""
    from src.models import Run, Attribution, Step, StepType, StepLayer, StepStatus
    
    run = Run(
        repo="user/test-repo",
        failing_commit="abc123",
        failure_logs="Test failed: assertion error",
        diff="",
        commit_message="Test commit"
    )
    
    # Add an attribution step
    attr = Attribution(
        claimed_cause="Off-by-one error",
        evidence_for=["Test expects 42", "Code returns 41"],
        evidence_against=[],
        alternatives_considered=[
            {"cause": "Type mismatch", "why_rejected": "Syntax is valid"}
        ],
        counterfactual_result="pass"
    )
    
    step = Step(
        type=StepType.ATTRIBUTION,
        layer=StepLayer.SEMANTIC,
        status=StepStatus.SUCCESS,
        attribution=attr,
    )
    
    run.add_step(step)
    return run


@pytest.fixture
def require_model_configuration():
    """
    Fixture that skips tests if model providers are not configured.
    Use this for integration tests that need real model providers.
    Tests MUST have SEMANTIC_PROVIDER and STRUCTURAL_PROVIDER set in .env
    """
    semantic = os.getenv("SEMANTIC_PROVIDER")
    structural = os.getenv("STRUCTURAL_PROVIDER")
    
    if not semantic or not structural:
        pytest.skip(
            "Model providers not configured. "
            "Set SEMANTIC_PROVIDER and STRUCTURAL_PROVIDER in .env file. "
            "Recommended: Use Groq for testing (GROQ_API_KEY required)"
        )
