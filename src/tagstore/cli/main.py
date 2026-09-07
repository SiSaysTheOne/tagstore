from pathlib import Path
from tagstore.lib.LibraryUtils import LibUtils
from tagstore.lib.FileSystemUtils import FileSystemUtils
from tagstudio.core.library.alchemy.library import Library

import click

# TODO Consider implementing a function to copy TagStudio entries from another .TagStudio directory if it exists in the target

class ExceptionHandlingGroup(click.Group):
    def invoke(self, ctx):
        try:
            return super().invoke(ctx)
        except Exception as e:
            click.echo(f"Error: {e}", err=True)
            ctx.exit(1)

@click.group(cls=ExceptionHandlingGroup)
def cli():
    pass

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

if __name__ == "__main__":
    cli()