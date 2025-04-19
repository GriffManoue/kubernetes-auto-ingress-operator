import kopf
import kubernetes
import logging
from kubernetes.client import (
    V1Ingress, V1IngressSpec, V1IngressRule, V1HTTPIngressRuleValue,
    V1HTTPIngressPath, V1IngressBackend, V1IngressServiceBackend,
    V1ServiceBackendPort, V1ObjectMeta, V1OwnerReference
)
from kubernetes.client.exceptions import ApiException

# Configure logging for the operator
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Define the annotation key used to trigger Ingress creation
AUTO_INGRESS_ANNOTATION = 'auto-ingress'

# Register handlers for Service events (create, update, resume)
@kopf.on.resume('', 'v1', 'services')
@kopf.on.update('', 'v1', 'services')
@kopf.on.create('', 'v1', 'services')
def manage_ingress(spec, meta, status, body, namespace, labels, name, annotations, diff, logger, old=None, **kwargs):
    """Manages Ingress resources based on Service annotations."""
    logger.info(f"Processing service {namespace}/{name}")

    # Get the Ingress path from the annotation
    auto_ingress_path = annotations.get(AUTO_INGRESS_ANNOTATION)
    # Create the resource name
    ingress_name = f"auto-ingress-{name}-http"
    # Initialize Kubernetes API
    networking_v1 = kubernetes.client.NetworkingV1Api()

    # Get the previous state of the annotation
    old_auto_ingress_path = None
    if old:
        old_annotations = old.get('metadata', {}).get('annotations', {})
        old_auto_ingress_path = old_annotations.get(AUTO_INGRESS_ANNOTATION)

    # --- Scenario: Annotation is present --- 
    if auto_ingress_path:
        logger.info(f"Found/Updated 'auto-ingress: {auto_ingress_path}' annotation for service {namespace}/{name}.")
        
        # Extract port information from the Service spec
        ports = spec.get('ports')
        if not ports:
            logger.warning(f"Service {namespace}/{name} has no ports defined. Cannot create/update Ingress.")
            return None # Stop if no ports are found
            
        # Use the first port defined in the service
        port_info = ports[0]
        service_port_name = port_info.get('name')
        service_port_number = port_info.get('port')
        if not service_port_name and not service_port_number:
            logger.warning(f"Service {namespace}/{name} has no name or port defined for the first port entry. Cannot create/update Ingress.")
            return None # Stop if port info is incomplete

        # Define the backend port for the Ingress rule.
        # Use port name if available, otherwise use the port number.
        backend_port = V1ServiceBackendPort(name=service_port_name) if service_port_name else V1ServiceBackendPort(number=service_port_number)
        logger.info(f"Using service port '{service_port_name or service_port_number}' for Ingress backend.")

        # Get Service UID and API version for setting OwnerReference
        service_uid = meta.get('uid')
        service_api_version = body.get('apiVersion')

        # Define the Ingress resource object
        ingress_def = V1Ingress(
            api_version="networking.k8s.io/v1",
            kind="Ingress",
            metadata=V1ObjectMeta(
                name=ingress_name,
                annotations={
                    # Add Traefik-specific annotation
                    "traefik.ingress.kubernetes.io/router.entrypoints": "web"
                },
                # Set the Service as the owner of this Ingress resource, for garbage collection
                owner_references=[
                    V1OwnerReference(
                        api_version=service_api_version,
                        kind='Service',
                        name=name,
                        uid=service_uid,
                        controller=True, 
                        block_owner_deletion=True
                    )
                ]
            ),
            spec=V1IngressSpec(
                rules=[
                    V1IngressRule(
                        http=V1HTTPIngressRuleValue(
                            paths=[
                                V1HTTPIngressPath(
                                    path=auto_ingress_path, 
                                    path_type="Prefix",
                                    backend=V1IngressBackend(
                                        service=V1IngressServiceBackend(
                                            name=name,
                                            port=backend_port
                                        )
                                    )
                                )
                            ]
                        )
                    )
                ]
            )
        )
        logger.info(f"Creating/Updating Ingress {ingress_name} in namespace {namespace} with path '{auto_ingress_path}' pointing to service port '{service_port_name or service_port_number}'")

        # Try to create or update the Ingress resource
        try:
            existing_ingress = None
            # Check if the Ingress already exists
            try:
                existing_ingress = networking_v1.read_namespaced_ingress(name=ingress_name, namespace=namespace)
            except ApiException as e:
                if e.status != 404: # Ignore "Not Found" errors
                    logger.error(f"Error checking for existing Ingress: {e}")
                    raise # Re-raise other API errors

            # If Ingress exists, update it; otherwise, create it.
            if existing_ingress:
                logger.info(f"Ingress {ingress_name} already exists. Updating.")
                networking_v1.replace_namespaced_ingress(name=ingress_name, namespace=namespace, body=ingress_def)
            else:
                logger.info(f"Creating new Ingress {ingress_name}.")
                networking_v1.create_namespaced_ingress(namespace=namespace, body=ingress_def)

        except ApiException as e:
            logger.error(f"Failed to create or update Ingress {ingress_name}: {e}")
            raise

    # --- Scenario: Annotation is NOT present --- 
    else:
        logger.info(f"No 'auto-ingress' annotation found for service {namespace}/{name}.")
        # Check if the annotation was present in the previous state (possibly deleted)
        if old_auto_ingress_path:
            logger.info(f"'auto-ingress' annotation was removed. Deleting Ingress {ingress_name}.")
            # Attempt to delete the Ingress resource
            try:
                networking_v1.delete_namespaced_ingress(name=ingress_name, namespace=namespace)
                logger.info(f"Successfully deleted Ingress {ingress_name}.")
            except ApiException as e:
                if e.status == 404: # If already deleted, log it as info
                    logger.info(f"Ingress {ingress_name} already deleted.")
                else: # For other errors, log and re-raise
                    logger.error(f"Failed to delete Ingress {ingress_name}: {e}")
                    raise # Re-raise unexpected errors
        else:
            # Annotation is not present now and wasn't present before.
            logger.info(f"Ingress {ingress_name} does not need to be created or deleted.")

# Register handler for Service deletion events
@kopf.on.delete('', 'v1', 'services')
def log_service_deletion(meta, name, namespace, logger, **kwargs):
    """Logs when a Service is deleted."""
    # Note: Actual Ingress deletion is handled by Kubernetes garbage collection.
    logger.info(f"Service {namespace}/{name} deleted. Corresponding Ingress (if any) will be garbage-collected.")
