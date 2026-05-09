# Camofox Browser Service

This kustomize overlay deploys
[`jo-inc/camofox-browser`](https://github.com/jo-inc/camofox-browser) as an
internal Kubernetes service for Surogates browser automation.

The default namespace is `surogate-default`, which matches the active local
Surogates Helm release. Override `namespace:` in `kustomization.yaml` if you
deploy agents into a different namespace.

## Image

The upstream project does not publish a GitHub container package. Build or
import an image into the local cluster before applying this overlay:

```bash
git clone https://github.com/jo-inc/camofox-browser
cd camofox-browser
make build ARCH=x86_64
docker tag camofox-browser:135.0.1-x86_64 camofox-browser:latest
```

Load the image into your local Kubernetes runtime as appropriate:

```bash
kind load docker-image camofox-browser:latest
k3d image import camofox-browser:latest -c <cluster>
minikube image load camofox-browser:latest
```

If you publish your own image, update `images:` in `kustomization.yaml`.

## Secrets

Create a secret when you want API bearer auth, cookie import, admin stop, or
VNC password protection:

```bash
KUBECONFIG=~/.surogate/kubeconfig kubectl -n surogate-default create secret generic camofox-browser-secret \
  --from-literal=CAMOFOX_ACCESS_KEY="$(openssl rand -hex 32)" \
  --from-literal=CAMOFOX_API_KEY="$(openssl rand -hex 32)" \
  --from-literal=CAMOFOX_ADMIN_KEY="$(openssl rand -hex 32)" \
  --from-literal=VNC_PASSWORD="$(openssl rand -hex 12)"
```

The deployment treats this secret as optional. Without it, the service is still
ClusterIP-only and NetworkPolicy-restricted, but all non-cookie routes are not
bearer-authenticated by Camofox itself.

## Deploy

```bash
KUBECONFIG=~/.surogate/kubeconfig kubectl apply -k k8s/camofox-browser
KUBECONFIG=~/.surogate/kubeconfig kubectl -n surogate-default rollout status deploy/camofox-browser
```

Check health locally:

```bash
KUBECONFIG=~/.surogate/kubeconfig kubectl -n surogate-default port-forward svc/camofox-browser 9377:9377
curl http://localhost:9377/health
```

Internal service URL for the Surogates harness:

```text
http://camofox-browser.surogate-default.svc.cluster.local:9377
```

noVNC is enabled on service port `6080` for interactive login flows. The
Surogates API should proxy this port to `agent-chat-react` rather than exposing
the service directly.
