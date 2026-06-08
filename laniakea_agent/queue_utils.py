"""
helper to re-queue a job when quota is insufficient.
"""
import os
import json
from redis import Redis
from rq import Queue

REDIS_HOST     = os.getenv("REDIS_HOST", "")
REDIS_PORT     = int(os.getenv("REDIS_PORT", "6379"))
REDIS_PASSWORD = os.getenv("REDIS_PASSWORD", "")
RETRY_COUNT_FIELD = "_quota_retry_count"

def requeue_job(job, retry_count: int) -> None:
    """
    Re-enqueue the job with an incremented retry counter
    """
    r = Redis(
        host=REDIS_HOST,
        port=REDIS_PORT,
        password=REDIS_PASSWORD,
        decode_responses=False,
    )
    provider = job.selected_provider.lower()
    q = Queue(provider, connection=r)

    job_dict = job.model_dump()
    job_dict[RETRY_COUNT_FIELD] = retry_count

    q.enqueue(
        "laniakea_agent.worker_wrapper.run_from_dict",
        job_dict,
        job_timeout="10h",
    )
