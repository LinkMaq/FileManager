import os
import re
import uuid
from pathlib import Path
from typing import List, Optional

from fastapi import FastAPI, File, UploadFile, HTTPException, Query, Form
from fastapi import Body
import json
from fastapi.responses import FileResponse, RedirectResponse
from urllib.parse import quote
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
import errno
import stat
import io


def get_root_dir() -> Path:
    env_root = os.getenv("FILE_MANAGER_ROOT", "")
    if env_root:
        # Disallow using the filesystem root as the application root to avoid
        # accidental exposure of the whole host filesystem. Allow other
        # absolute paths (e.g. mounted /data) but ensure they're resolved.
        r = Path(env_root).resolve()
        if r == Path('/'):
            # ignore insecure configuration and fall back to default
            return Path(os.getenv("FILE_MANAGER_DEFAULT_ROOT", "./data")).resolve()
        return r
    # Default: local ./data when run locally; in container we will mount to /data
    return Path(os.getenv("FILE_MANAGER_DEFAULT_ROOT", "./data")).resolve()


ROOT_DIR: Path = get_root_dir()
ROOT_DIR.mkdir(parents=True, exist_ok=True)
UPLOADS_DIR = ROOT_DIR / ".uploads"
UPLOADS_DIR.mkdir(parents=True, exist_ok=True)

# Maximum supported upload size (20 GiB)
MAX_UPLOAD_BYTES = 20 * 1024 ** 3


def resolve_safe_path(relative_path: str) -> Path:
    # Normalize to prevent traversal. Treat empty as "."
    safe_rel = (relative_path or ".").lstrip("/")
    # Build the candidate path and resolve symbolic links.
    abs_path = (ROOT_DIR / safe_rel).resolve()
    try:
        # This will raise ValueError if abs_path is not inside ROOT_DIR
        abs_path.relative_to(ROOT_DIR)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid path")
    return abs_path


def safe_filename(name: str) -> str:
    # Keep only the final name component to avoid any directory components.
    # Also disallow NUL bytes and control characters.
    if not name:
        return ""
    n = Path(name).name
    # strip control characters
    n = re.sub(r"[\x00-\x1f\x7f]+", "", n)
    return n


def validate_upload_id(upload_id: str) -> str:
    # Accept only UUIDs (v4 or otherwise) to prevent path traversal via uploadId
    try:
        # this will normalize and validate the UUID string
        u = uuid.UUID(upload_id)
        return str(u)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid uploadId")


def open_atomic(path: Path, mode: str = "wb", perms: int = 0o600):
    # Create and open a file with restrictive permissions atomically.
    # Returns a file-like object.
    flags = os.O_WRONLY | os.O_CREAT
    if "b" in mode:
        flags |= os.O_BINARY if hasattr(os, "O_BINARY") else 0
    if "x" in mode:
        flags |= os.O_EXCL
    if "a" in mode:
        flags |= os.O_APPEND
    if "+" in mode:
        flags |= os.O_RDWR
    # Truncate when writing normally
    if "w" in mode:
        flags |= os.O_TRUNC
    # Ensure parent exists
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(path), flags, perms)
    # Wrap fd in a python file object
    return os.fdopen(fd, mode)


def list_dir(target: Path):
    if not target.exists():
        raise HTTPException(status_code=404, detail="Path not found")
    if not target.is_dir():
        raise HTTPException(status_code=400, detail="Path is not a directory")
    items = []
    for entry in sorted(target.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower())):
        # Skip hidden files and directories (those starting with a dot)
        if entry.name.startswith('.'):
            continue
        stat = entry.stat()
        items.append({
            "name": entry.name,
            "isDir": entry.is_dir(),
            "size": 0 if entry.is_dir() else stat.st_size,
            "mtime": int(stat.st_mtime),
        })
    return {
        "cwd": str(target.relative_to(ROOT_DIR)) if target != ROOT_DIR else "",
        "items": items,
    }


app = FastAPI(title="Lightweight File Manager", version="1.0.0")

# Allow simple same-origin or local tools; keep permissive but simple
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/api/list")
def api_list(path: Optional[str] = Query(default="")):
    target = resolve_safe_path(path or "")
    return list_dir(target)


@app.get("/api/download")
def api_download(path: str = Query(...)):
    # Validate the requested path first
    _ = resolve_safe_path(path)
    # Redirect to a filename-based URL (so clients like wget will use the last URL segment
    # as the default filename). Keep slashes in the path when quoting so nested paths work.
    safe_rel = (path or "").lstrip("/")
    redirect_url = "/api/download/raw/" + quote(safe_rel, safe='/')
    return RedirectResponse(redirect_url)


@app.get("/api/download/raw/{file_path:path}")
def api_download_raw(file_path: str):
    target = resolve_safe_path(file_path)
    if not target.exists() or not target.is_file():
        raise HTTPException(status_code=404, detail="File not found")
    # Provide Content-Disposition (including RFC5987 filename*) as a fallback for clients
    # that respect it.
    filename = target.name
    filename_star = quote(filename, safe='')
    content_disposition = f"attachment; filename=\"{filename}\"; filename*=UTF-8''{filename_star}"
    headers = {"Content-Disposition": content_disposition}
    return FileResponse(str(target), headers=headers, media_type="application/octet-stream")


@app.post("/api/upload")
async def api_upload(
    path: Optional[str] = Query(default=""),
    files: List[UploadFile] = File(...),
):
    target_dir = resolve_safe_path(path or "")
    if not target_dir.exists():
        raise HTTPException(status_code=404, detail="Target path not found")
    if not target_dir.is_dir():
        raise HTTPException(status_code=400, detail="Target path is not a directory")
    for upload in files:
        fname = safe_filename(upload.filename)
        if not fname:
            raise HTTPException(status_code=400, detail="Invalid filename")
        dest = resolve_safe_path(str(Path(path or "") / fname))
        # refuse to overwrite symlinks or files outside root
        if dest.exists() and dest.is_symlink():
            raise HTTPException(status_code=400, detail="Refusing to overwrite symlink")
        if dest.exists() and dest.is_dir():
            raise HTTPException(status_code=400, detail=f"A directory named {fname} already exists")
        # Stream the upload to disk in chunks to support very large files without high memory usage.
        tmp_dest = dest.with_name(dest.name + ".part")
        try:
            # Use atomic open with restrictive perms
            with open_atomic(tmp_dest, mode="wb", perms=0o600) as f:
                chunk_size = 1024 * 1024  # 1MB
                while True:
                    chunk = await upload.read(chunk_size)
                    if not chunk:
                        break
                    f.write(chunk)
                f.flush()
                os.fsync(f.fileno())
            # final checks: do not overwrite symlink
            if dest.exists() and dest.is_symlink():
                tmp_dest.unlink()
                raise HTTPException(status_code=400, detail="Refusing to overwrite symlink")
            # Atomic replace
            os.replace(str(tmp_dest), str(dest))
            # ensure restrictive perms on final file
            try:
                os.chmod(str(dest), 0o600)
            except Exception:
                pass
        except HTTPException:
            raise
        except Exception:
            # Clean up partial file on error
            try:
                if tmp_dest.exists():
                    tmp_dest.unlink()
            except Exception:
                pass
            raise HTTPException(status_code=500, detail="Failed to save upload")
    return {"ok": True}


@app.post("/api/upload/init")
def api_upload_init(body: dict = Body(...)):
    # Initialize a resumable upload. Client may provide uploadId or leave blank.
    path = body.get("path", "")
    filename = body.get("filename")
    total_size = int(body.get("totalSize", 0))
    upload_id = body.get("uploadId") or str(uuid.uuid4())
    # If client provided an uploadId validate it, otherwise the generated one is fine
    if body.get("uploadId"):
        upload_id = validate_upload_id(upload_id)
    else:
        upload_id = str(uuid.UUID(upload_id))
    filename = safe_filename(filename)
    if not filename:
        raise HTTPException(status_code=400, detail="Missing filename")
    if total_size <= 0:
        raise HTTPException(status_code=400, detail="Invalid totalSize")
    if total_size > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=400, detail=f"Max upload size is {MAX_UPLOAD_BYTES} bytes")
    # validate parent exists
    parent = resolve_safe_path(path or "")
    if not parent.exists() or not parent.is_dir():
        raise HTTPException(status_code=400, detail="Parent directory invalid")
    meta = {
        "uploadId": upload_id,
        "path": path,
        "filename": filename,
        "totalSize": total_size,
    }
    meta_path = UPLOADS_DIR / (upload_id + ".json")
    # ensure uploads dir is inside ROOT_DIR
    try:
        meta_path.resolve().relative_to(UPLOADS_DIR)
    except Exception:
        raise HTTPException(status_code=500, detail="Server upload path invalid")
    with open_atomic(meta_path, mode="w", perms=0o600) as mf:
        json.dump(meta, mf)
    part_path = UPLOADS_DIR / (upload_id + ".part")
    # ensure empty file exists with restrictive perms
    if not part_path.exists():
        try:
            with open_atomic(part_path, mode="wb", perms=0o600):
                pass
        except Exception:
            raise HTTPException(status_code=500, detail="Failed to create upload part file")
    return {"uploadId": upload_id}


@app.post("/api/upload/chunk")
async def api_upload_chunk(
    uploadId: str = Form(...),
    offset: int = Form(...),
    chunk: UploadFile = File(...),
):
    # validate uploadId format
    uploadId = validate_upload_id(uploadId)
    meta_path = UPLOADS_DIR / (uploadId + ".json")
    if not meta_path.exists():
        raise HTTPException(status_code=404, detail="uploadId not found")
    # ensure meta_path is within uploads dir
    try:
        meta_path.resolve().relative_to(UPLOADS_DIR)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid uploadId")
    with meta_path.open("r", encoding="utf-8") as mf:
        meta = json.load(mf)
    part_path = UPLOADS_DIR / (uploadId + ".part")
    if part_path.exists() and part_path.is_symlink():
        raise HTTPException(status_code=400, detail="Corrupt upload state")
    # write chunk at offset
    try:
        # open/create with safe perms
        part_path.parent.mkdir(parents=True, exist_ok=True)
        if not part_path.exists():
            # create with restrictive perms
            with open_atomic(part_path, mode="wb", perms=0o600):
                pass
        # validate offset
        if offset < 0:
            raise HTTPException(status_code=400, detail="Invalid offset")
        # open in r+b
        with part_path.open("r+b") as f:
            f.seek(offset)
            # stream read and write
            read_size = 0
            while True:
                data = await chunk.read(1024 * 1024)
                if not data:
                    break
                f.write(data)
                read_size += len(data)
            f.flush()
            os.fsync(f.fileno())
            current_size = f.tell()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to write chunk")
    return {"ok": True, "received": current_size}


@app.get("/api/upload/status")
def api_upload_status(uploadId: str = Query(...)):
    uploadId = validate_upload_id(uploadId)
    meta_path = UPLOADS_DIR / (uploadId + ".json")
    if not meta_path.exists():
        raise HTTPException(status_code=404, detail="uploadId not found")
    with meta_path.open("r", encoding="utf-8") as mf:
        meta = json.load(mf)
    part_path = UPLOADS_DIR / (uploadId + ".part")
    received = part_path.stat().st_size if part_path.exists() else 0
    return {"uploadId": uploadId, "received": received, "totalSize": meta.get("totalSize")}


@app.post("/api/upload/complete")
def api_upload_complete(body: dict = Body(...)):
    upload_id = body.get("uploadId")
    if not upload_id:
        raise HTTPException(status_code=400, detail="Missing uploadId")
    upload_id = validate_upload_id(upload_id)
    meta_path = UPLOADS_DIR / (upload_id + ".json")
    if not meta_path.exists():
        raise HTTPException(status_code=404, detail="uploadId not found")
    with meta_path.open("r", encoding="utf-8") as mf:
        meta = json.load(mf)
    part_path = UPLOADS_DIR / (upload_id + ".part")
    if not part_path.exists() or part_path.is_symlink():
        raise HTTPException(status_code=404, detail="part file not found")
    received = part_path.stat().st_size
    total = int(meta.get("totalSize", 0))
    if received != total:
        raise HTTPException(status_code=400, detail=f"Incomplete upload: received {received} of {total}")
    # move to final destination
    filename = safe_filename(meta.get("filename"))
    if not filename:
        raise HTTPException(status_code=400, detail="Invalid filename in upload metadata")
    dest = resolve_safe_path(str(Path(meta.get("path", "")) / filename))
    # refuse to overwrite symlink
    if dest.exists() and dest.is_symlink():
        raise HTTPException(status_code=400, detail="Refusing to overwrite symlink")
    try:
        # atomic replace
        os.replace(str(part_path), str(dest))
        # ensure permissions
        try:
            os.chmod(str(dest), 0o600)
        except Exception:
            pass
        # remove meta
        try:
            meta_path.unlink()
        except Exception:
            pass
    except Exception:
        raise HTTPException(status_code=500, detail=f"Failed to finalize upload")
    return {"ok": True}


@app.post("/api/mkdir")
def api_mkdir(body: dict = Body(...)):
    path = body.get("path", "")
    name = body.get("name")
    if not name:
        raise HTTPException(status_code=400, detail="Missing name")
    # sanitize name and prevent directory components
    name = safe_filename(name)
    if not name:
        raise HTTPException(status_code=400, detail="Invalid directory name")
    parent = resolve_safe_path(path or "")
    if not parent.exists() or not parent.is_dir():
        raise HTTPException(status_code=400, detail="Parent directory invalid")
    target = resolve_safe_path(str(Path(path or "") / name))
    # avoid creating symlinked target
    if target.exists():
        raise HTTPException(status_code=400, detail="Target already exists")
    target.mkdir(parents=False, exist_ok=False)
    try:
        os.chmod(str(target), 0o700)
    except Exception:
        pass
    return {"ok": True}


@app.post("/api/rename")
def api_rename(body: dict = Body(...)):
    path = body.get("path", "")
    old_name = body.get("oldName")
    new_name = body.get("newName")
    if not old_name or not new_name:
        raise HTTPException(status_code=400, detail="Missing oldName or newName")
    old_name = safe_filename(old_name)
    new_name = safe_filename(new_name)
    if not old_name or not new_name:
        raise HTTPException(status_code=400, detail="Invalid names")
    source = resolve_safe_path(str(Path(path or "") / old_name))
    if not source.exists():
        raise HTTPException(status_code=404, detail="Source not found")
    dest = resolve_safe_path(str(Path(path or "") / new_name))
    if dest.exists():
        raise HTTPException(status_code=400, detail="Destination already exists")
    source.rename(dest)
    return {"ok": True}


@app.post("/api/delete")
def api_delete(body: dict = Body(...)):
    path = body.get("path", "")
    name = body.get("name")
    if not name:
        raise HTTPException(status_code=400, detail="Missing name")
    name = safe_filename(name)
    if not name:
        raise HTTPException(status_code=400, detail="Invalid name")
    target = resolve_safe_path(str(Path(path or "") / name))
    if not target.exists():
        raise HTTPException(status_code=404, detail="Target not found")
    if target.is_dir():
        # Only allow deleting empty directories to be safe
        try:
            target.rmdir()
        except OSError:
            raise HTTPException(status_code=400, detail="Directory not empty")
    else:
        target.unlink()
    return {"ok": True}


# Serve static frontend
static_dir = Path(__file__).parent / "static"
# Also expose a direct /iso route mapped to the server's iso directory so clients
# can download ISO files with clean filenames using URLs like
#   http://<host>/iso/Win11_25H2_Chinese_Simplified_x64.iso
iso_dir = ROOT_DIR / "iso"
iso_dir.mkdir(parents=True, exist_ok=True)
app.mount("/iso", StaticFiles(directory=str(iso_dir)), name="iso")

app.mount("/", StaticFiles(directory=str(static_dir), html=True), name="static")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("app.main:app", host="0.0.0.0", port=int(os.getenv("PORT", "8000")), reload=True)


