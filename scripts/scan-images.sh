#!/usr/bin/env bash
# Locally scan the container images for CVEs with Trivy (open-source, no account
# needed). This is the local equivalent of the AWS Inspector ECR scan.
#
#   ./scripts/scan-images.sh                 # build + scan both images (HIGH,CRITICAL)
#   ./scripts/scan-images.sh --all-severity  # include LOW/MEDIUM too
#   ./scripts/scan-images.sh --no-build      # scan existing images, skip docker build
#
# Suppressions for known false positives live in .trivyignore (read automatically).
# Install Trivy: brew install trivy   (https://trivy.dev)
set -euo pipefail

cd "$(dirname "$0")/.."

SEVERITY="HIGH,CRITICAL"
BUILD=1
for arg in "$@"; do
    case "$arg" in
        --all-severity) SEVERITY="LOW,MEDIUM,HIGH,CRITICAL" ;;
        --no-build)     BUILD=0 ;;
        *) echo "unknown option: $arg" >&2; exit 1 ;;
    esac
done

if ! command -v trivy >/dev/null 2>&1; then
    echo "trivy not found. Install it with: brew install trivy" >&2
    exit 1
fi

# repo tag -> Dockerfile build context
APP_IMAGE="valencia-app:local-scan"
MODELS_IMAGE="valencia-models:local-scan"

if [ "$BUILD" -eq 1 ]; then
    echo "→ building $APP_IMAGE"
    docker build -t "$APP_IMAGE" -f Dockerfile .
    echo "→ building $MODELS_IMAGE"
    docker build -t "$MODELS_IMAGE" -f models/Dockerfile models/
fi

# --ignorefile keeps documented false positives out of the report.
# Exit code 1 when a non-ignored vuln at the given severity is found, so this
# can gate CI.
STATUS=0
for img in "$APP_IMAGE" "$MODELS_IMAGE"; do
    echo
    echo "=================================================================="
    echo " Trivy scan: $img  (severity: $SEVERITY)"
    echo "=================================================================="
    trivy image \
        --scanners vuln \
        --severity "$SEVERITY" \
        --ignorefile .trivyignore \
        --exit-code 1 \
        "$img" || STATUS=1
done

echo
if [ "$STATUS" -eq 0 ]; then
    echo "✓ no un-ignored $SEVERITY vulnerabilities"
else
    echo "✗ found un-ignored $SEVERITY vulnerabilities (see report above)"
fi
exit "$STATUS"
