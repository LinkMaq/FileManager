import os
from pathlib import Path
from typing import List, Optional

from fastapi import FastAPI, File, UploadFile, HTTPException, Query
from fastapi import Body
from fastapi.responses import FileResponse
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
    target = resolve_safe_path(path)
    if not target.exists() or not target.is_file():
        raise HTTPException(status_code=404, detail="File not found")
    return FileResponse(str(target), filename=target.name)


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
        with dest.open("wb") as f:
            f.write(await upload.read())
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
app.mount("/", StaticFiles(directory=str(static_dir), html=True), name="static")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("app.main:app", host="0.0.0.0", port=int(os.getenv("PORT", "8000")), reload=True)


