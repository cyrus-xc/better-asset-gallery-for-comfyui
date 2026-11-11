"""Delete exact paths, then reconcile native history and asset references."""
import asyncio
import logging
from pathlib import Path

from sqlalchemy import false, select

from app.assets.database.models import Asset, AssetContent
from app.assets.database.queries.records import mark_contents_missing
from app.assets.services.asset_management import delete_asset_reference
from app.database.db import can_create_session, create_session, create_write_session
import folder_paths
from server import PromptServer

_lock = asyncio.Lock()
_REMOVED = object()
VIDEO_EXTENSIONS = {".mp4", ".webm", ".mov", ".mkv", ".avi"}


def resolve_file(item):
    kind = item.get("type", "output")
    if kind not in ("input", "output"):
        raise ValueError("Only input and output assets can be deleted")
    filename, subfolder = item.get("filename"), item.get("subfolder", "")
    if not isinstance(filename, str) or not filename or not isinstance(subfolder, str):
        raise ValueError("A filename and subfolder are required")
    if "\x00" in filename + subfolder:
        raise ValueError("Invalid filename")
    root = Path(folder_paths.get_directory_by_type(kind)).resolve()
    path = (root / subfolder / filename).resolve()
    if not path.is_relative_to(root) or path == root:
        raise ValueError("File must stay inside its asset directory")
    if path.exists() and not path.is_file():
        raise ValueError("Asset is not a file")
    return path, root, kind


def expand_job(job_id):
    if not isinstance(job_id, str) or not job_id:
        raise ValueError("Invalid job id")
    root = Path(folder_paths.get_output_directory()).resolve()
    queue = PromptServer.instance.prompt_queue
    with queue.mutex:
        paths = set(output_paths(queue.history.get(job_id, {}).get("outputs", {}), root))
    if can_create_session():
        with create_session() as session:
            filenames = session.scalars(select(AssetContent.path).join(Asset, Asset.content_id == AssetContent.id)
                                       .where(Asset.job_id == job_id, AssetContent.is_missing == false()))
            paths.update(path for filename in filenames if (path := Path(filename).resolve()).is_relative_to(root))
    if not paths:
        raise ValueError("This job has no saved output files")
    return [{"filename": path.name, "subfolder": path.parent.relative_to(root).as_posix(), "type": "output"} for path in paths]


def output_path(item, root):
    if not isinstance(item, dict) or item.get("type") != "output" or not isinstance(item.get("filename"), str):
        return None
    path = (root / item.get("subfolder", "") / item["filename"]).resolve()
    return path if path.is_relative_to(root) else None


def output_paths(value, root):
    path = output_path(value, root)
    if path is not None:
        yield path
    elif isinstance(value, dict):
        for child in value.values():
            yield from output_paths(child, root)
    elif isinstance(value, (list, tuple)):
        for child in value:
            yield from output_paths(child, root)


def prune(value, deleted, root):
    if output_path(value, root) in deleted:
        return _REMOVED
    if isinstance(value, dict):
        result = {key: child for key, item in value.items() if (child := prune(item, deleted, root)) is not _REMOVED}
        return _REMOVED if value and not result else result
    if isinstance(value, (list, tuple)):
        result = [child for item in value if (child := prune(item, deleted, root)) is not _REMOVED]
        return _REMOVED if value and not result else result
    return value


def reconcile_history(deleted, root):
    queue = PromptServer.instance.prompt_queue
    removed = updated = 0
    with queue.mutex:
        for job_id, record in list(queue.history.items()):
            outputs = record.get("outputs", {})
            if not deleted.intersection(output_paths(outputs, root)):
                continue
            kept = prune(outputs, deleted, root)
            paths = set() if kept is _REMOVED else set(output_paths(kept, root))
            if paths:
                record["outputs"] = kept
                updated += 1
            else:
                queue.delete_history_item(job_id)
                removed += 1
    return removed, updated


def asset_snapshot(deleted):
    if not can_create_session():
        return [], []
    names = {str(path) for path in deleted}
    for kind in ("input", "output"):
        logical_root = Path(folder_paths.get_directory_by_type(kind)).absolute()
        root = logical_root.resolve()
        names.update(str(logical_root / path.relative_to(root)) for path in deleted if path.is_relative_to(root))
    with create_session() as session:
        contents = list(session.scalars(select(AssetContent.id).where(AssetContent.path.in_(names))))
        ids = list(session.scalars(select(Asset.id).where(Asset.content_id.in_(contents))))
    return ids, contents


def remove_asset_records(ids, contents):
    for asset_id in ids:
        delete_asset_reference(asset_id)
    if contents:
        with create_write_session() as session:
            mark_contents_missing(session, contents)
            session.commit()


async def reconcile(deleted):
    root = Path(folder_paths.get_output_directory()).resolve()
    outputs = {path for path in deleted if path.is_relative_to(root)}
    ids, contents = await asyncio.to_thread(asset_snapshot, deleted)
    await asyncio.to_thread(remove_asset_records, ids, contents)
    removed, updated = reconcile_history(outputs, root)
    return {"assets_removed": len(ids), "history_removed": removed, "history_updated": updated}


async def delete_files(items):
    deleted, errors, targets = [], [], []
    removed_count = 0
    async with _lock:
        expanded = []
        for item in items:
            if isinstance(item, dict) and "job_id" in item:
                try:
                    expanded.extend(await asyncio.to_thread(expand_job, item["job_id"]))
                except ValueError as exc:
                    errors.append({"filename": item["job_id"], "error": str(exc)})
            else:
                expanded.append(item)
        for item in expanded:
            try:
                if not isinstance(item, dict):
                    raise ValueError("Invalid asset descriptor")
                path, root, kind = resolve_file(item)
                if path in targets:
                    continue
                targets.append(path)
                existed = path.is_file()
                path.unlink(missing_ok=True)
                deleted.append(path)
                removed_count += int(existed)
                if path.suffix.lower() in VIDEO_EXTENSIONS:
                    sidecar = path.with_suffix(".png")
                    if sidecar.resolve().is_relative_to(root) and (not sidecar.exists() or sidecar.is_file()):
                        try:
                            sidecar.unlink(missing_ok=True)
                            deleted.append(sidecar.resolve())
                        except OSError as exc:
                            errors.append({"filename": sidecar.name, "error": str(exc)})
            except (ValueError, OSError) as exc:
                errors.append({"filename": item.get("filename", "") if isinstance(item, dict) else "", "error": str(exc)})
        sync = {"assets_removed": 0, "history_removed": 0, "history_updated": 0}
        sync_error = None
        if deleted:
            try:
                sync = await reconcile(set(deleted))
            except Exception:
                logging.exception("Better Asset Gallery could not reconcile deleted files")
                sync_error = "Files were removed, but history cleanup failed. Retry deletion to finish cleanup."
        files = []
        for path in set(deleted):
            for kind in ("input", "output"):
                root = Path(folder_paths.get_directory_by_type(kind)).resolve()
                if path.is_relative_to(root):
                    relative = path.relative_to(root)
                    files.append({"filename": relative.name, "subfolder": relative.parent.as_posix() if len(relative.parts) > 1 else "", "type": kind})
                    break
    return {"deleted": removed_count, "files": files, "errors": errors, "sync": sync, "sync_error": sync_error}
