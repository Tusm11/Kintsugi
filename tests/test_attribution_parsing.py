"""Parsing of model replies in the Attribution Engine (the filler-evidence bug fix)."""

from unittest.mock import Mock

from src.attribution import AttributionEngine, parse_attribution_response, parse_evidence_lines
from src.models import Run
from src.sandbox import RepoSandbox


class TestEvidenceParsing:
    def test_none_reply_is_empty(self):
        assert parse_evidence_lines("NONE") == []
        assert parse_evidence_lines("None.") == []

    def test_filler_is_not_evidence(self):
        for filler in ["No contradicting evidence found", "No evidence found.", "There is no evidence",
                       "Nothing in the diff or logs contradicts this cause.", "None of the above", "N/A"]:
            assert parse_evidence_lines(filler) == [], filler

    def test_headings_bullets_and_blanks_are_dropped(self):
        reply = "**Evidence:**\n\n1. The diff changes the return value\n- Logs show 41 != 42\n## Notes\n"
        assert parse_evidence_lines(reply) == ["The diff changes the return value", "Logs show 41 != 42"]

    def test_substantive_no_evidence_line_is_kept(self):
        line = "No evidence that the loop bound changed in this commit"
        assert parse_evidence_lines(line) == [line]


class TestAttributionResponse:
    def test_uses_root_cause_and_alternatives_sections(self):
        reply = (
            "ROOT_CAUSE: total() in calc.py now returns 41\n"
            "WHY_IT_FAILS: the test asserts 42\n"
            "ALTERNATIVES:\n- flaky test\n- wrong fixture\n"
            "CONFIDENCE: 0.9\n"
        )
        assert parse_attribution_response(reply) == [
            "total() in calc.py now returns 41", "flaky test", "wrong fixture",
        ]

    def test_unformatted_reply_falls_back_to_lines(self):
        assert parse_attribution_response("Removed return statement\nsecond") == ["Removed return statement", "second"]


class TestEngineUsesParsedEvidence:
    def _engine(self, reply):
        provider = Mock()
        provider.call_with_retry.return_value = (True, Mock(content=reply))
        return AttributionEngine(sandbox=RepoSandbox(repo_paths={}), model_provider=provider), provider

    def test_none_counter_evidence_is_empty_list(self):
        engine, _ = self._engine("NONE")
        evidence, source = engine._gather_evidence_against_cause("cause", "diff", "logs")
        assert (evidence, source) == ([], "model")

    def test_injected_provider_is_used(self):
        engine, provider = self._engine("ROOT_CAUSE: x in a.py")
        causes, source = engine._extract_suspected_causes("diff", "logs")
        assert provider.call_with_retry.called and causes == ["x in a.py"] and source == "model"

    def test_gate_can_pass_when_model_finds_no_counter_evidence(self):
        from src.guardrails import ConfidenceGate
        from src.models import Attribution, Step, StepType

        engine, _ = self._engine("NONE")
        against, _ = engine._gather_evidence_against_cause("cause", "diff", "logs")
        run = Run(repo="r", failing_commit="c")
        run.add_step(Step(type=StepType.ATTRIBUTION, attribution=Attribution(
            claimed_cause="cause", evidence_for=["x"], evidence_against=against,
            alternatives_considered=[], counterfactual_result="pass", attribution_source="model")))
        assert ConfidenceGate().is_eligible_for_auto_apply(run)[0] is True
