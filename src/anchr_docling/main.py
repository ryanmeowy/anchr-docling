import asyncio

from fastapi import FastAPI, HTTPException

from anchr_docling.docling_parser import DoclingParseError, DoclingParser, SourceDownloadError
from anchr_docling.schemas import ParseRequest, ParseResponse

app = FastAPI(title="anchr-docling", version="0.1.0")
parser = DoclingParser()


@app.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/v1/parse", response_model=ParseResponse)
async def parse_document(request: ParseRequest) -> ParseResponse:
    try:
        return await asyncio.to_thread(parser.parse, request)
    except SourceDownloadError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except DoclingParseError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
