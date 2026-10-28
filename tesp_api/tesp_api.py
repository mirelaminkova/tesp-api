import asyncio

from loguru import logger

from pydantic.error_wrappers import ValidationError
from starlette.responses import RedirectResponse
from fastapi import FastAPI, APIRouter, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

import tesp_api.service.event_actions
from tesp_api.api.api import api_router
from tesp_api.api.error import api_handle_error, redact_validation_errors
from tesp_api.config.log_config import logg_configure
from tesp_api.config.properties import properties
from tesp_api.repository.task_repository import task_repository
from tesp_api.service.client.client_s3 import get_s3_config
from tesp_api.service import task_recovery
from tesp_api.service.job_polling import get_status_polling
from tesp_api.service.pulsar_service import pulsar_service
from tesp_api.service.resources import get_resource_config

root_router = APIRouter()
_background_tasks = set()
app = FastAPI(title="Tesp API", docs_url="/swagger-ui.html")


@root_router.get("/", status_code=307, include_in_schema=False)
async def get_root() -> RedirectResponse:
    return RedirectResponse(url="swagger-ui.html")


@app.on_event("startup")
async def startup_event():
    logg_configure()
    get_s3_config()  # fail at startup on invalid s3.* settings
    get_resource_config()  # and on invalid container.resource_limits / pulsar.native_specification.*
    get_status_polling()  # and on invalid pulsar.status.*, warning about deprecated ones
    await task_repository.init()
    asyncio.get_event_loop().set_debug(properties.logging.level == "DEBUG")
    # Resumes tasks left by stopped tesp-api processes, now and every task_recovery.SWEEP_SECONDS (issue #52).
    _background_tasks.add(asyncio.create_task(task_recovery.run(pulsar_service.get_operations)))


@app.on_event("shutdown")
async def shutdown_event():
    for task in _background_tasks:
        task.cancel()
    try:
        # The tasks this process waits for are taken over by the next process to start, or by another worker.
        await task_recovery.let_go()
    except Exception as error:
        logger.warning(f'Could not hand over the tasks of this tesp-api process [{type(error).__name__}: {error}]')


@app.exception_handler(ValidationError)
async def validation_error_handler(request: Request, exception: ValidationError):
    return api_handle_error(exception)

@app.exception_handler(RequestValidationError)
async def request_validation_error_handler(request: Request, exception: RequestValidationError):
    return JSONResponse(status_code=422, content={'detail': redact_validation_errors(exception.errors())})

app.include_router(root_router)
app.include_router(api_router)
