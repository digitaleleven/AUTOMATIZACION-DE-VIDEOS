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
    def test_accepts_4k_60_for_a_4k_master(self):
        self.assertEqual(
            validate_worker_render_profile(
                {
                    "output_width": 2160,
                    "output_height": 3840,
                    "output_fps": 60,
                },
                source_width=2160,
                source_height=3840,
            ),
            (2160, 3840, 60),
        )

    def test_rejects_4k_upscale_and_fps_above_60(self):
        profile = {
            "output_width": 2160,
            "output_height": 3840,
            "output_fps": 60,
        }
        with self.assertRaisesRegex(ValueError, "requiere una fuente maestra vertical"):
            validate_worker_render_profile(profile, source_width=1080, source_height=1920)
        profile["output_fps"] = 61
        with self.assertRaisesRegex(ValueError, "25, 30, 50 o 60"):
            validate_worker_render_profile(profile, source_width=2160, source_height=3840)


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

    def batch_upload(self):
        return FakeBatch(self)

    def read_file(self, remote_path):
        assert remote_path == "jobs/" + self.job_id + "/output.mp4"
        yield self.rendered_bytes[:5]
        yield self.rendered_bytes[5:]

    def remove_file(self, remote_path, recursive=False):
        self.removals.append((remote_path, recursive))


class FakeSDK:
    def __init__(self, volume, result):
        self.volume = volume
        self.result = result
        self.function_lookups = []

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
            return SimpleNamespace(remote=lambda job_id: self.owner.result)

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

    def test_render_client_accepts_4k_60_and_passes_profile_to_worker(self):
        source, effects, plan, transcript, job_id, output, volume, sdk = _input_bundle(
            self.root,
            b"verified-4k-mp4",
            profile=(2160, 3840, 60),
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
        self.assertEqual((result.width, result.height, result.fps), (2160, 3840, 60))
        worker_plan = json.loads(volume.uploads[f"/jobs/{job_id}/plan.json"])
        self.assertEqual(
            (worker_plan["output_width"], worker_plan["output_height"], worker_plan["output_fps"]),
            (2160, 3840, 60),
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
