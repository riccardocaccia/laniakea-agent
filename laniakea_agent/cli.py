"""
Entry point for laniakea-agent.

After running `pip install laniakea-agent`, the user can launch it with:
    laniakea-agent --queue openstack
    laniakea-agent --queue aws
    laniakea-agent --queue openstack --queue aws   # listens to both

The process loads the .env file from the current directory, connects to Redis, 
and spawns the RQ Worker listening on the specified queues.

RQ imports 'worker_wrapper.run_from_dict' by its string name, this works 
because laniakea_agent is an installed package and worker_wrapper is accessible 
as laniakea_agent.worker_wrapper. However, a local worker_wrapper in the working 
directory (if present) takes precedence, allowing for overrides without 
reinstalling the package.
"""

import argparse
import os
import sys


def main():
    parser = argparse.ArgumentParser(
        prog="laniakea-agent",
        description="Laniakea deployment agent which consumes jobs from Redis queues.",
    )
    parser.add_argument(
        "--queue", "-q",
        action="append",
        dest="queues",
        default=None,
        metavar="NAME",
        help="Queue name to listen on (e.g. openstack, aws). Repeat for multiple queues.",
    )
    parser.add_argument(
        "--env", "-e",
        default=".env",
        metavar="FILE",
        help="Path to .env file (default: .env in current directory).",
    )
    parser.add_argument(
        "--version", "-v",
        action="store_true",
        help="Print version and exit.",
    )
    args = parser.parse_args()
 
    if args.version:
        from laniakea_agent import __version__
        print(f"laniakea-agent {__version__}")
        sys.exit(0)
 
    # load .env
    env_path = os.path.abspath(args.env)
    if os.path.exists(env_path):
        from dotenv import load_dotenv
        load_dotenv(env_path)
        print(f"[config] loaded {env_path}")
    else:
        print(f"[config] .env not found at {env_path} using environment variables")
 
    # validate required env vars
    missing = [v for v in ["REDIS_HOST", "REDIS_PASSWORD", "AGENT_MASTER_PASSWORD", "LANIAKEA_API_URL"]
               if not os.getenv(v)]
    if missing:
        print(f"[error] missing required environment variables: {', '.join(missing)}")
        print("         set them in .env or export them before running laniakea-agent")
        sys.exit(1)
 
    # default queue
    queue_names = args.queues or ["openstack"]
 
    # connect Redis
    from redis import Redis
    from rq import Worker, Queue
 
    redis_conn = Redis(
        host=os.getenv("REDIS_HOST"),
        port=int(os.getenv("REDIS_PORT", "6379")),
        password=os.getenv("REDIS_PASSWORD"),
        decode_responses=False,
    )
 
    queues = [Queue(name, connection=redis_conn) for name in queue_names]
 
    print(f"[agent] laniakea-agent listening on queues: {queue_names}")
    print(f"[agent] redis: {os.getenv('REDIS_HOST')}:{os.getenv('REDIS_PORT', '6379')}")
    print(f"[agent] api:   {os.getenv('LANIAKEA_API_URL')}")
    print(f"[agent] id:    {os.getenv('AGENT_ID', 'laniakea-agent')}")
 
    # start heartbeat loop in background thread
    import threading
    from laniakea_agent.quota_check import send_heartbeat
 
    os_auth_url = os.getenv("OS_AUTH_URL", "")
    os_region   = os.getenv("OS_REGION_NAME", "RegionOne")
    provider    = os.getenv("AGENT_PROVIDER", "openstack")
 
    def _heartbeat_loop():
        import time
        agent_id = os.getenv("AGENT_ID", "laniakea-agent")
        while True:
            try:
                # Heartbeat sends quota info without a user token —
                # quota will be empty but the heartbeat itself signals
                # the agent is alive. Per-job quota is checked at pickup
                # using the user's Keystone token.
                send_heartbeat(
                    agent_id=agent_id,
                    provider=provider,
                    os_auth_url=os_auth_url,
                    region=os_region,
                    os_token="",  # no user token available at agent level
                )
            except Exception:
                pass
            time.sleep(30)
 
    hb_thread = threading.Thread(target=_heartbeat_loop, daemon=True)
    hb_thread.start()
    print(f"[agent] heartbeat loop started (every 30s)")
 
    Worker(queues, connection=redis_conn).work()
 
 
if __name__ == "__main__":
    main()

