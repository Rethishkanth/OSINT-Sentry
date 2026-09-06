"""Makes the acting user visible to model signals.

Signals fire deep inside the ORM with no access to the request, so the audit
writer cannot otherwise tell who made a change. This stashes the current
request for the duration of the response cycle.

The actor is resolved lazily, on read, rather than captured up front: DRF
authenticates JWTs inside the view, well after middleware runs, so reading
request.user here would record AnonymousUser for every token-authenticated API
call. DRF writes the authenticated user back onto the underlying HttpRequest,
so by the time a signal fires the attribute is correct.
"""

import contextvars

_current_request = contextvars.ContextVar('audit_request', default=None)


def get_current_actor():
    """The authenticated user for this request, or None."""
    request = _current_request.get()
    if request is None:
        return None
    user = getattr(request, 'user', None)
    if user is None or not user.is_authenticated:
        return None
    return user


def get_current_ip():
    request = _current_request.get()
    if request is None:
        return None
    forwarded = request.META.get('HTTP_X_FORWARDED_FOR', '')
    if forwarded:
        return forwarded.split(',')[0].strip()
    return request.META.get('REMOTE_ADDR')


class AuditActorMiddleware:
    """Bind the request to the audit context for its lifetime only."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        token = _current_request.set(request)
        try:
            return self.get_response(request)
        finally:
            # Reset even on error, so a failed request cannot leak its actor
            # into whatever this thread handles next.
            _current_request.reset(token)
