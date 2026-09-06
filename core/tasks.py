from celery import shared_task


@shared_task
def add(x, y):
    """Smoke-test task: proves the broker and worker are wired up."""
    return x + y
