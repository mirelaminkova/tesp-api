import time
from http import HTTPStatus
from typing import List

from loguru import logger
from bson.errors import InvalidId
from fastapi.encoders import jsonable_encoder
from fastapi.responses import Response
from pydantic.error_wrappers import ValidationError

from tesp_api.repository.error import CustomDataLayerError
from tesp_api.api.model.response_models import ErrorResponseModel


def get_error_response_model(error_status: HTTPStatus, message: str) -> ErrorResponseModel:
    return ErrorResponseModel(
        timestamp=int(time.time()),
        error=error_status.phrase,
        status=error_status.value,
        message=message)


def get_response_for_error_model(error_model: ErrorResponseModel) -> Response:
    return Response(
        error_model.json(),
        status_code=error_model.status,
        media_type='application/json')


def redact_validation_errors(errors: List[dict]) -> List[dict]:
    """
    JSON-ready request validation errors without the parts that repeat the request. For a URL like
    ``s3://key:sec/ret@host/bucket/key`` pydantic would echo ``:sec/ret@host/...`` back in ``msg`` and ``ctx``.
    """
    redacted = []
    for error in errors:
        ctx = error.get('ctx') or {}
        # A JSON decode error's ctx holds the parser message (shown in msg) and the whole request body (doc).
        echoed = [value for key, value in ctx.items()
                  if isinstance(value, str) and not (error['type'] == 'value_error.jsondecode' and key == 'msg')]
        msg = error['msg']
        if any(value in msg for value in echoed):
            msg = REDACTED_MESSAGES.get(error['type'], 'invalid value')
        safe_ctx = {key: value for key, value in ctx.items() if not isinstance(value, str)}
        redacted.append({'loc': error['loc'], 'msg': msg, 'type': error['type'],
                         **({'ctx': safe_ctx} if safe_ctx else {})})
    return jsonable_encoder(redacted)


REDACTED_MESSAGES = {'value_error.url.extra': 'URL invalid, extra characters found after valid URL'}


def api_handle_error(error: Exception) -> Response:
    match error:

        # All Database exceptions, response must not leak sensitive info
        case CustomDataLayerError() as data_layer_error:
            error_model = get_error_response_model(HTTPStatus.INTERNAL_SERVER_ERROR, message=str(data_layer_error))
            return get_response_for_error_model(error_model)

        # Model validation exception, client's failure therefore not logged
        case ValidationError() as request_validation_error:
            exc_str = f'{request_validation_error}'.replace('\n', ' ').replace('   ', ' ')
            error_model = get_error_response_model(HTTPStatus.UNPROCESSABLE_ENTITY, message=exc_str)
            return get_response_for_error_model(error_model)

        # ID validation exception, client's failure therefore not logged
        case InvalidId() as invalid_id_error:
            error_model = get_error_response_model(HTTPStatus.BAD_REQUEST, message=str(invalid_id_error))
            return get_response_for_error_model(error_model)

        # Default case, logs any unexpected error while response must not leak sensitive info
        case _ as unknown_error:
            logger.error(str(unknown_error))
            error_model = get_error_response_model(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                message="Unknown error occurred, contact system administrator")
            return get_response_for_error_model(error_model)
