# Tests for pipeline AI agent parameter adjustment (ticket 04).
# Plain assert style.

import sys
import tempfile
import shutil
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import numpy as np

from pipeline.agent import ParameterAgent
from pipeline.feedback import FeedbackStore, SegmentFeedback
from pipeline.jobs import PipelineService
from pipeline.models import PipelineJobStatus, SegmentStatus
from pipeline.quality import QualityVerificationResult


def _make_passing_quality_verifier():
    """Return a quality verifier that always passes, for agent tests."""

    class PassingQualityVerifier:
        def verify(self, **kwargs):
            return QualityVerificationResult(
                passed=True,
                overall_score=1.0,
                failure_reason=None,
                layer_results={},
            )

    return PassingQualityVerifier()


def _failed_quality(failure_reason: str) -> QualityVerificationResult:
    """Build a failed quality result with the given failure reason."""
    return QualityVerificationResult(
        passed=False,
        overall_score=0.0,
        failure_reason=failure_reason,
        layer_results={},
    )


def test_agent_lowers_temperature_on_asr_mismatch():
    agent = ParameterAgent()
    decision = agent.decide(
        base_params={"temperature": 0.8, "cfg_weight": 0.5, "exaggeration": 0.5, "seed": 0},
        quality_result=_failed_quality("asr_mismatch"),
    )
    assert decision.gen_params["temperature"] < 0.8
    assert decision.gen_params["cfg_weight"] > 0.5
    assert decision.gen_params["seed"] != 0
    assert "asr_mismatch" in decision.reason


def test_agent_lowers_exaggeration_on_silence():
    agent = ParameterAgent()
    decision = agent.decide(
        base_params={"temperature": 0.8, "cfg_weight": 0.5, "exaggeration": 0.5, "seed": 0},
        quality_result=_failed_quality("long_silence"),
    )
    assert decision.gen_params["exaggeration"] < 0.5
    assert decision.gen_params["seed"] != 0


def test_agent_lowers_exaggeration_on_clipping():
    agent = ParameterAgent()
    decision = agent.decide(
        base_params={"temperature": 0.8, "cfg_weight": 0.5, "exaggeration": 0.5, "seed": 0},
        quality_result=_failed_quality("clipping"),
    )
    assert decision.gen_params["exaggeration"] < 0.5


def test_agent_respects_parameter_bounds():
    agent = ParameterAgent()
    # Temperature already low, should not go below 0.1
    decision = agent.decide(
        base_params={"temperature": 0.1, "cfg_weight": 1.0, "exaggeration": 0.25, "seed": 0},
        quality_result=_failed_quality("asr_mismatch"),
    )
    assert decision.gen_params["temperature"] >= 0.1
    assert decision.gen_params["cfg_weight"] <= 1.0
    assert decision.gen_params["exaggeration"] >= 0.25


def _make_fake_synthesize_that_fails_until_agent(fail_until_attempt, sample_rate=24000):
    calls = []

    def fake_synthesize(text, audio_prompt_path=None, **kwargs):
        calls.append(kwargs)
        if len(calls) - 1 < fail_until_attempt:
            return None, None
        words = max(1, len(text.split()))
        duration = words / 3.0
        samples = np.full(int(duration * sample_rate), 0.3, dtype=np.float32)
        return samples, sample_rate

    return fake_synthesize, calls


def test_agent_recovery_after_base_retries():
    tmp = tempfile.mkdtemp()
    try:
        # All 4 base attempts fail, agent-adjusted attempt succeeds.
        fake, calls = _make_fake_synthesize_that_fails_until_agent(fail_until_attempt=4)
        service = PipelineService(
            base_dir=Path(tmp),
            synthesize_fn=fake,
            quality_verifier=_make_passing_quality_verifier(),
        )
        job_id = service.submit_job(
            text="This sentence is long enough to be its own segment.",
            voice_config={"mode": "predefined", "voice_id": "test.wav"},
            gen_params={"temperature": 0.8, "cfg_weight": 0.5, "exaggeration": 0.5, "seed": 0, "language": "en"},
        )
        service.run_job_sync(job_id, max_segment_duration=50.0, pause_ms=100)
        job = service.get_job(job_id)
        assert job.status == PipelineJobStatus.DONE, job.status
        assert job.segments[0].status == SegmentStatus.PASSED
        # At least 5 calls: 4 base attempts + 1 agent attempt.
        assert len(calls) >= 5, calls
        # The last call should use agent-adjusted params.
        assert calls[-1]["temperature"] != calls[0]["temperature"] or calls[-1]["seed"] != calls[0]["seed"]
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_agent_records_decision_in_log():
    tmp = tempfile.mkdtemp()
    try:
        fake, calls = _make_fake_synthesize_that_fails_until_agent(fail_until_attempt=4)
        service = PipelineService(
            base_dir=Path(tmp),
            synthesize_fn=fake,
            quality_verifier=_make_passing_quality_verifier(),
        )
        job_id = service.submit_job(
            text="This sentence is long enough to be its own segment.",
            voice_config={"mode": "predefined", "voice_id": "test.wav"},
            gen_params={"temperature": 0.8, "cfg_weight": 0.5, "exaggeration": 0.5, "seed": 0, "language": "en"},
        )
        service.run_job_sync(job_id, max_segment_duration=50.0, pause_ms=100)
        job = service.get_job(job_id)
        # Find an agent-adjusted attempt in the log.
        agent_logs = [log for log in job.segments[0].verification_log if log.get("agent_decision")]
        assert len(agent_logs) >= 1
        assert "reason" in agent_logs[0]["agent_decision"]
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    test_agent_lowers_temperature_on_asr_mismatch()
    test_agent_lowers_exaggeration_on_silence()
    test_agent_lowers_exaggeration_on_clipping()
    test_agent_respects_parameter_bounds()
    test_agent_recovery_after_base_retries()
    test_agent_records_decision_in_log()
    print("ALL AGENT TESTS PASSED")



def test_agent_lowers_temperature_on_whisperx_low_confidence():
    agent = ParameterAgent()
    decision = agent.decide(
        base_params={"temperature": 0.8, "cfg_weight": 0.5, "exaggeration": 0.5, "seed": 0},
        quality_result=_failed_quality("whisperx_low_confidence"),
    )
    assert decision.gen_params["temperature"] < 0.8
    assert decision.gen_params["cfg_weight"] > 0.5
    assert "whisperx_low_confidence" in decision.reason


def test_agent_lowers_temperature_on_wer_too_high():
    agent = ParameterAgent()
    decision = agent.decide(
        base_params={"temperature": 0.8, "cfg_weight": 0.5, "exaggeration": 0.5, "seed": 0},
        quality_result=_failed_quality("wer_too_high"),
    )
    assert decision.gen_params["temperature"] < 0.8
    assert decision.gen_params["cfg_weight"] > 0.5
    assert "wer_too_high" in decision.reason


def test_agent_adjusts_for_low_speaker_similarity():
    agent = ParameterAgent()
    layer_results = {
        "resemblyzer_speaker": {
            "metrics": {"cosine_similarity": 0.5},
        },
    }
    decision = agent.decide(
        base_params={"temperature": 0.8, "cfg_weight": 0.5, "exaggeration": 0.5, "seed": 0},
        quality_result=None,
        layer_results=layer_results,
    )
    assert decision.gen_params["temperature"] < 0.8
    assert decision.gen_params["cfg_weight"] > 0.5
    assert decision.gen_params["seed"] != 0
    assert "speaker_similarity_low" in decision.reason


def test_agent_adjusts_for_spectral_drift():
    agent = ParameterAgent()
    layer_results = {
        "librosa_spectral": {
            "metrics": {"spectral_contrast_ratio": 0.5, "mfcc_mse": 0.1},
        },
    }
    decision = agent.decide(
        base_params={"temperature": 0.8, "cfg_weight": 0.5, "exaggeration": 0.5, "seed": 0},
        quality_result=None,
        layer_results=layer_results,
    )
    assert decision.gen_params["exaggeration"] < 0.5
    assert decision.gen_params["seed"] != 0
    assert "spectral_drift" in decision.reason


def test_feedback_metrics_average_for_same_params():
    tmp = tempfile.mkdtemp()
    try:
        store = FeedbackStore(Path(tmp) / "feedback.jsonl")
        params = {"temperature": 0.7, "cfg_weight": 0.5, "exaggeration": 0.6, "seed": 888}
        store.append_metrics(
            job_id="j1",
            segment_index=0,
            layer_results={
                "resemblyzer_speaker": {"metrics": {"cosine_similarity": 0.6}},
                "librosa_spectral": {"metrics": {"spectral_contrast_ratio": 0.7, "mfcc_mse": 0.08}},
            },
            gen_params=params,
        )
        store.append_metrics(
            job_id="j1",
            segment_index=1,
            layer_results={
                "resemblyzer_speaker": {"metrics": {"cosine_similarity": 0.8}},
                "librosa_spectral": {"metrics": {"spectral_contrast_ratio": 0.9, "mfcc_mse": 0.02}},
            },
            gen_params=params,
        )

        agent = ParameterAgent(feedback_store=store)
        avg_sim, avg_contrast, avg_mfcc, count = agent._feedback_metrics_for_params(params)
        assert count == 2
        assert avg_sim == 0.7
        assert avg_contrast == 0.8
        assert avg_mfcc == 0.05
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_agent_never_changes_speed_on_duration_deviation():
    agent = ParameterAgent()
    decision = agent.decide(
        base_params={"temperature": 0.7, "cfg_weight": 0.5, "exaggeration": 0.6, "seed": 888, "speed_factor": 1.0},
        quality_result=_failed_quality("duration_deviation"),
        audio_metrics={"actual_duration": 20.0, "expected_duration": 10.0},
    )
    assert decision.gen_params["speed_factor"] == 1.0
    assert decision.gen_params["seed"] != 888
    assert "duration_deviation" in decision.reason


def test_agent_pins_speed_factor_back_to_one():
    agent = ParameterAgent()
    decision = agent.decide(
        base_params={"temperature": 0.7, "cfg_weight": 0.5, "exaggeration": 0.6, "seed": 888, "speed_factor": 1.3},
        quality_result=_failed_quality("long_silence"),
    )
    assert decision.gen_params["speed_factor"] == 1.0
    assert "pin speed_factor" in decision.reason
