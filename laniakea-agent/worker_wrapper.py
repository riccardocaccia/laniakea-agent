# transform the job (JWT obj) dict in a pydantic object
# then run the terraform_agent core script
from laniakea_agent.terraform_agent import Job, run_orchestration

def run_from_dict(job_dict):
    job = Job(**job_dict)
    return run_orchestration(job)
