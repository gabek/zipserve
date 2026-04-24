import hashlib
import os
import shutil
import zipfile
from pathlib import Path
from urllib.parse import quote, urlparse

import requests
from flask import Flask, abort, redirect, request, send_from_directory

app = Flask(__name__)

CACHE_DIR = Path(os.environ.get("ZIPSERVE_CACHE_DIR", "/tmp/zipserve-cache"))
CACHE_DIR.mkdir(parents=True, exist_ok=True)

GITHUB_HOSTS = {"github.com", "api.github.com", "codeload.github.com", "raw.githubusercontent.com"}
MAX_ZIP_BYTES = int(os.environ.get("ZIPSERVE_MAX_ZIP_BYTES", 512 * 1024 * 1024))
REQUEST_TIMEOUT = 60


def cache_key(url: str) -> str:
    return hashlib.sha256(url.encode("utf-8")).hexdigest()[:16]


def cache_path(key: str) -> Path:
    return CACHE_DIR / key


def is_github_url(url: str) -> bool:
    try:
        return urlparse(url).hostname in GITHUB_HOSTS
    except ValueError:
        return False


def download_zip(url: str, dest: Path) -> None:
    headers = {"Accept": "application/vnd.github+json, application/zip, */*"}
    token = os.environ.get("GITHUB_TOKEN")
    if token and is_github_url(url):
        headers["Authorization"] = f"Bearer {token}"

    with requests.get(url, headers=headers, stream=True, timeout=REQUEST_TIMEOUT, allow_redirects=True) as resp:
        resp.raise_for_status()
        total = 0
        with dest.open("wb") as fh:
            for chunk in resp.iter_content(chunk_size=64 * 1024):
                if not chunk:
                    continue
                total += len(chunk)
                if total > MAX_ZIP_BYTES:
                    raise ValueError(f"zip exceeds max size of {MAX_ZIP_BYTES} bytes")
                fh.write(chunk)


def safe_extract(zip_path: Path, dest: Path) -> None:
    dest_resolved = dest.resolve()
    with zipfile.ZipFile(zip_path) as zf:
        for member in zf.infolist():
            # Reject absolute paths and traversal attempts (zip-slip).
            name = member.filename
            if name.startswith("/") or ".." in Path(name).parts:
                raise ValueError(f"unsafe path in zip: {name}")
            target = (dest / name).resolve()
            if dest_resolved not in target.parents and target != dest_resolved:
                raise ValueError(f"zip entry escapes extraction dir: {name}")
        zf.extractall(dest)


def find_content_root(extract_dir: Path) -> Path:
    """GitHub zips wrap everything in a single top-level folder; descend into it."""
    entries = [p for p in extract_dir.iterdir() if not p.name.startswith(".")]
    if len(entries) == 1 and entries[0].is_dir():
        return entries[0]
    return extract_dir


def ensure_cached(url: str) -> str:
    key = cache_key(url)
    root = cache_path(key)
    ready_marker = root / ".ready"
    if ready_marker.exists():
        return key

    if root.exists():
        shutil.rmtree(root)
    root.mkdir(parents=True)

    zip_file = root / "archive.zip"
    extract_dir = root / "content"
    extract_dir.mkdir()

    try:
        download_zip(url, zip_file)
        safe_extract(zip_file, extract_dir)
        content_root = find_content_root(extract_dir)
        # Record the content root (relative to extract_dir) for later serving.
        rel = content_root.relative_to(extract_dir)
        (root / "content_root").write_text(str(rel))
        ready_marker.touch()
    except Exception:
        shutil.rmtree(root, ignore_errors=True)
        raise
    finally:
        if zip_file.exists():
            zip_file.unlink()

    return key


def resolve_serve_root(key: str) -> Path:
    root = cache_path(key)
    extract_dir = root / "content"
    rel_file = root / "content_root"
    if not extract_dir.exists() or not rel_file.exists():
        abort(404)
    rel = rel_file.read_text().strip()
    return (extract_dir / rel).resolve()


@app.get("/")
def index():
    url = request.args.get("url")
    if not url:
        return (
            "Usage: /?url=<zip_url>\n"
            "Set GITHUB_TOKEN env var to download from private GitHub repos.\n",
            200,
            {"Content-Type": "text/plain"},
        )
    try:
        key = ensure_cached(url)
    except requests.HTTPError as e:
        return f"download failed: {e.response.status_code} {e.response.reason}", 502
    except Exception as e:
        return f"error: {e}", 500
    return redirect(f"/view/{key}/?url={quote(url, safe='')}", code=302)


@app.get("/view/<key>/")
@app.get("/view/<key>/<path:subpath>")
def view(key: str, subpath: str = ""):
    serve_root = resolve_serve_root(key)

    # Auto-refresh cache if the caller still passes the original url and it was evicted.
    if not (cache_path(key) / ".ready").exists():
        url = request.args.get("url")
        if url and cache_key(url) == key:
            ensure_cached(url)
            serve_root = resolve_serve_root(key)
        else:
            abort(404)

    # Directory → serve index.html if present, otherwise a simple listing.
    target = (serve_root / subpath).resolve()
    if serve_root not in target.parents and target != serve_root:
        abort(404)

    if target.is_dir():
        index_html = target / "index.html"
        if index_html.exists():
            rel = index_html.relative_to(serve_root)
            return send_from_directory(serve_root, str(rel))
        return render_listing(key, subpath, target)

    if not target.exists():
        abort(404)
    return send_from_directory(serve_root, subpath)


def render_listing(key: str, subpath: str, directory: Path) -> str:
    entries = sorted(directory.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower()))
    base = f"/view/{key}/{subpath}".rstrip("/") + "/"
    rows = []
    if subpath:
        parent = "/".join(subpath.rstrip("/").split("/")[:-1])
        parent_href = f"/view/{key}/{parent}".rstrip("/") + "/"
        rows.append(f'<li><a href="{parent_href}">../</a></li>')
    for p in entries:
        name = p.name + ("/" if p.is_dir() else "")
        rows.append(f'<li><a href="{base}{quote(p.name)}{"/" if p.is_dir() else ""}">{name}</a></li>')
    body = "\n".join(rows)
    return (
        f"<!doctype html><meta charset=utf-8>"
        f"<title>{subpath or '/'}</title>"
        f"<h1>/{subpath}</h1><ul>{body}</ul>"
    )


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    app.run(host="127.0.0.1", port=port, debug=bool(os.environ.get("DEBUG")))
