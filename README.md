# TODO

set the env variables before runnig worker\_test\_redis.py.

worker\_test\_multi\_agent.py
  └── Worker.work() [wait]
        └── job arriva da Redis
              └── worker_wrapper.run_from_dict(dict)
                    └── terraform_agent.run_orchestration(job)
                          ├── api_client → CREATE_IN_PROGRESS
                          ├── vault_utils → legge credenziali
                          ├── Docker/Terraform → crea VM → ottieni IP
                          ├── ansible_agent → configura VM
                          │     ├── prepare_environment()
                          │     ├── execute_deployment()
                          │     └── cleanup()
                          └── [successo] api_client → CREATE_COMPLETE + email
                              [fallimento] destroy + api_client → CREATE_FAILED + email

