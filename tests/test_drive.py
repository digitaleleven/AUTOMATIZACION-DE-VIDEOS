import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from video_pipeline.config import PipelineConfig
from video_pipeline.drive import (
    DriveAccessError,
    create_test_file,
    download_drive_file,
    get_folder,
    picker_configuration,
    save_file_selections,
    save_folder_selection,
)
from video_pipeline.pipeline import sync_drive_media


class FakeResponse:
    def __init__(self, payload=None, *, status=200, content=b""):
        self.status_code = status
        self.headers = {}
        self.payload = payload
        self.content = content
        self.ok = 200 <= status < 300

    def json(self):
        return self.payload

    def iter_content(self, chunk_size):
        yield self.content


class DriveSelectionTests(unittest.TestCase):
    def test_picker_configuration_uses_the_single_file_scope(self):
        with patch.dict(
            os.environ,
            {
                "GOOGLE_DRIVE_CLIENT_ID": "client-id",
                "GOOGLE_DRIVE_API_KEY": "api-key",
                "GOOGLE_DRIVE_APP_ID": "project-number",
            },
            clear=True,
        ):
            settings = picker_configuration()
        self.assertEqual(settings["scope"], "https://www.googleapis.com/auth/drive.file")
        self.assertEqual(settings["client_id"], "client-id")

    def test_folder_and_file_selections_persist_only_ids_and_metadata(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "drive_folders.json"
            save_folder_selection("entrada", {"id": "folder-in", "name": "Entrada"}, path)
            save_folder_selection("procesados", {"id": "folder-out", "name": "Procesados"}, path)
            file_path = Path(directory) / "drive_files.json"
            selections = save_file_selections(
                "entrada",
                [{
                    "id": "file-1",
                    "name": "clip.mp4",
                    "mimeType": "video/mp4",
                    "parents": ["folder-in"],
                }],
                file_path,
            )
            self.assertEqual(selections["entrada"][0]["id"], "file-1")
            self.assertEqual(json.loads(path.read_text())["procesados"]["id"], "folder-out")
            self.assertNotIn("access_token", path.read_text())
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_folder_selection_rejects_nonfolder_results(self):
        session = Mock()
        session.request.return_value = FakeResponse({
            "id": "not-a-folder",
            "name": "Not a folder",
            "mimeType": "video/mp4",
        })
        with self.assertRaisesRegex(DriveAccessError, "no es una carpeta"):
            get_folder("not-a-folder", access_token="token", session=session)

    def test_download_creates_exclusive_file_and_never_overwrites(self):
        session = Mock()
        session.request.return_value = FakeResponse(content=b"drive-bytes")
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "video.mp4"
            download_drive_file("file-id", target, access_token="token", session=session)
            self.assertEqual(target.read_bytes(), b"drive-bytes")
            with self.assertRaisesRegex(DriveAccessError, "no se sobrescribió"):
                download_drive_file("file-id", target, access_token="token", session=session)
            self.assertEqual(target.read_bytes(), b"drive-bytes")
            self.assertEqual(session.request.call_count, 1)

    def test_test_file_creates_a_new_file_in_the_selected_parent(self):
        session = Mock()
        session.request.return_value = FakeResponse({
            "id": "new-file",
            "name": "prueba_conexion_drive_ab12cd34ef56.txt",
            "mimeType": "text/plain",
            "parents": ["processed-folder"],
        })
        result = create_test_file(
            "processed-folder",
            access_token="token",
            session=session,
        )
        self.assertEqual(result["id"], "new-file")
        request = session.request.call_args
        self.assertEqual(request.args[0], "POST")
        self.assertEqual(request.kwargs["json"]["parents"], ["processed-folder"])
        self.assertTrue(request.kwargs["json"]["name"].startswith("prueba_conexion_drive_"))


class DriveSyncTests(unittest.TestCase):
    def test_sync_downloads_selected_items_once_and_preserves_existing_downloads(self):
        folders = {
            "entrada": {"id": "folder-in", "name": "Entrada"},
            "logos": {"id": "folder-logos", "name": "Logos"},
        }
        items = {
            "entrada": [{
                "id": "video-123",
                "name": "clip.mp4",
                "mimeType": "video/mp4",
                "size": "4",
                "modifiedTime": "2026-10-23T10:00:00Z",
                "parents": ["folder-in"],
            }],
            "logos": [{
                "id": "logo-123",
                "name": "brand.png",
                "mimeType": "image/png",
                "size": "4",
                "modifiedTime": "2026-10-23T10:00:00Z",
                "parents": ["folder-logos"],
            }],
        }

        def fake_download(file_id, destination, **_kwargs):
            path = Path(destination)
            path.write_bytes(file_id.encode())
            return path

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = PipelineConfig(input_folder_id="folder-in", logos_folder_id="folder-logos")
            with patch("video_pipeline.pipeline.load_folder_selections", return_value=folders), patch(
                "video_pipeline.pipeline.load_file_selections", return_value=items
            ), patch(
                "video_pipeline.pipeline.get_drive_file",
                side_effect=lambda file_id, **_kwargs: next(
                    entry for selection in items.values() for entry in selection if entry["id"] == file_id
                ),
            ), patch(
                "video_pipeline.pipeline.download_drive_file", side_effect=fake_download
            ) as download:
                first = sync_drive_media(
                    config,
                    "token",
                    input_dir=root / "incoming",
                    logo_dir=root / "logos",
                    manifest_path=root / "manifest.json",
                )
                second = sync_drive_media(
                    config,
                    "token",
                    input_dir=root / "incoming",
                    logo_dir=root / "logos",
                    manifest_path=root / "manifest.json",
                )
                self.assertTrue(Path(first["videos"][0]["local_path"]).is_file())

        self.assertEqual([item["status"] for item in first["videos"]], ["downloaded"])
        self.assertEqual([item["status"] for item in first["logos"]], ["downloaded"])
        self.assertEqual([item["status"] for item in second["videos"]], ["already_downloaded"])
        self.assertEqual([item["status"] for item in second["logos"]], ["already_downloaded"])
        self.assertEqual(download.call_count, 2)

    def test_sync_requires_picker_selected_folders(self):
        with patch("video_pipeline.pipeline.load_folder_selections", return_value={}), patch(
            "video_pipeline.pipeline.load_file_selections", return_value={}
        ):
            with self.assertRaisesRegex(DriveAccessError, "Selecciona y valida"):
                sync_drive_media(PipelineConfig(), "token")

    def test_sync_refuses_a_file_that_is_not_in_its_selected_folder(self):
        folders = {
            "entrada": {"id": "folder-in", "name": "Entrada"},
            "logos": {"id": "folder-logos", "name": "Logos"},
        }
        items = {
            "entrada": [{
                "id": "video-123",
                "name": "clip.mp4",
                "mimeType": "video/mp4",
                "parents": ["different-folder"],
            }],
            "logos": [],
        }
        with tempfile.TemporaryDirectory() as directory, patch(
            "video_pipeline.pipeline.load_folder_selections", return_value=folders
        ), patch(
            "video_pipeline.pipeline.load_file_selections", return_value=items
        ), patch(
            "video_pipeline.pipeline.get_drive_file",
            return_value={
                "id": "video-123",
                "name": "clip.mp4",
                "mimeType": "video/mp4",
                "parents": ["different-folder"],
            },
        ), patch("video_pipeline.pipeline.download_drive_file") as download:
            with self.assertRaisesRegex(DriveAccessError, "ya no pertenece"):
                sync_drive_media(
                    PipelineConfig(input_folder_id="folder-in", logos_folder_id="folder-logos"),
                    "token",
                    input_dir=Path(directory) / "incoming",
                    logo_dir=Path(directory) / "logos",
                    manifest_path=Path(directory) / "manifest.json",
                )
        download.assert_not_called()


if __name__ == "__main__":
    unittest.main()
