#!/usr/bin/env bash
# Build and push all Surogates container images to GHCR.
#
# Usage:
#   ./images/build.sh              # build + push all, tag "latest"
#   ./images/build.sh 0.4.1        # build + push all, tag "0.4.1" + "latest"
#   ./images/build.sh 0.4.1 sandbox  # build + push only sandbox
#
# Mirrors the matrix in .github/workflows/release.yml.

set -euo pipefail

REGISTRY="ghcr.io/invergent-ai"
REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
VERSION="${1:-latest}"
FILTER="${2:-}"

declare -A IMAGES=(
  [api]="surogates-api"
  [worker]="surogates-worker"
  [sandbox]="surogates-agent-sandbox"
  [s3fs]="surogates-s3fs"
)

for dir in "${!IMAGES[@]}"; do
  name="${IMAGES[$dir]}"

  if [[ -n "$FILTER" && "$dir" != "$FILTER" ]]; then
    continue
  fi

  full="$REGISTRY/$name"
  echo "──────────────────────────────────────────"
  echo "Building $full ($dir)"
  echo "──────────────────────────────────────────"

  tags=("--tag" "$full:latest")
  if [[ "$VERSION" != "latest" ]]; then
    tags+=("--tag" "$full:$VERSION")
  fi

  docker build \
    "${tags[@]}" \
    --file "$REPO_ROOT/images/$dir/Dockerfile" \
    "$REPO_ROOT"

  echo "Pushing $full ..."
  docker push "$full:latest"
  if [[ "$VERSION" != "latest" ]]; then
    docker push "$full:$VERSION"
  fi

  echo "Importing $full:latest into k3d cluster ..."
  k3d image import "$full:latest" -c surogate

  echo ""
done

echo "Done."
