from __future__ import annotations

import hashlib
import os
from pathlib import Path


class LocalFileStorage:
    def __init__(self, root: str) -> None:
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def save(self, content: bytes) -> tuple[str, str]:
        digest = hashlib.sha256(content).hexdigest()
        directory = self.root / digest[:2]
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / digest
        if not path.exists():
            temporary = directory / f".{digest}.{os.getpid()}.tmp"
            temporary.write_bytes(content)
            os.chmod(temporary, 0o600)
            temporary.replace(path)
        return str(path), digest

    def read(self, uri: str) -> bytes:
        return self.resolve(uri).read_bytes()

    def resolve(self, uri: str) -> Path:
        path = Path(uri).resolve()
        if self.root not in path.parents:
            raise ValueError("source path escapes storage root")
        return path
