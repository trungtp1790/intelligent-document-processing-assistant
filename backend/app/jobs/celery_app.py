from celery import Celery

from app.config import settings, validate_runtime_settings

validate_runtime_settings()

celery_app = Celery("idp", broker=settings.redis_url, backend=settings.redis_url, include=["app.jobs.tasks"])
celery_app.conf.update(
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    worker_prefetch_multiplier=1,
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    task_track_started=True,
    broker_connection_retry_on_startup=True,
    task_routes={
        "app.jobs.tasks.process_invoice_job": {"queue": "invoice"},
        "app.jobs.tasks.resume_invoice_graph": {"queue": "invoice"},
    },
)
