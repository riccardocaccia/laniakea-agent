# Unpack the job.json\ info
from terraform_agent import Job, run_orchestration

def run_from_dict(job_dict):
    job = Job(**job_dict)
    return run_orchestration(job)
