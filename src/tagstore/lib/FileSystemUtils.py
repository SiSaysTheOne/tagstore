from pathlib import Path
from uuid import uuid4
import hashlib

class FileSystemUtils:
    @staticmethod
    def find_new_path(root_dir: Path, suffix: str) -> Path:
        while True:
            candidate = Path(uuid4().hex + suffix)
            if not (root_dir / candidate).exists():
                return candidate

    @staticmethod
    def hash_file(path: Path) -> str:
        hasher = hashlib.sha256()
        with path.open("rb") as f:
            for chunk in iter(lambda: f.read(1024 * 1024), b""):
                hasher.update(chunk)
        return hasher.hexdigest()

    @staticmethod
    def validate_file(source_file: Path, lib_file: Path) -> None:

        if source_file is None or lib_file is None:
            raise ValueError("Source file or library file is None.")

        if not lib_file.exists():
            raise FileNotFoundError(f"Library file missing after upload: {lib_file}")
        
        if not source_file.exists():
            raise FileNotFoundError(f"Source file missing: {source_file}")

        source_hash = FileSystemUtils.hash_file(source_file)
        lib_hash = FileSystemUtils.hash_file(lib_file)

        if source_hash != lib_hash:
            raise ValueError(f"Hash mismatch after upload: {source_file} ({source_hash}) != {lib_file} ({lib_hash})")

    @staticmethod
    def remove_empty_directories(dir: Path) -> None:
        directories_by_depth = sorted(
            (p for p in dir.rglob("*") if p.is_dir() and ".TagStudio" not in p.parts),
            key=lambda p: len(p.parts),
            reverse=True,
        )
        for directory in directories_by_depth:
            try:
                directory.rmdir()
            except OSError:
                pass