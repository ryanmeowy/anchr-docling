"""Shared Docling item utilities — no dependencies on images or blocks."""

from typing import Any


def resolve_block_type(item: Any) -> str:
    ref = getattr(item, "self_ref", "")
    if ref.startswith("#/groups/"):
        return "group"
    return enum_value(getattr(item, "label", None)) or ref.split("/")[1].rstrip("s")


def resolve_page_no(document: Any, item: Any) -> int | None:
    prov = getattr(item, "prov", None)
    if prov:
        return getattr(prov[0], "page_no", None)

    for child_ref in getattr(item, "children", []) or []:
        child = resolve_ref(document, child_ref)
        if child is None:
            continue
        page_no = resolve_page_no(document, child)
        if page_no is not None:
            return page_no
    return None


def resolve_bbox(item: Any) -> dict[str, Any] | None:
    prov = getattr(item, "prov", None)
    if not prov:
        return None
    bbox = getattr(prov[0], "bbox", None)
    if bbox is None:
        return None
    if hasattr(bbox, "model_dump"):
        return bbox.model_dump(mode="json")
    if isinstance(bbox, dict):
        return bbox
    return None


def resolve_parent_ref(item: Any) -> str | None:
    parent = getattr(item, "parent", None)
    return getattr(parent, "cref", None)


def resolve_child_refs(item: Any) -> list[str]:
    return [
        ref
        for child in getattr(item, "children", []) or []
        if (ref := getattr(child, "cref", None))
    ]


def collect_child_text(document: Any, item: Any) -> list[str]:
    texts: list[str] = []
    seen_refs: set[str] = set()
    for ref_item in [
        *(getattr(item, "captions", []) or []),
        *(getattr(item, "children", []) or []),
    ]:
        collect_ref_text(document, ref_item, texts, seen_refs)
    return texts


def collect_ref_text(
    document: Any,
    ref_item: Any,
    texts: list[str],
    seen_refs: set[str],
) -> None:
    ref = getattr(ref_item, "cref", None)
    if not ref or ref in seen_refs:
        return
    seen_refs.add(ref)

    item = resolve_ref(document, ref_item)
    if item is None:
        return

    text = getattr(item, "text", None)
    if text:
        texts.append(text)
    for child in getattr(item, "children", []) or []:
        collect_ref_text(document, child, texts, seen_refs)


def resolve_ref(document: Any, ref_item: Any) -> Any | None:
    try:
        return ref_item.resolve(document)
    except Exception:
        return None


def ref_to_block_id(ref: str) -> str:
    return ref.removeprefix("#/")


def enum_value(value: Any) -> str | None:
    if value is None:
        return None
    return getattr(value, "value", str(value))


def add_if_present(target: dict[str, Any], key: str, value: Any) -> None:
    if value is not None:
        target[key] = value
