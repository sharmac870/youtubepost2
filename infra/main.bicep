// youtubeposts2: every Azure resource, sized for the lowest running cost.
//
// What it creates (all in the target resource group) and what it costs at ~5 videos a day:
//   Storage account (tables = state, queues = work, blob = media)   ~ $0.10-0.50 / month
//   Key Vault (Standard, RBAC)                                      ~ $0.03 / month
//   Container Registry (Basic)                                      ~ $5 / month  (largest line; see README for GHCR)
//   Log Analytics (30-day retention, 0.2 GB/day cap)                 $0 inside the 5 GB/month free grant
//   Container Apps environment (consumption only, no VNet)           $0
//   4 Container Apps jobs + 1 scale-to-zero console app              ~ $0 inside the monthly free grant
// Deliberately NOT created (LLD Section 7.1 items that cost money for little gain at this scale):
//   PostgreSQL Flexible (~$13-25), Service Bus Standard/Premium ($10-670), Front Door Premium + WAF (~$330),
//   NAT Gateway (~$32), private endpoints (~$7 each), Premium ACR (~$50).

targetScope = 'resourceGroup'

@description('Azure region. Defaults to the resource group region (Central India keeps operator data in India).')
param location string = resourceGroup().location

@description('Object id of the person running the deployment: gets Key Vault Secrets Officer to load secrets.')
param deployerObjectId string

@description('Deploy the container apps and jobs (false on the very first run, before the image exists).')
param deployApps bool = false

@description('Container image, e.g. <acr>.azurecr.io/ytp:<tag>.')
param image string = ''

@description('Entra app registration (client id) used by the console sign-in.')
param authClientId string = ''

@description('Comma-separated Entra object ids or e-mails allowed into the console.')
param allowedUsers string = ''

@description('Comma-separated platforms to publish to.')
param platforms string = 'YOUTUBE'

@description('Instagram professional account id (optional: looked up from the token when empty).')
param igUserId string = ''

@description('Graph host: graph.instagram.com (Instagram Login) or graph.facebook.com (Facebook Login).')
param igApiHost string = 'graph.instagram.com'

@description('Claude model for scripts and moderation.')
param anthropicModel string = 'claude-haiku-5-5'

@description('Discovery schedule (UTC). Default 06:00, 12:00, 18:00, 00:00 IST.')
param discoveryCron string = '30 0,6,12,18 * * *'

var suffix = take(uniqueString(resourceGroup().id), 6)
var names = {
  storage: 'stytp2${suffix}'
  vault: 'kv-ytp2-${suffix}'
  registry: 'crytp2${suffix}'
  logs: 'log-ytp2'
  env: 'cae-ytp2'
  worker: 'id-ytp2-worker'
  console: 'id-ytp2-console'
}
var queueNames = ['q-discover', 'q-script', 'q-render', 'q-publish-youtube', 'q-publish-instagram']
var roles = {
  blobContributor: 'ba92f5b4-2d11-453d-a403-e96b0029c9fe'
  blobReader: '2a2b9908-6ea1-4ae2-8e65-a410df84e7d1'
  queueContributor: '974c5e8b-45b9-4653-ba55-5f855dd0fb88'
  tableContributor: '0a9a7e1f-b9d0-4cc4-a60d-0319b160aaa3'
  kvSecretsUser: '4633458b-17de-408a-b874-0445c86b69e6'
  kvSecretsOfficer: 'b86a8fe4-44ce-4948-aee5-eccb2c155cd7'
  acrPull: '7f951dda-4ed3-4680-a7ca-43fe172d538d'
}

// ----- data -----

resource storage 'Microsoft.Storage/storageAccounts@2023-05-01' = {
  name: names.storage
  location: location
  sku: { name: 'Standard_LRS' }
  kind: 'StorageV2'
  properties: {
    accessTier: 'Hot'
    allowBlobPublicAccess: false
    allowSharedKeyAccess: false          // Entra ID only: no account keys or connection strings anywhere
    minimumTlsVersion: 'TLS1_2'
    supportsHttpsTrafficOnly: true
    defaultToOAuthAuthentication: true
  }
}

resource blobService 'Microsoft.Storage/storageAccounts/blobServices@2023-05-01' = {
  parent: storage
  name: 'default'
  properties: { deleteRetentionPolicy: { enabled: true, days: 7 } }
}

resource media 'Microsoft.Storage/storageAccounts/blobServices/containers@2023-05-01' = {
  parent: blobService
  name: 'media'
  properties: { publicAccess: 'None' }
}

resource lifecycle 'Microsoft.Storage/storageAccounts/managementPolicies@2023-05-01' = {
  parent: storage
  name: 'default'
  properties: {
    policy: {
      rules: [
        {
          name: 'delete-media-after-45-days'   // LLD 6.3: 30 days after publish
          enabled: true
          type: 'Lifecycle'
          definition: {
            filters: { blobTypes: ['blockBlob'], prefixMatch: ['media/'] }
            actions: { baseBlob: { delete: { daysAfterModificationGreaterThan: 45 } } }
          }
        }
      ]
    }
  }
}

resource queueService 'Microsoft.Storage/storageAccounts/queueServices@2023-05-01' = {
  parent: storage
  name: 'default'
}

resource queues 'Microsoft.Storage/storageAccounts/queueServices/queues@2023-05-01' = [for q in concat(queueNames, map(queueNames, n => '${n}-poison')): {
  parent: queueService
  name: q
}]

resource tableService 'Microsoft.Storage/storageAccounts/tableServices@2023-05-01' = {
  parent: storage
  name: 'default'
}

resource tables 'Microsoft.Storage/storageAccounts/tableServices/tables@2023-05-01' = [for t in ['items', 'runs', 'topics', 'ledger', 'flags', 'metrics']: {
  parent: tableService
  name: t
}]

resource vault 'Microsoft.KeyVault/vaults@2023-07-01' = {
  name: names.vault
  location: location
  properties: {
    tenantId: subscription().tenantId
    sku: { family: 'A', name: 'standard' }
    enableRbacAuthorization: true
    enableSoftDelete: true
    softDeleteRetentionInDays: 30
    enablePurgeProtection: true
    publicNetworkAccess: 'Enabled'
  }
}

resource registry 'Microsoft.ContainerRegistry/registries@2023-07-01' = {
  name: names.registry
  location: location
  sku: { name: 'Basic' }
  properties: { adminUserEnabled: false }
}

resource logs 'Microsoft.OperationalInsights/workspaces@2023-09-01' = {
  name: names.logs
  location: location
  properties: {
    sku: { name: 'PerGB2018' }
    retentionInDays: 30
    workspaceCapping: { dailyQuotaGb: json('0.2') }
  }
}

// ----- identities and least-privilege roles -----

resource workerId 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = {
  name: names.worker
  location: location
}

resource consoleId 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = {
  name: names.console
  location: location
}

var storageGrants = [
  { who: 'worker', role: roles.blobContributor }
  { who: 'worker', role: roles.queueContributor }
  { who: 'worker', role: roles.tableContributor }
  { who: 'console', role: roles.blobReader }
  { who: 'console', role: roles.queueContributor }
  { who: 'console', role: roles.tableContributor }
]

resource storageRoles 'Microsoft.Authorization/roleAssignments@2022-04-01' = [for g in storageGrants: {
  scope: storage
  name: guid(storage.id, g.who == 'worker' ? workerId.id : consoleId.id, g.role)
  properties: {
    principalId: g.who == 'worker' ? workerId.properties.principalId : consoleId.properties.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', g.role)
  }
}]

// Only the workers can read platform tokens. The console holds no credentials.
resource workerSecrets 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: vault
  name: guid(vault.id, workerId.id, roles.kvSecretsUser)
  properties: {
    principalId: workerId.properties.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roles.kvSecretsUser)
  }
}

resource deployerSecrets 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: vault
  name: guid(vault.id, deployerObjectId, roles.kvSecretsOfficer)
  properties: {
    principalId: deployerObjectId
    principalType: 'User'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roles.kvSecretsOfficer)
  }
}

resource pulls 'Microsoft.Authorization/roleAssignments@2022-04-01' = [for who in ['worker', 'console']: {
  scope: registry
  name: guid(registry.id, who == 'worker' ? workerId.id : consoleId.id, roles.acrPull)
  properties: {
    principalId: who == 'worker' ? workerId.properties.principalId : consoleId.properties.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roles.acrPull)
  }
}]

// The console may read exactly one secret: its own sign-in client secret (created by deploy.sh before the apps).
resource authSecret 'Microsoft.KeyVault/vaults/secrets@2023-07-01' existing = {
  parent: vault
  name: 'auth-client-secret'
}

resource consoleReadsAuthSecret 'Microsoft.Authorization/roleAssignments@2022-04-01' = if (deployApps) {
  scope: authSecret
  name: guid(vault.id, consoleId.id, 'auth-client-secret')
  properties: {
    principalId: consoleId.properties.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roles.kvSecretsUser)
  }
}

// ----- compute (consumption-only Container Apps) -----

resource env 'Microsoft.App/managedEnvironments@2024-03-01' = if (deployApps) {
  name: names.env
  location: location
  properties: {
    appLogsConfiguration: {
      destination: 'log-analytics'
      logAnalyticsConfiguration: {
        customerId: logs.properties.customerId
        sharedKey: logs.listKeys().primarySharedKey
      }
    }
    workloadProfiles: [{ name: 'Consumption', workloadProfileType: 'Consumption' }]
  }
}

var commonEnv = [
  { name: 'BACKEND', value: 'azure' }
  { name: 'STORAGE_ACCOUNT', value: storage.name }
  { name: 'KEY_VAULT_URI', value: vault.properties.vaultUri }
  { name: 'PLATFORMS', value: platforms }
  { name: 'IG_USER_ID', value: igUserId }
  { name: 'IG_API_HOST', value: igApiHost }
  { name: 'ANTHROPIC_MODEL', value: anthropicModel }
  { name: 'MODERATION_MODEL', value: anthropicModel }
  { name: 'YT_PRIVACY_STATUS', value: 'private' }
]
var workerEnv = concat(commonEnv, [{ name: 'AZURE_CLIENT_ID', value: workerId.properties.clientId }])
var registries = [{ server: registry.properties.loginServer, identity: workerId.id }]

var jobs = [
  {
    name: 'job-ytp2-discover'
    trigger: 'Schedule'
    cron: discoveryCron
    args: ['discover']
    cpu: '0.5'
    memory: '1Gi'
    timeout: 900
    queues: []
  }
  {
    name: 'job-ytp2-sweep'
    trigger: 'Schedule'
    cron: '*/30 * * * *'
    args: ['sweep']
    cpu: '0.25'
    memory: '0.5Gi'
    timeout: 600
    queues: []
  }
  {
    name: 'job-ytp2-worker'
    trigger: 'Event'
    cron: ''
    args: ['work', '--queues', 'q-discover,q-script,q-publish-youtube,q-publish-instagram', '--max-runtime', '1500']
    cpu: '0.5'
    memory: '1Gi'
    timeout: 1800
    queues: ['q-discover', 'q-script', 'q-publish-youtube', 'q-publish-instagram']
  }
  {
    name: 'job-ytp2-render'
    trigger: 'Event'
    cron: ''
    args: ['work', '--queues', 'q-render', '--max-runtime', '2100']
    cpu: '2'
    memory: '4Gi'
    timeout: 2400
    queues: ['q-render']
  }
]

resource containerJobs 'Microsoft.App/jobs@2025-01-01' = [for j in jobs: if (deployApps) {
  name: j.name
  location: location
  identity: { type: 'UserAssigned', userAssignedIdentities: { '${workerId.id}': {} } }
  properties: {
    environmentId: env.id
    workloadProfileName: 'Consumption'
    configuration: {
      triggerType: j.trigger
      replicaTimeout: j.timeout
      replicaRetryLimit: 0          // retries are the queue's job (visibility timeout + dead-lettering)
      registries: registries
      scheduleTriggerConfig: j.trigger == 'Schedule' ? { cronExpression: j.cron, parallelism: 1, replicaCompletionCount: 1 } : null
      eventTriggerConfig: j.trigger == 'Event' ? {
        parallelism: 1
        replicaCompletionCount: 1
        scale: {
          minExecutions: 0
          maxExecutions: 1            // one publisher at a time: uploads are serialised per channel
          pollingInterval: 30
          rules: map(j.queues, q => {
            name: q
            type: 'azure-queue'
            metadata: { accountName: storage.name, queueName: q, queueLength: '1' }
            identity: workerId.id
          })
        }
      } : null
    }
    template: {
      containers: [
        {
          name: 'main'
          image: image
          args: j.args
          env: workerEnv
          resources: { cpu: json(j.cpu), memory: j.memory }
        }
      ]
    }
  }
}]

resource consoleApp 'Microsoft.App/containerApps@2024-03-01' = if (deployApps) {
  name: 'ca-ytp2-console'
  location: location
  identity: { type: 'UserAssigned', userAssignedIdentities: { '${consoleId.id}': {} } }
  dependsOn: [consoleReadsAuthSecret]
  properties: {
    environmentId: env.id
    workloadProfileName: 'Consumption'
    configuration: {
      ingress: { external: true, targetPort: 8000, transport: 'auto', allowInsecure: false }
      registries: [{ server: registry.properties.loginServer, identity: consoleId.id }]
      secrets: [
        {
          name: 'microsoft-provider-authentication-secret'
          keyVaultUrl: '${vault.properties.vaultUri}secrets/auth-client-secret'
          identity: consoleId.id
        }
      ]
    }
    template: {
      containers: [
        {
          name: 'console'
          image: image
          args: ['serve', '--host', '0.0.0.0', '--port', '8000']
          env: concat(commonEnv, [
            { name: 'AZURE_CLIENT_ID', value: consoleId.properties.clientId }
            { name: 'ALLOWED_USERS', value: allowedUsers }
          ])
          resources: { cpu: json('0.25'), memory: '0.5Gi' }
          probes: [{ type: 'Liveness', httpGet: { path: '/healthz', port: 8000 }, periodSeconds: 30 }]
        }
      ]
      scale: { minReplicas: 0, maxReplicas: 1 }   // scale to zero: costs nothing while nobody is looking
    }
  }
}

resource consoleAuth 'Microsoft.App/containerApps/authConfigs@2024-03-01' = if (deployApps && !empty(authClientId)) {
  parent: consoleApp
  name: 'current'
  properties: {
    platform: { enabled: true }
    globalValidation: {
      unauthenticatedClientAction: 'RedirectToLoginPage'
      redirectToProvider: 'azureactivedirectory'
      excludedPaths: ['/healthz']
    }
    identityProviders: {
      azureActiveDirectory: {
        enabled: true
        registration: {
          clientId: authClientId
          clientSecretSettingName: 'microsoft-provider-authentication-secret'
          openIdIssuer: '${environment().authentication.loginEndpoint}${subscription().tenantId}/v2.0'
        }
        validation: { allowedAudiences: [authClientId, 'api://${authClientId}'] }
      }
    }
  }
}

output storageAccount string = storage.name
output keyVaultUri string = vault.properties.vaultUri
output keyVaultName string = vault.name
output registryServer string = registry.properties.loginServer
output registryName string = registry.name
output consoleUrl string = deployApps ? 'https://${consoleApp!.properties.configuration.ingress.fqdn}' : ''
