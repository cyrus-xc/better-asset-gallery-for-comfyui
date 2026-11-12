import asyncio
import importlib
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import patch
from uuid import uuid4

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from PIL import Image
from PIL.PngImagePlugin import PngInfo
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import sessionmaker

PLUGIN = Path(__file__).resolve().parents[1]
CORE = Path(os.environ["COMFYUI_PATH"])
sys.path.insert(0, str(CORE))
package = ModuleType("better_assets_tests")
package.__path__ = [str(PLUGIN)]
sys.modules[package.__name__] = package
server = ModuleType("server")
server.PromptServer = SimpleNamespace(instance=None)
previous_server = sys.modules.get("server")
sys.modules["server"] = server
try:
    deletion = importlib.import_module(package.__name__ + ".deletion")
    metadata = importlib.import_module(package.__name__ + ".metadata")
    routes = importlib.import_module(package.__name__ + ".api")
finally:
    if previous_server is None:
        sys.modules.pop("server", None)
    else:
        sys.modules["server"] = previous_server
from app.database import db as core_db
from app.database.models import Base
from app.assets.database.models import Asset, AssetContent
import folder_paths


class Queue:
    def __init__(self):
        self.mutex = threading.RLock()
        self.history = {}

    def delete_history_item(self, job_id):
        with self.mutex:
            self.history.pop(job_id, None)


class GalleryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.output, self.input = self.root / "output", self.root / "input"
        self.output.mkdir()
        self.input.mkdir()
        self.patch_roots = patch.object(folder_paths, "get_directory_by_type", side_effect=lambda kind: str(self.root / kind))
        self.patch_output = patch.object(folder_paths, "get_output_directory", return_value=str(self.output))
        self.patch_roots.start()
        self.patch_output.start()
        self.engine = create_engine("sqlite:///" + str(self.root / "assets.sqlite"))

        @event.listens_for(self.engine, "connect")
        def foreign_keys(connection, record):
            connection.execute("PRAGMA foreign_keys=ON")

        Base.metadata.create_all(self.engine)
        self.session = sessionmaker(bind=self.engine)
        self.old_sessions = core_db.Session, core_db.WriteSession
        core_db.Session = core_db.WriteSession = self.session
        self.queue = Queue()
        server.PromptServer.instance = SimpleNamespace(prompt_queue=self.queue)
        deletion._lock = asyncio.Lock()
        definitions = web.RouteTableDef()
        routes.setup_routes(definitions)
        app = web.Application()
        app.add_routes(definitions)
        self.client = TestClient(TestServer(app))
        await self.client.start_server()
        self.addAsyncCleanup(self.cleanup)

    async def cleanup(self):
        await self.client.close()
        core_db.Session, core_db.WriteSession = self.old_sessions
        self.engine.dispose()
        self.patch_roots.stop()
        self.patch_output.stop()
        self.temp.cleanup()

    def image(self, relative, *, kind="output", graph=None, exif=False):
        path = self.root / kind / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        image = Image.new("RGB", (32, 24), (80, 120, 200))
        if exif:
            data = Image.Exif()
            data[0x110] = "prompt:" + json.dumps(graph)
            image.save(path, exif=data)
        else:
            info = PngInfo()
            if graph is not None:
                info.add_text("prompt", json.dumps(graph, ensure_ascii=False))
            image.save(path, pnginfo=info)
        return path

    def job(self, names, *, history=True, content_hash=None, graph=None):
        job_id = str(uuid4())
        images = []
        for relative in names:
            path = self.image(relative, graph=graph)
            images.append({"filename": path.name, "subfolder": path.parent.relative_to(self.output).as_posix(), "type": "output"})
            self.asset(path, job_id, content_hash=content_hash)
        if history:
            self.queue.history[job_id] = {"prompt": [0, job_id, graph or {}, {}, ["9"]], "outputs": {"9": {"images": images}}}
        return job_id

    def asset(self, path, job_id=None, *, content_hash=None):
        with self.session() as session:
            content = AssetContent(id=str(uuid4()), path=str(path), hash=content_hash, size_bytes=path.stat().st_size)
            session.add(content)
            session.flush()
            record = Asset(id=str(uuid4()), content_id=content.id, name=path.name, job_id=job_id)
            session.add(record)
            session.commit()

    async def delete(self, *files):
        items = [{"filename": Path(name).name, "subfolder": str(Path(name).parent), "type": "output"} if isinstance(name, str) else name for name in files]
        response = await self.client.post("/better-assets/delete", json={"files": items})
        return response.status, await response.json()

    def live_paths(self):
        with self.session() as session:
            return set(session.scalars(select(AssetContent.path).where(AssetContent.is_missing.is_(False))))

    async def test_delete_one_then_last_output_reconciles_history_and_assets(self):
        job_id = self.job(["album/a.png", "album/b.png"])
        status, result = await self.delete("album/a.png")
        self.assertEqual((status, result["deleted"]), (200, 1))
        self.assertFalse((self.output / "album/a.png").exists())
        self.assertEqual(self.queue.history[job_id]["outputs"]["9"]["images"][0]["filename"], "b.png")
        self.assertEqual(self.live_paths(), {str(self.output / "album/b.png")})
        await self.delete("album/b.png")
        self.assertNotIn(job_id, self.queue.history)
        with self.session() as session:
            self.assertEqual(list(session.scalars(select(Asset.id))), [])

    async def test_other_path_with_same_hash_and_basename_is_preserved(self):
        first = self.job(["first/same.png"], content_hash="same-bytes")
        other = self.job(["other/same.png"], content_hash="same-bytes")
        await self.delete("first/same.png")
        self.assertNotIn(first, self.queue.history)
        self.assertIn(other, self.queue.history)
        self.assertTrue((self.output / "other/same.png").exists())

    async def test_input_deletion_does_not_change_output_history(self):
        job_id = self.job(["same.png"])
        path = self.image("same.png", kind="input")
        self.asset(path)
        _, result = await self.delete({"filename": "same.png", "type": "input"})
        self.assertEqual(result["deleted"], 1)
        self.assertFalse(path.exists())
        self.assertIn(job_id, self.queue.history)
        self.assertEqual(self.live_paths(), {str(self.output / "same.png")})

    async def test_missing_file_cleanup_is_idempotent(self):
        job_id = self.job(["gone.png"])
        (self.output / "gone.png").unlink()
        _, result = await self.delete("gone.png")
        self.assertEqual(result["deleted"], 0)
        self.assertNotIn(job_id, self.queue.history)
        _, again = await self.delete("gone.png")
        self.assertEqual(again["deleted"], 0)
        self.assertFalse(again["errors"])

    async def test_batch_only_reconciles_files_that_were_removed(self):
        removed = self.job(["a.png"])
        failed = self.job(["b.png"])
        original_unlink = Path.unlink

        def unlink(path, *args, **kwargs):
            if path.name == "b.png":
                raise PermissionError("test: read-only file")
            return original_unlink(path, *args, **kwargs)

        with patch.object(Path, "unlink", unlink):
            _, result = await self.delete("a.png", "b.png", {"filename": "../outside.png", "type": "output"})
        self.assertEqual(result["deleted"], 1)
        self.assertEqual(len(result["errors"]), 2)
        self.assertNotIn(removed, self.queue.history)
        self.assertIn(failed, self.queue.history)
        self.assertTrue((self.output / "b.png").exists())

    async def test_reconciliation_failure_can_be_retried(self):
        job_id = self.job(["retry.png"])
        with patch.object(deletion, "remove_asset_records", side_effect=OSError("test: database unavailable")), self.assertLogs(level="ERROR"):
            status, result = await self.delete("retry.png")
        self.assertEqual(status, 500)
        self.assertTrue(result["sync_error"])
        self.assertIn(job_id, self.queue.history)
        status, result = await self.delete("retry.png")
        self.assertEqual(status, 200)
        self.assertEqual(result["deleted"], 0)
        self.assertNotIn(job_id, self.queue.history)

    async def test_concurrent_sibling_deletes_leave_no_stale_job(self):
        job_id = self.job(["a.png", "b.png"])
        results = await asyncio.gather(self.delete("a.png"), self.delete("b.png"))
        self.assertTrue(all(status == 200 for status, _ in results))
        self.assertNotIn(job_id, self.queue.history)
        self.assertFalse(self.live_paths())

    async def test_grouped_job_deletion_expands_all_saved_outputs(self):
        job_id = self.job(["a.png", "b.png"])
        other = self.job(["c.png"])
        _, result = await self.delete({"job_id": job_id})
        self.assertEqual(result["deleted"], 2)
        self.assertNotIn(job_id, self.queue.history)
        self.assertIn(other, self.queue.history)

    async def test_grouped_assets_resolve_without_history(self):
        job_id = self.job(["a.png", "b.png"], history=False)
        _, result = await self.delete({"job_id": job_id})
        self.assertEqual(result["deleted"], 2)
        self.assertFalse(self.live_paths())

    async def test_disabled_asset_database_still_cleans_history(self):
        job_id = self.job(["a.png"])
        with patch.object(deletion, "can_create_session", return_value=False):
            _, result = await self.delete("a.png")
        self.assertEqual(result["deleted"], 1)
        self.assertNotIn(job_id, self.queue.history)

    async def test_symlink_cannot_delete_outside_asset_roots(self):
        outside = self.root / "private.png"
        outside.write_bytes(b"preserve")
        (self.output / "link.png").symlink_to(outside)
        _, result = await self.delete("link.png", {"filename": "/etc/passwd", "type": "output"})
        self.assertEqual(result["deleted"], 0)
        self.assertEqual(len(result["errors"]), 2)
        self.assertEqual(outside.read_bytes(), b"preserve")

    async def test_video_sidecar_is_reconciled_together(self):
        job_id = self.job(["movie.png"])
        video = self.output / "movie.mp4"
        video.write_bytes(b"test fixture video")
        self.asset(video, job_id)
        self.queue.history[job_id]["outputs"]["9"]["videos"] = [{"filename": "movie.mp4", "subfolder": "", "type": "output"}]
        _, result = await self.delete("movie.mp4")
        self.assertEqual(len(result["files"]), 2)
        self.assertFalse(video.exists())
        self.assertFalse((self.output / "movie.png").exists())
        self.assertNotIn(job_id, self.queue.history)

    async def test_temp_previews_do_not_keep_an_empty_completed_job(self):
        job_id = self.job(["a.png"])
        self.queue.history[job_id]["outputs"]["preview"] = {"images": [{"filename": "preview.png", "type": "temp"}]}
        await self.delete("a.png")
        self.assertNotIn(job_id, self.queue.history)

    async def test_bad_request_and_directory_targets_are_rejected(self):
        response = await self.client.post("/better-assets/delete", json={"files": []})
        self.assertEqual(response.status, 400)
        (self.output / "directory").mkdir()
        _, result = await self.delete("directory", {"filename": "asset.bin", "type": "models"})
        self.assertEqual(len(result["errors"]), 2)
        self.assertTrue((self.output / "directory").is_dir())

    async def test_png_metadata_includes_qwen_instruction_and_exact_seed(self):
        graph = {
            "1": {"class_type": "CLIPTextEncodeQwenImageEditPlus", "inputs": {"prompt": "保留人物，修改背景。"}},
            "2": {"class_type": "KSampler", "inputs": {"positive": ["1", 0], "seed": 1844674407370955161, "cfg": 0, "steps": 20}},
            "3": {"class_type": "UNETLoader", "inputs": {"unet_name": "qwen-image.gguf"}},
        }
        self.image("qwen.png", graph=graph)
        response = await self.client.get("/better-assets/metadata?filename=qwen.png&type=output")
        result = await response.json()
        self.assertEqual(result["source"], "embedded")
        self.assertEqual(result["prompts"][0]["text"], "保留人物，修改背景。")
        self.assertEqual(result["prompts"][0]["role"], "positive")
        self.assertIn({"field": "seed", "value": "1844674407370955161", "node_id": "2"}, result["parameters"])
        self.assertEqual(result["models"][0]["name"], "qwen-image.gguf")
        self.assertEqual((result["file"]["width"], result["file"]["height"]), (32, 24))

    async def test_qwen_dual_outputs_keep_positive_and_negative_separate(self):
        for negative in ("", "  ", "blurry, low quality"):
            for reverse in (False, True):
                with self.subTest(negative=negative, reverse=reverse):
                    graph = {
                        "4": {"class_type": "TextEncodeQwenImage21", "inputs": {"prompt": "a quiet lake", "negative_prompt": negative}},
                        "6": {"class_type": "KSampler", "inputs": {"positive": ["4", int(reverse)], "negative": ["4", int(not reverse)]}},
                    }
                    self.image("paired.png", graph=graph)
                    response = await self.client.get("/better-assets/metadata?filename=paired.png&type=output")
                    result = await response.json()
                    expected = [("prompt", "negative" if reverse else "positive", "a quiet lake")]
                    if negative.strip() or not reverse:
                        expected.append(("negative_prompt", "positive" if reverse else "negative", negative.strip()))
                    self.assertEqual(sorted((p["field"], p["role"], p["text"]) for p in result["prompts"]), sorted(expected))

    async def test_qwen_linked_text_through_conditioning_node_keeps_output_role(self):
        graph = {
            "1": {"class_type": "PrimitiveString", "inputs": {"value": "a quiet lake"}},
            "2": {"class_type": "PrimitiveString", "inputs": {"value": "blurry"}},
            "4": {"class_type": "TextEncodeQwenImage21", "inputs": {"prompt": ["1", 0], "negative_prompt": ["2", 0]}},
            "5": {"class_type": "ConditioningSetTimestepRange", "inputs": {"conditioning": ["4", 1], "start": 0, "end": 1}},
            "6": {"class_type": "KSampler", "inputs": {"positive": ["4", 0], "negative": ["5", 0]}},
        }
        self.image("linked.png", graph=graph)
        response = await self.client.get("/better-assets/metadata?filename=linked.png&type=output")
        result = await response.json()
        self.assertEqual([(p["node_id"], p["role"], p["text"]) for p in result["prompts"]], [("1", "positive", "a quiet lake"), ("2", "negative", "blurry")])

    async def test_qwen_prompt_fields_without_sampler_have_distinct_roles(self):
        graph = {"4": {"class_type": "TextEncodeQwenImage21", "inputs": {"prompt": "a quiet lake", "negative_prompt": "blurry"}}}
        self.image("unlinked.png", graph=graph)
        response = await self.client.get("/better-assets/metadata?filename=unlinked.png&type=output")
        result = await response.json()
        self.assertEqual([(p["field"], p["role"]) for p in result["prompts"]], [("prompt", "positive"), ("negative_prompt", "negative")])

    async def test_empty_linked_negative_prompt_is_distinct_from_missing_metadata(self):
        graph = {
            "1": {"class_type": "PrimitiveString", "inputs": {"value": "  "}},
            "2": {"class_type": "CLIPTextEncode", "inputs": {"text": ["1", 0]}},
            "3": {"class_type": "KSampler", "inputs": {"negative": ["2", 0]}},
        }
        self.image("empty-negative.png", graph=graph)
        response = await self.client.get("/better-assets/metadata?filename=empty-negative.png&type=output")
        result = await response.json()
        self.assertEqual([(p["node_id"], p["role"], p["text"]) for p in result["prompts"]], [("1", "negative", "")])
        self.image("no-prompt-data.png")
        response = await self.client.get("/better-assets/metadata?filename=no-prompt-data.png&type=output")
        result = await response.json()
        self.assertEqual(result["prompts"], [])

    async def test_history_metadata_without_embedded_prompt(self):
        graph = {"1": {"class_type": "CLIPTextEncode", "inputs": {"text": "a quiet lake"}},
                 "2": {"class_type": "ConditioningZeroOut", "inputs": {"conditioning": ["1", 0]}},
                 "3": {"class_type": "KSampler", "inputs": {"positive": ["1", 0], "negative": ["2", 0]}}}
        job_id = self.job(["plain.png"])
        self.queue.history[job_id]["prompt"][2] = graph
        response = await self.client.get("/better-assets/metadata?filename=plain.png&type=output")
        result = await response.json()
        self.assertEqual(result["source"], "history")
        self.assertEqual([item["role"] for item in result["prompts"]], ["positive"])

    async def test_webp_exif_prompt_and_missing_metadata(self):
        graph = {"1": {"class_type": "CLIPTextEncode", "inputs": {"text": "a lake"}}}
        self.image("example.webp", graph=graph, exif=True)
        response = await self.client.get("/better-assets/metadata?filename=example.webp&type=output")
        self.assertEqual((await response.json())["prompts"][0]["text"], "a lake")
        self.image("plain.png")
        response = await self.client.get("/better-assets/metadata?filename=plain.png&type=output")
        result = await response.json()
        self.assertIsNone(result["source"])
        self.assertEqual(result["prompts"], [])


if __name__ == "__main__":
    unittest.main()
