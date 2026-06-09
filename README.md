# anchr-docling

Anchr 文档解析的 Docling 边车服务。

暴露一个轻量 HTTP API。`anchr-app` 传入签名下载 URL，服务返回可用于现有 `TextParseResult` 管线的 Markdown 文本。

## 运行

```bash
cd ~/code/anchr-docling
python -m venv .venv
source .venv/bin/activate
pip install -e .
uvicorn anchr_docling.main:app --host 127.0.0.1 --port 8091
```

Apple Silicon 上服务默认用 CPU 运行 Docling，因为 PyTorch MPS 在某些 PDF 转换路径上会因不支持的 `float64` 张量而失败。可覆盖：

```bash
export ANCHR_DOCLING_DEVICE=cpu
```
使用mps需要将transformers版本按如下设置

```text
 "transformers>=5.8.1,<5.9.0",
```

启动时默认通过 Docling 官方的 `docling.utils.model_downloader.download_models()` API 预取 PDF 布局/表格模型并初始化默认转换器。如果希望运行时启动时不触碰模型缓存或网络，可禁用：

```bash
export ANCHR_DOCLING_PRELOAD_MODELS=false
```

OCR 模型预取默认关闭，因为 OCR 仅在 `"ocr": true` 或 OCR 回退触发时才会用到。如需启动时也准备已配置的 OCR 引擎：

```bash
export ANCHR_DOCLING_PRELOAD_OCR_MODELS=true
```

## API

健康检查：

```bash
curl http://127.0.0.1:8091/healthz
```

解析文档：

```bash
curl -X POST http://127.0.0.1:8091/v1/parse \
  -H 'Content-Type: application/json' \
  -d '{
    "requestId": "task_1:item_1",
    "sourceUrl": "https://example.com/prd.pdf",
    "fileName": "prd.pdf",
    "mimeType": "application/pdf",
    "options": {
      "outputFormat": "markdown",
      "ocr": false,
      "ocrFallback": false,
      "tableStructure": true
    }
  }'
```

响应：

```json
{
  "requestId": "task_1:item_1",
  "parser": "docling",
  "format": "markdown",
  "text": "# 解析后的文档\n...",
  "pages": [
    { "pageNo": 1, "text": "# 解析后的文档\n..." }
  ],
  "images": null
}
```

`outputFormat` 支持 `markdown`、`html`、`text`、`json`、`blocks`、`chunks`。

### markdown / html / text

- `markdown` — Docling 的 `export_to_markdown()`。保留结构（标题、表格，图片显示为 `<!-- image -->` 占位符）。
- `html` — Docling 的 `export_to_html()`。
- `text` — Docling 的 `export_to_text()`。纯文本。

### json

`text` 包含聚合后的文档文本，`document` 包含 Docling 的完整结构化 JSON 对象。

### blocks

`blocks` 包含面向应用的块列表。每个块有 `blockId`、`type`、`text`、`pageNo`、`parentRef`、`bbox`、`children`。图片块额外包含：

```json
{
  "blockId": "pictures/0",
  "type": "picture",
  "childrenText": ["图 1：架构总览"],
  "imageKey": "docling-images/pictures_0_task_1_item_42.png",
  "imageUploadStatus": "uploaded",
  "imageMimeType": "image/png",
  "imageWidth": 1200,
  "imageHeight": 800
}
```

### chunks

Chunks 基于 **markdown** 文本切分（非纯文本），因此标题、表格、图片占位符等结构得以保留。每个 chunk 有两个文本字段：

- `text` — 完整 markdown 格式，喂给 LLM。
- `textPlain` — 去除格式的纯文本，用于向量索引 / embedding 搜索。

切分以 `##` 标题为硬边界，尽量保持表格完整，按 `chunkMaxChars` 控制长度。

```json
{
  "format": "chunks",
  "chunks": [
    {
      "chunkId": "chunks/0",
      "type": "section",
      "text": "## 架构概述\n\n系统分为**三层**：\n\n- 接入层\n- 业务层\n\n<!-- image -->",
      "textPlain": "架构概述 系统分为三层： 接入层 业务层",
      "pageRange": [1, 2],
      "charCount": 85,
      "source": "native"
    }
  ]
}
```

OCR 解析的 PDF 默认不生成 chunks（避免将 OCR 噪声编入索引）。如需覆盖，设置 `chunkOcrPolicy: "include"`。

## OSS 图片导出

提供 OSS 凭证后，服务会从 PDF 中提取图片，以 PNG 格式上传至阿里云 OSS，并在输出中用 URL 引用。支持 `markdown`、`blocks`、`chunks` 三种输出格式。

### 请求

```json
{
  "options": { "outputFormat": "markdown" },
  "oss": {
    "endpoint": "oss-cn-hangzhou.aliyuncs.com",
    "bucket": "my-bucket",
    "basePath": "docling-images",
    "encryptedCredentials": {
      "iv": "<base64, 16 字节>",
      "ciphertext": "<base64>"
    }
  }
}
```

| 字段 | 说明 |
|-------|------|
| `endpoint` | OSS endpoint，如 `oss-cn-hangzhou.aliyuncs.com` |
| `bucket` | OSS bucket 名称 |
| `basePath` | 可选，图片 key 的前缀路径 |
| `encryptedCredentials.iv` | AES-256-CBC IV，16 字节，base64 编码 |
| `encryptedCredentials.ciphertext` | AES-256-CBC 密文，base64 编码 |
| `encryptedCredentials.tag` | 不使用（CBC 模式），可不传 |

`ciphertext` 解密后的明文为 JSON：

```json
{
  "accessKeyId": "STS.xxx",
  "accessKeySecret": "xxx",
  "securityToken": "xxx",
  "expiration": "2026-06-10T00:00:00Z"
}
```

加密方式为 AES-256-CBC + PKCS7Padding。32 字节密钥在服务端配置：

```bash
export ANCHR_DOCLING_OSS_ENCRYPT_KEY="<32 字节密钥，原文或 base64>"
```

### 图片 key 格式

```
{basePath}/pictures_0_{requestId}.png
```

`requestId` 中的 `/` 和 `:` 会被替换为 `_`。`requestId` 为空时使用毫秒时间戳兜底，确保不同文档的图片不会重名覆盖。

### 图片输出

`markdown` 和 `chunks` 模式下，`text` / `pages[].text` / `chunks[].text` 中的 `<!-- image -->` 占位符会被替换为 `![alt](url)`。同时图片信息以结构化数据形式返回在顶层 `images` 字段中：

```json
{
  "images": [
    {
      "url": "https://my-bucket.oss-cn-hangzhou.aliyuncs.com/docling-images/pictures_0_task_1_item_42.png",
      "pageNo": 1,
      "blockId": "pictures/0",
      "alt": "图 1：架构总览"
    }
  ]
}
```

未提供 OSS 凭证时 `images` 为 `null`。

## 注意事项

- 本项目不持有存储、任务、数据库状态或 chunk 持久化。
- 调用方负责获取 STS 临时凭证，并用共享 AES 密钥加密后传入。
- 本服务仅下载源文件 URL、运行 Docling、返回解析结果及可选的 OSS 图片上传。
- OCR 默认关闭。仅对扫描件或纯图片文档启用 `"ocr": true`。
- 部分 PDF 含有损坏或自定义字体的文本层，服务默认会拒绝明显乱码的文本。使用 `"ocrFallback": true` 对这些文档重试 OCR。
- OCR 回退默认使用 RapidOCR 并强制全页 OCR。可通过以下环境变量切换引擎：

```bash
export ANCHR_DOCLING_OCR_ENGINES=ocrmac,rapidocr
export ANCHR_DOCLING_OCR_LANG=zh-Hans,en-US
```
