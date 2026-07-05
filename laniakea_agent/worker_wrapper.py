# transform the job (JWT obj) dict in a pydantic object
# then run the terraform_agent core script
from laniakea_agent.terraform_agent import Job, run_orchestration
from laniakea_agent.destroy import run_destroy
from laniakea_agent.api_client import update_deployment_status

def run_from_dict(job_dict: dict):
    job = Job(**job_dict)
    return run_orchestration(job)

def destroy_from_dict(job_dict: dict):
    """
    Entrypoint for destroy jobs enqueued by the API
    (DELETE /api/deployments/{uuid} on a CREATE_COMPLETE deployment).
    """
    job  = Job(**job_dict)
    uuid = job.deployment_uuid
    ok = run_destroy(job)
    if ok:
        update_deployment_status(uuid, "DELETE_COMPLETE",
                                 status_reason="Resources destroyed via dashboard request.")
    else:
        update_deployment_status(uuid, "DELETE_FAILED",
                                 status_reason="Terraform destroy failed: check agent logs.")
