# 输出格式、Blocks 与图片导出

## 背景

当前 sidecar 负责包装 Docling 的解析结果，供 Spring Boot 项目消费。Markdown 是默认
响应格式，`outputFormat=json` 暴露 Docling 的结构化文档模型。下一步需要提供一个
更稳定、面向业务调用方的投影结构，方便调用方索引、渲染，以及挂载图片等媒体资源。

## 1. `outputFormat=blocks`

`blocks` 已实现为 Docling 文档模型上的一层投影，而不是直接透传原始 Docling JSON。

建议响应结构：

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

实现来源：

- 使用 `document.iterate_items(with_groups=True, traverse_pictures=True)` 按文档树
  顺序遍历 blocks。
- 使用 `item.self_ref` 作为 `blockId` 的来源，例如 `#/texts/0`。
- 使用 `item.label.value` 作为 `type`，例如 `section_header`、`paragraph`、
  `picture` 或 `table`。
- 使用 `item.parent.cref` 作为 `parentRef`。
- 使用 `item.children` 生成子节点 refs。
- 使用 `item.prov[0].page_no` 和 `item.prov[0].bbox` 生成 `pageNo` 和 `bbox`。

注意事项：

- 第一版应包含 tables，否则 Docling 的表格输出在 block 模式下会难以消费。
- 保留 Docling JSON pointer refs，例如 `#/texts/0`、`#/groups/1`，方便调用方在需要
  时回查原始 JSON。
- 对图片 block，聚合 caption 和图片子节点文本到 `childrenText`。

## 2. JSON 模式的 `pages`

已调整为 `pages[].text` 表示页面文本，而不是原始 Docling page 对象。完整的 Docling
JSON 对象通过顶层 `document` 字段暴露。

建议响应结构：

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

当前行为：

- `text` 始终表示文本。
- `document` 在 `json` 模式下包含完整 Docling JSON 对象。
- `pages[].text` 包含页面级聚合文本。
- `pages[].blockRefs` 列出该页出现的 block refs。

这样可以降低调用方消费成本，避免 `text` 字段一会儿是字符串、一会儿是对象。

## 3. 图片导出与 OSS 上传

图片导出要求 sidecar 将解析出的图片上传到 OSS，调用方通过稳定的 `imageKey` 引用图片。

### 推荐方案：STS token + 轻量 AES 加密

Spring Boot 在调用 sidecar 前签发临时 STS token（或预签名基础信息），并随解析请求传给
sidecar。sidecar 使用该 token 直接上传图片到 OSS，不需要回调 Spring Boot。

STS 凭证使用固定共享 AES 密钥加密。该密钥同时配置在 Spring Boot 和 sidecar 中，轻量版
实现不做密钥轮换。

```text
Spring Boot ──签发 STS token──→ OSS
Spring Boot ──POST /v1/parse（含加密 token）──→ docling sidecar
docling sidecar ──直接上传图片（解密后使用 token）──→ OSS
docling sidecar ──返回含 imageKey 的结果──→ Spring Boot
```

这个方案比其它方案更简单：

- **没有反向依赖**：sidecar 不需要知道 Spring Boot 的地址。
- **没有逐图片网络往返**：图片上传在解析流程内完成，不需要每张图片再请求一次签名。
- **token 自包含**：sidecar 只需要 OSS endpoint、bucket、basePath 和 STS token；
  这些信息 Spring Boot 已经知道，可以一次性传入请求。

请求结构新增 `oss` 字段。OSS 凭证由 Spring Boot 在发送前加密，sidecar 使用启动时从
环境变量加载的共享 AES 密钥解密。

```json
{
  "sourceUrl": "...",
  "options": {},
  "oss": {
    "endpoint": "https://oss-cn-hangzhou.aliyuncs.com",
    "bucket": "anchr-documents",
    "basePath": "images/2024/",
    "encryptedCredentials": {
      "iv": "base64-encoded-12-byte-iv",
      "ciphertext": "base64-encoded-ciphertext",
      "tag": "base64-encoded-16-byte-tag"
    }
  }
}
```

加密约定：

- 双方配置一把固定的 AES-256 共享密钥。sidecar 从
  `ANCHR_DOCLING_OSS_ENCRYPT_KEY` 读取；Spring Boot 从自己的配置读取。轻量版不支持
  密钥轮换。
- Spring Boot 调用 sidecar 前，将 STS 凭证序列化为 JSON，并使用 AES-GCM 加密。
- `iv`、`ciphertext`、`tag` 都使用 base64 编码。轻量版不包含 `alg`、`keyId`、`aad`
  等字段。
- sidecar 在解析时使用固定共享密钥解密 `encryptedCredentials`，然后使用解密得到的
  STS 凭证直接上传 OSS。
- 明文凭证不会出现在 HTTP body 中。

加密前的明文结构：

```json
{
  "accessKeyId": "...",
  "accessKeySecret": "...",
  "securityToken": "...",
  "expiration": "2026-05-29T12:00:00Z"
}
```

最小安全边界：

- Spring Boot 应签发短有效期 STS token，例如 5-15 分钟。
- STS policy 只允许 `oss:PutObject`。
- STS policy 只允许写入本次请求的 `basePath` 下。
- sidecar 不打印 STS token、AccessKeySecret 或解密后的完整凭证，也不将这些凭证落盘。
  日志里只能出现 `requestId`、`imageKey`、上传状态和脱敏错误信息。
- 该方案适合同机或可信内网调用。如果 sidecar 暴露在不可信网络中，应在前置网关或
  反向代理层终止 HTTPS。

Docling sidecar 侧行为：

- 启动时从 `ANCHR_DOCLING_OSS_ENCRYPT_KEY` 加载共享 AES 密钥。
- 图片导出默认开启：PDF pipeline 默认设置 `generate_page_images=True`，以便 picture
  block 能拿到裁剪图。
- 使用 `PictureItem.get_image(doc)` 从页面图像中裁剪图片。
- 将图片编码为 PNG/JPEG。
- 如果请求中包含可用的 `oss.encryptedCredentials`，解密后使用 STS 凭证通过 OSS SDK
  上传图片。
- 如果请求中没有 STS 凭证，或者凭证解密/上传失败，不影响文档解析；picture block
  保留图片元数据和上传状态，响应顶层返回 warning 说明原因。
- 将上传得到的 `imageKey` 和上传状态写回对应的 picture block。

建议的 picture block：

```json
{
  "blockId": "pictures/2",
  "type": "picture",
  "pageNo": 1,
  "bbox": {},
  "childrenText": [],
  "imageKey": "images/2024/pictures_2.png",
  "imageUploadStatus": "uploaded",
  "imageUploadError": null,
  "imageMimeType": "image/png",
  "imageWidth": 640,
  "imageHeight": 320
}
```

`imageUploadStatus` 取值：

- `uploaded`：图片已成功上传，`imageKey` 可用。
- `skipped_no_credentials`：请求中没有提供 STS 凭证，跳过上传；`imageKey` 为 `null`。
- `failed`：已尝试上传但失败；`imageKey` 为 `null`，`imageUploadError` 包含脱敏错误说明。
- `no_image`：Docling 没有生成可裁剪图片；`imageKey` 为 `null`。

如果存在图片上传跳过或失败，响应顶层应包含 `warnings`：

```json
{
  "requestId": "xxx",
  "parser": "docling",
  "format": "blocks",
  "blocks": [],
  "warnings": [
    {
      "code": "image_upload_skipped_no_credentials",
      "message": "OSS credentials were not provided; image upload was skipped.",
      "blockId": "pictures/2"
    },
    {
      "code": "image_upload_failed",
      "message": "Failed to upload image to OSS.",
      "blockId": "pictures/3"
    }
  ]
}
```

### 其它上传协议（暂不采用）

- **Spring Boot 在解析前预签一组固定上传 URL。**
  实现简单，但解析前不知道图片数量，容易多签或不够用。
- **sidecar 发现图片后回调 Spring Boot 获取上传目标。**
  会引入反向依赖（docling → Spring Boot），并增加逐图片网络延迟。STS 方案可以避免它。
- **sidecar 将图片 bytes 或 base64 返回给 Spring Boot，再由 Spring Boot 上传。**
  最容易实现，但会显著放大解析响应体；只适合小文档或原型验证。

### 推荐落地顺序

1. 生成固定 AES-256 密钥，在 sidecar 配置 `ANCHR_DOCLING_OSS_ENCRYPT_KEY`；Spring Boot
   配置同一把密钥。
2. 在 `ParseRequest` schema 中增加可选 `oss` 字段，保证现有调用方不受影响。
3. 为响应 schema 增加顶层 `warnings` 字段。
4. 为图片 block 增加图片元数据和 `imageUploadStatus`，没有 STS 时返回
   `skipped_no_credentials`。
5. 默认开启 Docling 图片生成能力（`generate_page_images=True`）。
6. 在 sidecar 中实现 OSS 上传：解密凭证，然后使用解密得到的 STS token 上传；上传失败
   时写入 `imageUploadStatus=failed` 和脱敏 warning，不影响解析成功返回。
