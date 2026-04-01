# NOTE: to be completed and modified once THE FINAL VERSION OF VAULT IS UP

def get_vault_client():
    client = hvac.Client(
        url='',
        token=''
    )
    return client

def get_secrets(path):
    client = get_vault_client()
    try:
        # Try read with KV Version 2
        read_response = client.secrets.kv.v2.read_secret_version(
            mount_point='',
            path=path
        )
        return read_response['data']['data']
    except Exception:
        try:
            # If it fails, try KV Version 1
            read_response = client.secrets.kv.v1.read_secret(
                mount_point='',
                path=path
            )
            return read_response['data']
        except Exception as e:
            print(f"Error: {e}")
            return None
