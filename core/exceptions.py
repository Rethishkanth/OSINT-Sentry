from rest_framework.views import exception_handler as drf_exception_handler


def api_exception_handler(exc, context):
    """Give every DRF error the same shape: {"error": {...}}.

    Keeps the frontend from having to branch on DRF's several native error
    layouts (list, dict of field errors, or {"detail": ...}).
    """
    response = drf_exception_handler(exc, context)
    if response is None:
        return None

    detail = response.data
    if isinstance(detail, dict) and set(detail) == {'detail'}:
        payload = {'message': str(detail['detail'])}
    elif isinstance(detail, dict):
        payload = {'message': 'Validation failed', 'fields': detail}
    else:
        payload = {'message': 'Request failed', 'detail': detail}

    payload['status_code'] = response.status_code
    response.data = {'error': payload}
    return response
