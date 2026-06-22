"""
helper to re-queue a job when quota is insufficient.
"""
import os
import json
from redis import Redis
from rq import Queue

# REDIS host
REDIS_HOST     = os.getenv("REDIS_HOST", "")
REDIS_PORT     = int(os.getenv("REDIS_PORT", "1908"))
REDIS_PASSWORD = os.getenv("REDIS_PASSWORD", "")
RETRY_COUNT_FIELD = "_quota_retry_count"   # possible retry for a job

def requeue_job(job, retry_count: int) -> None:
    """
    Re-enqueue the job with an incremented retry counter
    """
    r = Redis(
        host=REDIS_HOST,
        port=REDIS_PORT,
        password=REDIS_PASSWORD,
        decode_responses=False,    # I don't care, we work with RQ and binary file
    )
    provider = job.selected_provider.lower()
    q = Queue(provider, connection=r)

    job_dict = job.model_dump()
    job_dict[RETRY_COUNT_FIELD] = retry_count

    q.enqueue(
        "laniakea_agent.worker_wrapper.run_from_dict",
        job_dict,
        job_timeout="10h",   # gives 10h to complete the job before killing it
    )
