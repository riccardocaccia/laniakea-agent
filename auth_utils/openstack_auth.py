from keystoneauth1 import loading
from keystoneauth1 import session
import logging

logger = logging.getLogger(__name__)

def get_openstack_admin_creds():
    ''' access to retrieve OpenStack credentials from Vault '''
    # CHANGE THE VAULT PATH ONCE VAULT BUILD FINAL COMPLETED
    return get_secrets("SECRET/infrastructure/openstack/admin")

def get_keystone_token(aai_token, auth_url, project_id):
    try:
        loader = loading.get_plugin_loader('v3oidcaccesstoken')
        
        auth = loader.load_from_options(
            auth_url=auth_url,
            ### NOTE ###
            identity_provider='recas-bari', 
            ############
            protocol='openid',
            access_token=aai_token,
            project_id=project_id
        )
        
        sess = session.Session(auth=auth, verify=True) 
        
        token_os = sess.get_token()
        logger.info("Token AAI exchange -> OpenStack: Success!")
        return token_os

    except Exception as e:
        logger.error(f"ERROR [OpenStack Auth]: {e}")
        return None
