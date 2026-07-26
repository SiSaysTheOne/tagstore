from pathlib import Path
import b2sdk.v2 as b2
from tagstore.LibUtils import LibUtils
from tagstore.FileSystemUtils import FileSystemUtils
from tagstudio.core.library.alchemy.library import Library

def get_b2_bucket(key_id: str, app_key: str, bucket_name: str) -> b2.Bucket:
    info = b2.InMemoryAccountInfo()
    api = b2.B2Api(info)
    api.authorize_account("production", key_id, app_key)
    return api.get_bucket_by_name(bucket_name)

# TODO: Consider implementing a function to copy TagStudio entries from another .TagStudio directory if it exists in the target

def main() -> None:
    LIBRARY_PATH = Path("/home/simon/Desktop/backup/test2/lib")
    FILES_PATH = Path("/home/simon/Desktop/backup/test2/files")

    LIBRARY_PATH.mkdir(parents=True, exist_ok=True)

    lib = Library()
    status = lib.open_library(LIBRARY_PATH)
    if not status.success:
        raise RuntimeError(f"Failed to open library: {status.message}")

    lib_hash_initial = FileSystemUtils.hash_file(LIBRARY_PATH / ".TagStudio" / "ts_library.sqlite")

    LibUtils.add_all_files_to_library(lib)

    LibUtils.flatten_library(lib)

    for file in FILES_PATH.rglob("*"):
        # NOTE: This does not upload metadata files from other libraries
        if file.is_file() and ".TagStudio" not in file.parts:
            lib_file = LibUtils.upload_file(lib, file)
            FileSystemUtils.validate_file(file, lib_file)
            file.unlink(missing_ok=True)

    lib.close()
    print("Library closed successfully.")

    FileSystemUtils.remove_empty_directories(LIBRARY_PATH)
    FileSystemUtils.remove_empty_directories(FILES_PATH)

    lib_hash_final = FileSystemUtils.hash_file(LIBRARY_PATH / ".TagStudio" / "ts_library.sqlite")
    print(f"Library hash (initial): {lib_hash_initial}")
    print(f"Library hash (final): {lib_hash_final}")
    if(lib_hash_initial == lib_hash_final):
        print("Library operations were very likely idempotent.")

    # --- Backblaze B2 ---
    # bucket = get_b2_bucket(
    #     key_id="YOUR_KEY_ID",
    #     app_key="YOUR_APPLICATION_KEY",
    #     bucket_name="your-bucket-name",
    # )
    # print(bucket)


if __name__ == "__main__":
    main()
