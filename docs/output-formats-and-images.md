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

Image export requires the sidecar to upload extracted images to OSS so callers can
reference them via a stable key.

### Recommended approach: STS token

Spring Boot issues a temporary STS token (or pre-signed base URL) before calling the
sidecar and passes it as part of the parse request. The sidecar uses the token to upload
images directly to OSS — no callback to Spring Boot is needed.

```
Spring Boot ──签发 STS token──→ OSS
Spring Boot ──POST /v1/parse (含 token)──→ docling
docling ──直接上传图片 (用 token)──→ OSS
docling ──返回含 imageKey 的结果──→ Spring Boot
```

Why this is simpler than the alternatives:

- **No reverse dependency**: the sidecar does not need to know Spring Boot's address.
- **No per-image round-trip**: uploads happen inline during parsing, no extra network calls.
- **Token is self-contained**: the sidecar only needs an OSS endpoint, bucket name, and
  the STS token — all of which Spring Boot already knows and can pass in one request.

ParseRequest addition — OSS credentials are encrypted by Spring Boot before
transmission. The sidecar decrypts them with a private key loaded from its
environment at startup.

```json
{
  "sourceUrl": "...",
  "options": {},
  "oss": {
    "endpoint": "https://oss-cn-hangzhou.aliyuncs.com",
    "bucket": "anchr-documents",
    "encryptedCredentials": "base64-encoded ciphertext",
    "basePath": "images/2024/"
  }
}
```

Encryption contract:

- A shared AES key (e.g. 256-bit) is configured on both sides. The sidecar loads it
  from `ANCHR_DOCLING_OSS_ENCRYPT_KEY` at startup; Spring Boot loads it from its own
  configuration.
- Before calling the sidecar, Spring Boot encrypts the STS credentials (token + access
  key + secret key, serialized as JSON) with AES-GCM, producing a single
  `encryptedCredentials` ciphertext (base64-encoded).
- The sidecar decrypts `encryptedCredentials` with the same key at parse time and
  uses the resulting STS credentials to upload directly to OSS.
- No plaintext credentials ever appear in an HTTP body.

Docling side:

- Load the shared AES key from `ANCHR_DOCLING_OSS_ENCRYPT_KEY` env var at startup.
- Enable page image generation in the PDF pipeline with `generate_page_images=True`.
- Use `PictureItem.get_image(doc)` to crop picture images from page images.
- Encode each image as PNG/JPEG.
- Decrypt `encryptedCredentials` from the request, then upload via OSS SDK using the
  decrypted STS credentials.
- Store the resulting `imageKey` on the matching picture block.

Suggested picture block:

```json
{
  "blockId": "pictures/2",
  "type": "picture",
  "pageNo": 1,
  "bbox": {},
  "childrenText": [],
  "imageKey": "images/2024/abc123.png"
}
```

### Alternative upload contracts (rejected)

- **Spring Boot pre-signs a fixed list of upload URLs before parsing.**
  Simple, but the image count is unknown before parsing — easy to over- or under-allocate.
- **Sidecar calls Spring Boot when it discovers an image and requests an upload target.**
  Creates a reverse dependency (docling → Spring Boot) and adds per-image network latency.
  Avoided by the STS-token approach above.
- **Sidecar returns image bytes or base64 to Spring Boot and lets Spring Boot upload.**
  Easiest to implement but makes parse responses very large; still a reasonable fallback
  for small documents or prototyping.

### Recommended sequence

1. Generate an AES-256 key and configure `ANCHR_DOCLING_OSS_ENCRYPT_KEY` on the sidecar;
   configure the same key on the Spring Boot side.
2. Add `oss` fields to `ParseRequest` schema (optional, so existing callers are unaffected).
3. Add image metadata to picture blocks with `imageKey: null` (no upload yet).
4. Enable Docling image generation (`generate_page_images=True`).
5. Implement OSS upload in the sidecar: decrypt credentials, then upload using the
   decrypted STS token.
