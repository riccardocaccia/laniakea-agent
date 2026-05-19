"""
This file must reside in the working directory from which laniakea-agent 
is launched (or where the RQ worker runs directly).

RQ imports job functions as strings: "worker_wrapper.run_from_dict".
To locate this module, RQ searches within sys.path, the working directory 
is always the first entry, ensuring this file is found before the package.

If you are using the laniakea-agent command, this file will be created 
automatically in the current directory upon the first startup if it does not exist.
"""

from laniakea_agent.worker_wrapper import run_from_dict  # noqa: F401
