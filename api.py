import asyncio
import json

from aiohttp import web

from .deletion import delete_files, resolve_file
from .metadata import read_metadata


def setup_routes(routes):
    @routes.post("/better-assets/delete")
    async def delete_assets(request):
        try:
            body = await request.json()
        except (json.JSONDecodeError, UnicodeDecodeError):
            return web.json_response({"error": "Expected a JSON object"}, status=400)
        if not isinstance(body, dict) or not isinstance(body.get("files"), list) or not body["files"]:
            return web.json_response({"error": "files must be a non-empty list of asset descriptors"}, status=400)
        result = await delete_files(body["files"])
        return web.json_response(result, status=500 if result["sync_error"] else 200)

    @routes.get("/better-assets/metadata")
    async def asset_metadata(request):
        try:
            path, root, kind = resolve_file(dict(request.query))
            result = await asyncio.to_thread(read_metadata, path, root, kind)
        except ValueError as exc:
            return web.json_response({"error": str(exc)}, status=400)
        except FileNotFoundError:
            return web.json_response({"error": "File no longer exists"}, status=404)
        return web.json_response(result)
