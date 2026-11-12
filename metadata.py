"""Read saved metadata and trace prompt text through a ComfyUI API graph."""
import json
from pathlib import Path

from PIL import Image, UnidentifiedImageError

from server import PromptServer

from .deletion import output_paths


TEXT_FIELDS = {"text", "text_g", "text_l", "prompt", "instruction", "instructions"}
MODEL_FIELDS = {"ckpt_name", "unet_name", "vae_name", "clip_name", "clip_name1", "clip_name2", "lora_name", "model_name"}
PARAMETER_FIELDS = {"steps", "cfg", "sampler_name", "scheduler", "seed", "noise_seed", "denoise", "strength_model", "strength_clip"}


def parse_json(value):
    if not isinstance(value, str):
        return value
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return value


def embedded_metadata(path):
    metadata, image_info = {}, {}
    try:
        with Image.open(path) as image:
            image_info = {"width": image.width, "height": image.height, "format": image.format, "mode": image.mode}
            for key in ("prompt", "workflow", "parameters"):
                if key in image.info:
                    metadata[key] = parse_json(image.info[key])
            exif = image.getexif()
            for tag, key in ((0x110, "prompt"), (0x10F, "workflow")):
                if key not in metadata and isinstance(exif.get(tag), str):
                    value = exif[tag]
                    if value.startswith(key + ":"):
                        value = value[len(key) + 1:]
                    metadata[key] = parse_json(value)
            comment = exif.get(0x9286)
            if isinstance(comment, bytes):
                if comment.startswith(b"UNICODE\x00"):
                    comment = comment[8:].decode("utf-16-be", errors="replace")
                else:
                    comment = comment.removeprefix(b"ASCII\x00\x00\x00").decode("utf-8", errors="replace")
            if isinstance(comment, str) and comment.strip():
                metadata.setdefault("parameters", comment)
    except (UnidentifiedImageError, OSError):
        pass
    return metadata, image_info


def history_metadata(path, output_root):
    queue = PromptServer.instance.prompt_queue
    with queue.mutex:
        for job_id, record in queue.history.items():
            if path not in set(output_paths(record.get("outputs", {}), output_root)):
                continue
            prompt = record.get("prompt", [])
            graph = prompt[2] if len(prompt) > 2 and isinstance(prompt[2], dict) else None
            extra = prompt[3] if len(prompt) > 3 and isinstance(prompt[3], dict) else {}
            return {"prompt": graph, "workflow": extra.get("extra_pnginfo", {}).get("workflow"), "job_id": job_id}
    return {}


def prompt_info(graph):
    if not isinstance(graph, dict):
        return {"prompts": [], "models": [], "parameters": []}
    nodes = {str(key): value for key, value in graph.items() if isinstance(value, dict) and isinstance(value.get("inputs"), dict)}
    prompts, models, parameters = [], [], []
    seen_prompts, seen_models = set(), set()

    def reference(value):
        return (str(value[0]), value[1]) if isinstance(value, list) and len(value) == 2 and str(value[0]) in nodes and isinstance(value[1], int) else None

    def text_inputs(link, role, seen):
        if link in seen:
            return
        seen = seen | {link}
        node_id, output_index = link
        node = nodes[node_id]
        if node.get("class_type") == "ConditioningZeroOut":
            return
        fields = node["inputs"]
        if node.get("class_type") == "TextEncodeQwenImage21":
            field = {0: "prompt", 1: "negative_prompt"}.get(output_index)
            fields = {field: node["inputs"].get(field)} if field else {}
        for field, value in fields.items():
            linked = reference(value)
            if linked:
                yield from text_inputs(linked, role, seen)
            elif field in TEXT_FIELDS | {"value", "negative_prompt"} and isinstance(value, str) and value.strip():
                yield node_id, field, value.strip(), role

    def add_prompt(node_id, field, text, role):
        key = (node_id, field, role)
        if key in seen_prompts:
            return
        seen_prompts.add(key)
        node = nodes[node_id]
        prompts.append({"node_id": node_id, "node_type": node.get("class_type", ""), "field": field, "role": role, "text": text})

    for node_id, node in nodes.items():
        for role in ("positive", "negative"):
            linked = reference(node["inputs"].get(role))
            if linked:
                for values in text_inputs(linked, role, set()):
                    add_prompt(*values)
    used = {(entry["node_id"], entry["field"]) for entry in prompts}
    for node_id, node in nodes.items():
        node_type = node.get("class_type", "")
        text_roles = {"prompt": "positive", "negative_prompt": "negative"} if node_type == "TextEncodeQwenImage21" else {
            field: "instruction" if "qwen" in node_type.lower() else "text" for field in TEXT_FIELDS
        }
        for field, value in node["inputs"].items():
            if field in text_roles and isinstance(value, str) and value.strip() and (node_id, field) not in used:
                add_prompt(node_id, field, value.strip(), text_roles[field])
            if field in MODEL_FIELDS and isinstance(value, str) and value:
                key = (field, value)
                if key not in seen_models:
                    seen_models.add(key)
                    models.append({"field": field, "name": value, "node_id": node_id})
            if field in PARAMETER_FIELDS and isinstance(value, (str, int, float)):
                parameters.append({"field": field, "value": str(value) if "seed" in field else value, "node_id": node_id})
    return {"prompts": prompts, "models": models, "parameters": parameters}


def read_metadata(path, root, kind):
    stat = path.stat()
    metadata, image_info = embedded_metadata(path)
    history = history_metadata(path, root) if kind == "output" else {}
    graph = metadata.get("prompt") or history.get("prompt")
    workflow = metadata.get("workflow") or history.get("workflow")
    result = {
        "file": {"filename": path.name, "subfolder": path.parent.relative_to(root).as_posix() if path.parent != root else "",
                 "type": kind, "size": stat.st_size, "modified": stat.st_mtime, **image_info},
        "job_id": history.get("job_id"),
        "source": "embedded" if metadata.get("prompt") else "history" if graph else None,
        **prompt_info(graph),
        "parameters_text": metadata.get("parameters") if isinstance(metadata.get("parameters"), str) else None,
        "prompt_json": json.dumps(graph, ensure_ascii=False, indent=2) if graph else None,
        "workflow_json": json.dumps(workflow, ensure_ascii=False, indent=2) if workflow else None,
    }
    return result
