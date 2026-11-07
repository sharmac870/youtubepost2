#!/usr/bin/env bash
# Deploys youtubeposts2 to Azure. Needs the Azure CLI signed in (`az login`) as the subscription owner.
#
#   infra/deploy.sh all        base resources -> sign-in app -> image build -> apps and jobs (idempotent)
#   infra/deploy.sh base       storage, Key Vault, registry, logs, identities and roles only
#   infra/deploy.sh image      build the container image in Azure (ACR Tasks; no local Docker needed)
#   infra/deploy.sh apps       deploy/update the jobs and the console with the latest image
#   infra/deploy.sh secrets    store API keys and tokens in Key Vault (prompts; nothing is echoed or saved)
#   infra/deploy.sh what-if    preview changes
#
# Overrides: AZURE_SUBSCRIPTION_ID, RESOURCE_GROUP, PLATFORMS, IG_USER_ID, IG_API_HOST, ANTHROPIC_MODEL.
set -euo pipefail

SUBSCRIPTION="${AZURE_SUBSCRIPTION_ID:-8117e214-4622-48b8-85b9-a6feb362c220}"
RESOURCE_GROUP="${RESOURCE_GROUP:-youtubeposts2}"
APP_REGISTRATION="youtubeposts2 console"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
ACTION="${1:-}"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

az account set --subscription "$SUBSCRIPTION"
echo "Subscription: $(az account show --query name -o tsv) ($SUBSCRIPTION) · resource group: $RESOURCE_GROUP" >&2
[[ "$(az group exists -n "$RESOURCE_GROUP")" == "true" ]] || { echo "Resource group $RESOURCE_GROUP not found." >&2; exit 1; }

output() { az deployment group show -g "$RESOURCE_GROUP" -n ytp2-base --query "properties.outputs.$1.value" -o tsv; }

deployer() { echo "${DEPLOYER_OBJECT_ID:-$(az ad signed-in-user show --query id -o tsv)}"; }

ensure_sign_in_app() {
  if [[ -n "${AUTH_CLIENT_ID:-}" && -n "${DEPLOYER_OBJECT_ID:-}" ]]; then return; fi   # CI: set up already
  AUTH_CLIENT_ID="$(az ad app list --display-name "$APP_REGISTRATION" --query '[0].appId' -o tsv)"
  if [[ -z "$AUTH_CLIENT_ID" ]]; then
    AUTH_CLIENT_ID="$(az ad app create --display-name "$APP_REGISTRATION" --sign-in-audience AzureADMyOrg --query appId -o tsv)"
    echo "Created app registration '$APP_REGISTRATION' ($AUTH_CLIENT_ID)" >&2
  fi
  az ad sp show --id "$AUTH_CLIENT_ID" -o none 2>/dev/null || az ad sp create --id "$AUTH_CLIENT_ID" -o none
  local vault; vault="$(output keyVaultName)"
  if ! az keyvault secret show --vault-name "$vault" -n auth-client-secret -o none 2>/dev/null; then
    for attempt in 1 2 3 4 5 6; do   # the Secrets Officer role can take a minute to apply
      if (umask 077; az ad app credential reset --id "$AUTH_CLIENT_ID" --display-name console-sign-in --years 2 \
            --query password -o tsv | tr -d '\n' >"$WORK/auth") &&
         az keyvault secret set --vault-name "$vault" -n auth-client-secret --file "$WORK/auth" --encoding utf-8 -o none; then
        rm -f "$WORK/auth"; break
      fi
      rm -f "$WORK/auth"; sleep 20
    done
  fi
}

allowed_users() {
  local extra=""
  [[ -f "$ROOT/infra/allowed-users.txt" ]] && extra="$(grep -vE '^[[:space:]]*(#|$)' "$ROOT/infra/allowed-users.txt" | paste -sd, - || true)"
  echo "$(deployer)${extra:+,$extra}"
}

deploy() {   # $1 = deployApps, $2 = image
  local name="ytp2-base"; [[ "$1" == "true" ]] && name="ytp2-apps"
  az deployment group create -g "$RESOURCE_GROUP" -n "$name" -f "$ROOT/infra/main.bicep" \
    -p deployerObjectId="$(deployer)" deployApps="$1" image="${2:-}" authClientId="${AUTH_CLIENT_ID:-}" \
       allowedUsers="$(allowed_users)" platforms="${PLATFORMS:-YOUTUBE}" igUserId="${IG_USER_ID:-}" \
       igApiHost="${IG_API_HOST:-graph.instagram.com}" anthropicModel="${ANTHROPIC_MODEL:-claude-haiku-5-5}" \
    --query properties.outputs -o json
}

build_image() {
  local registry tag; registry="$(output registryName)"; tag="$(date -u +%Y%m%d%H%M%S)"
  az acr build -r "$registry" -t "ytp:$tag" -t "ytp:latest" "$ROOT" --no-logs -o none >&2
  echo "$(output registryServer)/ytp:$tag"
}

latest_image() {
  local registry tag; registry="$(output registryName)"
  tag="$(az acr repository show-tags -n "$registry" --repository ytp --orderby time_desc --top 2 -o tsv | grep -v latest | head -1)"
  echo "$(output registryServer)/ytp:$tag"
}

set_redirect() {
  [[ -n "${CI:-}" ]] && return
  local url; url="$(az containerapp show -g "$RESOURCE_GROUP" -n ca-ytp2-console --query properties.configuration.ingress.fqdn -o tsv)"
  az ad app update --id "$AUTH_CLIENT_ID" --web-redirect-uris "https://$url/.auth/login/aad/callback" --enable-id-token-issuance true
  echo "Console: https://$url" >&2
}

store_secrets() {
  local vault; vault="$(output keyVaultName)"
  for pair in "anthropic-api-key:Anthropic API key" "youtube-api-key:YouTube Data API key (read-only, for trends)" \
              "ig-access-token:Instagram long-lived access token" "meta-app-secret:Meta app secret (optional)" \
              "pexels-api-key:Pexels API key (free, for stock footage)" "pixabay-api-key:Pixabay API key (optional)" \
              "fal-api-key:fal.ai API key (optional, AI images when no clip fits)"; do
    local name="${pair%%:*}" label="${pair#*:}" value
    read -r -s -p "$label (Enter to skip): " value; echo
    if [[ -n "$value" ]]; then
      (umask 077; printf '%s' "$value" >"$WORK/s")
      az keyvault secret set --vault-name "$vault" -n "$name" --file "$WORK/s" --encoding utf-8 -o none
      rm -f "$WORK/s"; echo "Stored $name." >&2
    fi
  done
  echo "YouTube upload token: run  KEY_VAULT_URI=$(output keyVaultUri) python -m ytp bootstrap-youtube client_secret.json" >&2
}

case "$ACTION" in
  base)    deploy false ;;
  image)   build_image ;;
  apps)    ensure_sign_in_app; deploy true "$(latest_image)"; set_redirect ;;
  apps-with-new-image) ensure_sign_in_app; img="$(build_image)"; deploy true "$img"; set_redirect ;;
  all)     deploy false; ensure_sign_in_app; img="$(build_image)"; deploy true "$img"; set_redirect ;;
  secrets) store_secrets ;;
  what-if) az deployment group what-if -g "$RESOURCE_GROUP" -f "$ROOT/infra/main.bicep" -p deployerObjectId="$(deployer)" ;;
  *)       sed -n '2,12p' "$0"; exit 2 ;;
esac
