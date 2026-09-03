import asyncio
import logging
from dataclasses import dataclass, field

log = logging.getLogger(__name__)


@dataclass
class Job:
    id: str
    status: str = "queued"
    result: dict | None = None
    error: str | None = None


class JobManager:
    def __init__(self):
        self.jobs: dict[str, Job] = {}
        self.processed_events: set[str] = set()

    def accept_event(self, event_id: str) -> bool:
        if event_id in self.processed_events:
            return False
        self.processed_events.add(event_id)
        return True

    def submit(self, job_id: str, operation) -> Job:
        job = Job(job_id)
        self.jobs[job_id] = job
        log.info("Agent job queued job_id=%s", job_id)

        async def execute():
            job.status = "running"
            log.info("Agent job started job_id=%s", job_id)
            try:
                job.result = await operation()
                job.status = "completed"
                log.info("Agent job completed job_id=%s", job_id)
            except Exception as exc:
                job.error = str(exc)
                job.status = "failed"
                log.error("Agent job failed job_id=%s error=%s exception_type=%s",
                          job_id, exc, type(exc).__name__, exc_info=True)

        asyncio.create_task(execute())
        return job
