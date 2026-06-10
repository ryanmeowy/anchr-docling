from typing import Any

from anchr_docling.errors import add_warning
from anchr_docling.images import (
    ImageUploadContext,
    attach_picture_image_metadata,
)
from anchr_docling.schemas import ParseWarning
from anchr_docling._utils import (
    add_if_present,
    collect_child_text,
    enum_value,
    ref_to_block_id,
    resolve_block_type,
    resolve_bbox,
    resolve_child_refs,
    resolve_page_no,
    resolve_parent_ref,
    resolve_ref,
)


def export_blocks(
    document: Any,
    upload_context: ImageUploadContext | None,
    warnings: list[ParseWarning],
) -> list[dict[str, Any]]:
    blocks: list[dict[str, Any]] = []
    for item, _ in document.iterate_items(with_groups=True, traverse_pictures=True):
        ref = getattr(item, "self_ref", None)
        if not ref or ref == "#/body":
            continue

        block = {
            "blockId": ref_to_block_id(ref),
            "type": resolve_block_type(item),
        }
        add_if_present(block, "text", getattr(item, "text", None))
        if block["type"] == "group":
            add_if_present(block, "label", enum_value(getattr(item, "label", None)))
        add_if_present(block, "pageNo", resolve_page_no(document, item))
        add_if_present(block, "parentRef", resolve_parent_ref(item))
        add_if_present(block, "bbox", resolve_bbox(item))

        child_refs = resolve_child_refs(item)
        if child_refs:
            block["children"] = child_refs

        if resolve_block_type(item) == "picture":
            block["childrenText"] = collect_child_text(document, item)
            attach_picture_image_metadata(document, item, block, upload_context, warnings)

        blocks.append(block)
    return blocks
