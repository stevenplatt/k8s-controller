import datetime
import logging
import sys
import kopf
import kubernetes
import time
import random
import os
from dateutil.relativedelta import relativedelta
from dateutil.parser import isoparse


# --- Initial execution print (for very basic check) ---
print(f"[{datetime.datetime.now(datetime.timezone.utc).isoformat()}] Operator script execution started.", flush=True)


# --- Configure logging ---
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[logging.StreamHandler(sys.stdout)] # Ensure logs go to stdout
)
logger = logging.getLogger(__name__)
logger.info("Operator script's root logger configured.")
logger.info(f"Python version: {sys.version}")
logger.info(f"Kopf version: {kopf.__version__}")
logger.info(f"Kubernetes client version: {kubernetes.__version__}")


# --- Constants ---
OPERATOR_NAMESPACE = os.environ.get("OPERATOR_NAMESPACE", "default")
API_GROUP = "stable.example.com"
API_VERSION = "v1alpha1"
CRD_PLURAL = "noderefreshes"
DEFAULT_REFRESH_DAYS = 3
DEFAULT_COOLDOWN_SECONDS = 300
RETRY_DELAY_SECONDS = 30
MAX_RETRIES = 5


# --- Kubernetes API Clients ---
core_v1_api = None
custom_objects_api = None
apps_v1_api = None


try:
    logger.info("Attempting to load Kubernetes configuration...")
    try:
        kubernetes.config.load_incluster_config()
        logger.info("Successfully loaded in-cluster Kubernetes config.")
    except kubernetes.config.ConfigException as e1:
        logger.info(f"In-cluster config loading failed (Error: {e1}). Trying kube-config (for local development).")
        try:
            kubernetes.config.load_kube_config()
            logger.info("Successfully loaded local kube-config.")
        except kubernetes.config.ConfigException as e2:
            logger.error(f"CRITICAL: Could not configure Kubernetes client after trying in-cluster and local kube-config. Error (kube-config): {e2}")
            logger.error("Ensure your KUBECONFIG environment variable is set correctly if running locally, or that the operator's ServiceAccount has permissions if running in-cluster.")
            raise RuntimeError(f"Kubernetes client configuration failed. In-cluster error: {e1}. Kube-config error: {e2}")

    # Initialize API clients only after successful config loading
    core_v1_api = kubernetes.client.CoreV1Api()
    custom_objects_api = kubernetes.client.CustomObjectsApi()
    apps_v1_api = kubernetes.client.AppsV1Api()
    logger.info("Kubernetes API clients initialized successfully.")

except RuntimeError as e:
    logger.critical(f"Fatal error during Kubernetes client initialization: {e}")
    sys.exit(1)
except Exception as e:
    logger.critical(f"An unexpected critical error occurred during initial setup: {e}", exc_info=True)
    sys.exit(1)


# --- Helper Functions ---
def format_label_selector(labels):
    """ Formats a dictionary of labels into a Kubernetes label selector string. """
    return ",".join([f"{k}={v}" for k, v in labels.items()])


def get_nodes_by_selector(label_selector):
    """ Fetches nodes matching the label selector. """
    try:
        nodes = core_v1_api.list_node(label_selector=label_selector)
        return nodes.items
    except kubernetes.client.ApiException as e:
        logger.error(f"Error fetching nodes with selector '{label_selector}': {e}")
        return []
    except Exception as e: # Catch errors, e.g. if core_v1_api is None
        logger.error(f"Unexpected error fetching nodes: {e}", exc_info=True)
        return []


def is_node_ready(node):
    """ Checks if a Kubernetes node is in Ready condition. """
    if not node.status or not node.status.conditions:
        return False
    for condition in node.status.conditions:
        if condition.type == "Ready" and condition.status == "True":
            return True
    return False


def is_node_schedulable(node):
    """ Checks if a node is schedulable (not cordoned). """
    return node.spec.unschedulable is None or node.spec.unschedulable is False


def cordon_node(node_name):
    """ Marks a node as unschedulable (cordons it). """
    logger.info(f"Cordoning node: {node_name}")
    patch_body = {"spec": {"unschedulable": True}}
    try:
        core_v1_api.patch_node(node_name, patch_body)
        logger.info(f"Successfully cordoned node: {node_name}")
        return True
    except kubernetes.client.ApiException as e:
        logger.error(f"Failed to cordon node {node_name}: {e}")
        return False
    except Exception as e:
        logger.error(f"Unexpected error cordoning node {node_name}: {e}", exc_info=True)
        return False


def uncordon_node(node_name):
    """ Marks a node as schedulable (uncordons it). """
    logger.info(f"Uncordoning node: {node_name}")
    patch_body = {"spec": {"unschedulable": False}}
    try:
        core_v1_api.patch_node(node_name, patch_body)
        logger.info(f"Successfully uncordoned node: {node_name}")
        return True
    except kubernetes.client.ApiException as e:
        logger.error(f"Failed to uncordon node {node_name}: {e}")
        return False
    except Exception as e:
        logger.error(f"Unexpected error uncordoning node {node_name}: {e}", exc_info=True)
        return False


def get_pods_on_node(node_name, namespace=None):
    """ Lists pods running on a specific node. """
    field_selector = f"spec.nodeName={node_name}"
    try:
        if namespace:
            pods = core_v1_api.list_namespaced_pod(namespace, field_selector=field_selector)
        else:
            pods = core_v1_api.list_pod_for_all_namespaces(field_selector=field_selector)
        # Filter out pods in terminal states (Succeeded, Failed)
        active_pods = [
            pod for pod in pods.items
            if pod.status.phase not in ["Succeeded", "Failed"]
        ]
        return active_pods
    except kubernetes.client.ApiException as e:
        logger.error(f"Error fetching pods on node {node_name}: {e}")
        return []
    except Exception as e:
        logger.error(f"Unexpected error fetching pods on node {node_name}: {e}", exc_info=True)
        return []


def evict_pod(pod):
    """ Evicts a single pod using the Eviction API. Respects PDBs. """
    logger.info(f"Attempting to evict pod: {pod.metadata.namespace}/{pod.metadata.name}")
    logger.info(f"Kubernetes client version at this point: {kubernetes.__version__}")
    
    eviction_body = kubernetes.client.V1Eviction(
        metadata=kubernetes.client.V1ObjectMeta(
            name=pod.metadata.name,
            namespace=pod.metadata.namespace
        ),
        delete_options=kubernetes.client.V1DeleteOptions()
    )
    
    try:
        # Use core_v1_api to evict the pod
        core_v1_api.create_namespaced_pod_eviction(
            name=pod.metadata.name,
            namespace=pod.metadata.namespace,
            body=eviction_body
        )
        logger.info(f"Successfully initiated eviction for pod: {pod.metadata.namespace}/{pod.metadata.name}")
        return True
    except kubernetes.client.ApiException as e:
        if e.status == 429: # Too Many Requests - PDB preventing eviction
            logger.warning(f"Eviction API call failed for pod {pod.metadata.namespace}/{pod.metadata.name} due to PDB limit (429). Will retry.")
        elif e.status == 404: # Pod already gone
             logger.info(f"Pod {pod.metadata.namespace}/{pod.metadata.name} not found for eviction (already deleted?).")
             return True # Treat as success
        else:
            logger.error(f"Eviction API call failed for pod {pod.metadata.namespace}/{pod.metadata.name}: {e.status} - {e.reason} - {e.body}")
        return False
    except AttributeError as ae: # Catching AttributeError specifically here for more targeted logging
        logger.error(f"AttributeError during pod eviction for {pod.metadata.namespace}/{pod.metadata.name}: {ae}. This usually indicates an outdated Kubernetes client library or an incorrect API object.", exc_info=True)
        logger.error(f"Available methods on core_v1_api: {[m for m in dir(core_v1_api) if 'eviction' in m or 'delete' in m]}")
        return False
    except Exception as e: # Catch any other unexpected errors
        logger.error(f"Unexpected error evicting pod {pod.metadata.namespace}/{pod.metadata.name}: {e}", exc_info=True)
        return False


def drain_node(node_name, patch, status):
    """ Cordon and drain pods from a node safely. """
    if not cordon_node(node_name):
        raise kopf.TemporaryError(f"Failed to cordon node {node_name}. Retrying.", delay=RETRY_DELAY_SECONDS)

    update_status(patch, status, phase="ProcessingNode", current_node=node_name, message="Node cordoned, starting drain.")

    attempt = 0
    max_drain_attempts = 10
    while attempt < max_drain_attempts:
        pods_to_evict = get_pods_on_node(node_name)
        pods_to_evict = [
            p for p in pods_to_evict
            if not (p.metadata.namespace == OPERATOR_NAMESPACE and p.metadata.labels.get("app") == "node-refresh-operator")
            and not any(owner.kind == "DaemonSet" for owner in p.metadata.owner_references or [])
        ]

        if not pods_to_evict:
            logger.info(f"No more pods to evict on node {node_name}.")
            break

        logger.info(f"Found {len(pods_to_evict)} pods to evict on node {node_name}. Drain attempt {attempt + 1}/{max_drain_attempts}")
        update_status(patch, status, message=f"Draining node {node_name}. Pods remaining: {len(pods_to_evict)}")

        evicted_in_pass = 0
        failed_in_pass = 0
        for pod in pods_to_evict:
            if evict_pod(pod):
                evicted_in_pass += 1
            else:
                failed_in_pass += 1
            time.sleep(1) # Small delay between eviction attempts

        if failed_in_pass > 0 and evicted_in_pass == 0:
            logger.warning(f"Failed to evict any pods in this pass on {node_name}. Waiting before retry...")
            attempt += 1
            time.sleep(RETRY_DELAY_SECONDS) # Wait for PDB status to potentially change
        elif failed_in_pass > 0:
             logger.info(f"Evicted {evicted_in_pass}, failed {failed_in_pass} on {node_name}. Continuing drain...")
             attempt += 1
             time.sleep(10) # Shorter wait if progress was made
        else:
             logger.info(f"Successfully initiated eviction for {evicted_in_pass} pods on {node_name}.")
             time.sleep(5) # Check again relatively quickly


    final_pods = get_pods_on_node(node_name)
    final_pods = [
        p for p in final_pods
        if not (p.metadata.namespace == OPERATOR_NAMESPACE and p.metadata.labels.get("app") == "node-refresh-operator")
        and not any(owner.kind == "DaemonSet" for owner in p.metadata.owner_references or [])
    ]

    if final_pods:
        pod_names = [f"{p.metadata.namespace}/{p.metadata.name}" for p in final_pods]
        logger.error(f"Failed to drain node {node_name}. Pods remaining: {pod_names}")
        update_status(patch, status, phase="Failed", message=f"Failed to drain node {node_name}. Pods remaining: {pod_names}")
        raise kopf.PermanentError(f"Failed to drain node {node_name}. Pods remaining: {pod_names}")
    else:
        logger.info(f"Successfully drained node: {node_name}")
        update_status(patch, status, message=f"Successfully drained node {node_name}.")
        return True


def update_status(patch, status_obj, phase=None, current_node=None, message=None, timestamp=None, add_condition=None):
    """ Safely updates the status subresource of the CRD. """
    now = datetime.datetime.now(datetime.timezone.utc).isoformat()

    if 'status' not in patch:
        patch['status'] = {}

    if phase:
        patch['status']['phase'] = phase
    if current_node is not None:
         patch['status']['currentNode'] = current_node
    if message:
        new_condition = {
            'type': phase if phase else status_obj.get('phase', 'Unknown'),
            'status': "True",
            'lastTransitionTime': now,
            'reason': phase if phase else "Processing",
            'message': message,
        }
        if 'conditions' not in patch['status']:
             patch['status']['conditions'] = []
        patch['status']['conditions'] = patch['status'].get('conditions', [])[-9:] + [new_condition]

    if timestamp:
         patch['status']['lastRefreshTimestamp'] = timestamp

    if add_condition:
        if 'conditions' not in patch['status']:
             patch['status']['conditions'] = []
        found = False
        for i, cond in enumerate(patch['status'].get('conditions', [])):
            if cond['type'] == add_condition['type']:
                patch['status']['conditions'][i] = add_condition
                found = True
                break
        if not found:
            patch['status']['conditions'].append(add_condition)


# --- Kopf Handlers ---
@kopf.on.startup()
async def configure_kopf(settings: kopf.OperatorSettings, **kwargs):
    logger.info("Kopf startup event triggered: Applying operator settings.")
    settings.posting.level = logging.INFO
    settings.watching.reconnect_delay = 5
    settings.execution.max_workers = 5
    settings.networking.error_backoff = 10
    logger.info(f"Kopf settings configured: {settings}")


@kopf.timer(API_GROUP, API_VERSION, CRD_PLURAL, interval=300.0, idle=60.0, initial_delay=30)
async def check_node_refreshes(spec, status, name, namespace, patch, memo: kopf.Memo, **kwargs):
    op_logger = logging.getLogger(__name__ + ".timer")
    op_logger.info(f"Timer check for NodeRefresh '{name}' (memo: {memo})")

    target_labels = spec.get('targetNodeLabels')
    if not target_labels:
        op_logger.warning(f"NodeRefresh '{name}' is missing 'targetNodeLabels'. Skipping.")
        return

    refresh_days = spec.get('refreshScheduleDays', DEFAULT_REFRESH_DAYS)
    last_refresh_str = status.get('lastRefreshTimestamp')
    current_phase = status.get('phase', 'Idle')

    if current_phase not in ['Idle', 'Succeeded', 'Failed', 'WaitingCooldown']:
        op_logger.info(f"NodeRefresh '{name}' is currently in phase '{current_phase}'. Timer yields.")
        return

    if current_phase == 'WaitingCooldown':
        last_transition_time_str = None
        if status.get('conditions'):
             for cond in reversed(status.get('conditions', [])):
                 if cond.get('type') == 'WaitingCooldown' and cond.get('status') == 'True':
                     last_transition_time_str = cond.get('lastTransitionTime')
                     break
        if last_transition_time_str:
            cooldown_start_time = isoparse(last_transition_time_str)
            cooldown_seconds = spec.get('nodeCooldownSeconds', DEFAULT_COOLDOWN_SECONDS)
            if datetime.datetime.now(datetime.timezone.utc) < cooldown_start_time + datetime.timedelta(seconds=cooldown_seconds):
                 op_logger.info(f"NodeRefresh '{name}' is in cooldown period. Timer yields.")
                 return
            else:
                 op_logger.info(f"NodeRefresh '{name}' cooldown finished. Resetting phase to Idle.")
                 update_status(patch, status, phase='Idle', message="Cooldown finished.")
        else:
             op_logger.warning(f"NodeRefresh '{name}' in WaitingCooldown phase but couldn't find start time. Resetting to Idle.")
             update_status(patch, status, phase='Idle', message="Resetting from cooldown due to missing timestamp.")

    refresh_needed = False
    if not last_refresh_str:
        refresh_needed = True
        op_logger.info(f"NodeRefresh '{name}': No previous refresh timestamp. Refresh is due.")
    else:
        try:
            last_refresh_time = isoparse(last_refresh_str)
            next_refresh_time = last_refresh_time + relativedelta(days=refresh_days)
            if datetime.datetime.now(datetime.timezone.utc) >= next_refresh_time:
                refresh_needed = True
                op_logger.info(f"NodeRefresh '{name}': Schedule met. Last refresh: {last_refresh_str}. Refresh is due.")
            else:
                op_logger.info(f"NodeRefresh '{name}': Refresh not yet due. Next: {next_refresh_time.isoformat()}")
        except ValueError:
            op_logger.error(f"NodeRefresh '{name}': Invalid lastRefreshTimestamp: {last_refresh_str}. Assuming refresh due.")
            refresh_needed = True

    if refresh_needed:
        op_logger.info(f"Triggering refresh cycle for NodeRefresh '{name}' due to schedule.")
        update_status(patch, status, phase='FindingNodes', current_node="", message="Refresh cycle triggered by schedule.")

def should_process_phase(status: kopf.Status, **_):
    if not status:
        return False
    current_phase = status.get('phase')
    return current_phase in ['FindingNodes', 'ProcessingNode']

@kopf.on.field(API_GROUP, API_VERSION, CRD_PLURAL, field='status.phase', when=should_process_phase)
async def process_node_refresh(spec, status, name, namespace, patch, memo: kopf.Memo, retry, **kwargs):
    op_logger = logging.getLogger(__name__ + ".reconciler")
    current_phase = status.get('phase')
    op_logger.info(f"Processing NodeRefresh '{name}' in phase '{current_phase}' (Attempt {retry+1}, memo: {memo})")

    target_labels = spec.get('targetNodeLabels')
    label_selector = format_label_selector(target_labels)
    cooldown_seconds = spec.get('nodeCooldownSeconds', DEFAULT_COOLDOWN_SECONDS)

    try:
        if current_phase == 'FindingNodes':
            op_logger.info(f"NodeRefresh '{name}': Searching for nodes with labels: {label_selector}")
            nodes = get_nodes_by_selector(label_selector)
            if not nodes:
                op_logger.warning(f"NodeRefresh '{name}': No nodes found for labels: {label_selector}. Setting to Idle.")
                update_status(patch, status, phase='Idle', message="No target nodes found.")
                return

            schedulable_nodes = [n for n in nodes if is_node_schedulable(n) and is_node_ready(n)]
            if not schedulable_nodes:
                 op_logger.warning(f"NodeRefresh '{name}': Found {len(nodes)} nodes, but none are schedulable/ready. Will retry.")
                 update_status(patch, status, message="Waiting for a schedulable/ready target node.")
                 raise kopf.TemporaryError("No suitable node to refresh yet.", delay=RETRY_DELAY_SECONDS * 2)

            node_to_refresh = random.choice(schedulable_nodes)
            node_name = node_to_refresh.metadata.name
            op_logger.info(f"NodeRefresh '{name}': Selected node '{node_name}' for refresh.")
            update_status(patch, status, phase='ProcessingNode', current_node=node_name, message=f"Selected node {node_name} for refresh.")

        elif current_phase == 'ProcessingNode':
            node_name = status.get('currentNode')
            if not node_name:
                 op_logger.error(f"NodeRefresh '{name}': Phase ProcessingNode, but currentNode not set. Resetting.")
                 update_status(patch, status, phase='FindingNodes', message="Error: Missing currentNode.")
                 raise kopf.TemporaryError("Missing current node.", delay=5)

            op_logger.info(f"NodeRefresh '{name}': Starting processing for node '{node_name}'")
            try:
                node = core_v1_api.read_node(node_name)
                if not all(item in node.metadata.labels.items() for item in target_labels.items()):
                    op_logger.warning(f"NodeRefresh '{name}': Node '{node_name}' no longer matches labels. Finding new node.")
                    update_status(patch, status, phase='FindingNodes', current_node="", message=f"Node {node_name} labels changed.")
                    return
            except kubernetes.client.ApiException as e:
                 if e.status == 404:
                     op_logger.warning(f"NodeRefresh '{name}': Node '{node_name}' not found. Finding new node.")
                     update_status(patch, status, phase='FindingNodes', current_node="", message=f"Node {node_name} not found.")
                     return
                 else:
                     raise kopf.TemporaryError(f"API error checking node {node_name}: {e}", delay=RETRY_DELAY_SECONDS)

            nodes = get_nodes_by_selector(label_selector)
            replacement_candidates = [
                n for n in nodes
                if n.metadata.name != node_name and is_node_ready(n) and is_node_schedulable(n)
            ]
            if not replacement_candidates:
                op_logger.warning(f"NodeRefresh '{name}': Node '{node_name}' needs replacement, but no other ready/schedulable nodes with labels '{label_selector}' found.")
                update_status(patch, status, message=f"Waiting for replacement for {node_name}.")
                raise kopf.TemporaryError("Waiting for replacement node.", delay=RETRY_DELAY_SECONDS * 3)
            else:
                 op_logger.info(f"NodeRefresh '{name}': Found {len(replacement_candidates)} replacement candidates for {node_name}.")

            op_logger.info(f"NodeRefresh '{name}': Initiating drain for node '{node_name}'.")
            drain_success = drain_node(node_name, patch, status)
            if not drain_success: # This condition might be redundant if drain_node always raises on failure
                 raise kopf.PermanentError(f"Drain failed for node {node_name}.")

            op_logger.info(f"NodeRefresh '{name}': Drain successful for '{node_name}'. Uncordoning.")
            if not uncordon_node(node_name):
                 op_logger.error(f"NodeRefresh '{name}': Failed to uncordon node '{node_name}'. Continuing, but manual check recommended.")
                 update_status(patch, status, add_condition={
                     'type': 'Warning', 'status': 'True', 'lastTransitionTime': datetime.datetime.now(datetime.timezone.utc).isoformat(),
                     'reason': 'UncordonFailed', 'message': f'Failed to uncordon node {node_name} after drain.'
                 })
            else:
                 op_logger.info(f"NodeRefresh '{name}': Successfully uncordoned node '{node_name}'.")

            nodes = get_nodes_by_selector(label_selector)
            remaining_schedulable_nodes = [n for n in nodes if is_node_schedulable(n) and is_node_ready(n) and n.metadata.name != node_name]

            if len(remaining_schedulable_nodes) >= 1:
                 op_logger.info(f"NodeRefresh '{name}': Node '{node_name}' refreshed. Cooldown before next potential node.")
                 update_status(patch, status, phase='WaitingCooldown', current_node="", message=f"Node {node_name} refreshed. Cooldown.", timestamp=datetime.datetime.now(datetime.timezone.utc).isoformat())
            else:
                 op_logger.info(f"NodeRefresh '{name}': Node '{node_name}' refreshed. No other schedulable target nodes. Cycle complete.")
                 update_status(patch, status, phase='Succeeded', current_node="", message=f"Refresh cycle completed after {node_name}.", timestamp=datetime.datetime.now(datetime.timezone.utc).isoformat())

    except kopf.TemporaryError as e:
        op_logger.warning(f"NodeRefresh '{name}': Temporary error: {e}. Kopf will retry.")
        update_status(patch, status, message=f"Temporary issue: {e}")
        raise
    except kopf.PermanentError as e:
        op_logger.error(f"NodeRefresh '{name}': Permanent error: {e}. Setting status to Failed.")
        update_status(patch, status, phase='Failed', message=f"Permanent error: {e}")
    except Exception as e:
        op_logger.exception(f"NodeRefresh '{name}': Unexpected error during processing phase {current_phase}.") # Logs traceback
        update_status(patch, status, phase='Failed', message=f"Unexpected error: {str(e)}")


@kopf.on.delete(API_GROUP, API_VERSION, CRD_PLURAL, optional=True)
async def on_delete(spec, name, namespace, memo: kopf.Memo, **kwargs):
    op_logger = logging.getLogger(__name__ + ".deleter")
    op_logger.info(f"NodeRefresh resource '{name}' deleted (memo: {memo}). No specific cleanup action implemented.")

logger.info("Operator script fully parsed. Kopf handlers should be registered now.")