"""
Network discovery for Laniakea agent.

Interrogates Neutron to discover the network topology of the target
OpenStack cloud, returning the names of public/private networks and
whether the cloud uses floating IPs or direct public IPs.

Called by terraform_agent.run_orchestration() before Terraform,
after the Keystone token has been obtained.

Topology detection logic:
  - If networks with router:external=True exist → floating_ip topology
    (GARR-style: VM on private net + floating IP from external pool)
  - Otherwise → direct topology
    (ReCaS-style: shared=True net is public, shared=False is private)
"""

import logging
from typing import Optional

import requests

logger = logging.getLogger(__name__)


def discover_networks(
    neutron_url: str,
    os_token: str,
    network_type: str,
) -> dict:
    """
    Discover network topology and names from Neutron.

    Parameters
    ----------
    neutron_url : str
        Neutron endpoint (from endpoint_overrides_network or catalog).
        e.g. "https://neutron.recas.ba.infn.it/v2.0"
    os_token : str
        Valid scoped Keystone token.
    network_type : str
        "public" or "private" — what the user requested.

    Returns
    -------
    dict with keys:
        topology        : "direct" | "floating_ip"
        public_net_name : str   name of the public/external network
        private_net_name: str   name of the private network
        use_floating_ip : bool  whether Terraform should allocate a floating IP

    Raises
    ------
    RuntimeError if required networks cannot be discovered.
    """
    neutron_url = neutron_url.rstrip("/")
    # Some clouds (e.g. GARR) return the Neutron endpoint without /v2.0
    if not neutron_url.endswith("/v2.0"):
        neutron_url = neutron_url + "/v2.0"
    headers     = {"X-Auth-Token": os_token}

    def _get_all_networks() -> list:
        resp = requests.get(
            f"{neutron_url}/networks",
            headers=headers,
            verify=False,
            timeout=10,
        )
        resp.raise_for_status()
        return resp.json().get("networks", [])

    # Step 1 — fetch all networks and filter client-side
    # Some Neutron versions (e.g. GARR) reject router:external as a query
    # param but return the field correctly in each network object
    all_nets      = _get_all_networks()
    external_nets = [n for n in all_nets if n.get("router:external") is True]
    project_nets  = [n for n in all_nets if not n.get("router:external")]

    if external_nets:
        # GARR-style: floating IP topology
        topology        = "floating_ip"
        use_floating_ip = network_type == "public"

        # public net = first external network (floating IP pool)
        public_net_name = external_nets[0]["name"]

        # private net = first non-external, non-shared network of the project
        project_nets = [n for n in all_nets if not n.get("router:external") and not n.get("shared")]
        if not project_nets:
            project_nets = [n for n in all_nets if not n.get("router:external")]
        if not project_nets:
            raise RuntimeError(
                "Network discovery: no private network found on this cloud. "
                "Cannot deploy with floating_ip topology."
            )
        private_net_name = project_nets[0]["name"]

    else:
        # ReCaS-style: direct topology
        # public net  = shared network (accessible from outside)
        # private net = non-shared network (project-only)
        topology        = "direct"
        use_floating_ip = False

        shared_nets  = [n for n in all_nets if n.get("shared") is True]
        private_nets = [n for n in all_nets if n.get("shared") is False]

        if not shared_nets:
            raise RuntimeError(
                "Network discovery: no shared (public) network found on this cloud. "
                "Cannot deploy with direct topology."
            )
        if not private_nets:
            raise RuntimeError(
                "Network discovery: no private network found on this cloud. "
                "Cannot deploy with direct topology."
            )

        public_net_name  = shared_nets[0]["name"]
        private_net_name = private_nets[0]["name"]

    result = {
        "topology":         topology,
        "public_net_name":  public_net_name,
        "private_net_name": private_net_name,
        "use_floating_ip":  use_floating_ip,
    }

    logger.info(
        f"[network_discovery] topology={topology!r} "
        f"public={public_net_name!r} private={private_net_name!r} "
        f"use_floating_ip={use_floating_ip}"
    )

    return result
