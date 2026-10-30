from __future__ import annotations

import hashlib
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from video_pipeline import modal_renderer
from video_pipeline.modal_render_app import validate_worker_render_profile


class ModalWorkerProfileTests(unittest.TestCase):
    def test_accepts_4k_30_for_a_4k_master(self):
        self.assertEqual(
            validate_worker_render_profile(
                {
                    "output_width": 2160,
                    "output_height": 3840,
                    "output_fps": 30,
                },
                source_width=2160,
                source_height=3840,
            ),
            (2160, 3840, 30),
        )

    def test_rejects_4k_upscale_and_non_30_fps_master(self):
        profile = {
            "output_width": 2160,
            "output_height": 3840,
            "output_fps": 30,
        }
        with self.assertRaisesRegex(ValueError, "requiere una fuente maestra vertical"):
            validate_worker_render_profile(profile, source_width=1080, source_height=1920)
        profile["output_fps"] = 60
        with self.assertRaisesRegex(ValueError, "exactamente 30 FPS"):
            validate_worker_render_profile(profile, source_width=2160, source_height=3840)
        profile["output_fps"] = 61
        with self.assertRaisesRegex(ValueError, "25, 30, 50 o 60"):
            validate_worker_render_profile(profile, source_width=2160, source_height=3840)
        with self.assertRaisesRegex(ValueError, "no se puede reducir"):
            validate_worker_render_profile(
                {"output_width": 1080, "output_height": 1920, "output_fps": 30},
                source_width=2160,
                source_height=3840,
            )


class FakeBatch:
    def __init__(self, volume):
        self.volume = volume

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def put_file(self, local_path, remote_path):
        self.volume.uploads[remote_path] = Path(local_path).read_bytes()


class FakeVolume:
    def __init__(self, rendered_bytes: bytes):
        self.rendered_bytes = rendered_bytes
        self.uploads = {}
        self.removals = []
        self.files = {}

    def batch_upload(self):
        return FakeBatch(self)

    def read_file(self, remote_path):
        if remote_path == "jobs/" + self.job_id + "/output.mp4":
            payload = self.rendered_bytes
        elif remote_path in self.files:
            payload = self.files[remote_path]
        else:
            raise FileNotFoundError(remote_path)
        yield payload[:5]
        yield payload[5:]

    def remove_file(self, remote_path, recursive=False):
        self.removals.append((remote_path, recursive))


class FakeSDK:
    def __init__(self, volume, result):
        self.volume = volume
        self.result = result
        self.function_lookups = []
        self.remote_calls = []
        self.results_by_operation = {}

    class _VolumeFactory:
        def __init__(self, owner):
            self.owner = owner

        def from_name(self, name, create_if_missing=False):
            assert name == modal_renderer.MODAL_VOLUME_NAME
            assert create_if_missing is True
            return self.owner.volume

    class _FunctionFactory:
        def __init__(self, owner):
            self.owner = owner

        def from_name(self, app_name, function_name):
            self.owner.function_lookups.append((app_name, function_name))
            def remote(job_id, operation="render"):
                self.owner.remote_calls.append((job_id, operation))
                return self.owner.results_by_operation.get(operation, self.owner.result)
            return SimpleNamespace(remote=remote)

    @property
    def Volume(self):
        return self._VolumeFactory(self)

    @property
    def Function(self):
        return self._FunctionFactory(self)


def _input_bundle(tmp_path: Path, output_bytes: bytes, profile=(1080, 1920, 30)):
    source = tmp_path / "source.MOV"
    source.write_bytes(b"unmodified original bytes")
    effects = tmp_path / "effects"
    effects.mkdir()
    plan = {
        "cuts": [],
        "effect_cues": [],
        "expected_output_duration_seconds": 1.0,
        "output_width": profile[0],
        "output_height": profile[1],
        "output_fps": profile[2],
    }
    transcript = {"words": []}
    job_id = "a" * 32
    output = tmp_path / "outputs" / "result.mp4"
    result = {
        "job_id": job_id,
        "output_path": f"jobs/{job_id}/output.mp4",
        "output_size_bytes": len(output_bytes),
        "output_sha256": hashlib.sha256(output_bytes).hexdigest(),
        "duration_seconds": 1.0,
        "width": profile[0],
        "height": profile[1],
        "fps": profile[2],
    }
    volume = FakeVolume(output_bytes)
    volume.job_id = job_id
    sdk = FakeSDK(volume, result)
    return source, effects, plan, transcript, job_id, output, volume, sdk


def _modal_status(ready=True):
    return {
            "sdk_installed": True,
            "credentials_configured": ready,
            "app_name": modal_renderer.MODAL_APP_NAME,
            "function_name": modal_renderer.MODAL_FUNCTION_NAME,
            "deployment_status": "not_checked",
        }


class ModalRenderClientTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_uploads_inputs_and_returns_verified_mp4(self):
        output_bytes = b"verified-mp4-content"
        source, effects, plan, transcript, job_id, output, volume, sdk = _input_bundle(
            self.root, output_bytes
        )
        logo = effects / "client-brand-logo.png"
        logo.write_bytes(b"local-logo-asset")
        with patch.object(modal_renderer, "modal_configuration_status", return_value=_modal_status()):
            result = modal_renderer.ModalRenderClient(sdk).render(
                job_id=job_id,
                input_path=source,
                transcript=transcript,
                plan=plan,
                effects_dir=effects,
                output_path=output,
                show_subtitles=False,
            )

        self.assertEqual(output.read_bytes(), output_bytes)
        self.assertEqual(source.read_bytes(), b"unmodified original bytes")
        self.assertEqual(result.size_bytes, len(output_bytes))
        self.assertEqual(result.sha256, hashlib.sha256(output_bytes).hexdigest())
        self.assertEqual((result.width, result.height), (1080, 1920))
        self.assertEqual(result.fps, 30)
        self.assertTrue(result.temporary_storage_cleaned)
        self.assertEqual(volume.removals, [(f"jobs/{job_id}", True)])
        self.assertEqual(
            sdk.function_lookups,
            [(modal_renderer.MODAL_APP_NAME, modal_renderer.MODAL_FUNCTION_NAME)],
        )
        self.assertIn(f"/jobs/{job_id}/manifest.json", volume.uploads)
        self.assertIn(f"/jobs/{job_id}/source.mov", volume.uploads)
        self.assertEqual(
            volume.uploads[f"/jobs/{job_id}/effects/client-brand-logo.png"],
            b"local-logo-asset",
        )

    def test_normalization_cache_hit_downloads_verified_proxy_without_worker_submission(self):
        source = self.root / "source.MOV"
        source.write_bytes(b"keep-original")
        output = self.root / "analysis-proxy.mp4"
        job_id = "b" * 32
        proxy = b"cached-normalized-proxy"
        source_hash = hashlib.sha256(source.read_bytes()).hexdigest()
        prefix = f"normalized-cache/{source_hash}/{modal_renderer.NORMALIZATION_PROFILE_ID}"
        manifest = {
            "source_sha256": source_hash,
            "profile_id": modal_renderer.NORMALIZATION_PROFILE_ID,
            "proxy_path": f"{prefix}/proxy.mp4",
            "proxy_sha256": hashlib.sha256(proxy).hexdigest(),
            "proxy_size_bytes": len(proxy),
            "duration_seconds": 2.0,
            "profile": {
                "width": 1080, "height": 1920, "fps": 30,
                "codec": "h264", "pixel_format": "yuv420p", "color_range": "tv",
                "color_primaries": "bt709", "color_transfer": "bt709",
                "color_space": "bt709", "audio_codec": "aac",
                "audio_sample_rate": 48000, "audio_channels": 2,
                "duration_seconds": 2.0,
            },
            "capabilities": {"ffmpeg_version": "ffmpeg test"},
            "processing_seconds": 12.5,
        }
        volume = FakeVolume(b"unused")
        volume.job_id = job_id
        volume.files[f"{prefix}/manifest.json"] = json.dumps(manifest).encode()
        volume.files[f"{prefix}/proxy.mp4"] = proxy
        sdk = FakeSDK(volume, {})
        with patch.object(modal_renderer, "modal_configuration_status", return_value=_modal_status()):
            result = modal_renderer.ModalRenderClient(sdk).normalize_input(
                job_id=job_id,
                input_path=source,
                output_path=output,
            )

        self.assertTrue(result.cache_hit)
        self.assertEqual(result.output_path.read_bytes(), proxy)
        self.assertEqual(source.read_bytes(), b"keep-original")
        self.assertEqual(sdk.remote_calls, [])
        self.assertEqual(result.remote_processing_seconds, 0)

    def test_normalization_miss_stages_source_once_for_following_render(self):
        source, effects, plan, transcript, job_id, output, volume, sdk = _input_bundle(
            self.root, b"render-result"
        )
        proxy = b"new-normalized-proxy"
        source_hash = hashlib.sha256(source.read_bytes()).hexdigest()
        cache_path = (
            f"normalized-cache/{source_hash}/{modal_renderer.NORMALIZATION_PROFILE_ID}/proxy.mp4"
        )
        sdk.results_by_operation["normalize"] = {
            "job_id": job_id,
            "normalized_proxy_path": cache_path,
            "source_sha256": source_hash,
            "profile_id": modal_renderer.NORMALIZATION_PROFILE_ID,
            "proxy_sha256": hashlib.sha256(proxy).hexdigest(),
            "proxy_size_bytes": len(proxy),
            "duration_seconds": 1.0,
            "profile": {
                "width": 1080, "height": 1920, "fps": 30,
                "codec": "h264", "pixel_format": "yuv420p", "color_range": "tv",
                "color_primaries": "bt709", "color_transfer": "bt709",
                "color_space": "bt709", "audio_codec": "aac",
                "audio_sample_rate": 48000, "audio_channels": 2,
                "duration_seconds": 1.0,
            },
            "capabilities": {"ffmpeg_version": "ffmpeg test"},
            "cache_hit": False,
            "processing_seconds": 5.0,
        }
        volume.files[cache_path] = proxy
        client = modal_renderer.ModalRenderClient(sdk)
        with patch.object(modal_renderer, "modal_configuration_status", return_value=_modal_status()):
            normalization = client.normalize_input(
                job_id=job_id,
                input_path=source,
                render_source_path=source,
                output_path=self.root / "normalized.mp4",
            )
            client.render(
                job_id=job_id,
                input_path=source,
                transcript=transcript,
                plan=plan,
                effects_dir=effects,
                output_path=output,
                show_subtitles=False,
            )

        self.assertFalse(normalization.cache_hit)
        self.assertEqual(normalization.output_path.read_bytes(), proxy)
        self.assertEqual(sdk.remote_calls, [(job_id, "normalize"), (job_id, "render")])
        self.assertEqual(
            volume.uploads[f"/jobs/{job_id}/source.mov"],
            b"unmodified original bytes",
        )
        manifest = json.loads(volume.uploads[f"/jobs/{job_id}/manifest.json"])
        self.assertEqual(manifest["normalized_proxy_path"], cache_path)

    def test_4k_normalization_uses_verified_4k_profile_and_one_normalize_call(self):
        source, _effects, _plan, _transcript, job_id, _output, volume, sdk = _input_bundle(
            self.root, b"render-result"
        )
        proxy = b"verified-4k-normalized-proxy"
        source_hash = hashlib.sha256(source.read_bytes()).hexdigest()
        profile_id = modal_renderer.MASTER_4K_NORMALIZATION_PROFILE_ID
        cache_path = f"normalized-cache/{source_hash}/{profile_id}/proxy.mp4"
        sdk.results_by_operation["normalize"] = {
            "job_id": job_id,
            "normalized_proxy_path": cache_path,
            "source_sha256": source_hash,
            "profile_id": profile_id,
            "proxy_sha256": hashlib.sha256(proxy).hexdigest(),
            "proxy_size_bytes": len(proxy),
            "duration_seconds": 59.98,
            "profile": {
                "width": 2160, "height": 3840, "fps": 30,
                "codec": "h264", "pixel_format": "yuv420p", "color_range": "tv",
                "color_primaries": "bt709", "color_transfer": "bt709",
                "color_space": "bt709", "audio_codec": "aac",
                "audio_sample_rate": 48000, "audio_channels": 2,
                "duration_seconds": 59.98,
            },
            "capabilities": {"ffmpeg_version": "ffmpeg test"},
            "cache_hit": False,
            "processing_seconds": 42.0,
        }
        volume.files[cache_path] = proxy
        client = modal_renderer.ModalRenderClient(sdk)
        source_profile = SimpleNamespace(
            width=3840,
            height=2160,
            duration=59.98,
            rotation=90,
            color_range="tv",
            color_primaries="bt2020",
            color_transfer="arib-std-b67",
            color_space="bt2020nc",
        )
        with (
            patch.object(modal_renderer, "modal_configuration_status", return_value=_modal_status()),
            patch("video_pipeline.media.probe_video", return_value=source_profile),
        ):
            result = client.normalize_input(
                job_id=job_id,
                input_path=source,
                output_path=self.root / "normalized-4k.mp4",
                profile_id=profile_id,
            )

        self.assertEqual(result.profile_id, profile_id)
        self.assertEqual((result.profile["width"], result.profile["height"], result.profile["fps"]),
                         (2160, 3840, 30))
        self.assertEqual(sdk.remote_calls, [(job_id, "normalize")])
        self.assertEqual(result.remote_processing_seconds, 42.0)

    def test_4k_normalization_budget_block_happens_before_modal_access(self):
        source, _effects, _plan, _transcript, job_id, _output, volume, sdk = _input_bundle(
            self.root, b"render-result"
        )
        source_profile = SimpleNamespace(
            width=2160,
            height=3840,
            duration=59.98,
            rotation=0,
            color_range="tv",
            color_primaries="bt2020",
            color_transfer="arib-std-b67",
            color_space="bt2020nc",
        )
        with (
            patch(
                "video_pipeline.cost_controls.enforce_4k_normalization_budget",
                side_effect=RuntimeError("budget-block"),
            ),
            patch.object(modal_renderer, "modal_configuration_status", return_value=_modal_status()),
            patch("video_pipeline.media.probe_video", return_value=source_profile),
        ):
            with self.assertRaisesRegex(RuntimeError, "budget-block"):
                modal_renderer.ModalRenderClient(sdk).normalize_input(
                    job_id=job_id,
                    input_path=source,
                    output_path=self.root / "normalized-4k-blocked.mp4",
                    profile_id=modal_renderer.MASTER_4K_NORMALIZATION_PROFILE_ID,
                )
        self.assertEqual(sdk.remote_calls, [])
        self.assertEqual(volume.uploads, {})

    def test_cleanup_removes_only_job_staging_and_preserves_cache_path(self):
        volume = FakeVolume(b"unused")
        volume.job_id = "c" * 32
        sdk = FakeSDK(volume, {})
        client = modal_renderer.ModalRenderClient(sdk)
        job_id = "c" * 32
        client._staged_jobs.add(job_id)

        with patch.object(modal_renderer, "modal_configuration_status", return_value=_modal_status()):
            self.assertTrue(client.cleanup_staged_job(job_id))

        self.assertEqual(volume.removals, [(f"jobs/{job_id}", True)])
        self.assertFalse(any("normalized-cache/" in path for path, _ in volume.removals))
        self.assertFalse(client.cleanup_staged_job("invalid"))

    def test_quick_analysis_proxy_is_not_attached_to_full_source_render(self):
        source, effects, plan, transcript, job_id, output, volume, sdk = _input_bundle(
            self.root, b"full-render-result"
        )
        analysis_source = self.root / "analysis-clip.mov"
        analysis_source.write_bytes(b"five-second-analysis-clip")
        analysis_hash = hashlib.sha256(analysis_source.read_bytes()).hexdigest()
        cache_path = (
            f"normalized-cache/{analysis_hash}/{modal_renderer.NORMALIZATION_PROFILE_ID}/proxy.mp4"
        )
        proxy = b"analysis-only-proxy"
        sdk.results_by_operation["normalize"] = {
            "job_id": job_id,
            "normalized_proxy_path": cache_path,
            "source_sha256": analysis_hash,
            "profile_id": modal_renderer.NORMALIZATION_PROFILE_ID,
            "proxy_sha256": hashlib.sha256(proxy).hexdigest(),
            "proxy_size_bytes": len(proxy),
            "duration_seconds": 1.0,
            "profile": {
                "width": 1080, "height": 1920, "fps": 30,
                "codec": "h264", "pixel_format": "yuv420p", "color_range": "tv",
                "color_primaries": "bt709", "color_transfer": "bt709",
                "color_space": "bt709", "audio_codec": "aac",
                "audio_sample_rate": 48000, "audio_channels": 2,
                "duration_seconds": 1.0,
            },
            "capabilities": {"ffmpeg_version": "ffmpeg test"},
            "cache_hit": False,
            "processing_seconds": 1.0,
        }
        volume.files[cache_path] = proxy
        client = modal_renderer.ModalRenderClient(sdk)
        with patch.object(modal_renderer, "modal_configuration_status", return_value=_modal_status()):
            client.normalize_input(
                job_id=job_id,
                input_path=analysis_source,
                render_source_path=source,
                output_path=self.root / "analysis-proxy-output.mp4",
            )
            client.render(
                job_id=job_id,
                input_path=source,
                transcript=transcript,
                plan=plan,
                effects_dir=effects,
                output_path=output,
                show_subtitles=False,
            )

        render_manifest = json.loads(volume.uploads[f"/jobs/{job_id}/manifest.json"])
        self.assertNotIn("normalized_proxy_path", render_manifest)
        self.assertEqual(volume.uploads[f"/jobs/{job_id}/source.mov"], b"unmodified original bytes")

    def test_1080_analysis_proxy_is_not_attached_to_4k_master_render(self):
        source, effects, plan, transcript, job_id, output, volume, sdk = _input_bundle(
            self.root, b"full-4k-render-result", profile=(2160, 3840, 30)
        )
        source_hash = hashlib.sha256(source.read_bytes()).hexdigest()
        client = modal_renderer.ModalRenderClient(sdk)
        client._normalized_jobs[job_id] = {
            "source_sha256": source_hash,
            "profile_id": modal_renderer.NORMALIZATION_PROFILE_ID,
            "remote_proxy_path": "normalized-cache/" + source_hash + "/1080/proxy.mp4",
            "proxy_sha256": "a" * 64,
            "width": 1080,
            "height": 1920,
        }
        with patch.object(modal_renderer, "modal_configuration_status", return_value=_modal_status()):
            client.render(
                job_id=job_id,
                input_path=source,
                transcript=transcript,
                plan=plan,
                effects_dir=effects,
                output_path=output,
                show_subtitles=False,
            )
        manifest = json.loads(volume.uploads[f"/jobs/{job_id}/manifest.json"])
        self.assertNotIn("normalized_proxy_path", manifest)
        self.assertEqual(volume.uploads[f"/jobs/{job_id}/source.mov"], b"unmodified original bytes")

    def test_render_client_accepts_4k_30_and_passes_profile_to_worker(self):
        source, effects, plan, transcript, job_id, output, volume, sdk = _input_bundle(
            self.root,
            b"verified-4k-mp4",
            profile=(2160, 3840, 30),
        )
        with patch.object(modal_renderer, "modal_configuration_status", return_value=_modal_status()):
            result = modal_renderer.ModalRenderClient(sdk).render(
                job_id=job_id,
                input_path=source,
                transcript=transcript,
                plan=plan,
                effects_dir=effects,
                output_path=output,
                show_subtitles=False,
            )
        self.assertEqual((result.width, result.height, result.fps), (2160, 3840, 30))
        worker_plan = json.loads(volume.uploads[f"/jobs/{job_id}/plan.json"])
        self.assertEqual(
            (worker_plan["output_width"], worker_plan["output_height"], worker_plan["output_fps"]),
            (2160, 3840, 30),
        )

    def test_rejects_invalid_job_and_existing_output_without_calling_modal(self):
        source, effects, plan, transcript, job_id, output, _volume, sdk = _input_bundle(
            self.root, b"mp4"
        )
        output.parent.mkdir()
        output.write_bytes(b"keep-existing")
        with patch.object(modal_renderer, "modal_configuration_status", return_value=_modal_status()):
            with self.assertRaisesRegex(ValueError, "Identificador"):
                modal_renderer.ModalRenderClient(sdk).render(
                    job_id="not-a-job-id",
                    input_path=source,
                    transcript=transcript,
                    plan=plan,
                    effects_dir=effects,
                    output_path=self.root / "new.mp4",
                    show_subtitles=False,
                )
            with self.assertRaisesRegex(FileExistsError, "sobrescribir"):
                modal_renderer.ModalRenderClient(sdk).render(
                    job_id=job_id,
                    input_path=source,
                    transcript=transcript,
                    plan=plan,
                    effects_dir=effects,
                    output_path=output,
                    show_subtitles=False,
                )
        self.assertEqual(output.read_bytes(), b"keep-existing")
        self.assertEqual(sdk.function_lookups, [])

    def test_discards_corrupt_partial_download(self):
        source, effects, plan, transcript, job_id, output, _volume, sdk = _input_bundle(
            self.root, b"bad-result"
        )
        sdk.result["output_sha256"] = "0" * 64
        with patch.object(modal_renderer, "modal_configuration_status", return_value=_modal_status()):
            with self.assertRaisesRegex(RuntimeError, "checksum"):
                modal_renderer.ModalRenderClient(sdk).render(
                    job_id=job_id,
                    input_path=source,
                    transcript=transcript,
                    plan=plan,
                    effects_dir=effects,
                    output_path=output,
                    show_subtitles=False,
                )
        self.assertFalse(output.exists())
        self.assertFalse(output.with_name(output.name + ".part").exists())

    def test_fails_closed_without_profile(self):
        source, effects, plan, transcript, job_id, output, _volume, sdk = _input_bundle(
            self.root, b"mp4"
        )
        with patch.object(modal_renderer, "modal_configuration_status", return_value=_modal_status(False)):
            with self.assertRaisesRegex(RuntimeError, "no está listo"):
                modal_renderer.ModalRenderClient(sdk).render(
                    job_id=job_id,
                    input_path=source,
                    transcript=transcript,
                    plan=plan,
                    effects_dir=effects,
                    output_path=output,
                    show_subtitles=False,
                )
        self.assertEqual(sdk.function_lookups, [])
