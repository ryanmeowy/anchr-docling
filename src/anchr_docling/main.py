import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Response, status

from anchr_docling.config import Settings, settings
from anchr_docling.docling_parser import DoclingParser
from anchr_docling.jobs import (
    ActiveJobError,
    JobManager,
    JobNotFoundError,
    QueueFullError,
    RequestConflictError,
)
from anchr_docling.schemas import JobResponse, ParseRequest
from anchr_docling.security import require_bearer_token


def create_app(
    *,
    parser: Any | None = None,
    app_settings: Settings | None = None,
) -> FastAPI:
    active_settings = app_settings or settings
    active_parser = parser or DoclingParser()

    @asynccontextmanager
    async def lifespan(application: FastAPI) -> AsyncIterator[None]:
        active_settings.validate_runtime()
        if active_settings.preload_models:
            await asyncio.to_thread(active_parser.preload)
        manager = JobManager(active_parser, active_settings)
        application.state.settings = active_settings
        application.state.jobs = manager
        await manager.start()
        try:
            yield
        finally:
            await manager.stop()

    application = FastAPI(
        title="anchr-docling",
        version="0.2.0",
        lifespan=lifespan,
        docs_url="/docs" if active_settings.enable_docs else None,
        redoc_url="/redoc" if active_settings.enable_docs else None,
        openapi_url="/openapi.json" if active_settings.enable_docs else None,
    )

    @application.get("/healthz")
    def healthz() -> dict[str, object]:
        manager: JobManager = application.state.jobs
        return {
            "status": "ok",
            "running": manager.running_count,
            "queued": manager.queued_count,
            "queueCapacity": manager.capacity,
        }

    @application.post(
        "/v1/jobs",
        response_model=JobResponse,
        status_code=status.HTTP_202_ACCEPTED,
        dependencies=[Depends(require_bearer_token)],
    )
    async def submit_job(request: ParseRequest, response: Response) -> JobResponse:
        manager: JobManager = application.state.jobs
        try:
            record, created = manager.submit(request)
        except QueueFullError as exc:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail=str(exc),
                headers={"Retry-After": "5"},
            ) from exc
        except RequestConflictError as exc:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
        if not created:
            response.status_code = status.HTTP_200_OK
        return record.response()

    @application.get(
        "/v1/jobs/{job_id}",
        response_model=JobResponse,
        dependencies=[Depends(require_bearer_token)],
    )
    async def get_job(job_id: str) -> JobResponse:
        manager: JobManager = application.state.jobs
        try:
            return manager.get(job_id).response()
        except JobNotFoundError as exc:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="job not found",
            ) from exc

    @application.delete(
        "/v1/jobs/{job_id}",
        status_code=status.HTTP_204_NO_CONTENT,
        dependencies=[Depends(require_bearer_token)],
    )
    async def delete_job(job_id: str) -> Response:
        manager: JobManager = application.state.jobs
        try:
            manager.delete(job_id)
        except JobNotFoundError as exc:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="job not found",
            ) from exc
        except ActiveJobError as exc:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="active jobs cannot be deleted",
            ) from exc
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    return application


app = create_app()
