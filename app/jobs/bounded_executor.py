"""
Bounded Background Job Executor Module (Item 6)
Manages asynchronous background jobs with bounded concurrency, queue governance,
DB persistence, duplicate reuse, cancellation, and restart recovery.
"""

import time
import uuid
import logging
import threading
from queue import Queue, Full, Empty
from typing import Dict, Any, Callable, Optional, List, Tuple

logger = logging.getLogger(__name__)

STATUS_QUEUED = "queued"
STATUS_RUNNING = "running"
STATUS_COMPLETED = "completed"
STATUS_FAILED = "failed"
STATUS_CANCELLED = "cancelled"
STATUS_INTERRUPTED = "interrupted"

class JobDescriptor:
    def __init__(
        self,
        job_id: str,
        job_type: str,
        project_name: str,
        fn: Callable,
        args: tuple = (),
        kwargs: dict = None,
        metadata: dict = None,
        resource_class: str = "default",
    ):
        self.job_id = job_id
        self.job_type = job_type
        self.project_name = project_name
        self.fn = fn
        self.args = args or ()
        self.kwargs = kwargs or {}
        self.metadata = metadata or {}
        self.resource_class = resource_class or "default"
        self.status = STATUS_QUEUED
        self.progress: float = 0.0
        self.result: Any = None
        self.error_message: Optional[str] = None
        self.created_at = time.time()
        self.started_at: Optional[float] = None
        self.finished_at: Optional[float] = None
        self.cancel_requested = False

class BoundedJobExecutor:
    def __init__(
        self,
        max_workers: int = 4,
        max_queue_size: int = 100,
        db_manager=None,
        resource_limits: Optional[Dict[str, Any]] = None,
    ):
        self.max_workers = max(1, max_workers)
        self.max_queue_size = max(1, max_queue_size)
        self.db_manager = db_manager
        
        self._jobs: Dict[str, JobDescriptor] = {}
        self._workers: List[threading.Thread] = []
        self._lock = threading.Lock()
        self._shutdown_event = threading.Event()
        self._buckets = {}
        self._resource_limits = dict(resource_limits or {})

        # Keep the historical default queue for callers that do not opt into
        # resource classes. Configured resource classes get independent
        # queues/workers so a disk-heavy export cannot consume DB workers.
        self._create_bucket("default", self.max_workers, self.max_queue_size)
        for resource_name, raw_limit in self._resource_limits.items():
            if str(resource_name) == "default":
                continue
            if isinstance(raw_limit, dict):
                workers = raw_limit.get("max_workers", raw_limit.get("workers", 1))
                queue_size = raw_limit.get("max_queue_size", raw_limit.get("queue_size", 25))
            else:
                workers, queue_size = raw_limit, 25
            self._create_bucket(
                str(resource_name), max(1, int(workers)), max(1, int(queue_size))
            )
        self._queue = self._buckets["default"]["queue"]
        self._start_workers()

    def _create_bucket(self, name: str, workers: int, queue_size: int) -> None:
        if name in self._buckets:
            return
        self._buckets[name] = {
            "queue": Queue(maxsize=queue_size),
            "max_workers": max(1, int(workers)),
            "max_queue_size": max(1, int(queue_size)),
            "workers": [],
        }

    def _start_workers(self):
        for resource_name, bucket in self._buckets.items():
            for i in range(bucket["max_workers"]):
                suffix = "-{}".format(i)
                name = "CoverageJobWorker-{}{}".format(resource_name, suffix)
                t = threading.Thread(
                    target=self._worker_loop,
                    args=(resource_name, bucket["queue"]),
                    name=name,
                    daemon=True,
                )
                t.start()
                bucket["workers"].append(t)
                self._workers.append(t)

    def _worker_loop(self, resource_name: str, queue: Queue):
        while not self._shutdown_event.is_set():
            try:
                job = queue.get(timeout=0.5)
            except Empty:
                continue

            with self._lock:
                if job.cancel_requested:
                    job.status = STATUS_CANCELLED
                    job.finished_at = time.time()
                    self._persist_job_state(job)
                    queue.task_done()
                    continue
                job.status = STATUS_RUNNING
                job.started_at = time.time()
                self._persist_job_state(job)

            try:
                # Execute job function
                res = job.fn(*job.args, **job.kwargs)
                with self._lock:
                    if job.cancel_requested:
                        job.status = STATUS_CANCELLED
                    else:
                        job.status = STATUS_COMPLETED
                        job.result = res
                        job.progress = 1.0
                    job.finished_at = time.time()
                    self._persist_job_state(job)
            except Exception as e:
                logger.error(f"[JobExecutor] Job {job.job_id} failed: {e}", exc_info=True)
                with self._lock:
                    job.status = STATUS_FAILED
                    job.error_message = str(e)
                    job.finished_at = time.time()
                    self._persist_job_state(job)
            finally:
                queue.task_done()

    def submit_job(
        self,
        job_type: str,
        project_name: str,
        fn: Callable,
        args: tuple = (),
        kwargs: dict = None,
        job_id: Optional[str] = None,
        reuse_existing: bool = True,
        metadata: Optional[dict] = None,
        resource_class: str = "default",
    ) -> JobDescriptor:
        """Submit a new background job with optional duplicate reuse."""
        kwargs = kwargs or {}
        with self._lock:
            # 1. Check duplicate reuse
            if reuse_existing:
                for existing in self._jobs.values():
                    if (existing.job_type == job_type and 
                        existing.project_name == project_name and 
                        existing.status in (STATUS_QUEUED, STATUS_RUNNING)):
                        return existing

            resource_class = str(resource_class or "default")
            if resource_class not in self._buckets:
                resource_class = "default"
            bucket = self._buckets[resource_class]
            jid = job_id or f"job_{uuid.uuid4().hex[:16]}"
            job = JobDescriptor(
                job_id=jid,
                job_type=job_type,
                project_name=project_name,
                fn=fn,
                args=args,
                kwargs=kwargs,
                metadata=metadata,
                resource_class=resource_class,
            )
            
            try:
                bucket["queue"].put_nowait(job)
                self._jobs[jid] = job
                self._persist_job_state(job)
                return job
            except Full:
                raise RuntimeError(
                    "Background job queue is full for resource '{}' ({} pending jobs).".format(
                        resource_class, bucket["max_queue_size"]
                    )
                )

    def cancel_job(self, job_id: str) -> bool:
        """Request job cancellation."""
        with self._lock:
            job = self._jobs.get(job_id)
            if not job:
                return False
            if job.status in (STATUS_COMPLETED, STATUS_FAILED, STATUS_CANCELLED):
                return False
            job.cancel_requested = True
            if job.status == STATUS_QUEUED:
                job.status = STATUS_CANCELLED
                job.finished_at = time.time()
                self._persist_job_state(job)
            return True

    def get_job(self, job_id: str) -> Optional[JobDescriptor]:
        with self._lock:
            return self._jobs.get(job_id)

    def metrics(self) -> Dict[str, Any]:
        """Return queue/worker counts split by resource class."""
        with self._lock:
            jobs = len(self._jobs)
            resources = {
                name: {
                    "workers": bucket["max_workers"],
                    "queue_capacity": bucket["max_queue_size"],
                    "queued": bucket["queue"].qsize(),
                    "active": sum(
                        1 for job in self._jobs.values()
                        if job.resource_class == name and job.status == STATUS_RUNNING
                    ),
                }
                for name, bucket in self._buckets.items()
            }
        return {"jobs": jobs, "resources": resources}

    def _persist_job_state(self, job: JobDescriptor):
        """Save job state to DB if db_manager provided."""
        if not self.db_manager:
            return
        try:
            if hasattr(self.db_manager, "upsert_background_job"):
                self.db_manager.upsert_background_job(
                    job_id=job.job_id,
                    project_name=job.project_name,
                    job_type=job.job_type,
                    status=job.status,
                    progress=job.progress,
                    error_message=job.error_message
                )
        except Exception as e:
            logger.warning(f"[JobExecutor] Error persisting job {job.job_id}: {e}")

    def recover_interrupted_jobs(self):
        """Mark orphaned running jobs as interrupted on server start."""
        if not self.db_manager:
            return
        try:
            if hasattr(self.db_manager, "mark_stale_running_jobs_interrupted"):
                self.db_manager.mark_stale_running_jobs_interrupted()
        except Exception as e:
            logger.warning(f"[JobExecutor] Error recovering jobs: {e}")

    def shutdown(self, wait: bool = True):
        self._shutdown_event.set()
        if wait:
            for t in self._workers:
                t.join(timeout=1.0)
