# Pipeline job orchestration: submit, run, compose.

import logging
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

import numpy as np
import soundfile as sf

from config import (
    get_audio_output_format,
    get_audio_sample_rate,
    get_output_path,
    get_pipeline_max_retry_count,
    get_pipeline_verification_thresholds,
)
from pipeline.agent import AgentDecision, ParameterAgent
from pipeline.composer import compose_segments, loudnorm_final_audio
from pipeline.feedback import FeedbackStore
from pipeline.generator import generate_segment_audio
from pipeline.models import (
    AttemptResult,
    PipelineJob,
    PipelineJobStatus,
    PipelineJobSummary,
    Segment,
    SegmentStatus,
)
from pipeline.quality import PipelineQualityVerifier, QualityVerificationResult
from pipeline.segmenter import estimate_segment_duration, split_text_into_segments
from pipeline.store import JobStore

logger = logging.getLogger(__name__)

SynthesizeFn = Callable[..., Tuple[Optional[np.ndarray], Optional[int]]]


class PipelineService:
    """High-level interface for creating and running pipeline jobs."""

    def __init__(
        self,
        base_dir: Optional[Path] = None,
        synthesize_fn: Optional[SynthesizeFn] = None,
        parameter_agent: Optional[ParameterAgent] = None,
        quality_verifier: Optional[PipelineQualityVerifier] = None,
    ):
        self.base_dir = (
            base_dir
            if base_dir is not None
            else get_output_path(ensure_absolute=True) / "pipeline_jobs"
        )
        self.store = JobStore(self.base_dir)
        # Default to the real engine.synthesize; tests inject a mock.
        self.synthesize_fn = synthesize_fn
        # Multi-layer quality verifier is lazily initialized unless injected.
        self._quality_verifier = quality_verifier
        # Rule-based parameter agent.
        self._parameter_agent = parameter_agent
        # Feedback store for the AI agent.
        self.feedback_store = FeedbackStore(self.base_dir / "feedback.jsonl")

    def _get_quality_verifier(self) -> PipelineQualityVerifier:
        if self._quality_verifier is None:
            self._quality_verifier = PipelineQualityVerifier.from_config()
        return self._quality_verifier

    def _get_parameter_agent(self) -> ParameterAgent:
        if self._parameter_agent is None:
            self._parameter_agent = ParameterAgent(feedback_store=self.feedback_store)
        return self._parameter_agent

    def submit_job(
        self,
        text: str,
        voice_config: Dict[str, Any],
        gen_params: Dict[str, Any],
        pipeline_config: Optional[Dict[str, Any]] = None,
    ) -> str:
        """Create a new pipeline job and persist it."""
        job_id = f"pipe-{uuid.uuid4().hex[:12]}"
        now = time.time()
        job = PipelineJob(
            job_id=job_id,
            text=text,
            voice_config=voice_config,
            gen_params=gen_params,
            status=PipelineJobStatus.PENDING,
            segments=[],
            created_at=now,
            updated_at=now,
            pipeline_config=pipeline_config or {},
        )
        self.store.save(job)
        logger.info(f"Created pipeline job {job_id}")
        return job_id

    def get_job(self, job_id: str) -> Optional[PipelineJob]:
        return self.store.load(job_id)

    def list_jobs(self) -> List[PipelineJobSummary]:
        return self.store.list_jobs()

    def run_job_sync(
        self,
        job_id: str,
        max_segment_duration: float = 50.0,
        pause_ms: float = 150.0,
    ) -> Optional[PipelineJob]:
        """
        Synchronously run a pipeline job from pending to done/failed.

        This blocks the calling thread; the API should call it inside a
        background task or executor.
        """
        job = self.get_job(job_id)
        if job is None:
            logger.error(f"Pipeline job {job_id} not found")
            return None
        if job.status not in (PipelineJobStatus.PENDING, PipelineJobStatus.RUNNING):
            logger.warning(f"Pipeline job {job_id} is not runnable (status={job.status})")
            return job

        synthesize_fn = self.synthesize_fn
        if synthesize_fn is None:
            import engine

            synthesize_fn = engine.synthesize

        job.status = PipelineJobStatus.RUNNING
        self.store.save(job)

        try:
            language = job.gen_params.get("language", "en")
            segments = split_text_into_segments(
                job.text, max_duration=max_segment_duration, language=language
            )
            logger.info(f"Job {job_id}: split into {len(segments)} segment(s)")

            audio_prompt_path = self._resolve_audio_prompt_path(job.voice_config)
            segment_files: List[Path] = []

            for idx, segment_text in enumerate(segments):
                seg_record = Segment(
                    index=idx,
                    text=segment_text,
                    status=SegmentStatus.PENDING,
                    gen_params=dict(job.gen_params),
                )
                job.segments.append(seg_record)
                job.status = PipelineJobStatus.RUNNING
                self.store.save(job)

                expected_duration = estimate_segment_duration(segment_text, language)
                seg_path = self._generate_and_verify_segment(
                    job=job,
                    seg_record=seg_record,
                    audio_prompt_path=audio_prompt_path,
                    expected_duration=expected_duration,
                    synthesize_fn=synthesize_fn,
                    language=language,
                )
                if seg_path is not None:
                    segment_files.append(seg_path)

            if not segment_files:
                job.status = PipelineJobStatus.FAILED
                self.store.save(job)
                return job

            final_format = job.pipeline_config.get(
                "output_format", get_audio_output_format()
            )
            target_sr = job.pipeline_config.get(
                "sample_rate", get_audio_sample_rate()
            )
            final_dir = self.base_dir / job_id
            final_path = final_dir / f"final.{final_format}"

            # Read loudnorm targets from the audio_metrics layer config.
            verification_cfg = get_pipeline_verification_thresholds()
            audio_metrics_cfg = verification_cfg.get("layers", {}).get("audio_metrics", {})
            audio_metrics_thresholds = audio_metrics_cfg.get("thresholds", {})
            target_lufs = float(audio_metrics_thresholds.get("target_lufs", -16.0))
            true_peak = float(audio_metrics_thresholds.get("true_peak_max_dbtp", -1.5))

            temp_wav = final_dir / "final_temp.wav"
            normalized_wav = final_dir / "final_normalized.wav"
            ok = compose_segments(segment_files, temp_wav, sr=target_sr, pause_ms=pause_ms)
            if ok:
                # Final loudnorm ensures consistent loudness across the whole long-form output.
                ok = loudnorm_final_audio(
                    temp_wav,
                    normalized_wav,
                    target_lufs=target_lufs,
                    true_peak=true_peak,
                    sample_rate=target_sr,
                )
                if ok:
                    if final_format == "wav":
                        try:
                            normalized_wav.replace(final_path)
                        except OSError:
                            # Fallback: copy if atomic replace is unavailable.
                            import shutil
                            shutil.move(str(normalized_wav), str(final_path))
                    else:
                        ok = self._encode_to_format(
                            normalized_wav, final_path, final_format, target_sr
                        )

            # Best-effort cleanup of intermediate composed files.
            try:
                temp_wav.unlink(missing_ok=True)
            except Exception:
                pass
            try:
                normalized_wav.unlink(missing_ok=True)
            except Exception:
                pass

            if not ok:
                job.status = PipelineJobStatus.FAILED
                self.store.save(job)
                return job

            job.final_audio_path = str(final_path)
            job.status = PipelineJobStatus.DONE
            self.store.save(job)
            logger.info(f"Job {job_id} completed: {final_path}")
            return job

        except Exception as e:
            logger.error(f"Pipeline job {job_id} failed: {e}", exc_info=True)
            job.status = PipelineJobStatus.FAILED
            self.store.save(job)
            return job

    def _generate_and_verify_segment(
        self,
        job: PipelineJob,
        seg_record: Segment,
        audio_prompt_path: Optional[str],
        expected_duration: float,
        synthesize_fn: SynthesizeFn,
        language: str = "en",
    ) -> Optional[Path]:
        """
        Generate one segment, retry on failure, verify quality, return path.

        Phase 1: base retries with original parameters.
        Phase 2: agent-adjusted parameters after base retries are exhausted.

        Returns the segment WAV path if it passes, None if it ultimately fails.
        """
        job_id = job.job_id
        seg_dir = self.base_dir / job_id / "segments"
        seg_dir.mkdir(parents=True, exist_ok=True)
        seg_path = seg_dir / f"{seg_record.index}.wav"

        max_retries = job.pipeline_config.get(
            "max_retry_count", get_pipeline_max_retry_count()
        )
        quality_verifier = self._get_quality_verifier()
        agent = self._get_parameter_agent()

        # Track the last attempt to feed the agent in Phase 2.
        last_attempt: Optional[AttemptResult] = None

        # Phase 1: base retries.
        for attempt in range(max_retries + 1):
            result = self._attempt_segment(
                job=job,
                seg_record=seg_record,
                audio_prompt_path=audio_prompt_path,
                expected_duration=expected_duration,
                synthesize_fn=synthesize_fn,
                language=language,
                seg_path=seg_path,
                quality_verifier=quality_verifier,
                attempt=attempt,
                agent_decision=None,
            )
            if result.passed:
                return seg_path
            last_attempt = result

        # Phase 2: agent-adjusted attempt.
        last_quality = last_attempt.quality_result if last_attempt else None
        decision = agent.decide(
            base_params=seg_record.gen_params,
            quality_result=last_quality,
            audio_metrics=last_attempt.audio_metrics if last_attempt else None,
            layer_results=last_quality.layer_results if last_quality else None,
        )
        seg_record.gen_params = decision.gen_params
        result = self._attempt_segment(
            job=job,
            seg_record=seg_record,
            audio_prompt_path=audio_prompt_path,
            expected_duration=expected_duration,
            synthesize_fn=synthesize_fn,
            language=language,
            seg_path=seg_path,
            quality_verifier=quality_verifier,
            attempt=max_retries + 1,
            agent_decision=decision,
        )
        if result.passed:
            return seg_path

        seg_record.status = SegmentStatus.FAILED
        self.store.save(job)
        logger.error(
            f"Job {job_id} segment {seg_record.index} failed after base retries and agent adjustment"
        )
        return None

    def _attempt_segment(
        self,
        job: PipelineJob,
        seg_record: Segment,
        audio_prompt_path: Optional[str],
        expected_duration: float,
        synthesize_fn: SynthesizeFn,
        language: str,
        seg_path: Path,
        quality_verifier: PipelineQualityVerifier,
        attempt: int,
        agent_decision: Optional[AgentDecision],
    ) -> AttemptResult:
        """
        Single generation + verification attempt.

        Returns an AttemptResult with the pass flag, the aggregated quality
        result, and the basic audio metrics for agent feedback.
        """
        job_id = job.job_id
        seg_record.status = SegmentStatus.GENERATING
        self.store.save(job)

        audio_np, sr = generate_segment_audio(
            text=seg_record.text,
            audio_prompt_path=audio_prompt_path,
            gen_params=seg_record.gen_params,
            synthesize_fn=synthesize_fn,
        )

        if audio_np is None or sr is None:
            log_entry: Dict[str, Any] = {
                "attempt": attempt,
                "passed": False,
                "failure_reason": "generation returned no audio",
                "metrics": {},
            }
            if agent_decision:
                log_entry["agent_decision"] = {
                    "reason": agent_decision.reason,
                    "deltas": agent_decision.deltas,
                    "gen_params": agent_decision.gen_params,
                }
            seg_record.verification_log.append(log_entry)
            seg_record.retry_count = attempt
            self.store.save(job)
            empty_quality = QualityVerificationResult(
                passed=False,
                overall_score=0.0,
                failure_reason="generation returned no audio",
                layer_results={},
            )
            return AttemptResult(passed=False, quality_result=empty_quality)

        sf.write(str(seg_path), audio_np, sr, subtype="pcm_16")

        # Run the multi-layer quality verifier (basic audio + audio metrics + content).
        # The audio prompt doubles as the reference voice for speaker-similarity layers.
        quality_result = quality_verifier.verify(
            audio_path=str(seg_path),
            original_text=seg_record.text,
            reference_voice_path=audio_prompt_path,
            language=language,
            expected_duration=expected_duration,
        )

        log_entry: Dict[str, Any] = {
            "attempt": attempt,
            "passed": quality_result.passed,
            "score": quality_result.overall_score,
            "quality": {
                "passed": quality_result.passed,
                "overall_score": quality_result.overall_score,
                "failure_reason": quality_result.failure_reason,
                "layer_results": quality_result.layer_results,
            },
        }
        if agent_decision:
            log_entry["agent_decision"] = {
                "reason": agent_decision.reason,
                "deltas": agent_decision.deltas,
                "gen_params": agent_decision.gen_params,
            }

        seg_record.verification_log.append(log_entry)

        # Persist per-layer metrics to the feedback store for the agent loop.
        try:
            self.feedback_store.append_metrics(
                job_id=job.job_id,
                segment_index=seg_record.index,
                layer_results=quality_result.layer_results,
                gen_params=seg_record.gen_params,
            )
        except Exception as e:
            logger.warning(f"Failed to append metrics to feedback store: {e}")

        # Extract audio metrics from the basic audio layer for agent feedback.
        basic_layer = quality_result.layer_results.get("basic_audio", {})
        audio_metrics = basic_layer.get("metrics", {})

        if quality_result.passed:
            seg_record.status = SegmentStatus.PASSED
            seg_record.audio_path = str(seg_path)
            seg_record.audio_score = quality_result.overall_score
            seg_record.score = quality_result.overall_score
            seg_record.failure_reason = None
            seg_record.retry_count = attempt
            self.store.save(job)
            return AttemptResult(
                passed=True, quality_result=quality_result, audio_metrics=audio_metrics
            )

        seg_record.retry_count = attempt
        seg_record.failure_reason = quality_result.failure_reason
        self.store.save(job)
        logger.warning(
            f"Job {job_id} segment {seg_record.index} attempt {attempt} failed: "
            f"{quality_result.failure_reason}"
        )
        return AttemptResult(
            passed=False, quality_result=quality_result, audio_metrics=audio_metrics
        )

    def _resolve_audio_prompt_path(self, voice_config: Dict[str, Any]) -> Optional[str]:
        mode = voice_config.get("mode")
        if mode == "predefined":
            from config import get_predefined_voices_path
            from utils import safe_resolve_within

            voices_dir = get_predefined_voices_path(ensure_absolute=True)
            voice_id = voice_config.get("voice_id")
            if not voice_id:
                return None
            try:
                return str(safe_resolve_within(voices_dir, voice_id))
            except ValueError:
                return None
        elif mode == "clone":
            from config import get_reference_audio_path
            from utils import safe_resolve_within

            ref_dir = get_reference_audio_path(ensure_absolute=True)
            ref_filename = voice_config.get("reference_audio_filename")
            if not ref_filename:
                return None
            try:
                return str(safe_resolve_within(ref_dir, ref_filename))
            except ValueError:
                return None
        return None

    def _encode_to_format(
        self, wav_path: Path, output_path: Path, output_format: str, sr: int
    ) -> bool:
        try:
            data, _ = sf.read(str(wav_path), dtype="float32")
            from utils import encode_audio

            encoded = encode_audio(data, sample_rate=sr, output_format=output_format)
            if encoded is None:
                return False
            output_path.parent.mkdir(parents=True, exist_ok=True)
            with open(output_path, "wb") as f:
                f.write(encoded)
            return True
        except Exception as e:
            logger.error(f"Failed to encode final audio to {output_format}: {e}", exc_info=True)
            return False
