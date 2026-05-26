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
  "pages": []
}
```

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
