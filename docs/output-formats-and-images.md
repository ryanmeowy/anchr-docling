# Output Formats, Blocks, and Image Export

## Background

The sidecar currently wraps Docling output for Spring Boot consumption. Markdown is the
default response format, and `outputFormat=json` exposes Docling's structured document
model. The next useful step is to add a stable application-facing projection that is
easier for callers to index, render, and attach media to.

## 1. `outputFormat=blocks`

Implemented as a projection over Docling's document model, not as a direct
pass-through of raw Docling JSON.

Suggested response shape:

```json
{
  "requestId": "xxx",
  "parser": "docling",
  "format": "blocks",
  "blocks": [
    {
      "blockId": "texts/0",
      "type": "section_header",
      "text": "连接器",
      "pageNo": 3,
      "parentRef": "#/body",
      "bbox": {}
    },
    {
      "blockId": "groups/1",
      "type": "group",
      "label": "form_area",
      "pageNo": 1,
      "children": []
    },
    {
      "blockId": "pictures/2",
      "type": "picture",
      "pageNo": 1,
      "bbox": {},
      "childrenText": [],
      "imageKey": null
    }
  ]
}
```

Implementation source:

- Use `document.iterate_items(with_groups=True, traverse_pictures=True)` to walk blocks
  in document-tree order.
- Use `item.self_ref`, such as `#/texts/0`, as the source of `blockId`.
- Use `item.label.value` for `type`, for example `section_header`, `paragraph`,
  `picture`, or `table`.
- Use `item.parent.cref` for `parentRef`.
- Use `item.children` to produce child refs.
- Use `item.prov[0].page_no` and `item.prov[0].bbox` for `pageNo` and `bbox`.

Notes:

- Include tables in the first implementation. Otherwise Docling table output becomes
  hard to consume in block mode.
- Preserve Docling JSON pointer refs (`#/texts/0`, `#/groups/1`) so callers can map
  blocks back to raw JSON when needed.
- For pictures, aggregate captions and picture-child text into `childrenText`.

## 2. JSON Mode `pages`

Implemented so that `pages[].text` is page text, not the raw Docling page object.
The full Docling JSON object is exposed through the top-level `document` field.

Recommended response shape:

```json
{
  "format": "json",
  "text": "全文聚合文本...",
  "document": {},
  "pages": [
    {
      "pageNo": 1,
      "text": "这一页聚合文本...",
      "blockRefs": ["#/texts/1", "#/groups/0", "#/pictures/2"]
    }
  ]
}
```

Current behavior:

- `text` should always mean text.
- `document` contains the full Docling JSON object in `json` mode.
- `pages[].text` contains page-level aggregated text.
- `pages[].blockRefs` lists refs for blocks appearing on the page.

This keeps the response easier to consume and avoids overloading `text` with both
strings and objects.

## 3. Image Export and OSS Upload

Image export is feasible, but it is a larger feature than the output-format changes
because it needs a sidecar-to-Spring Boot upload contract.

Docling side:

- Enable page image generation in the PDF pipeline with `generate_page_images=True`.
- Use `PictureItem.get_image(doc)` to crop picture images from page images.
- Encode or save each image as PNG/JPEG.
- Upload the image using a signed OSS upload target from Spring Boot.
- Return the resulting `imageKey` on the matching picture block.

Suggested picture block:

```json
{
  "blockId": "pictures/2",
  "type": "picture",
  "pageNo": 1,
  "bbox": {},
  "childrenText": [],
  "imageKey": "oss/path/to/image.png"
}
```

Upload contract options:

- Spring Boot pre-signs a fixed list of upload URLs before parsing.
  This is simple, but the image count is unknown before parsing, so it can over- or
  under-allocate.
- Sidecar calls Spring Boot when it discovers an image and requests an upload target.
  This is the recommended approach. Spring Boot returns `uploadUrl`, `imageKey`, and
  any required headers.
- Sidecar returns image bytes or base64 to Spring Boot and lets Spring Boot upload.
  This is easiest to implement but can make parse responses very large.

Recommended sequence:

1. Add image metadata to picture blocks with `imageKey: null`.
2. Define the Spring Boot upload-signature endpoint.
3. Enable Docling image generation and upload extracted picture images to OSS.
