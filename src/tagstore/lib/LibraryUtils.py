from pathlib import Path
import filecmp
import shutil
import b2sdk.v2 as b2
from tagstore.lib.TimeFormats import get_time
from tagstore.lib.FileSystemUtils import FileSystemUtils
from tagstudio.core.library.alchemy.fields import TextField
from tagstudio.core.library.alchemy.library import Library
from tagstudio.core.library.alchemy.models import Tag
from datetime import datetime as dt
from tagstudio.core.library.alchemy.models import Entry

class fs_driver_local:
    # Implement local filesystem driver for uploading files to a local directory

    def __init__(self, root_dir: Path) -> None:
        self.root_dir = root_dir

    def upload(self, local_path: Path, remote_path: str) -> None:
        destination = self.root_dir / remote_path
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(local_path, destination)

    def fetch_metadata(self) -> dict[str, dict]:
        metadata = {}
        for file in self.root_dir.rglob("*"):
            if file.is_file():
                metadata[file.relative_to(self.root_dir).as_posix()] = {
                    "size": file.stat().st_size,
                    "modified_time": file.stat().st_mtime,
                }
        return metadata

class fs_driver_B2:
    # Implement Backblaze B2 driver for uploading files to B2 cloud storage

    def __init__(self, key_id: str, app_key: str, bucket_name: str) -> None:
        self.key_id = key_id
        self.app_key = app_key
        self.bucket_name = bucket_name
        self.bucket: b2.Bucket | None = None
        self.metadata_cache: dict[str, b2.FileVersion] = {}

    def auth(self) -> b2.Bucket:
        info = b2.InMemoryAccountInfo()
        api = b2.B2Api(info)
        api.authorize_account("production", self.key_id, self.app_key)
        self.bucket = api.get_bucket_by_name(self.bucket_name)
        return self.bucket

    def upload(self, local_path: Path, remote_path: str) -> None:
        if self.bucket is None:
            raise RuntimeError("Not authenticated; call auth() first.")

        # TODO: Handle large files, retries, and progress reporting as needed.
        self.bucket.upload_local_file(local_file=str(local_path), file_name=remote_path)

    def fetch_metadata(self, force_refresh: bool = False) -> dict[str, b2.FileVersion]:
        if self.bucket is None:
            raise RuntimeError("Not authenticated; call auth() first.")

        if self.metadata_cache and not force_refresh:
            return self.metadata_cache

        # TODO: Narrow this down to whatever metadata fields are actually needed downstream.
        for file_version, _ in self.bucket.ls(latest_only=True):
            self.metadata_cache[file_version.file_name] = file_version

        return self.metadata_cache

    # TODO Decide how you want the backup to work and what its responsibilities are

    # Clones the .TagStore directory from the backup
    # TODO def fetch_library

# TODO: Convert to fs_driver_local
class LibUtils:
    tag_cache: dict[tuple[Library, str], Tag] = {}
    
    @staticmethod
    def get_or_create_tag(lib: Library, name: str, parent: Tag | None) -> Tag:
        cached = LibUtils.tag_cache.get((lib, name))
        if cached is not None:
            return cached

        tag = lib.get_tag_by_name(name)
        if tag is not None:
            LibUtils.tag_cache[(lib, name)] = tag
            return tag

        parent_ids = {parent.id} if parent is not None else set()
        new_tag = lib.add_tag(Tag(name=name), parent_ids=parent_ids)
        if new_tag is None:
            raise RuntimeError(f"Failed to create tag '{name}'")
        LibUtils.tag_cache[(lib, name)] = new_tag
        return new_tag

    @staticmethod
    def tag_directory_hierarchy(lib: Library, path: Path, entry_id: int) -> None:
        parts = [part for part in path.parent.parts if part not in (".", "")]
        print(f"tagging entry {entry_id} with path {path} with directory hierarchy: {parts}")
        if not parts:
            return

        parent_tag: Tag | None = None
        for part in parts:
            parent_tag = LibUtils.get_or_create_tag(lib=lib, name=part, parent=parent_tag)
            print(f"created tag for entry {entry_id} with tag {parent_tag.name}")

        lib.add_tags_to_entries(entry_ids=entry_id, tag_ids=parent_tag.id)

    @staticmethod
    def add_all_files_to_library(lib: Library) -> None:
        for file in lib.library_dir.rglob("*"):
            if file.is_file() and ".TagStudio" not in file.parts:
                relative = file.relative_to(lib.library_dir)
                if not lib.has_entry_with_path(relative):
                    lib.add_entries([Entry(
                        path=relative,
                        folder=lib.folder,
                        fields=[],
                        date_added=dt.now(),
                    )])

    @staticmethod
    def upload_file(lib: Library, files_dir: Path, uploaded_file: Path) -> Path | None:
    
        relative = uploaded_file.relative_to(files_dir)

        # Generate hash
        file_hash = FileSystemUtils.hash_file(uploaded_file)

        # Compare hash to existing files in the library
        colliding_entry: Entry | None = None
        for entry in lib.all_entries():
            if any(field.name == "file_hash" and field.value == file_hash for field in entry.fields):
                colliding_entry = entry
                break

        # If the file already exists in the library, copy tags and skip file upload
        if colliding_entry is not None:
            existing_path = lib.library_dir / colliding_entry.path
            # If the hash collides, compare file bytestreams
            if existing_path.exists() and filecmp.cmp(uploaded_file, existing_path, shallow=False):
                # If the bytestreams are identical, copy new tags onto the existing file and inform the user
                LibUtils.tag_directory_hierarchy(lib, relative, colliding_entry.id)
                print(f"{uploaded_file} duplicates existing entry {existing_path}; merged tags instead of adding a new entry.")
                return existing_path

        # Copy file to library root with a new unique name
        libFile_relativePath = FileSystemUtils.find_new_path(lib.library_dir, uploaded_file.suffix)
        libFile_fullPath = Path(shutil.copy2(uploaded_file, lib.library_dir / libFile_relativePath))
        if libFile_fullPath != lib.library_dir / libFile_relativePath:
            print(f"Copy failed: {uploaded_file} -> {lib.library_dir / libFile_relativePath}")
            return None

        # Create default tags
        entry_ids = lib.add_entries([Entry(
            path=libFile_relativePath,
            folder=lib.folder,
            fields=[],
            date_added=dt.now(),
        )])
        lib.add_field_to_entries(
            entry_ids=entry_ids,
            field=TextField(name="file_hash", value=file_hash, is_multiline=False),
        )
        lib.add_field_to_entries(
            entry_ids=entry_ids,
            field=TextField(name="name", value=uploaded_file.stem, is_multiline=False),
        )

        file_time = get_time(uploaded_file.stem)        
        if file_time is not None:
            lib.add_field_to_entries(
                entry_ids=entry_ids,
                field=TextField(name="filename_time", value=file_time, is_multiline=False),
            )
        else:
            print(f"Warning: Could not extract time from filename {uploaded_file.stem}")

        print(f"tagging file {uploaded_file} with tags based on its directory hierarchy")
        LibUtils.tag_directory_hierarchy(lib, relative, entry_ids[0])
        # Return the new path of the file in the library after moving it to the root
        return libFile_fullPath

    @staticmethod
    def flatten_library(lib: Library) -> None:
        LibUtils.tag_cache.clear()
        for entry in lib.all_entries():
            if any(field.name == "filename_time" for field in entry.fields):
                continue

            entry_full_path = lib.library_dir / entry.path
            if not entry_full_path.exists():
                print(f"Entry path does not exist: {entry_full_path}")
                continue
            
            file_time = get_time(entry.path.stem)
            
            if file_time is not None:
                lib.add_field_to_entries(
                    entry_ids=entry.id,
                    field=TextField(name="filename_time", value=file_time, is_multiline=False),
                )

            if entry.path.parent == Path(".") and file_time is None:
                new_path = entry.path
            else:
                LibUtils.tag_directory_hierarchy(lib, entry.path, entry.id)
                new_path = FileSystemUtils.find_new_path(lib.library_dir, entry.path.suffix)
                dest_path = Path(shutil.move(entry_full_path, lib.library_dir / new_path))
                if dest_path != lib.library_dir / new_path:
                    print(f"Move failed: {entry_full_path} -> {lib.library_dir / new_path}")
                    continue
            
            lib.update_entry_path(entry.id, new_path)