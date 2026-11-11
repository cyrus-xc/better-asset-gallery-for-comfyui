"""Better Asset Gallery: native ComfyUI assets with file deletion and metadata."""
from server import PromptServer

from .api import setup_routes

NODE_CLASS_MAPPINGS = {}
WEB_DIRECTORY = "./web"

setup_routes(PromptServer.instance.routes)

__all__ = ["NODE_CLASS_MAPPINGS", "WEB_DIRECTORY"]
