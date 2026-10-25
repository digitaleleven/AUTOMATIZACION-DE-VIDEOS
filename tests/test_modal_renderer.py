from __future__ import annotations

import hashlib
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from video_pipeline import modal_renderer


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


def _input_bundle(tmp_path: Path, output_bytes: bytes):
    source = tmp_path / "source.MOV"
    source.write_bytes(b"unmodified original bytes")
    effects = tmp_path / "effects"
    effects.mkdir()
    plan = {"cuts": [], "effect_cues": [], "expected_output_duration_seconds": 1.0}
    transcript = {"words": []}
    job_id = "a" * 32
    output = tmp_path / "outputs" / "result.mp4"
    result = {
        "job_id": job_id,
        "output_path": f"jobs/{job_id}/output.mp4",
        "output_size_bytes": len(output_bytes),
        "output_sha256": hashlib.sha256(output_bytes).hexdigest(),
        "duration_seconds": 1.0,
        "width": 1080,
        "height": 1920,
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
        self.assertTrue(result.temporary_storage_cleaned)
        self.assertEqual(volume.removals, [(f"jobs/{job_id}", True)])
        self.assertEqual(
            sdk.function_lookups,
            [(modal_renderer.MODAL_APP_NAME, modal_renderer.MODAL_FUNCTION_NAME)],
        )
        self.assertIn(f"/jobs/{job_id}/manifest.json", volume.uploads)
        self.assertIn(f"/jobs/{job_id}/source.mov", volume.uploads)

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
