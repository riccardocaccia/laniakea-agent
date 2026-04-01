# Script to let agent listening on a specified redis queue: ONLY TEST

from redis import Redis
from rq import Worker, Queue

r = Redis(host='', port=6379,
          username='agent_name', password='secret_queue_openstack')
q = Queue('openstack', connection=r)

if __name__ == '__main__':
    print("OpenStack AGENT NAME listening...")
    Worker([q], connection=r).work()
