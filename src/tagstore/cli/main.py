import hashlib
from pathlib import Path
from tagstore.lib.LibraryUtils import LibUtils
from tagstore.lib.FileSystemUtils import FileSystemUtils
from tagstore.lib import B2S3Utils
from tagstudio.core.library.alchemy.library import Library

from botocore.exceptions import BotoCoreError, ClientError
import click

# TODO Consider implementing a function to copy TagStudio entries from another .TagStudio directory if it exists in the target

class ExceptionHandlingGroup(click.Group):
    def invoke(self, ctx):
        try:
            return super().invoke(ctx)
        except (click.exceptions.Exit, click.Abort):
            # Normal exit / Ctrl-C sentinels - let click handle them.
            raise
        except Exception as e:
            click.echo(f"Error: {e}", err=True)
            ctx.exit(1)

@click.group(cls=ExceptionHandlingGroup)
def cli():
    pass

# Files that compose a library: (flat file system, .TagStudio/ts_library.sqlite, and .TagStudio/ts_ignore)

# TODO Abstract this CLI to be backup-solution agnostic
# TODO? Abstract this CLI to be local filesystem agnostic

@cli.command()
@click.option("--library-dir", required=True, help="Path to the library directory")
@click.option(
    "--override-existing",
    is_flag=True,
    default=False,
    help="Override existing files in the target.",
)
def cloneLibrary(library_dir: str, override_existing: bool):
    library_dir = Path(library_dir)
    library_dir.mkdir(parents=True, exist_ok=True)
    if(not library_dir.exists() or not library_dir.is_dir()):
        raise FileNotFoundError(f"Library path does not exist or is not a directory: {library_dir}")
    if(library_dir / ".TagStudio" / "ts_library.sqlite").exists() and not override_existing:
        raise FileExistsError(f"Library path already contains a TagStudio library: {library_dir}")

    client, bucket = _connect()

    # Pull cloud library to local library
    object_keys = B2S3Utils.list_object_keys(bucket, client)
    if not object_keys:
        raise FileNotFoundError(f"Bucket '{bucket}' is empty; nothing to clone.")
    for key in object_keys:
        B2S3Utils.download_file(bucket, key, library_dir / key, client)

    # Reconstruct tagstudio library
    lib = Library()
    status = lib.open_library(library_dir)
    if not status.success:
        raise RuntimeError(f"Failed to open library after clone: {status.message}")
    lib.close()

    # TODO Verify that thumbnails are regenerated
        # If not, create a new method to do so

    click.echo(f"OK: cloned {len(object_keys)} object(s) from '{bucket}' into '{library_dir}'")

# TODO? Keep a ledger of all changes; only push affected files
# TODO? Parallelize uploads/downloads with threads
@cli.command()
@click.option("--library-dir", required=True, help="Path to the library directory")
@click.option(
    "--override-existing",
    is_flag=True,
    default=False,
    help="Override existing files in the target.",
)
@click.option(
    "--clear-bucket",
    is_flag=True,
    default=False,
    help="Clear the bucket before uploading.",
)
def backupLibrary(library_dir: str, override_existing: bool, clear_bucket: bool):

    library_dir = Path(library_dir)
    if(not library_dir.exists() or not library_dir.is_dir()):
        raise FileNotFoundError(f"Library path does not exist or is not a directory: {library_dir}")
    
    client, bucket = _connect()

    object_keys = B2S3Utils.list_object_keys(bucket, client)
    if(clear_bucket):
        for key in object_keys:
            B2S3Utils.delete_object(bucket, key, client)
        click.echo(f"OK: cleared {len(object_keys)} object(s) from '{bucket}'")
        object_keys = []
    elif(object_keys and not override_existing):
        raise FileExistsError(f"Bucket '{bucket}' is not empty. Use --override-existing to override.")

    # Push local library to the cloud bucket
    thumbs_dir = library_dir / ".TagStudio" / "thumbs"
    files = [
        file for file in library_dir.rglob("*")
        if file.is_file() and not file.is_relative_to(thumbs_dir)
    ]
    for file in files:
        key = file.relative_to(library_dir).as_posix()
        file_hash_sha1 = hashlib.sha1(file.read_bytes()).hexdigest()
        B2S3Utils.upload_file(bucket, key, file, client, file_hash_sha1=file_hash_sha1)

    click.echo(f"OK: backed up {len(files)} file(s) from '{library_dir}' to '{bucket}'")

# TODO: Declare as a local filesystem operation
@cli.command()
@click.option("--library-dir", required=True, help="Path to the library directory")
@click.option("--files-dir", required=True, help="Path to the files directory")
@click.option("--delete-source", is_flag=True, default=False, help="Delete source files after copying")
def copyFilesToLibrary(library_dir: str, files_dir: str, delete_source: bool) -> None:
    library_dir = Path(library_dir)
    library_dir.mkdir(parents=True, exist_ok=True)
    files_dir = Path(files_dir)

    if(not library_dir.exists() or not library_dir.is_dir()):
        raise FileNotFoundError(f"Library path does not exist or is not a directory: {library_dir}")
    if(not files_dir.exists() or not files_dir.is_dir()):
        raise FileNotFoundError(f"Files path does not exist or is not a directory: {files_dir}")
    if(library_dir == files_dir):
        raise ValueError("Library path and files path cannot be the same.")
    if(library_dir in files_dir.parents):
        raise ValueError("Library path cannot be a parent of files path.")
    if(files_dir in library_dir.parents):
        raise ValueError("Files path cannot be a parent of library path.")

    lib = Library()
    status = lib.open_library(library_dir)
    if not status.success:
        raise RuntimeError(f"Failed to open library: {status.message}")

    lib_hash_initial = FileSystemUtils.hash_file(library_dir / ".TagStudio" / "ts_library.sqlite")

    LibUtils.add_all_files_to_library(lib)

    LibUtils.flatten_library(lib)

    for file in files_dir.rglob("*"):
        # NOTE: This does not upload metadata files from other libraries
        if file.is_file() and ".TagStudio" not in file.parts:
            lib_file = LibUtils.upload_file(lib, files_dir, file)
            FileSystemUtils.validate_file(file, lib_file)
            if delete_source:
                file.unlink(missing_ok=True)

    lib.close()
    print("Library closed successfully.")

    FileSystemUtils.remove_empty_directories(library_dir)
    FileSystemUtils.remove_empty_directories(files_dir)

    lib_hash_final = FileSystemUtils.hash_file(library_dir / ".TagStudio" / "ts_library.sqlite")
    print(f"Library hash (initial): {lib_hash_initial}")
    print(f"Library hash (final): {lib_hash_final}")
    if(lib_hash_initial == lib_hash_final):
        print("Library operations were probably idempotent.")


# ---------------------------------------------------------------------------
# Backblaze B2 (S3-compatible API) operations
#
# Adapted from https://github.com/backblaze-b2-samples/b2-python-s3-sample/
# All configuration is read from the .env file (see ./.env) - no CLI options:
#   BUCKET_ENDPOINT, KEY_ID, APPLICATION_KEY, BUCKET_NAME
# ---------------------------------------------------------------------------


def _connect():
    """Return (client, bucket_name) built entirely from the .env file."""
    creds = B2S3Utils.B2Credentials.from_env()
    bucket = B2S3Utils.bucket_from_env()
    return B2S3Utils.get_b2_client(creds), bucket


@cli.group()
def b2() -> None:
    """Authenticate to and sync against the Backblaze B2 bucket from .env."""


@b2.command("auth")
def b2_auth() -> None:
    """Verify the .env credentials can reach the configured bucket."""
    creds = B2S3Utils.B2Credentials.from_env()
    bucket = B2S3Utils.bucket_from_env()
    try:
        B2S3Utils.authenticate(bucket, creds)
    except (ClientError, BotoCoreError) as exc:
        raise RuntimeError(f"Authentication to '{bucket}' failed: {exc}") from exc
    click.echo(f"OK: authenticated to '{bucket}' via {creds.endpoint}")


@b2.command("list")
def b2_list() -> None:
    """List object keys with size / mtime / md5 for the configured bucket."""
    client, bucket = _connect()
    try:
        rows = B2S3Utils.list_object_metadata(bucket, client)
    except (ClientError, BotoCoreError) as exc:
        raise RuntimeError(f"Listing '{bucket}' failed: {exc}") from exc
    print(f"{'Last Modified':<20}  {'Size':>12}  {'MD5':<32}  Key")
    print("-" * 80)
    # TODO: Fixed-size print
    for row in rows:
        click.echo(f"{row.last_modified:%Y-%m-%d %H:%M:%S}  {row.size:>12d}  {row.md5}  {row.key}")
    click.echo(f"\n{bucket}: {len(rows)} object(s)", err=True)

@b2.command("upload")
@click.option("--file-path", required=True, help="Path to the file to upload")
@click.option(
    "--replace-all-versions",
    is_flag=True,
    default=False,
    help="After uploading, permanently delete every other version of this key.",
)
def b2_upload(file_path: str, replace_all_versions: bool) -> None:
    """Upload a file to the configured bucket."""

    client, bucket = _connect()
    try:
        result = B2S3Utils.upload_file(
            bucket=bucket,
            key=Path(file_path).name,
            src=Path(file_path),
            client=client,
            content_type=None, # TODO: Maybe pull from tagstudio's set of known content types?
            file_hash_sha1=hashlib.sha1(Path(file_path).read_bytes()).hexdigest(),
            metadata=None,
            prune_old_versions=replace_all_versions,
        )
    except (ClientError, BotoCoreError) as exc:
        raise RuntimeError(f"Uploading '{file_path}' to '{bucket}' failed: {exc}") from exc
    click.echo(f"OK: uploaded '{file_path}' to '{bucket}' (version {result.version_id})")

@b2.command("delete")
@click.option("--key", required=True, help="Key of the file to delete")
def b2_delete(key: str) -> None:
    """Delete a file from the configured bucket.

    On a versioned bucket (B2's default) this hides the current version behind
    a delete marker rather than erasing history - see "delete-all-versions" to
    remove every version permanently.
    """

    client, bucket = _connect()
    try:
        B2S3Utils.delete_object(
            bucket=bucket,
            key=key,
            client=client
        )
    except (ClientError, BotoCoreError) as exc:
        raise RuntimeError(f"Deleting '{key}' from '{bucket}' failed: {exc}") from exc
    click.echo(f"OK: deleted '{key}' from '{bucket}'")

@b2.command("versions")
@click.option("--key", default=None, help="Only show versions of this exact key (default: every key).")
def b2_versions(key: str | None) -> None:
    """List every stored version (and delete marker) of objects in the bucket."""

    client, bucket = _connect()
    try:
        versions = B2S3Utils.list_object_versions(bucket, client, prefix=key or "")
    except (ClientError, BotoCoreError) as exc:
        raise RuntimeError(f"Listing versions in '{bucket}' failed: {exc}") from exc
    if key:
        versions = [v for v in versions if v.key == key]
    print(f"{'Last Modified':<20}  {'Latest':<6}  {'Size':>12}  {'MD5':<32}  {'Version ID':<40}  Key")
    print("-" * 130)
    for v in versions:
        size = f"{v.size:>12d}" if v.size is not None else f"{'(deleted)':>12}"
        click.echo(
            f"{v.last_modified:%Y-%m-%d %H:%M:%S}  {'yes' if v.is_latest else 'no':<6}  "
            f"{size}  {(v.md5 or ''):<32}  {v.version_id:<40}  {v.key}"
        )
    click.echo(f"\n{bucket}: {len(versions)} version(s)", err=True)

@b2.command("delete-version")
@click.option("--key", required=True, help="Key of the object")
@click.option("--version-id", required=True, help="Specific version id to delete (see 'b2 versions')")
def b2_delete_version(key: str, version_id: str) -> None:
    """Permanently delete one specific version of an object."""

    client, bucket = _connect()
    try:
        B2S3Utils.delete_object_version(bucket=bucket, key=key, version_id=version_id, client=client)
    except (ClientError, BotoCoreError) as exc:
        raise RuntimeError(
            f"Deleting version '{version_id}' of '{key}' from '{bucket}' failed: {exc}"
        ) from exc
    click.echo(f"OK: deleted version '{version_id}' of '{key}' from '{bucket}'")

@b2.command("delete-all-versions")
@click.option("--key", required=True, help="Key of the object to remove completely")
def b2_delete_all_versions(key: str) -> None:
    """Permanently delete every version and delete marker of an object."""

    client, bucket = _connect()
    try:
        count = B2S3Utils.delete_all_versions(bucket=bucket, key=key, client=client)
    except (ClientError, BotoCoreError) as exc:
        raise RuntimeError(f"Deleting all versions of '{key}' from '{bucket}' failed: {exc}") from exc
    click.echo(f"OK: deleted {count} version(s) of '{key}' from '{bucket}'")