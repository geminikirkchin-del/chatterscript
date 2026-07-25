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
)
from pipeline.agent import AgentDecision, ParameterAgent
from pipeline.asr import ASRVerifier
from pipeline.composer import compose_segments
from pipeline.feedback import FeedbackStore
from pipeline.generator import generate_segment_audio
from pipeline.models import (
    PipelineJob,
    PipelineJobStatus,
    PipelineJobSummary,
    Segment,
    SegmentStatus,
)
from pipeline.segmenter import estimate_segment_duration, split_text_into_segments
from pipeline.store import JobStore
from pipeline.verifier import VerificationThresholds, verify_audio

logger = logging.getLogger(__name__)

SynthesizeFn = Callable[..., Tuple[Optional[np.ndarray], Optional[int]]]


class PipelineService:
    """High-level interface for creating and running pipeline jobs."""

    def __init__(
        self,
        base_dir: Optional[Path] = None,
        synthesize_fn: Optional[SynthesizeFn] = None,
        asr_verifier: Optional[ASRVerifier] = None,
        parameter_agent: Optional[ParameterAgent] = None,
    ):
        self.base_dir = (
            base_dir
            if base_dir is not None
            else get_output_path(ensure_absolute=True) / "pipeline_jobs"
        )
        self.store = JobStore(self.base_dir)
        # Default to the real engine.synthesize; tests inject a mock.
        self.synthesize_fn = synthesize_fn
        # ASR verifier is lazily initialized unless injected.
        self._asr_verifier = asr_verifier
        # Rule-based parameter agent.
        self._parameter_agent = parameter_agent
        # Feedback store for the AI agent.
        self.feedback_store = FeedbackStore(self.base_dir / "feedback.jsonl")

    def _get_asr_verifier(self) -> Optional[ASRVerifier]:
        if self._asr_verifier is None:
            from config import get_pipeline_asr_config, get_tts_device

            asr_config = get_pipeline_asr_config()
            if not asr_config.get("enabled", True):
                return None
            self._asr_verifier = ASRVerifier(
                model_name=asr_config.get("model", "small"),
                device=get_tts_device(),
                compute_type=asr_config.get("compute_type", "float16"),
            )
        return self._asr_verifier

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

            if final_format == "wav":
                ok = compose_segments(segment_files, final_path, sr=target_sr, pause_ms=pause_ms)
            else:
                # Compose WAV first, then encode to target format.
                temp_wav = final_dir / "final_temp.wav"
                ok = compose_segments(segment_files, temp_wav, sr=target_sr, pause_ms=pause_ms)
                if ok:
                    ok = self._encode_to_format(temp_wav, final_path, final_format, target_sr)
                    try:
                        temp_wav.unlink(missing_ok=True)
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
        Generate one segment, retry on failure, verify audio + ASR quality, return path.

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
        thresholds = self._load_verification_thresholds()
        asr_verifier = self._get_asr_verifier()
        agent = self._get_parameter_agent()

        # Track the last verification failures and metrics to feed the agent.
        last_audio_failure: Optional[str] = None
        last_asr_failure: Optional[str] = None
        last_audio_metrics: Dict[str, Any] = {}

        # Phase 1: base retries.
        for attempt in range(max_retries + 1):
            passed, audio_failure, asr_failure, audio_metrics = self._attempt_segment(
                job=job,
                seg_record=seg_record,
                audio_prompt_path=audio_prompt_path,
                expected_duration=expected_duration,
                synthesize_fn=synthesize_fn,
                language=language,
                seg_path=seg_path,
                thresholds=thresholds,
                asr_verifier=asr_verifier,
                attempt=attempt,
                agent_decision=None,
            )
            if passed:
                return seg_path
            if audio_failure:
                last_audio_failure = audio_failure
                last_audio_metrics = audio_metrics or {}
            if asr_failure:
                last_asr_failure = asr_failure

        # Phase 2: agent-adjusted attempt.
        decision = agent.decide(
            base_params=seg_record.gen_params,
            audio_failure=last_audio_failure,
            asr_failure=last_asr_failure,
            audio_metrics=last_audio_metrics,
        )
        seg_record.gen_params = decision.gen_params
        passed, _, _, _ = self._attempt_segment(
            job=job,
            seg_record=seg_record,
            audio_prompt_path=audio_prompt_path,
            expected_duration=expected_duration,
            synthesize_fn=synthesize_fn,
            language=language,
            seg_path=seg_path,
            thresholds=thresholds,
            asr_verifier=asr_verifier,
            attempt=max_retries + 1,
            agent_decision=decision,
        )
        if passed:
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
        thresholds: VerificationThresholds,
        asr_verifier: Optional[ASRVerifier],
        attempt: int,
        agent_decision: Optional[AgentDecision],
    ) -> Tuple[bool, Optional[str], Optional[str]]:
        """
        Single generation + verification attempt.

        Returns (passed, audio_failure_reason, asr_failure_reason).
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
            return False, None, None, {}

        sf.write(str(seg_path), audio_np, sr, subtype="pcm_16")

        audio_result = verify_audio(
            audio_np,
            sr,
            expected_duration=expected_duration,
            thresholds=thresholds,
        )

        combined_passed = audio_result.passed
        combined_score = audio_result.score
        failure_reason = audio_result.failure_reason
        audio_failure = audio_result.failure_reason
        asr_failure: Optional[str] = None
        asr_result = None

        if audio_result.passed:
            if asr_verifier is not None:
                asr_result = asr_verifier.verify(
                    str(seg_path),
                    seg_record.text,
                    language=language,
                )
                if not asr_result.passed:
                    combined_passed = False
                    failure_reason = asr_result.failure_reason
                    asr_failure = asr_result.failure_reason
                # Combine scores: 60% audio, 40% ASR.
                asr_score = getattr(asr_result, "similarity", 1.0)
                combined_score = 0.6 * audio_result.score + 0.4 * asr_score

        log_entry: Dict[str, Any] = {
            "attempt": attempt,
            "passed": combined_passed,
            "score": combined_score,
            "audio": {
                "passed": audio_result.passed,
                "score": audio_result.score,
                "failure_reason": audio_result.failure_reason,
                "metrics": audio_result.metrics,
            },
        }
        if asr_result is not None:
            log_entry["asr"] = {
                "passed": asr_result.passed,
                "similarity": asr_result.similarity,
                "transcription": asr_result.transcription,
                "failure_reason": asr_result.failure_reason,
                "metrics": asr_result.metrics,
            }
        if agent_decision:
            log_entry["agent_decision"] = {
                "reason": agent_decision.reason,
                "deltas": agent_decision.deltas,
                "gen_params": agent_decision.gen_params,
            }

        seg_record.verification_log.append(log_entry)

        if combined_passed:
            seg_record.status = SegmentStatus.PASSED
            seg_record.audio_path = str(seg_path)
            seg_record.audio_score = audio_result.score
            seg_record.asr_score = (
                asr_result.similarity if asr_result is not None else 1.0
            )
            seg_record.score = combined_score
            seg_record.failure_reason = None
            seg_record.retry_count = attempt
            self.store.save(job)
            return True, None, None, audio_result.metrics

        seg_record.retry_count = attempt
        seg_record.failure_reason = failure_reason
        self.store.save(job)
        logger.warning(
            f"Job {job_id} segment {seg_record.index} attempt {attempt} failed: "
            f"{failure_reason}"
        )
        return False, audio_failure, asr_failure, audio_result.metrics

    def _load_verification_thresholds(self) -> VerificationThresholds:
        from config import get_pipeline_verification_thresholds

        cfg = get_pipeline_verification_thresholds()
        return VerificationThresholds(
            max_silence_ms=cfg.get("max_silence_ms", 500.0),
            clip_threshold=cfg.get("clip_threshold", 0.99),
            min_rms=cfg.get("min_rms", 0.01),
            max_duration_deviation=cfg.get("max_duration_deviation", 0.50),
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
