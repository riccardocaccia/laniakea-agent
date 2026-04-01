# transform the job dict in a job object
# readed by the terraform_agent
from terraform_agent import Job, run_orchestration

def run_from_dict(job_dict):
    job = Job(**job_dict)
    return run_orchestration(job)
