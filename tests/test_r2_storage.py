from pathlib import Path
import tempfile
import unittest

from video_pipeline.r2_storage import (
    EXPECTED_BUCKET,
    R2StorageError,
    configuration_status,
    upload_mp4,
)


class FakeR2Client:
    def __init__(self, size: int):
        self.size = size
        self.uploaded = None

    def upload_file(self, filename, bucket, key, **kwargs):
        self.uploaded = (filename, bucket, key, kwargs)

    def head_object(self, *, Bucket, Key):
        return {"ContentLength": self.size}


class R2StorageTests(unittest.TestCase):
    def setUp(self):
        self.env = {
            "R2_ACCESS_KEY_ID": "fake-access",
            "R2_SECRET_ACCESS_KEY": "fake-secret",
            "R2_ENDPOINT_URL": "https://account.r2.cloudflarestorage.com",
            "R2_BUCKET_NAME": EXPECTED_BUCKET,
        }

    def test_status_never_returns_secret_values(self):
        status = configuration_status(self.env)
        self.assertTrue(status["ready"])
        self.assertNotIn("fake-access", repr(status))
        self.assertNotIn("fake-secret", repr(status))

    def test_rejects_wrong_bucket_and_non_https_endpoint(self):
        env = {**self.env, "R2_BUCKET_NAME": "other-bucket"}
        self.assertFalse(configuration_status(env)["ready"])
        env = {**self.env, "R2_ENDPOINT_URL": "http://account.r2.cloudflarestorage.com"}
        self.assertFalse(configuration_status(env)["ready"])

    def test_uploads_private_mp4_and_verifies_size(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "sample-final.mp4"
            output.write_bytes(b"valid-test-mp4")
            client = FakeR2Client(output.stat().st_size)
            result = upload_mp4(output, "a" * 32, client=client, environ=self.env)

        self.assertEqual(client.uploaded[1], EXPECTED_BUCKET)
        self.assertEqual(client.uploaded[2], f"renders/{'a' * 32}/sample-final.mp4")
        self.assertEqual(client.uploaded[3]["ExtraArgs"]["ContentType"], "video/mp4")
        self.assertEqual(result["size_bytes"], len(b"valid-test-mp4"))
        self.assertTrue(result["verified"])
        self.assertFalse(result["public"])

    def test_rejects_size_mismatch_without_leaking_endpoint(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "sample.mp4"
            output.write_bytes(b"abc")
            with self.assertRaisesRegex(R2StorageError, "tamaño distinto"):
                upload_mp4(output, "b" * 32, client=FakeR2Client(99), environ=self.env)


if __name__ == "__main__":
    unittest.main()
