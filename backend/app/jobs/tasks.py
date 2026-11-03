from app.config import settings
from app.jobs.celery_app import celery_app
from app.services.extraction import process_invoice_job


@celery_app.task(name="app.jobs.tasks.process_invoice_job")
def process_invoice_task(job_id: str) -> None:
    if settings.workflow_engine == "langgraph":
        from app.workflows.invoice_graph import run_invoice_graph

        run_invoice_graph(job_id)
    else:
        process_invoice_job(job_id)


@celery_app.task(name="app.jobs.tasks.resume_invoice_graph")
def resume_invoice_graph_task(job_id: str, decision_id: str) -> None:
    from app.workflows.invoice_graph import run_invoice_graph

    run_invoice_graph(job_id, decision_id)
