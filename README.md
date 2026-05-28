# anchr-docling

Docling sidecar service for Anchr document parsing.

This service exposes a small HTTP API. `anchr-app` can pass a signed download URL, and this service returns Markdown text that can be converted into the existing `TextParseResult` pipeline.

## Run

```bash
cd ~/code/anchr-docling
python -m venv .venv
source .venv/bin/activate
pip install -e .
uvicorn anchr_docling.main:app --host 127.0.0.1 --port 8091
```

On Apple Silicon, the service defaults Docling to CPU because PyTorch MPS can fail on
some PDF conversion paths with unsupported `float64` tensors. You can override it:

```bash
export ANCHR_DOCLING_DEVICE=cpu
```

By default, startup prefetches Docling's PDF layout/table model artifacts through
Docling's official `docling.utils.model_downloader.download_models()` API and initializes
the default converter before the first parse request. Disable this if the runtime should
start without touching the model cache or network:

```bash
export ANCHR_DOCLING_PRELOAD_MODELS=false
```

OCR model prefetching is disabled by default because OCR is only used when `"ocr": true`
or OCR fallback is triggered. Enable it when you want startup to also prepare configured
OCR engines:

```bash
export ANCHR_DOCLING_PRELOAD_OCR_MODELS=true
```

## API

Health check:

```bash
curl http://127.0.0.1:8091/healthz
```

Parse a document:

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

Response:

```json
{
  "requestId": "task_1:item_1",
  "parser": "docling",
  "format": "markdown",
  "text": "# Parsed document\n...",
  "pages": [
    {
      "pageNo": 1,
      "text": "# Parsed document\n..."
    }
  ]
}
```

`outputFormat` supports `markdown`, `html`, `text`, and `json`. For `json`, the
`text` field contains Docling's structured JSON object, and each `pages[].text`
contains the corresponding page object from Docling's JSON output.

## Notes

- This project intentionally does not own storage, tasks, database state, or chunk persistence.
- Java remains responsible for task state and OSS signing.
- This service only downloads the signed URL, runs Docling, and returns parse text.
- OCR is disabled by default. Enable `"ocr": true` only for scanned PDFs or image-only documents.
- Some PDFs contain broken/custom font text layers. The service rejects obviously garbled text by default.
  Use `"ocrFallback": true` to retry those documents with OCR.
- OCR fallback uses RapidOCR by default and forces full-page OCR. You can switch engines:

```bash
export ANCHR_DOCLING_OCR_ENGINES=ocrmac,rapidocr
export ANCHR_DOCLING_OCR_LANG=zh-Hans,en-US
```
