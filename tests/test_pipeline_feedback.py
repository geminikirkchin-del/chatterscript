# Tests for pipeline feedback store schema ownership.
# Plain assert style.

import json
import sys
import tempfile
import shutil
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from pipeline.feedback import FeedbackStore


def test_legacy_metrics_entry_with_params_key_is_readable():
    """Legacy entries (rating="metrics", extra["params"], no kind) still load."""
    tmp = tempfile.mkdtemp()
    try:
        path = Path(tmp) / "feedback.jsonl"
        legacy = {
            "job_id": "j1",
            "segment_index": 0,
            "rating": "metrics",
            "comment": None,
            "timestamp": 123.0,
            "extra": {
                "layer_results": {"resemblyzer_speaker": {"metrics": {"cosine_similarity": 0.6}}},
                "params": {"temperature": 0.7, "seed": 42},
            },
        }
        path.write_text(json.dumps(legacy) + "\n", encoding="utf-8")

        store = FeedbackStore(path)
        metrics = store.read_recent_metrics()
        assert len(metrics) == 1
        assert metrics[0].kind == "metrics"
        # Legacy "params" key is normalized to "gen_params" on read.
        assert metrics[0].extra["gen_params"] == {"temperature": 0.7, "seed": 42}
        assert "params" not in metrics[0].extra
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_append_user_feedback_round_trip():
    """append_user_feedback builds a kind="feedback" entry with gen_params."""
    tmp = tempfile.mkdtemp()
    try:
        store = FeedbackStore(Path(tmp) / "feedback.jsonl")
        ok = store.append_user_feedback(
            job_id="j1",
            segment_index=2,
            rating="reject",
            comment="sounds off",
            gen_params={"temperature": 0.9, "seed": 7},
            segment_text="hello world",
            score=0.55,
            failure_reason="clipping",
        )
        assert ok

        recent = store.read_recent()
        assert len(recent) == 1
        fb = recent[0]
        assert fb.kind == "feedback"
        assert fb.rating == "reject"
        assert fb.comment == "sounds off"
        assert fb.extra["gen_params"] == {"temperature": 0.9, "seed": 7}
        assert fb.extra["segment_text"] == "hello world"
        assert fb.extra["score"] == 0.55
        assert fb.extra["failure_reason"] == "clipping"
        # User feedback is not a metrics entry.
        assert store.read_recent_metrics() == []
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_append_metrics_sets_kind_and_stays_backward_compatible():
    """append_metrics entries carry kind="metrics" and legacy rating="metrics"."""
    tmp = tempfile.mkdtemp()
    try:
        store = FeedbackStore(Path(tmp) / "feedback.jsonl")
        ok = store.append_metrics(
            job_id="j1",
            segment_index=0,
            layer_results={"librosa_spectral": {"metrics": {"mfcc_mse": 0.03}}},
            gen_params={"temperature": 0.8},
        )
        assert ok

        # Raw JSONL keeps rating="metrics" for backward compatibility.
        raw = json.loads((Path(tmp) / "feedback.jsonl").read_text(encoding="utf-8").strip())
        assert raw["rating"] == "metrics"
        assert raw["kind"] == "metrics"

        metrics = store.read_recent_metrics()
        assert len(metrics) == 1
        assert metrics[0].kind == "metrics"
        assert metrics[0].extra["gen_params"] == {"temperature": 0.8}
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    test_legacy_metrics_entry_with_params_key_is_readable()
    test_append_user_feedback_round_trip()
    test_append_metrics_sets_kind_and_stays_backward_compatible()
    print("ALL FEEDBACK TESTS PASSED")
