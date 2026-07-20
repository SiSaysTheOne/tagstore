from pathlib import Path
from uuid import uuid4

import shutil
import b2sdk.v2 as b2
from tagstore.LibUtils import LibUtils
from tagstore.TimeFormats import get_time
from tagstudio.core.library.alchemy.fields import TextField
from tagstudio.core.library.alchemy.library import Library
from tagstudio.core.library.alchemy.models import Tag
from datetime import datetime as dt
from tagstudio.core.library.alchemy.models import Entry, Folder

def get_b2_bucket(key_id: str, app_key: str, bucket_name: str) -> b2.Bucket:
    info = b2.InMemoryAccountInfo()
    api = b2.B2Api(info)
    api.authorize_account("production", key_id, app_key)
    return api.get_bucket_by_name(bucket_name)


def find_new_path(root_dir: Path, suffix: str) -> Path:
    while True:
        candidate = Path(uuid4().hex + suffix)
        if not (root_dir / candidate).exists():
            return candidate

def tag_directory_hierarchy(lib: Library, entry: Entry) -> None:
    parts = [part for part in entry.path.parent.parts if part not in (".", "")]
    if not parts:
        return

    parent_tag: Tag | None = None
    for part in parts:
        parent_tag = LibUtils.get_or_create_tag(lib, part, parent_tag)

    lib.add_tags_to_entries(entry_ids=entry.id, tag_ids=parent_tag.id)


LIBRARY_PATH = Path("/home/simon/Desktop/backup/test")

def add_all_files_to_library(lib: Library) -> None:
    folder = lib.folder
    for file in LIBRARY_PATH.rglob("*"):
        if file.is_file() and ".TagStudio" not in file.parts:
            relative = file.relative_to(LIBRARY_PATH)
            if not lib.has_entry_with_path(relative):
                lib.add_entries([Entry(
                    path=relative,
                    folder=folder,
                    fields=[],
                    date_added=dt.now(),
                )])

def upload_file() -> None:
    #TODO
    # Generate hash

    # Compare hash to existing files in the library

    # If the hash doesn't collide, add without complaint

    # If the hash collides, compare file bytestreams

    # If the bytestreams are identical, copy new tags onto the existing file and inform the user

    pass

def flatten_library(lib: Library) -> None:
    LibUtils.tag_cache.clear()
    for entry in lib.all_entries():
        if any(field.name == "filename_time" for field in entry.fields):
            continue

        entry_full_path = LIBRARY_PATH / entry.path
        if not entry_full_path.exists():
            print(f"Entry path does not exist: {entry_full_path}")
            continue
        
        file_time = get_time(entry)
        
        if file_time is not None:
            lib.add_field_to_entries(
                entry_ids=entry.id,
                field=TextField(name="filename_time", value=file_time, is_multiline=False),
            )

        tag_directory_hierarchy(lib, entry)

        new_path = find_new_path(LIBRARY_PATH, entry.path.suffix)
        dest_path = Path(shutil.move(entry_full_path, LIBRARY_PATH / new_path))
        if dest_path != LIBRARY_PATH / new_path:
            print(f"Move failed: {entry_full_path} -> {LIBRARY_PATH / new_path}")
            continue
        lib.update_entry_path(entry.id, new_path)

def remove_empty_directories(lib: Library) -> None:
    # for folder in lib.all_folders():
    #     folder_path = LIBRARY_PATH / folder.path
    #     if folder_path.is_dir() and not any(folder_path.iterdir()):
    #         try:
    #             folder_path.rmdir()
    #             lib.remove_folder(folder.id)
    #         except Exception as e:
    #             print(f"Failed to remove empty directory {folder_path}: {e}")

    directories_by_depth = sorted(
        (p for p in LIBRARY_PATH.rglob("*") if p.is_dir() and ".TagStudio" not in p.parts),
        key=lambda p: len(p.parts),
        reverse=True,
    )
    for directory in directories_by_depth:
        try:
            directory.rmdir()
        except OSError:
            pass



def main() -> None:
    LIBRARY_PATH.mkdir(parents=True, exist_ok=True)

    # print("status 0")

    lib = Library()
    status = lib.open_library(LIBRARY_PATH)
    if not status.success:
        raise RuntimeError(f"Failed to open library: {status.message}")

    add_all_files_to_library(lib)

    flatten_library(lib)

    # tag = lib.get_tag_by_name("Landscape")
    # if not tag:
    #     tag = lib.add_tag(Tag(name="Landscape"))

    # if entry and tag:
    #     lib.add_tags_to_entries(entry_ids=entry.id, tag_ids=tag.id)

    # print("status 2")

    # tag = lib.get_tag_by_name("test")
    # if not tag:
    #     tag = lib.add_tag(Tag(name="test"))

    # all_entry_ids = [entry.id for entry in lib.all_entries()]
    # lib.add_tags_to_entries(entry_ids=all_entry_ids, tag_ids=tag.id)
    # lib.add_field_to_entries(
    #     entry_ids=all_entry_ids,
    #     field=TextField(name="test_field", value="test_value", is_multiline=False),
    # )

    remove_empty_directories(lib)

    lib.close()
    print("Library closed successfully.")

    # --- Backblaze B2 ---
    # bucket = get_b2_bucket(
    #     key_id="YOUR_KEY_ID",
    #     app_key="YOUR_APPLICATION_KEY",
    #     bucket_name="your-bucket-name",
    # )
    # print(bucket)


if __name__ == "__main__":
    main()
