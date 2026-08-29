from .celery_app import celery_app


@celery_app.task
def test_task(name: str):
    print(f"Hello from Celery, {name}!")
    return f"Task completed for {name}"