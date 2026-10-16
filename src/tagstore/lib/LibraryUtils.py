from pathlib import Path
import filecmp
import shutil
import b2sdk.v2 as b2
from tagstore.lib.TimeFormats import get_time
from tagstore.lib.FileSystemUtils import FileSystemUtils
from tagstudio.core.library.alchemy.fields import TextField
from tagstudio.core.library.alchemy.library import Library
from tagstudio.core.library.alchemy.models import Tag
from tagstudio.core.constants import RESERVED_TAG_END
from datetime import datetime as dt
from tagstudio.core.library.alchemy.models import Entry

# TODO: Add a mode to that applies tags as a set rather than a lineage (Squash)

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
    def get_tag(lib: Library, name: str) -> Tag | None:
        cached = LibUtils.tag_cache.get((lib, name))
        if cached is not None:
            return cached

        tag = lib.get_tag_by_name(name)
        if tag is not None:
            LibUtils.tag_cache[(lib, name)] = tag
            return tag

        return None

    @staticmethod
    def create_tag(lib: Library, name: str, parent: Tag | None) -> Tag:
        parent_ids = {parent.id} if parent is not None else set()
        new_tag = lib.add_tag(Tag(name=name), parent_ids=parent_ids)
        if new_tag is None:
            raise RuntimeError(f"Failed to create tag '{name}'")
        LibUtils.tag_cache[(lib, name)] = new_tag
        return new_tag

    # Tuple[0] = Tag, Tuple[1] = bool (True if created, False if already existed)
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
        directory_topToBottom = list(path.parent.parts).__reversed__()
        
        # Create a new tag for the top-level directory if it is not already a tag
        # For each subsequent directory, create a new tag if there is not a child tag with that name under the previous diretory's tag
        
        parts = [part for part in directory_topToBottom if part not in (".", "")]
        print(f"tagging entry {entry_id} with path {path} with directory hierarchy: {parts}")
        if not parts:
            return

        current_tag: Tag | None = LibUtils.get_or_create_tag(lib, parts[0], parent=None)  # Create the root tag if nonexistent
        if current_tag is None:
            raise RuntimeError(f"Failed to create or retrieve root tag '{parts[0]}' for entry {entry_id}")

        for part in parts[1:]:

            if part in current_tag.children:
                current_tag = current_tag.children[part]
            else:
                current_tag = LibUtils.create_tag(lib, part, current_tag) 

            if current_tag is None:
                raise RuntimeError(f"Current tag is None while processing part '{part}' for entry {entry_id}")

        lib.add_tags_to_entries(entry_ids=entry_id, tag_ids=current_tag.id)

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
            if any(
                field.name == "file_hash" 
                and field.value == file_hash for field in entry.fields
            ):
                colliding_entry = entry
                break

        # If the file already exists in the library, copy tags and skip file upload
        if colliding_entry is not None:
            colliding_path = lib.library_dir / colliding_entry.path
            # If the hash collides, compare file bytestreams
            if colliding_path.exists() and filecmp.cmp(uploaded_file, colliding_path, shallow=False):
                # If the bytestreams are identical, copy new tags onto the existing file and inform the user
                LibUtils.tag_directory_hierarchy(lib, relative, colliding_entry.id)
                print(f"{uploaded_file} duplicates existing entry {colliding_path}; merged tags instead of adding a new entry.")
                return colliding_path

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

    # @staticmethod
    # def map_tags(host: Library, target: Library) -> dict[int, int]:
    #     """Map every target tag id to an equivalent host tag id, creating host tags as needed.

    #     Tags match on name plus (already mapped) parents, so same-named tags under
    #     different parents stay distinct.
    #     """
    #     host_index: list[tuple[int, str, frozenset[int]]] = [
    #         (tag.id, tag.name, frozenset(parent.id for parent in tag.parent_tags)) for tag in host.tags
    #     ]
    #     tag_map: dict[int, int] = {}
    #     cyclic_links: list[tuple[int, int]] = []

    #     def resolve(tag_id: int, visiting: set[int]) -> int:
    #         if tag_id in tag_map:
    #             return tag_map[tag_id]
    #         # Built-in tags (Archived, Favorite, Meta, ...) have the same ids in every library
    #         if tag_id <= RESERVED_TAG_END:
    #             tag_map[tag_id] = tag_id
    #             return tag_id

    #         tag = target.get_tag(tag_id)
    #         if tag is None:
    #             raise RuntimeError(f"Target tag {tag_id} disappeared while merging")

    #         visiting.add(tag_id)
    #         parent_ids: set[int] = set()
    #         for parent in tag.parent_tags:
    #             if parent.id in visiting:
    #                 # Parent cycle: link it once both ends exist in the host
    #                 cyclic_links.append((parent.id, tag_id))
    #             else:
    #                 parent_ids.add(resolve(parent.id, visiting))
    #         visiting.discard(tag_id)

    #         match = next(
    #             (host_id for host_id, name, parents in host_index if name == tag.name and parents == parent_ids),
    #             None,
    #         )
    #         if match is None:
    #             has_color = tag.color_slug is not None and host.get_tag_color(tag.color_slug, tag.color_namespace) is not None
    #             new_tag = host.add_tag(
    #                 Tag(
    #                     name=tag.name,
    #                     shorthand=tag.shorthand,
    #                     icon=tag.icon,
    #                     color_namespace=tag.color_namespace if has_color else None,
    #                     color_slug=tag.color_slug if has_color else None,
    #                     is_category=tag.is_category,
    #                     is_hidden=tag.is_hidden,
    #                 ),
    #                 parent_ids=parent_ids,
    #             )
    #             if new_tag is None:
    #                 raise RuntimeError(f"Failed to create tag '{tag.name}' in host library")
    #             # add_tag(aliases=...) inserts them before they have a tag id, so add them afterwards
    #             for alias in tag.aliases:
    #                 host.add_alias(alias.name, new_tag.id)
    #             print(f"created tag '{tag.name}' in host library")
    #             match = new_tag.id
    #             host_index.append((match, tag.name, frozenset(parent_ids)))

    #         tag_map[tag_id] = match
    #         return match

    #     for tag in target.tags:
    #         resolve(tag.id, set())
    #     for parent_id, child_id in cyclic_links:
    #         host.add_parent_tag(tag_map[parent_id], tag_map[child_id])

    #     return tag_map

    @staticmethod
    def merge_library(host: Library, target: Library) -> tuple[list[Path], list[Path], list[Path]]:
        """Copy every target entry (file, fields, tags) into host.

        Files already in host (same hash and bytes) are not copied again; the target's
        fields and tags are merged onto the existing entry instead, so re-running is safe.
        Returns (copied, deduplicated, missing) lists of target file paths.
        """
        LibUtils.tag_cache.clear()
        tag_map = LibUtils.map_tags(host, target)

        # file_hash -> (host entry id, host path, fields already on that entry)
        host_hashes: dict[str, tuple[int, Path, set]] = {}
        for entry in host.all_entries(with_joins=True):
            for field in entry.text_fields:
                if field.name == "file_hash" and field.value:
                    host_hashes[field.value] = (entry.id, entry.path, set(entry.fields))

        copied: list[Path] = []
        deduplicated: list[Path] = []
        missing: list[Path] = []
        for entry in target.all_entries(with_joins=True):
            source = target.library_dir / entry.path
            if not source.is_file():
                print(f"Entry path does not exist: {source}")
                missing.append(source)
                continue

            file_hash = FileSystemUtils.hash_file(source)
            existing = host_hashes.get(file_hash)
            if(existing is not None
               and (host.library_dir / existing[1]).is_file()
               and filecmp.cmp(source, host.library_dir / existing[1], shallow=False)):
                entry_id, _, host_fields = existing
                print(f"{source} duplicates existing entry {host.library_dir / existing[1]}; merged tags and fields instead of adding a new entry.")
                deduplicated.append(source)
            else:
                new_path = FileSystemUtils.find_new_path(host.library_dir, source.suffix)
                shutil.copy2(source, host.library_dir / new_path)
                FileSystemUtils.validate_file(source, host.library_dir / new_path)
                entry_id = host.add_entries([Entry(
                    path=new_path,
                    folder=host.folder,
                    fields=[],
                    date_created=entry.date_created,
                    date_modified=entry.date_modified,
                    date_added=entry.date_added,
                )])[0]
                host_fields = set()
                host_hashes[file_hash] = (entry_id, new_path, host_fields)
                copied.append(source)

            fields = list(entry.fields)
            if not any(field.name == "file_hash" for field in fields):
                fields.append(TextField(name="file_hash", value=file_hash, is_multiline=False))
            if not any(field.name == "name" for field in fields):
                fields.append(TextField(name="name", value=Path(entry.filename).stem, is_multiline=False))
            for field in fields:
                if field not in host_fields:
                    host.add_field_to_entries(entry_ids=entry_id, field=field)
                    host_fields.add(field)

            host.add_tags_to_entries(entry_ids=entry_id, tag_ids=[tag_map[tag.id] for tag in entry.tags])
            if entry.path.parent != Path("."):
                LibUtils.tag_directory_hierarchy(host, entry.path, entry_id)

        return copied, deduplicated, missing
