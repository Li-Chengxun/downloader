"""Build a source update archive using an explicit allowlist; never include secrets."""
from __future__ import annotations

import hashlib
import io
from pathlib import Path
import tarfile

ROOT = Path(__file__).resolve().parents[1]
NAME = "douyin-downloader-update-20261008-r2"


def build() -> Path:
    sources = {p.relative_to(ROOT).as_posix(): p for p in (ROOT / "backend").glob("*.py")}
    for pattern in ("backend/static/*.html", "backend/static/*.css", "backend/static/vendor/*.js", "backend/tests/test_*.py"):
        sources.update({p.relative_to(ROOT).as_posix(): p for p in ROOT.glob(pattern)})
    for name in ("backend/Dockerfile", "backend/.dockerignore", "backend/requirements.txt", ".env.example", "README.md", "UPDATE.md", "deploy/update.sh", "deploy/apply-update.sh"):
        sources[name] = ROOT / name
    # Templates are references only; the installer preserves the server's live configuration.
    sources["templates/docker-compose.yml"] = ROOT / "docker-compose.yml"
    sources["templates/nginx-http.conf"] = ROOT / "deploy/nginx.conf"
    sources["templates/nginx-https.conf"] = ROOT / "deploy/nginx-https.conf"
    payload = {}
    for name, path in sorted(sources.items()):
        if path.is_symlink():
            raise ValueError(f"Refusing symlink: {path}")
        payload[name] = path.read_bytes().replace(b"\r\n", b"\n")
    payload["SHA256SUMS"] = "".join(f"{hashlib.sha256(data).hexdigest()}  {name}\n" for name, data in payload.items()).encode()
    output = ROOT / f"{NAME}.tar.gz"
    with tarfile.open(output, "w:gz") as archive:
        for name, data in payload.items():
            item = tarfile.TarInfo(f"{NAME}/{name}")
            item.size = len(data)
            item.mode = 0o755 if name.endswith(".sh") else 0o644
            item.mtime = 1791417600
            archive.addfile(item, io.BytesIO(data))
    output.with_name(output.name + ".sha256").write_text(f"{hashlib.sha256(output.read_bytes()).hexdigest()}  {output.name}\n", encoding="utf-8", newline="\n")
    # Verify all contents, hashes, paths and Linux line endings from the finished archive.
    with tarfile.open(output, "r:gz") as archive:
        members = archive.getmembers()
        assert len(members) == len(payload)
        for member in members:
            assert member.isfile() and member.name.startswith(NAME + "/")
            assert ".." not in Path(member.name).parts
            name = member.name[len(NAME) + 1:]
            data = archive.extractfile(member).read()
            assert data == payload[name] and b"\r\n" not in data
            assert name != ".env" and "certs/" not in name and "__pycache__" not in name
    print(f"Built and verified: {output.name} ({output.stat().st_size:,} bytes, {len(payload)} files)")
    return output


if __name__ == "__main__":
    build()
