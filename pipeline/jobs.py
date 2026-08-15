# Pipeline job orchestration: submit, run, compose.

import logging
import re
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

import numpy as np
import soundfile as sf

from config import (
    config_manager,
    get_audio_output_format,
    get_audio_sample_rate,
    get_output_path,
    get_pipeline_max_retry_count,
)
from pipeline.agent import AgentDecision, ParameterAgent
from pipeline.composer import (
    compose_segments,
    compose_segments_with_timing,
    loudnorm_final_audio,
    write_srt,
)
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


def _final_loudnorm_targets() -> Tuple[float, float, float]:
    """
    Broadcast loudness targets for the composed final audio, read from
    pipeline.verification.final_loudnorm. These are independent of the
    per-segment audio_metrics thresholds; DEFAULT_CONFIG guarantees the keys.
    """
    cfg = config_manager.get("pipeline.verification.final_loudnorm", {})
    return (
        float(cfg["target_lufs"]),
        float(cfg["true_peak_dbtp"]),
        float(cfg["lra"]),
    )


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

    def _job_dir(self, job: PipelineJob) -> Path:
        return self.base_dir / (job.folder_name or job.job_id)

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
        job_name: Optional[str] = None,
    ) -> str:
        """Create a new pipeline job and persist it."""
        job_id = f"pipe-{uuid.uuid4().hex[:12]}"
        folder_name = job_id
        if job_name:
            slug = re.sub(r"[^\w\-]+", "_", job_name).strip("_") or "job"
            folder_name = f"{job_id}_{slug}"
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
            job_name=job_name,
            folder_name=folder_name,
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
        max_segment_duration: float = 15.0,
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
                else:
                    # A failed segment still has its last polished attempt on
                    # disk — include it so the final has no narrative gaps.
                    # Verification status stays FAILED in the dashboard; the
                    # final simply uses the best take we could get.
                    best_effort = (
                        self._job_dir(job) / "segments" / f"{seg_record.index}.wav"
                    )
                    if best_effort.exists():
                        segment_files.append(best_effort)
                        logger.warning(
                            f"Job {job_id} segment {idx}: including unverified "
                            f"best-effort audio (all attempts failed)"
                        )

            total_gen = sum((s.generation_time_sec or 0) for s in job.segments)
            total_verify = sum((s.verification_time_sec or 0) for s in job.segments)
            job.total_generation_time_sec = round(total_gen, 3)
            job.total_verification_time_sec = round(total_verify, 3)

            if not segment_files:
                job.status = PipelineJobStatus.FAILED
                self.store.save(job)
                return job

            final_path = self._compose_final_audio(job, segment_files, pause_ms)
            if final_path is None:
                job.status = PipelineJobStatus.FAILED
                self.store.save(job)
                return job

            job.final_audio_path = str(final_path)
            job.status = PipelineJobStatus.DONE
            self.store.save(job)
            logger.info(
                f"Job {job_id} completed: {final_path} "
                f"(gen={job.total_generation_time_sec:.1f}s, "
                f"verify={job.total_verification_time_sec:.1f}s)"
            )
            return job

        except Exception as e:
            logger.error(f"Pipeline job {job_id} failed: {e}", exc_info=True)
            total_gen = sum((s.generation_time_sec or 0) for s in job.segments)
            total_verify = sum((s.verification_time_sec or 0) for s in job.segments)
            job.total_generation_time_sec = round(total_gen, 3)
            job.total_verification_time_sec = round(total_verify, 3)
            job.status = PipelineJobStatus.FAILED
            self.store.save(job)
            return job

    def retry_failed_segments(
        self,
        job_id: str,
        pause_ms: float = 150.0,
    ) -> Optional[PipelineJob]:
        """
        Re-run only the failed segments of a finished job and recompose final.

        Segments that already passed keep their verified audio; failed segments
        are reset to the job's original generation params and go through the
        full generate-verify-retry loop again. This is the cheap way to push a
        job to "all passed" without regenerating everything.
        """
        job = self.get_job(job_id)
        if job is None:
            logger.error(f"Pipeline job {job_id} not found")
            return None
        if job.status not in (PipelineJobStatus.DONE, PipelineJobStatus.FAILED):
            logger.warning(f"Pipeline job {job_id} is busy (status={job.status})")
            return job

        failed = [s for s in job.segments if s.status == SegmentStatus.FAILED]
        if not failed:
            logger.info(f"Job {job_id}: no failed segments to retry")
            return job

        synthesize_fn = self.synthesize_fn
        if synthesize_fn is None:
            import engine

            synthesize_fn = engine.synthesize

        language = job.gen_params.get("language", "en")
        audio_prompt_path = self._resolve_audio_prompt_path(job.voice_config)
        job.status = PipelineJobStatus.RUNNING

        for seg_record in failed:
            # Reset to the job's original params; the previous run's agent
            # mutations belong to that attempt history.
            seg_record.status = SegmentStatus.PENDING
            seg_record.gen_params = dict(job.gen_params)
            seg_record.failure_reason = None
            seg_record.retry_count = 0
            seg_record.audio_path = None
            self.store.save(job)

            expected_duration = estimate_segment_duration(seg_record.text, language)
            self._generate_and_verify_segment(
                job=job,
                seg_record=seg_record,
                audio_prompt_path=audio_prompt_path,
                expected_duration=expected_duration,
                synthesize_fn=synthesize_fn,
                language=language,
            )

        # Compose from all segments in index order. Passed segments use their
        # verified audio; still-failing segments fall back to their last
        # polished attempt on disk so the final has no narrative gaps.
        segment_files = []
        for s in sorted(job.segments, key=lambda s: s.index):
            if s.status == SegmentStatus.PASSED and s.audio_path:
                segment_files.append(Path(s.audio_path))
            else:
                best_effort = self._job_dir(job) / "segments" / f"{s.index}.wav"
                if best_effort.exists():
                    segment_files.append(best_effort)
                    logger.warning(
                        f"Job {job_id} segment {s.index}: including unverified "
                        f"best-effort audio (all attempts failed)"
                    )
        total_gen = sum((s.generation_time_sec or 0) for s in job.segments)
        total_verify = sum((s.verification_time_sec or 0) for s in job.segments)
        job.total_generation_time_sec = round(total_gen, 3)
        job.total_verification_time_sec = round(total_verify, 3)

        if not segment_files:
            job.status = PipelineJobStatus.FAILED
            self.store.save(job)
            return job

        final_path = self._compose_final_audio(job, segment_files, pause_ms)
        if final_path is None:
            job.status = PipelineJobStatus.FAILED
            self.store.save(job)
            return job

        job.final_audio_path = str(final_path)
        job.status = PipelineJobStatus.DONE
        self.store.save(job)
        logger.info(f"Job {job_id} retry-failed complete: {final_path}")
        return job

    def _compose_final_audio(
        self,
        job: PipelineJob,
        segment_files: List[Path],
        pause_ms: float,
    ) -> Optional[Path]:
        """
        Compose verified segment WAVs into the final output with loudnorm.

        Also writes a sentence-level SRT subtitle file based on the composed
        segment timings.

        Returns the final path on success, None on failure.
        """
        job_id = job.job_id
        final_format = job.pipeline_config.get(
            "output_format", get_audio_output_format()
        )
        target_sr = job.pipeline_config.get(
            "sample_rate", get_audio_sample_rate()
        )
        final_dir = self._job_dir(job)
        base_name = "final"
        if job.job_name:
            base_name = re.sub(r"[^\w\-]+", "_", job.job_name).strip("_") or "final"
        final_path = final_dir / f"{base_name}.{final_format}"

        # Final loudnorm targets come from the dedicated broadcast-target
        # config, independent of the lenient per-segment audio_metrics
        # thresholds (defaults guaranteed by DEFAULT_CONFIG merge).
        target_lufs, true_peak, lra = _final_loudnorm_targets()

        # Structural guarantee: every segment that ships in a final is
        # polished (denoise + pause normalization). Generation-time polish
        # already handles new audio; this pass covers legacy files from before
        # the polish step existed. Polish is idempotent — a second pass on an
        # already-polished file is a near no-op (the noise profile is gone,
        # pauses are already within tolerance).
        from pipeline.polish import polish_segment_audio

        for seg_file in segment_files:
            try:
                polish_segment_audio(str(seg_file))
            except Exception as e:
                logger.warning(f"Compose-time polish failed for {seg_file}: {e}")

        temp_wav = final_dir / "final_temp.wav"
        normalized_wav = final_dir / "final_normalized.wav"
        ok, timings = compose_segments_with_timing(
            segment_files, temp_wav, sr=target_sr, pause_ms=pause_ms
        )
        if ok and timings:
            # Write sentence-level SRT from composed segment timings.
            srt_path = final_dir / f"{base_name}.srt"
            srt_entries = [
                (start, end, seg.text)
                for (start, end), seg in zip(timings, job.segments)
            ]
            write_srt(srt_path, srt_entries)
            job.srt_path = str(srt_path)

        if ok:
            # Final loudnorm ensures consistent loudness across the whole long-form output.
            ok = loudnorm_final_audio(
                temp_wav,
                normalized_wav,
                target_lufs=target_lufs,
                true_peak=true_peak,
                lra=lra,
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

        return final_path if ok else None

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
        seg_dir = self._job_dir(job) / "segments"
        seg_dir.mkdir(parents=True, exist_ok=True)
        seg_path = seg_dir / f"{seg_record.index}.wav"

        max_retries = job.pipeline_config.get(
            "max_retry_count", get_pipeline_max_retry_count()
        )
        quality_verifier = self._get_quality_verifier()
        agent = self._get_parameter_agent()

        # Track the last attempt to feed the agent in Phase 2.
        last_attempt: Optional[AttemptResult] = None

        # Phase 1: base retries. Bump the seed per attempt so every retry is a
        # genuinely new generation — Chatterbox is fully deterministic for a
        # fixed seed, so without this the base retries re-verified identical
        # audio and only the agent attempt ever rolled differently. The
        # sequence (base_seed + attempt) stays deterministic and reproducible.
        base_seed = seg_record.gen_params.get("seed")
        for attempt in range(max_retries + 1):
            if base_seed is not None:
                seg_record.gen_params["seed"] = base_seed + attempt
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

        gen_start = time.perf_counter()
        audio_np, sr = generate_segment_audio(
            text=seg_record.text,
            audio_prompt_path=audio_prompt_path,
            gen_params=seg_record.gen_params,
            synthesize_fn=synthesize_fn,
        )
        gen_time = time.perf_counter() - gen_start

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
            seg_record.generation_time_sec = gen_time
            self.store.save(job)
            empty_quality = QualityVerificationResult(
                passed=False,
                overall_score=0.0,
                failure_reason="generation returned no audio",
                layer_results={},
            )
            return AttemptResult(
                passed=False,
                quality_result=empty_quality,
                generation_time_sec=gen_time,
            )

        temp_seg_path = seg_path.with_suffix(".tmp.wav")
        try:
            sf.write(str(temp_seg_path), audio_np, sr, subtype="pcm_16")
            # Atomic publish: readers (including the UI audio player) only see
            # the fully-written file, never a partially-written one.
            try:
                temp_seg_path.replace(seg_path)
            except OSError:
                import shutil
                shutil.move(str(temp_seg_path), str(seg_path))
        except Exception as e:
            # A transient write failure (e.g. Windows file lock from a media
            # player holding the segment open) must not kill the whole job —
            # treat it like a generation failure so the retry loop continues.
            logger.warning(
                f"Job {job.job_id} segment {seg_record.index} attempt {attempt}: "
                f"audio write failed: {e}"
            )
            log_entry = {
                "attempt": attempt,
                "passed": False,
                "failure_reason": "audio_write_failed",
                "metrics": {"error": str(e)},
            }
            if agent_decision:
                log_entry["agent_decision"] = {
                    "reason": agent_decision.reason,
                    "deltas": agent_decision.deltas,
                    "gen_params": agent_decision.gen_params,
                }
            seg_record.verification_log.append(log_entry)
            seg_record.retry_count = attempt
            seg_record.generation_time_sec = gen_time
            self.store.save(job)
            write_failed_quality = QualityVerificationResult(
                passed=False,
                overall_score=0.0,
                failure_reason="audio_write_failed",
                layer_results={},
            )
            return AttemptResult(
                passed=False,
                quality_result=write_failed_quality,
                generation_time_sec=gen_time,
            )

        # Polish the generated audio BEFORE verification: spectral-subtract the
        # constant vocoder noise bed, then normalize over-long pauses. The
        # verification chain therefore checks the exact audio that ships, and
        # the repair report keeps the model's raw behaviour visible in logs.
        polish_report: Dict[str, Any] = {}
        try:
            from pipeline.polish import polish_segment_audio

            polish_report = polish_segment_audio(str(seg_path))
        except Exception as e:
            # Polish must never kill an attempt — unpolished audio is still valid.
            logger.warning(
                f"Job {job.job_id} segment {seg_record.index} attempt {attempt}: "
                f"polish failed, continuing with unpolished audio: {e}"
            )
            polish_report = {"error": str(e)}

        # Run the multi-layer quality verifier (basic audio + audio metrics + content).
        # The audio prompt doubles as the reference voice for speaker-similarity layers.
        verify_start = time.perf_counter()
        quality_result = quality_verifier.verify(
            audio_path=str(seg_path),
            original_text=seg_record.text,
            reference_voice_path=audio_prompt_path,
            language=language,
            expected_duration=expected_duration,
        )
        verify_time = time.perf_counter() - verify_start

        log_entry: Dict[str, Any] = {
            "attempt": attempt,
            "passed": quality_result.passed,
            "score": quality_result.overall_score,
            "polish": polish_report,
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

        seg_record.generation_time_sec = (seg_record.generation_time_sec or 0) + gen_time
        seg_record.verification_time_sec = (seg_record.verification_time_sec or 0) + verify_time

        if quality_result.passed:
            seg_record.status = SegmentStatus.PASSED
            seg_record.audio_path = str(seg_path)
            seg_record.audio_score = quality_result.overall_score
            seg_record.score = quality_result.overall_score
            seg_record.failure_reason = None
            seg_record.retry_count = attempt
            self.store.save(job)
            return AttemptResult(
                passed=True,
                quality_result=quality_result,
                audio_metrics=audio_metrics,
                generation_time_sec=gen_time,
                verification_time_sec=verify_time,
            )

        seg_record.retry_count = attempt
        seg_record.failure_reason = quality_result.failure_reason
        self.store.save(job)
        logger.warning(
            f"Job {job_id} segment {seg_record.index} attempt {attempt} failed: "
            f"{quality_result.failure_reason}"
        )
        return AttemptResult(
            passed=False,
            quality_result=quality_result,
            audio_metrics=audio_metrics,
            generation_time_sec=gen_time,
            verification_time_sec=verify_time,
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
