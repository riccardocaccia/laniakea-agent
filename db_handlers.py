import psycopg2
from psycopg2.extras import RealDictCursor
import datetime

def get_db_connection():
    """
    Connection function to write and retrieve informations from the Data base
    """
    # NOTE: CHANGE HERE
    return psycopg2.connect(
        host="localhost",
        database="orchestrator_db",
        user="orchestrator_user",
        password="la_tua_password"
    )

def start_log_deployment(deployment_uuid, status="IN_PROGRESS"):
    """
    Initializes or updates the deployment record in the tracking database.

    This function performs an 'UPSERT' operation:
    - If the deployment UUID is new, it creates a fresh entry with timestamps and the initial status.
    - If the UUID already exists, it updates the status and 'update_time' to reflect the restart.

    This ensures that the orchestration lifecycle is traceable by external monitoring tools 
    or dashboards from the very beginning of the process.
    """
    conn = get_db_connection()
    # create a cursor that will write inside the db
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO deployments (uuid, status, creation_time, update_time) 
        VALUES (%s, %s, %s, %s) 
        ON CONFLICT (uuid) 
        DO UPDATE SET status=%s, update_time=%s
        """,
        (deployment_uuid, status, datetime.datetime.now(), datetime.datetime.now(), status, datetime.datetime.now())
    )
    conn.commit()
    cur.close()
    conn.close()

def update_log_status(deployment_uuid, status, logs=None, ip_address=None):
    """
    This function, updates the deployment records.

    'Update log status' performs an UPDATE of the status and time of a deployment in real time. 
    """
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute(
        """
        UPDATE deployments 
        SET status=%s, status_reason=%s, update_time=%s, outputs=%s 
        WHERE uuid=%s
        """,
        (status, logs, datetime.datetime.now(), ip_address, deployment_uuid)
    )
    conn.commit()
    cur.close()
    conn.close()
