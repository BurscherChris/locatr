import asyncio
from dataclasses import dataclass, field

@dataclass
class Job:
    id: str
    status: str = "queued"
    result: dict | None = None
    error: str | None = None

class JobManager:
    def __init__(self): self.jobs: dict[str, Job] = {}; self.processed_events: set[str] = set()
    def accept_event(self, event_id: str) -> bool:
        if event_id in self.processed_events: return False
        self.processed_events.add(event_id); return True
    def submit(self, job_id: str, operation) -> Job:
        job = Job(job_id); self.jobs[job_id] = job
        async def execute():
            job.status = "running"
            try: job.result = await operation(); job.status = "completed"
            except Exception as exc: job.error = str(exc); job.status = "failed"
        asyncio.create_task(execute())
        return job
