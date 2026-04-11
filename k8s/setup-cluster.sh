#!/bin/bash

# Surogates K3D Development Cluster Setup
#
# Sets up a local k3d cluster with all dependencies:
#   - Traefik (ingress)
#   - PostgreSQL (session store)
#   - Redis (work queue)
#   - Garage (object storage)
#   - Surogates namespace + secrets + ConfigMaps
#
# Prerequisites: curl, wget, jq, envsubst, libnss3-tools (for mkcert)
#
# Usage:
#   ./setup-cluster.sh          # Full setup
#   ./setup-cluster.sh deploy   # Apply surogates manifests only (after cluster exists)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"

export SUROGATES_DIR="${HOME}/.surogates"
export CLUSTER_NAME="surogates"
export SERVERS=1
export AGENTS=1
export API_PORT=6443
export HTTP_PORT=80
export HTTPS_PORT=443

export KUBECTL="${SUROGATES_DIR}/bin/kubectl"
export HELM="${SUROGATES_DIR}/bin/helm"
export MKCERT="${SUROGATES_DIR}/bin/mkcert"
export K3D="${SUROGATES_DIR}/bin/k3d"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
NC='\033[0m'

mkdir -p "${SUROGATES_DIR}/bin"
mkdir -p "${SUROGATES_DIR}/garage-data/meta" "${SUROGATES_DIR}/garage-data/data"
sudo chmod -R 777 "${SUROGATES_DIR}/garage-data"

# ---------------------------------------------------------------------------
# Binary installation
# ---------------------------------------------------------------------------

install_kubectl() {
    if [ -f "${SUROGATES_DIR}/bin/kubectl" ]; then return; fi
    echo -e "${CYAN}Installing kubectl...${NC}"
    kubectl_version=$(curl -L -s https://dl.k8s.io/release/stable.txt)
    curl -L -o "${SUROGATES_DIR}/bin/kubectl" "https://dl.k8s.io/release/${kubectl_version}/bin/linux/amd64/kubectl"
    chmod +x "${SUROGATES_DIR}/bin/kubectl"
}

install_helm() {
    if [ -f "${SUROGATES_DIR}/bin/helm" ]; then return; fi
    echo -e "${CYAN}Installing helm...${NC}"
    mkdir -p "${SUROGATES_DIR}/tmp"
    local helm_version="v3.17.3"
    wget -q -O "${SUROGATES_DIR}/tmp/helm-${helm_version}-linux-amd64.tar.gz" \
        "https://get.helm.sh/helm-${helm_version}-linux-amd64.tar.gz"
    cd "${SUROGATES_DIR}/tmp"
    tar -xzf "helm-${helm_version}-linux-amd64.tar.gz"
    mv linux-amd64/helm "${SUROGATES_DIR}/bin/helm"
    chmod +x "${SUROGATES_DIR}/bin/helm"
    rm -rf "${SUROGATES_DIR}/tmp"
}

install_k3d() {
    if [ -f "${SUROGATES_DIR}/bin/k3d" ]; then return; fi
    echo -e "${CYAN}Installing k3d...${NC}"
    curl -s https://raw.githubusercontent.com/k3d-io/k3d/main/install.sh | K3D_INSTALL_DIR="${SUROGATES_DIR}/bin" bash
}

install_mkcert() {
    if [ -f "${SUROGATES_DIR}/bin/mkcert" ]; then return; fi
    echo -e "${CYAN}Installing mkcert...${NC}"
    wget -q -O "${SUROGATES_DIR}/bin/mkcert" \
        https://github.com/FiloSottile/mkcert/releases/download/v1.4.4/mkcert-v1.4.4-linux-amd64
    chmod +x "${SUROGATES_DIR}/bin/mkcert"
}

# ---------------------------------------------------------------------------
# Helm repositories
# ---------------------------------------------------------------------------

setup_helm_repositories() {
    echo -e "${CYAN}Setting up Helm repositories...${NC}"
    "$HELM" repo add traefik https://traefik.github.io/charts 2>/dev/null || true
    "$HELM" repo add bitnami https://charts.bitnami.com/bitnami 2>/dev/null || true
    "$HELM" repo add prometheus-community https://prometheus-community.github.io/helm-charts 2>/dev/null || true
    "$HELM" repo add charts-derwitt-dev https://charts.derwitt.dev 2>/dev/null || true
    "$HELM" repo update
}

# ---------------------------------------------------------------------------
# Cluster creation
# ---------------------------------------------------------------------------

create_cluster() {
    echo -e "${CYAN}Creating k3d cluster '${CLUSTER_NAME}'...${NC}"

    # Ensure host aliases for local access.
    for host in k8s.localhost surogates.k8s.localhost garage.k3s.local minio-console.k3s.local; do
        grep -qF "$host" /etc/hosts || sudo sh -c "echo '127.0.0.1 $host' >> /etc/hosts"
    done

    tmp_config=$(mktemp /tmp/k3d-config-XXXXXX.yaml)
    envsubst < "${SCRIPT_DIR}/cluster.yml" > "$tmp_config"
    "$K3D" cluster create --config "$tmp_config"
    rm -f "$tmp_config"
}

# ---------------------------------------------------------------------------
# Infrastructure components
# ---------------------------------------------------------------------------

install_traefik() {
    echo -e "${CYAN}Installing Traefik...${NC}"
    "$MKCERT" -key-file "${SUROGATES_DIR}/ssl.key.pem" -cert-file "${SUROGATES_DIR}/ssl.cert.pem" \
        "*.k8s.localhost" "*.k3s.local"
    "$KUBECTL" create secret generic traefik-tls-secret \
        --from-file=tls.crt="${SUROGATES_DIR}/ssl.cert.pem" \
        --from-file=tls.key="${SUROGATES_DIR}/ssl.key.pem" \
        -n kube-system 2>/dev/null || true
    "$HELM" install traefik traefik/traefik --version 35.4.0 -n kube-system \
        -f "$SCRIPT_DIR/traefik/values.yml"
    "$KUBECTL" apply -f "${SCRIPT_DIR}/traefik/middleware.yml"
}

install_db() {
    echo -e "${CYAN}Installing PostgreSQL...${NC}"
    "$HELM" install surogates-db bitnami/postgresql \
        -f "${SCRIPT_DIR}/db/values.yml"
}

install_redis() {
    echo -e "${CYAN}Installing Redis...${NC}"
    "$HELM" install surogates-redis bitnami/redis \
        -f "${SCRIPT_DIR}/redis/values.yml"
}

install_garage() {
    echo -e "${CYAN}Installing Garage S3...${NC}"
    "$HELM" install surogates-s3 charts-derwitt-dev/garage --wait --version 2.3.1 \
        -f "${SCRIPT_DIR}/garage/values.yml"
    "$KUBECTL" apply -f "${SCRIPT_DIR}/garage/nodeport.yaml"
}

# ---------------------------------------------------------------------------
# Surogates deployment
# ---------------------------------------------------------------------------

deploy_surogates() {
    echo -e "${CYAN}Deploying Surogates resources...${NC}"

    # Namespace + secrets + ConfigMaps.
    "$KUBECTL" apply -f "${SCRIPT_DIR}/surogates/namespace.yaml"
    "$KUBECTL" apply -f "${SCRIPT_DIR}/surogates/secrets.yaml"
    "$KUBECTL" apply -f "${SCRIPT_DIR}/surogates/configmaps.yaml"

    # RBAC from production base manifests.
    "$KUBECTL" apply -f "${PROJECT_DIR}/k8/base/worker-rbac.yaml"
    "$KUBECTL" apply -f "${PROJECT_DIR}/k8/base/sandbox-rbac.yaml"

    # Dev ingress route.
    "$KUBECTL" apply -f "${SCRIPT_DIR}/surogates/ingress.yaml"

    echo -e "${GREEN}  Surogates namespace ready. Deploy workloads with:${NC}"
    echo -e "${CYAN}    kubectl apply -k ${PROJECT_DIR}/k8/base/${NC}"
}

# ---------------------------------------------------------------------------
# Dev config for running API/worker locally (outside cluster)
# ---------------------------------------------------------------------------

create_dev_config() {
    cat >"${SUROGATES_DIR}/config.yaml" <<EOF
# Auto-generated dev config — connects to k3d cluster services via NodePort.
# API server + worker run locally, sandbox pods run in k3d.
db:
  url: "postgresql+asyncpg://surogates:surogates@127.0.0.1:32432/surogates"

redis:
  url: "redis://localhost:32379/0"

api:
  host: "0.0.0.0"
  port: 8000

worker:
  concurrency: 10
  api_base_url: "http://localhost:8000"
  use_api_for_harness_tools: true

sandbox:
  backend: "kubernetes"
  k8s_namespace: "surogates"
  default_timeout: 300

storage:
  backend: "s3"
  endpoint: "http://localhost:30900"
  region: "garage"
  # After running 'garage key create surogates-key', paste the key here:
  access_key: ""
  secret_key: ""

org_id: "3096721b-ecf8-4bbb-b6dd-f822cbf7e4f8"
jwt_secret: "dev-secret-do-not-use-in-production"
log_level: "DEBUG"
EOF
    echo -e "${GREEN}  Dev config written to ${SUROGATES_DIR}/config.yaml${NC}"
}

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

if [ "${1:-}" = "deploy" ]; then
    export KUBECONFIG="${SUROGATES_DIR}/kubeconfig"
    deploy_surogates
    exit 0
fi

# Check for existing clusters.
install_k3d
existing=$("$K3D" cluster list -o json 2>/dev/null | jq -r '.[].name' 2>/dev/null || true)
if [ -n "$existing" ]; then
    echo -e "${RED}✖ ERROR: Existing k3d clusters found. Delete them first:${NC}"
    echo -e "${CYAN}    $K3D cluster delete --all${NC}"
    exit 1
fi

# Install binaries.
install_kubectl
install_helm
install_mkcert

# Setup.
setup_helm_repositories
create_cluster

sleep 3

"$K3D" kubeconfig write "$CLUSTER_NAME" --output "$SUROGATES_DIR/kubeconfig"
export KUBECONFIG="$SUROGATES_DIR/kubeconfig"

echo -e "${CYAN}  KUBECONFIG=${SUROGATES_DIR}/kubeconfig${NC}"

# Install infrastructure.
install_traefik
install_db
install_redis
install_garage

# Deploy surogates resources.
deploy_surogates

# Generate dev config for local runs.
create_dev_config

echo ""
echo -e "${GREEN}✓ Cluster setup complete!${NC}"
echo ""
echo -e "  ${CYAN}export KUBECONFIG=${SUROGATES_DIR}/kubeconfig${NC}"
echo ""
echo -e "  Services (NodePort → localhost):"
echo -e "    DB:      ${CYAN}postgresql://surogates:surogates@localhost:32432/surogates${NC}"
echo -e "    Redis:   ${CYAN}redis://localhost:32379${NC}"
echo -e "    Garage:  ${CYAN}http://localhost:30900${NC}  (S3 API)"
echo ""
echo -e "  Dev config: ${CYAN}${SUROGATES_DIR}/config.yaml${NC}"
echo ""
echo -e "  ${YELLOW}Next steps:${NC}"
echo -e "    1. Complete Garage layout setup (see instructions above)"
echo -e "${CYAN}    GARAGE_POD=\$(kubectl get pods -l app.kubernetes.io/name=garage -o jsonpath='{.items[0].metadata.name}')${NC}"
echo -e "${CYAN}    kubectl exec -ti \$GARAGE_POD -- /garage layout assign -z dc1 -c 1T \$(kubectl exec \$GARAGE_POD -- /garage node id -q | cut -d@ -f1)${NC}"
echo -e "${CYAN}    kubectl exec -ti \$GARAGE_POD -- /garage layout apply --version 1${NC}"
echo -e "${CYAN}    kubectl exec -ti \$GARAGE_POD -- /garage key create surogates-key${NC}"
echo -e "${CYAN}    kubectl exec -ti \$GARAGE_POD -- /garage key allow surogates-key --create-bucket${NC}"
echo -e "${YELLOW}  Then paste 'Key ID' and 'Secret key' into config.dev.yaml storage.access_key / secret_key${NC}"
echo -e "    2. Paste Garage key into ${CYAN}${SUROGATES_DIR}/config.yaml${NC} storage.access_key / secret_key"
echo -e "    3. Run API server:  ${CYAN}SUROGATES_CONFIG=${SUROGATES_DIR}/config.yaml surogates api${NC}"
echo -e "    4. Run worker:      ${CYAN}SUROGATES_CONFIG=${SUROGATES_DIR}/config.yaml surogates worker${NC}"
