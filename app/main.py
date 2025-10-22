import os
from pathlib import Path
from typing import List, Optional

from fastapi import FastAPI, File, UploadFile, HTTPException, Query, Form
from fastapi import Body
import uuid
import json
from fastapi.responses import FileResponse, RedirectResponse
from urllib.parse import quote
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles


def get_root_dir() -> Path:
    env_root = os.getenv("FILE_MANAGER_ROOT", "")
    if env_root:
        return Path(env_root).resolve()
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
    abs_path = (ROOT_DIR / safe_rel).resolve()
    if not str(abs_path).startswith(str(ROOT_DIR)):
        raise HTTPException(status_code=400, detail="Invalid path")
    return abs_path


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
        dest = resolve_safe_path(str(Path(path or "") / upload.filename))
        if dest.exists() and dest.is_dir():
            raise HTTPException(status_code=400, detail=f"A directory named {upload.filename} already exists")
        # Stream the upload to disk in chunks to support very large files without high memory usage.
        tmp_dest = dest.with_name(dest.name + ".part")
        try:
            # Ensure parent exists
            tmp_dest.parent.mkdir(parents=True, exist_ok=True)
            with tmp_dest.open("wb") as f:
                chunk_size = 1024 * 1024  # 1MB
                while True:
                    chunk = await upload.read(chunk_size)
                    if not chunk:
                        break
                    f.write(chunk)
            # Atomic rename to final destination
            tmp_dest.replace(dest)
        except Exception:
            # Clean up partial file on error
            try:
                if tmp_dest.exists():
                    tmp_dest.unlink()
            except Exception:
                pass
            raise
    return {"ok": True}


@app.post("/api/upload/init")
def api_upload_init(body: dict = Body(...)):
    # Initialize a resumable upload. Client may provide uploadId or leave blank.
    path = body.get("path", "")
    filename = body.get("filename")
    total_size = int(body.get("totalSize", 0))
    upload_id = body.get("uploadId") or str(uuid.uuid4())
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
    with meta_path.open("w", encoding="utf-8") as mf:
        json.dump(meta, mf)
    part_path = UPLOADS_DIR / (upload_id + ".part")
    # ensure empty file exists
    if not part_path.exists():
        part_path.parent.mkdir(parents=True, exist_ok=True)
        with part_path.open("wb"):
            pass
    return {"uploadId": upload_id}


@app.post("/api/upload/chunk")
async def api_upload_chunk(
    uploadId: str = Form(...),
    offset: int = Form(...),
    chunk: UploadFile = File(...),
):
    meta_path = UPLOADS_DIR / (uploadId + ".json")
    if not meta_path.exists():
        raise HTTPException(status_code=404, detail="uploadId not found")
    with meta_path.open("r", encoding="utf-8") as mf:
        meta = json.load(mf)
    part_path = UPLOADS_DIR / (uploadId + ".part")
    # write chunk at offset
    try:
        # open/create in r+b mode
        part_path.parent.mkdir(parents=True, exist_ok=True)
        mode = "r+b" if part_path.exists() else "w+b"
        with part_path.open(mode) as f:
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
        raise HTTPException(status_code=500, detail=f"Failed to write chunk: {e}")
    return {"ok": True, "received": current_size}


@app.get("/api/upload/status")
def api_upload_status(uploadId: str = Query(...)):
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
    meta_path = UPLOADS_DIR / (upload_id + ".json")
    if not meta_path.exists():
        raise HTTPException(status_code=404, detail="uploadId not found")
    with meta_path.open("r", encoding="utf-8") as mf:
        meta = json.load(mf)
    part_path = UPLOADS_DIR / (upload_id + ".part")
    if not part_path.exists():
        raise HTTPException(status_code=404, detail="part file not found")
    received = part_path.stat().st_size
    total = int(meta.get("totalSize", 0))
    if received != total:
        raise HTTPException(status_code=400, detail=f"Incomplete upload: received {received} of {total}")
    # move to final destination
    dest = resolve_safe_path(str(Path(meta.get("path", "")) / meta.get("filename")))
    try:
        part_path.replace(dest)
        # remove meta
        meta_path.unlink()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to finalize upload: {e}")
    return {"ok": True}


@app.post("/api/mkdir")
def api_mkdir(body: dict = Body(...)):
    path = body.get("path", "")
    name = body.get("name")
    if not name:
        raise HTTPException(status_code=400, detail="Missing name")
    parent = resolve_safe_path(path or "")
    if not parent.exists() or not parent.is_dir():
        raise HTTPException(status_code=400, detail="Parent directory invalid")
    target = resolve_safe_path(str(Path(path or "") / name))
    target.mkdir(parents=False, exist_ok=False)
    return {"ok": True}


@app.post("/api/rename")
def api_rename(body: dict = Body(...)):
    path = body.get("path", "")
    old_name = body.get("oldName")
    new_name = body.get("newName")
    if not old_name or not new_name:
        raise HTTPException(status_code=400, detail="Missing oldName or newName")
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


