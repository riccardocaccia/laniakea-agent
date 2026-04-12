import os
import sys
from redis import Redis
from rq import Worker, Queue

r = Redis(
    host=os.getenv("REDIS_HOST", ""),
    port=int(os.getenv("REDIS_PORT", "6379")),
    password=os.getenv("REDIS_PASSWORD", ""),
    decode_responses=False,)

# which queue to listen
q = Queue('openstack', connection=r)

# wait until we put jobs in the queue 
if __name__ == '__main__':
    print("OpenStack agent listening on...")
    Worker([q], connection=r).work()
