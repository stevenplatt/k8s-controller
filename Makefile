# Makefile for Kubernetes Controller project
# Sets up a kind cluster for local development

# Configuration Variables
CLUSTER_NAME := k8s-controller-demo
KIND_CONFIG := _kind-config.yaml
KUBECTL_CONTEXT := kind-$(CLUSTER_NAME)

.PHONY: all check-prerequisites install-kind install-kubectl create-cluster delete-cluster deploy-controller deploy-crd deploy-resource deploy-nginx demo help

deploy: create-cluster deploy-crd deploy-controller deploy-nginx demo

destroy: delete-cluster

help:
	@echo "Makefile commands:"
	@echo "  check-prerequisites  - Check if all prerequisites are installed"
	@echo "  install-kubectl     - Install kubectl"
	@echo "  install-kind        - Install kind (Kubernetes in Docker)"
	@echo "  create-cluster      - Create a kind cluster for local development"
	@echo "  delete-cluster      - Delete the kind cluster"
	@echo "  all                 - Run the entire setup process (default)"
	@echo "  help                - Show this help message"

check-prerequisites:
	@echo "Checking for Homebrew..."
	@if ! command -v brew >/dev/null 2>&1; then \
		echo "Homebrew is not installed. Please install Homebrew to proceed."; \
		exit 1; \
	else \
		echo "Homebrew is installed"; \
	fi
	@echo "Checking for Docker..."
	@if ! command -v docker >/dev/null 2>&1; then \
		echo "Docker is not installed. Please install Docker to proceed."; \
		exit 1; \
	else \
		echo "Docker is installed"; \
	fi

install-kubectl: check-prerequisites
	@echo "Checking for kubectl..."
	@if ! command -v kubectl >/dev/null 2>&1; then \
		echo "kubectl is not installed. Installing kubectl..."; \
		brew install kubectl; \
		echo "kubectl installation complete"; \
	else \
		echo "kubectl is installed"; \
	fi

install-kind: install-kubectl
	@echo "Checking for kind..."
	@if ! command -v kind >/dev/null 2>&1; then \
		echo "kind is not installed. Installing kind..."; \
		brew install kind; \
		echo "kind installation complete"; \
	else \
		echo "kind is installed"; \
	fi

create-cluster: check-prerequisites install-kind
	@echo "Checking for $(KIND_CONFIG)..."
	@if [ ! -f $(KIND_CONFIG) ]; then \
		echo "$(KIND_CONFIG) is missing. Please create this file."; \
		exit 1; \
	else \
		echo "$(KIND_CONFIG) exists"; \
	fi

	@echo "Creating kind cluster using configuration from $(KIND_CONFIG)..."
	@kind create cluster --name $(CLUSTER_NAME) --config $(KIND_CONFIG)
	
	@echo "Cluster creation initiated!"

	@echo "Switching kubectl context..."
	@kubectl config use-context $(KUBECTL_CONTEXT)
	@echo "Current kubectl context is now: $$(kubectl config current-context)"
	@echo ""

	@echo "Waiting for all Kind nodes to be Ready..."
	@EXPECTED_NODES=$$(grep -c "role:" $(KIND_CONFIG)); \
	echo "Expecting $$EXPECTED_NODES nodes to be Ready..."; \
	until [ "$$(kubectl get nodes -o jsonpath='{range .items[*]}{.status.conditions[?(@.type=="Ready")].status}{"\n"}{end}' | grep -c "True")" -eq "$$EXPECTED_NODES" ]; do \
		echo "Waiting for nodes to be Ready ($$(kubectl get nodes -o jsonpath='{range .items[*]}{.status.conditions[?(@.type=="Ready")].status}{"\n"}{end}' | grep -c "True")/$$EXPECTED_NODES)..."; \
		sleep 5; \
	done
	@echo "All nodes are Ready!"
	@echo ""

	@echo "Verifying node labels..."
	@kubectl get nodes --show-labels
	@echo ""

delete-cluster:
	@echo "Deleting kind cluster..."
	kind delete cluster --name $(CLUSTER_NAME)
	@echo "Cluster deletion complete!"

deploy-controller:
	@echo "Building the controller image locally..."
	@docker build --no-cache -t node-refresh-operator:latest ./controller/src
	@echo ""

	@echo "Moving controller image to kind cluster..."
	@kind load docker-image node-refresh-operator:latest --name $(CLUSTER_NAME)
	@echo ""

	@echo "Deploying controller to cluster..."
	@kubectl apply -f ./controller/controller-deployment.yaml
	@echo ""

	@echo "Waiting for controller pod to be ready..."
	@kubectl wait --for=condition=ready pod -l app=node-refresh-operator -n kube-system --timeout=120s
	@echo ""

	@echo "Controller pod status:"
	@kubectl get pods -l app=node-refresh-operator -n kube-system -o custom-columns=POD:.metadata.name,STATUS:.status.phase,NODE:.spec.nodeName,READY:.status.containerStatuses[0].ready
	@echo ""

deploy-crd:
	@echo "Creating NodeRefresh Resource Definition (CRD)..."
	@if kubectl get crd noderefreshes.hippocratic.ai > /dev/null 2>&1; then \
		echo "NodeRefresh CRD already exists"; \
	else \
		kubectl apply -f resource-definition/noderefresh-crd.yaml; \
	fi
	@echo ""

deploy-nginx:
	@echo "Creating example NGINX deployment..."
	@if kubectl get deployment nginx-example > /dev/null 2>&1; then \
		echo "NGINX deployment already exists"; \
	else \
		kubectl apply -f _nginx-deployment.yaml; \
	fi
	@echo ""

	@kubectl get pods -l app=nginx -o custom-columns=POD:.metadata.name,STATUS:.status.phase,NODE:.spec.nodeName
	@echo ""

deploy-resource:
	@echo "Creating NodeRefresh Resource..."
	@if kubectl get noderefresh refresh-free-nodes > /dev/null 2>&1; then \
		echo "NodeRefresh resource already exists"; \
	else \
		kubectl apply -f resource-definition/resource/noderefresh-resource.yaml; \
	fi
	@echo ""

demo: deploy-controller deploy-resource
	@echo "Printing controller logs to terminal..."
	@echo ""
	@POD=$$(kubectl get pods -n kube-system | grep "node-refresh" | head -n 1 | awk '{print $$1}') && \
	if [ -n "$$POD" ]; then \
		kubectl logs  -n kube-system -f $$POD; \
	else \
		echo "No node-refresh pods found"; \
		exit 1; \
	fi
