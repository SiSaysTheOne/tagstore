import shutil
import b2sdk.v2 as b2
from tagstudio.core.library.alchemy.library import Library
from tagstudio.core.library.alchemy.models import Tag
from datetime import datetime as dt

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