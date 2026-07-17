# FIXME: update

from keystoneauth1 import loading
from keystoneauth1 import session
import logging

logger = logging.getLogger(__name__)

def get_openstack_admin_creds():
    '''
    access to retrieve OpenStack credentials from Vault 
    '''
    # NOTE: CHANGE THE VAULT PATH ONCE VAULT BUILD FINAL COMPLETED
    return get_secrets("SECRET/infrastructure/openstack/admin")

def get_keystone_token(aai_token, auth_url, project_id, identity_provider=""):
    """
    Exchange an OIDC AAI token for a Keystone token via the federated
    identity provider configured for the target cloud.
    Returns None (skip, no error) when no identity_provider is configured:
    clouds without OIDC federation (e.g. GARR) use app credentials.
    """
    if not identity_provider:
        logger.info("[OpenStack Auth] No Keystone identity provider configured for this cloud — skipping OIDC exchange.")
        return None
    try:
        loader = loading.get_plugin_loader('v3oidcaccesstoken')

        auth = loader.load_from_options(
            auth_url=auth_url,
            identity_provider=identity_provider,
            protocol='openid',
            access_token=aai_token,
            project_id=project_id
        )
        
        sess = session.Session(auth=auth, verify=True) 
        token_os = sess.get_token()
        logger.info("Token AAI exchange: success!")
        return token_os

    except Exception as e:
        logger.error(f"ERROR [OpenStack Auth]: {e}")
        return None
