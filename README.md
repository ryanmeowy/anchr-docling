# anchr-docling

Anchr 文档解析的 Docling 边车服务。

暴露一个轻量 HTTP API。`anchr-app` 传入签名下载 URL，服务返回可用于现有 `TextParseResult` 管线的 Markdown 文本。

## 运行

```bash
cd ~/code/anchr-docling
python -m venv .venv
source .venv/bin/activate
pip install -e .
export ANCHR_DOCLING_API_TOKEN="$(openssl rand -hex 32)"
export ANCHR_DOCLING_ALLOWED_DOWNLOAD_HOSTS="anchr.oss-cn-shanghai.aliyuncs.com"
uvicorn anchr_docling.main:app --host <YOUR MACHINE IP> --port 8091
```

服务启动时会强制校验内部 Token（至少 32 个字符）和下载域名白名单。Token 必须同时配置到 `anchr-app` 的 `APP_DOCLING_API_TOKEN`。

Apple Silicon 上服务默认用 `cpu` 运行 Docling。也可设为 `mps` 利用 GPU 加速，但需注意：

- PyTorch MPS 在某些 PDF 转换路径上会因不支持的 `float64` 张量而失败
- **公式识别模型（`formulaEnrichment`）不支持 MPS**，开启公式识别时必须使用 `cpu` 或 `cuda`

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

### 健康检查

```bash
curl http://<YOUR MACHINE IP>:8091/healthz
```

健康检查不需要鉴权，并返回当前运行数、排队数和队列容量。

### 提交解析任务

```bash
POST /v1/jobs
Authorization: Bearer <internal-token>
Content-Type: application/json
```

任务 API 是异步的。默认只有一个解析 worker，最多等待 8 个任务；队列满时返回 `429` 和 `Retry-After`。`requestId` 必填并作为幂等键：相同请求重复提交返回已有任务，不同请求复用同一 ID 返回 `409`。

#### 请求参数

##### 顶层

| 字段 | 类型 | 必填 | 默认 | 说明                            |
|------|------|------|------|-------------------------------|
| `requestId` | string | **是** | — | 请求幂等标识，同时用作图片 OSS key 的后缀     |
| `sourceUrl` | string | **是** | — | 源文件下载地址，支持 PDF / 图片           |
| `fileName` | string | 否 | — | 文件名（含后缀），用于推断文件类型。未传则从 URL 路径提取 |
| `mimeType` | string | 否 | — | 保留字段，暂未使用, 后续可能用于类型校验         |
| `options` | object | 否 | — | 解析选项                          |
| `oss` | object | 否 | — | OSS 图片上传配置，不传则不导出图片           |

##### options

| 字段 | 类型 | 必填 | 默认 | 说明 |
|------|------|------|------|------|
| `outputFormat` | string | 否 | `"markdown"` | 输出格式：`markdown` / `html` / `text` / `json` / `blocks` / `chunks` |
| `ocr` | bool | 否 | `false` | 是否启用 OCR。图片输入时必须为 `true` |
| `ocrFallback` | bool | 否 | `false` | 文本质量校验失败时自动回退 OCR |
| `tableStructure` | bool | 否 | `true` | 是否检测表格结构 |
| `validateTextQuality` | bool | 否 | `true` | 是否校验文本质量（拒绝乱码） |
| `chunkMinTokens` | int | 否 | `400` | chunks 最小 token 数。自定义 chunker 中作为段落合并的下限阈值；native chunker 中控制合并行为 |
| `chunkMaxTokens` | int | 否 | `800` | chunks 最大 token 数。自定义 chunker 按 `token × 2 ≈ chars` 换算；native chunker 直接按 token 切分 |
| `formulaEnrichment` | bool | 否 | `false` | 启用 VLM 公式识别模型，将数学公式转为 LaTeX。需下载额外模型（~500MB）。**MPS 设备不支持此模型，需设 `ANCHR_DOCLING_DEVICE=cpu`** |
| `useNativeChunker` | bool | 否 | `false` | `true` 使用 Docling 原生 HybridChunker（支持 bbox + headings）；`false` 使用自定义 markdown chunker |

##### oss

| 字段 | 类型 | 必填 | 默认 | 说明 |
|------|------|------|------|------|
| `endpoint` | string | **是** | — | OSS endpoint，如 `oss-cn-hangzhou.aliyuncs.com` |
| `bucket` | string | **是** | — | OSS bucket 名称 |
| `basePath` | string | 否 | `""` | 图片 key 的前缀路径 |
| `encryptedCredentials` | object | **是** | — | 加密后的 STS 临时凭证 |

##### oss.encryptedCredentials

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `iv` | string | **是** | AES-256-CBC IV，16 字节，base64 |
| `ciphertext` | string | **是** | AES-256-CBC 密文，base64 |

#### 示例请求

```bash
curl -X POST http://<Mac的Tailscale-IP>:8091/v1/jobs \
  -H "Authorization: Bearer $ANCHR_DOCLING_API_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{
    "requestId": "task_1:item_1",
    "sourceUrl": "https://example.com/prd.pdf",
    "fileName": "prd.pdf",
    "options": {
      "outputFormat": "markdown",
      "ocr": false
    }
  }'
```

新任务返回 `202 Accepted`：

```json
{
  "jobId": "4c54b626-e367-4c92-a351-62d52f68948a",
  "requestId": "task_1:item_1",
  "status": "queued",
  "createdAt": "2026-07-10T10:00:00Z"
}
```

轮询任务：

```bash
curl http://<Mac的Tailscale-IP>:8091/v1/jobs/<jobId> \
  -H "Authorization: Bearer $ANCHR_DOCLING_API_TOKEN"
```

状态依次为 `queued`、`running`、`succeeded` 或 `failed`。成功时 `result` 字段包含下述解析响应；Spring 读取成功结果后会确认并释放内存：

```bash
curl -X DELETE http://<Mac的Tailscale-IP>:8091/v1/jobs/<jobId> \
  -H "Authorization: Bearer $ANCHR_DOCLING_API_TOKEN"
```

运行或排队中的任务不能删除。未确认结果会在 10 分钟后清理，**服务重启后内存任务会丢失**，由 Spring 使用相同 `requestId` 重新提交。

#### 响应

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

| 字段 | 类型 | 说明 |
|------|------|------|
| `requestId` | string\|null | 回传请求 ID |
| `parser` | string | 固定 `"docling"` |
| `format` | string | 输出格式 |
| `text` | string | 聚合后的文档文本 |
| `pages` | array | 按页文本，每项含 `pageNo` 和 `text` |
| `document` | object\|null | JSON 格式的完整 Docling 结构化文档（仅 `outputFormat: "json"`） |
| `blocks` | array\|null | 块列表（仅 `outputFormat: "blocks"`） |
| `chunks` | array\|null | chunk 列表（仅 `outputFormat: "chunks"`） |
| `images` | array\|null | 图片结构化数据（markdown / blocks / chunks + OSS 凭证时返回） |
| `warnings` | array\|null | 警告列表 |

`outputFormat` 支持 `markdown`、`html`、`text`、`json`、`blocks`、`chunks`。

## 安全与资源控制

- `/v1/*` 必须携带 Bearer Token；`/healthz` 是唯一无需鉴权的接口。
- Swagger、ReDoc 和 OpenAPI 默认关闭；本地调试可设置 `ANCHR_DOCLING_ENABLE_DOCS=true`。
- `ANCHR_DOCLING_ALLOWED_DOWNLOAD_HOSTS` 是逗号分隔的精确主机名白名单，只允许 HTTPS 默认端口。原始文件和 Markdown 内嵌图片的每次重定向都会重新校验。
- `ANCHR_DOCLING_MAX_DOWNLOAD_MB` 和 `ANCHR_DOCLING_MAX_IMAGE_MEGAPIXELS` 继续限制下载体积和图片解码尺寸。
- 队列限制并发和等待数量，但不能强制终止正在解析的单个恶意文件；单文件硬超时和独立进程隔离不在当前版本范围内。

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

#### 自定义 chunker（默认 `useNativeChunker: false`）

基于 markdown 文本切分，按 `##` 标题为硬边界、尽量保持表格完整，以 `chunkMinTokens` / `chunkMaxTokens` 控制粒度（内部 `token × 2 ≈ chars` 换算）。

```json
{
  "chunkId": "chunks/0",
  "type": "section",
  "text": "## 架构概述\n\n系统分为**三层**：\n\n<!-- image -->",
  "textPlain": "架构概述 系统分为三层：",
  "pageRange": [1, 2],
  "charCount": 85,
  "source": "native"
}
```

#### 原生 chunker（`useNativeChunker: true`）

使用 Docling 的 `HybridChunker`，item 级切分后按 token 做 split/merge。额外输出 `bboxes` 和 `headings`：

```json
{
  "chunkId": "chunks/0",
  "type": "section",
  "text": "## MySQL 实战45讲\n\n从原理到实战，丁奇带你搞懂 MySQL",
  "textPlain": "MySQL 实战45讲 从原理到实战，丁奇带你搞懂 MySQL",
  "pageRange": [1, 1],
  "charCount": 42,
  "source": "native",
  "bboxes": [
    { "pageNo": 1, "bbox": { "l": 44, "t": 227, "r": 259, "b": 210 } },
    { "pageNo": 1, "bbox": { "l": 44, "t": 210, "r": 300, "b": 190 } }
  ],
  "headings": ["MySQL 实战45讲"]
}
```

| 字段 | 出现条件 | 说明 |
|------|---------|------|
| `text` | 必含 | 完整 markdown，喂 LLM |
| `textPlain` | 必含 | 去格式纯文本，建 embedding 索引 |
| `pageRange` | 必含 | `[起始页, 结束页]` |
| `bboxes` | native chunker | 文档坐标系的 bbox 列表（`l,t,r,b` 单位 point，原点左下角），前端可用 PDF.js 映射到 PDF 上高亮 |
| `headings` | native chunker | 当前 chunk 所在章节的标题层级 |


## 公式识别

设置 `"formulaEnrichment": true` 启用 VLM 公式识别模型，可自动检测文档中的数学公式并转为 LaTeX：

```json
{
  "options": {
    "outputFormat": "chunks",
    "ocr": true,
    "formulaEnrichment": true
  }
}
```

开启后，`$$...$$` 块中的内容为模型识别出的 LaTeX 公式（如 `$$x = \frac{-b \pm \sqrt{b^2 - 4ac}}{2a}$$`），可直接被 KaTeX / MathJax 渲染。

### 限制

| 项目 | 说明 |
|------|------|
| 设备要求 | 公式 VLM 模型（`codeformulav2`）**不支持 MPS**。Apple Silicon 上需设 `ANCHR_DOCLING_DEVICE=cpu`，否则报错 `MPS is not supported by this model` |
| 模型大小 | 首次启动需下载约 500MB 的 VLM 模型文件 |
| 处理速度 | 公式识别增加额外推理步骤，处理时间会有明显增加 |
| 输出清洗 | 无论是否开启公式识别，输出中的 `<!-- formula-not-decoded -->`、`ParseError: KaTeX parse error:` 等 OCR 残留文本会自动清洗 |

### 混合文字+公式场景

当前 Docling 的 Layout 分析模型在**中文文字与数学公式混排**的图片上分割准确度有限，容易将文字区域误判为公式（或反之），导致识别错误率极高。**纯公式图片**的识别效果尚可。

对于混合场景，更推荐使用 [PaddleOCR](https://github.com/PaddlePaddle/PaddleOCR) 或者 [MinerU](https://github.com/opendatalab/MinerU)，两者对中英文混排 + 公式的识别效果更好：

- **PaddleOCR**：中文 OCR 准确率高，支持公式区域检测
- **MinerU**：专为 PDF/图片转 Markdown 设计，内置公式识别管线，对混合排版支持更好

建议策略：

| 场景 | 推荐工具 |
|------|---------|
| 纯公式图片 | 本服务 `formulaEnrichment: true` |
| 混合文字+公式 | PaddleOCR + MinerU |
| 纯文档（无公式） | 本服务（默认配置） |


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
- 公式识别默认关闭。对数学/理工类文档建议开启 `"formulaEnrichment": true`，注意需 `ANCHR_DOCLING_DEVICE=cpu`。
- 部分 PDF 含有损坏或自定义字体的文本层，服务默认会拒绝明显乱码的文本。使用 `"ocrFallback": true` 对这些文档重试 OCR。
- OCR 回退默认使用 RapidOCR 并强制全页 OCR。可通过以下环境变量切换引擎：

```bash
export ANCHR_DOCLING_OCR_ENGINES=ocrmac,rapidocr
export ANCHR_DOCLING_OCR_LANG=zh-Hans,en-US
```
