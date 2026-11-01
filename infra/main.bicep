targetScope = 'resourceGroup'

@description('Azure region for backend and data-plane resources.')
param location string = 'australiasoutheast'

@description('Azure region for Static Web Apps (must be SWA-supported; not available in australiasoutheast).')
param staticWebAppLocation string = 'eastasia'

@description('Environment name for resource naming, tags, and the Container App ENVIRONMENT variable (e.g. demo).')
param environment string = 'demo'

@description('Short resource name prefix.')
param prefix string = 'eki'

@description('Resource tags applied to all resources.')
param tags object = {}

@secure()
@description('Supabase PostgreSQL DATABASE_URL (prefer session pooler host for IPv4). Required when deployContainerApp is true. Never logged or outputted.')
param databaseUrl string = ''

@secure()
@description('External LLM API key (optional). Used only when enableLlmApiKey is true. CI: --parameters llmApiKey=... enableLlmApiKey=true')
param llmApiKey string = ''

@description('When true, wire ACA secret llm-api-key. Set from deploy scripts when LLM_API_KEY is present — do not derive via empty(secure).')
param enableLlmApiKey bool = false

@description('PostgreSQL schema used by FastAPI / Alembic (Supabase). Shared project with Agent Platform; isolate via schema.')
param postgresSchema string = 'knowledge_intelligence'

@description('When false (Phase 1), skip Container App until a public GHCR image exists.')
param deployContainerApp bool = true

@description('Full public image reference, e.g. ghcr.io/<github-username>/knowledge-intelligence-api:<tag>. Required when deployContainerApp is true. No registry credentials (public pull).')
param containerImage string = 'ghcr.io/qingyanyang/knowledge-intelligence-api:latest'

@description('Container CPU cores.')
param containerCpu string = '0.25'

@description('Container memory.')
param containerMemory string = '0.5Gi'

@description('Minimum Container App replicas.')
param minReplicas int = 0

@description('Maximum Container App replicas.')
param maxReplicas int = 2

@description('SPA / CORS origins for Blob CORS and API env.')
param corsAllowedOrigins array = [
  'http://localhost:5173'
]

@description('Static Web Apps SKU.')
param staticWebAppSkuName string = 'Free'

var uniqueToken = uniqueString(resourceGroup().id)
var envShort = toLower(environment)
var commonTags = union({
  project: 'enterprise-knowledge-intelligence'
  environment: environment
  managedBy: 'bicep'
}, tags)

var logAnalyticsName = toLower('${prefix}-${envShort}-law')
var appInsightsName = toLower('${prefix}-${envShort}-ai')
var storageAccountName = toLower('${prefix}${envShort}st${uniqueToken}')
var containerAppsEnvironmentName = toLower('${prefix}-${envShort}-cae')
var containerAppName = toLower('${prefix}-${envShort}-api')
var staticWebAppName = toLower('${prefix}-${envShort}-swa-${uniqueToken}')
var uamiName = toLower('${prefix}-${envShort}-uami')

var corsOriginsCsv = join(corsAllowedOrigins, ',')

module logAnalytics 'modules/log-analytics.bicep' = {
  name: 'log-analytics'
  params: {
    location: location
    name: logAnalyticsName
    tags: commonTags
  }
}

// Depends on LAW via logAnalytics.outputs.id (workspace-based App Insights).
module appInsights 'modules/app-insights.bicep' = {
  name: 'app-insights'
  params: {
    location: location
    name: appInsightsName
    logAnalyticsWorkspaceId: logAnalytics.outputs.id
    tags: commonTags
  }
}

module storage 'modules/storage.bicep' = {
  name: 'storage'
  params: {
    location: location
    name: storageAccountName
    networkDefaultAction: 'Allow'
    corsAllowedOrigins: corsAllowedOrigins
    tags: commonTags
  }
}

module managedIdentity 'modules/managed-identity.bicep' = {
  name: 'managed-identity'
  params: {
    location: location
    name: uamiName
    tags: commonTags
  }
}

// Blob Data Contributor on UAMI when Container App is deployed (no ACR / AcrPull).
module identityRbac 'modules/identity-rbac.bicep' = {
  name: 'identity-rbac'
  params: {
    uamiPrincipalId: managedIdentity.outputs.principalId
    roleAssignmentNamePrefix: uamiName
    storageAccountName: storage.outputs.name
    assignBlobDataContributor: deployContainerApp
  }
}

// Depends on LAW via customerId + primarySharedKey outputs.
module containerAppEnv 'modules/container-app-env.bicep' = {
  name: 'container-app-env'
  params: {
    location: location
    name: containerAppsEnvironmentName
    logAnalyticsCustomerId: logAnalytics.outputs.customerId
    logAnalyticsSharedKey: logAnalytics.outputs.primarySharedKey
    tags: commonTags
  }
}

module staticWebApp 'modules/static-web-app.bicep' = {
  name: 'static-web-app'
  params: {
    location: staticWebAppLocation
    name: staticWebAppName
    skuName: staticWebAppSkuName
    tags: commonTags
  }
}

// Phase 2: public GHCR image + Supabase DATABASE_URL required.
module containerApp 'modules/container-app.bicep' = if (deployContainerApp) {
  name: 'container-app'
  params: {
    location: location
    appName: containerAppName
    environmentId: containerAppEnv.outputs.id
    containerImage: containerImage
    uamiResourceId: managedIdentity.outputs.id
    uamiClientId: managedIdentity.outputs.clientId
    appInsightsConnectionString: appInsights.outputs.connectionString
    storageBlobEndpoint: storage.outputs.blobEndpoint
    environmentName: environment
    databaseUrl: databaseUrl
    postgresSchema: postgresSchema
    llmApiKey: llmApiKey
    enableLlmApiKey: enableLlmApiKey
    containerCpu: containerCpu
    containerMemory: containerMemory
    minReplicas: minReplicas
    maxReplicas: maxReplicas
    corsAllowedOrigins: corsOriginsCsv
    tags: commonTags
  }
  dependsOn: [
    identityRbac
  ]
}

output resourceGroupName string = resourceGroup().name
output location string = location
output staticWebAppLocation string = staticWebAppLocation

output logAnalyticsWorkspaceName string = logAnalytics.outputs.name
output appInsightsName string = appInsights.outputs.name
output appInsightsConnectionString string = appInsights.outputs.connectionString

output storageAccountName string = storage.outputs.name
output storageBlobEndpoint string = storage.outputs.blobEndpoint

output uamiName string = managedIdentity.outputs.name
output uamiId string = managedIdentity.outputs.id
output uamiClientId string = managedIdentity.outputs.clientId
output uamiPrincipalId string = managedIdentity.outputs.principalId

output containerAppsEnvironmentName string = containerAppEnv.outputs.name
output containerAppName string = deployContainerApp ? containerApp!.outputs.name : ''
output containerAppUrl string = deployContainerApp ? containerApp!.outputs.url : ''

output staticWebAppName string = staticWebApp.outputs.name
output staticWebAppUrl string = staticWebApp.outputs.url

output postgresSchema string = postgresSchema
