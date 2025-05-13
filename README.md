# Node Refresh Controller for Kubernetes

A Kubernetes operator that automatically refreshes nodes in a cluster to ensure node health and reliability.

## Overview

The Node Refresh Controller is designed to safely drain and refresh nodes that match specific labels (`llm-plan:free` in this repo example) according to a configurable schedule (currently set at 3 days). This helps maintain cluster health by ensuring nodes don't run for too long without being refreshed, preventing issues related to resource leaks, kernel updates, or other long-running node problems.

## Features

- **Label-based targeting**: Select which nodes to refresh using Kubernetes labels
- **Configurable refresh schedule**: Set how frequently nodes should be refreshed (in days)
- **Safe node draining**: Respects Pod Disruption Budgets (PDBs) when evicting pods
- **Retry mechanism**: Configurable wait time (cool down) and retries between node operations
- **Status udpates**: Detailed status reporting in the Custom Resource
- **Minimal disruption**: Identifies replacement node and only drains one node at a time
- **Logging**: Logs are configured for all major controller operations

## Caveats and Considerations

- **Kopf instead of direct API calls**: The repository code uses kopf (Kubernetes Operator Pythonic Framework) instead of direct API calls. An initial version of `controller.py` attempted to use only raw API calls, but was unreliable and exceeded 1000 lines of code before being retired in favor of kopf. Kopf is [recommended by the developers of kubernetes](https://kubernetes.io/docs/concepts/extend-kubernetes/operator/#writing-operator) for building custom controllers.
- **Healthchecks**: Healthchecks and disruption budgets are used in conjunction and inherited from the application deployment, rather than configured in the controller itself
- **Cordon instead of delete**: The controller logic currently cordons nodes before draining them and then un-cordons them. This is to simulate removing and adding nodes, without conflicting with how the Kind cluster manages docker containers underneath. Additional commands to delete nodes can be added in a production scenario.
- **Helm compatible**: For simplicity, the controller and example application deployment do not use Helm packages. The controller container build however, contains an example of passing ENV vars, which could be used with Helm to customize the controller at deployment (`ENV CONTROLLER_NAMESPACE=default` is the example passed in the `Dockerfile` and later inherited in `controller.py`). An arbitrary number of parameters can be added this way and managed through Helm packaging.
- **Additional tests**: In production use, additional linting and testing can be applied to `controller.py` and the API calls that it makes. They have been omitted here in favor of safety-checking `try` patterns for timeliness.

## Prerequisites

At this time, the repository code has only been validated on an Apple Mac (arch: ARM64).
The following tools must be installed on your host machine to run the repository code:

- `Make`: For running project automations
- `Docker`: For building and running containers
- `Homebrew`: For installing required tools (MacOS)

As part of the repository automations, these additional tools are later installed during local deployment:

- `kubectl`: For interacting with the Kubernetes cluster
- `kind`: For creating a local Kubernetes cluster

## Repository Structure

```bash
├── Makefile                          # Commands for deployment and testing
├── README.md                         # This documentation file
├── _kind-config.yaml                 # Kind cluster configuration
├── _nginx-deployment.yaml            # Example application deployment for testing
├── controller/                       # Controller code and deployment files
│   ├── controller-deployment.yaml    # Kubernetes deployment configuration
│   └── src/                          # Source code directory
│       ├── Dockerfile                # Container image definition
│       ├── controller.py             # Main operator code
│       └── requirements.txt          # Python dependencies
├── resource-definition/              # Custom Resource Definition files
│   ├── noderefresh-crd.yaml          # CRD Schema definition
│   └── resource/                     # Example resources
│       └── noderefresh-resource.yaml # Example NodeRefresh resource
```

## Quick Start

### Deployment

To deploy the controller and set up a demo environment:

```bash
make deploy
```

This command:

1. Creates a Kind cluster with the configuration from `_kind-config.yaml`
2. Deploys the Custom Resource Definition
3. Builds and deploys the controller
4. Deploys an example NGINX application
5. Creates a NodeRefresh resource that targets nodes with the `llm-plan: free` label

### Teardown

To destroy the cluster and clean up:

```bash
make destroy
```

This command:

1. Deletes the Kind cluster and all resources inside of it

## How It Works

### The NodeRefresh Custom Resource

The controller operates based on `NodeRefresh` resources that specify:

```yaml
apiVersion: stable.example.com/v1alpha1
kind: NodeRefresh
metadata:
  name: refresh-worker-nodes
spec:
  targetNodeLabels:
    llm-plan: "free"             # Target nodes with this label
  refreshScheduleDays: 3         # Refresh matching nodes every 3 day
  nodeCooldownSeconds: 60        # Wait 1 minute between node operations
```

### Reconciliation Loop

The controller follows a state-machine approach with the following phases:

1. **Idle**: Waiting for the next refresh cycle based on `refreshScheduleDays`
2. **FindingNodes**: Looking for nodes matching the target labels
3. **ProcessingNode**: Cordoning and draining a selected node
4. **WaitingCooldown**: Pausing between node operations
5. **Succeeded**: Refresh cycle completed successfully
6. **Failed**: Encountered an error during the process

The reconciliation process:

1. A timer checks every 5 minutes if a refresh is due based on the last refresh timestamp
2. When a refresh is needed, the controller finds all nodes matching the target labels
3. A random eligible node is selected for refreshing
4. The node is cordoned (marked as unschedulable) to prevent new pods from being scheduled
5. All non-DaemonSet pods are safely evicted, respecting Pod Disruption Budgets
6. The node is uncordoned to make it schedulable again
7. The controller enters a cooldown period before selecting the next node
8. After all matching nodes are refreshed, the cycle completes

### Safety Mechanisms

- **Pod Disruption Budget Respect**: The controller uses the Kubernetes Eviction API which respects PDBs
- **DaemonSet Protection**: Pods managed by DaemonSets are not evicted
- **Operator Self-Protection**: The controller pod does not evict itself
- **One Node at a Time**: Only one node is refreshed at a time to maintain service availability
- **Retries with Backoff**: Failed operations are retried with increasing delays
- **Reserved node usage**: The controller uses a `nodeSelector` to run on isolated nodes with label: `llm-plan: reserved`

## Monitoring and Status

By default, the `make deploy` command begins outputting log data to the active console after deployment completes.

Example outputs:

```bash
[2025-05-13 18:35:45,984] __kopf_script_0__/us [INFO    ] Successfully drained node: k8s-controller-demo-worker3
[2025-05-13 18:35:45,984] __kopf_script_0__/us [INFO    ] NodeRefresh 'refresh-worker-nodes': Drain successful for 'k8s-controller-demo-worker3'. Uncordoning.
[2025-05-13 18:35:45,984] __kopf_script_0__/us [INFO    ] Uncordoning node: k8s-controller-demo-worker3

[2025-05-13 18:40:27,872] kopf.objects         [DEBUG   ] [refresh-worker-nodes] Patching with: {'metadata': {'annotations': {'kopf.zalando.org/last-handled-configuration': '{"spec":{"nodeCooldownSeconds":60,"refreshScheduleDays":1,"targetNodeLabels":{"llm-plan":"free"}},"status":{"phase":"Idle"}}\n'}}}

[2025-05-13 19:33:44,643] __kopf_script_0__/us [INFO    ] Timer check for NodeRefresh 'refresh-worker-nodes' (memo: {})
[2025-05-13 19:33:44,644] __kopf_script_0__/us [INFO    ] NodeRefresh 'refresh-worker-nodes': Refresh not yet due. Next: 2025-05-14T18:35:46.005235+00:00
```

You can also monitor the status of the NodeRefresh resource:

```bash
kubectl get noderefresh refresh-worker-nodes -o yaml
```

The status field will show:

- The current phase
- Which node is being processed
- A timestamp of the last refresh
- Detailed conditions with status messages

## Troubleshooting

### Common Issues

1. **Nodes not refreshing**:
   - Check that node labels match the `targetNodeLabels` in your NodeRefresh resource
   - Verify the controller logs for any errors

2. **Node stuck in draining state**:
   - A PDB may be preventing pod eviction. Check if any PDBs are too restrictive
   - Some pods might not have a controller (Deployment, StatefulSet) and can't be evicted

3. **Controller crashes**:
   - Check the controller logs: `kubectl logs -n kube-system -l app=node-refresh-operator`

## Thank You

A special thank you to Hippocratic.ai for reviewing this repositories and for employment consideration.

## License

This repository is licensed under the GNU General Public License v3.0 - see the LICENSE file for details
