targetScope = 'resourceGroup'

@description('Azure region for backend and data-plane resources.')
param location string = 'australiasoutheast'

@description('Azure region for Static Web Apps (must be SWA-supported; not available in australiasoutheast).')
param staticWebAppLocation string = 'eastasia'

@description('Environment name (used in resource naming and tags).')
param environment string = 'demo'

@description('Short resource name prefix.')
param prefix string = 'eki'

@description('Resource tags applied to all resources.')
param tags object = {}

@description('PostgreSQL administrator login (migrations / bootstrap only — not used by the Container App runtime).')
param postgresAdminLogin string = 'pgadmin'

@secure()
@description('PostgreSQL administrator password. Local: export POSTGRES_ADMIN_PASSWORD. CI: override via --parameters.')
param postgresAdminPassword string

@description('Least-privilege application DB login used by FastAPI at runtime.')
param postgresAppLogin string = 'eki_app'

@secure()
@description('Least-privilege application DB password. Create the role with scripts/create-postgres-app-user.sh before Phase 2. Empty is fine while deployContainerApp is false.')
param postgresAppPassword string = ''

@secure()
@description('External LLM API key. IaC-owned secret (declared in Container App Bicep). CI: --parameters llmApiKey=...')
param llmApiKey string = ''

@description('PostgreSQL major version.')
param postgresVersion string = '16'

@description('PostgreSQL compute SKU.')
param postgresSkuName string = 'Standard_B1ms'

@description('PostgreSQL compute tier.')
param postgresSkuTier string = 'Burstable'

@description('PostgreSQL storage size in GB.')
param postgresStorageSizeGb int = 32

@description('PostgreSQL backup retention in days.')
param postgresBackupRetentionDays int = 7

@description('Database name (schemas via Alembic, not Bicep).')
param postgresDatabaseName string = 'knowledge_intelligence_db'

@description('Allow Azure services firewall rule (0.0.0.0). Module-level default is false; set true only for low-cost non-VNet MVP. Keep false for production.')
param allowAzureServicesAccess bool = false

@description('Extra PostgreSQL firewall rules: [{ name, startIpAddress, endIpAddress }].')
param postgresFirewallRules array = []

@description('azure.extensions allowlist value (vector = pgvector). CREATE EXTENSION is Alembic responsibility.')
param postgresAzureExtensions string = 'vector'

@description('When false (Phase 1), skip Container App until a real FastAPI image exists in ACR. UAMI + AcrPull still deploy.')
param deployContainerApp bool = false

@description('Full image reference for the API Container App (required when deployContainerApp is true).')
param containerImage string = ''

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

@description('ACR SKU.')
param acrSkuName string = 'Basic'

var uniqueToken = uniqueString(resourceGroup().id)
var envShort = toLower(environment)
var commonTags = union({
  project: 'enterprise-knowledge-intelligence'
  environment: environment
  managedBy: 'bicep'
}, tags)

var logAnalyticsName = toLower('${prefix}-${envShort}-law')
var appInsightsName = toLower('${prefix}-${envShort}-ai')
var acrName = toLower('${prefix}${envShort}acr${uniqueToken}')
var storageAccountName = toLower('${prefix}${envShort}st${uniqueToken}')
var postgresServerName = toLower('${prefix}-${envShort}-pg-${uniqueToken}')
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

module containerRegistry 'modules/container-registry.bicep' = {
  name: 'container-registry'
  params: {
    location: location
    name: acrName
    skuName: acrSkuName
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

module postgresql 'modules/postgresql.bicep' = {
  name: 'postgresql'
  params: {
    location: location
    serverName: postgresServerName
    administratorLogin: postgresAdminLogin
    administratorPassword: postgresAdminPassword
    databaseName: postgresDatabaseName
    postgresVersion: postgresVersion
    skuName: postgresSkuName
    skuTier: postgresSkuTier
    storageSizeGb: postgresStorageSizeGb
    backupRetentionDays: postgresBackupRetentionDays
    publicNetworkAccess: 'Enabled'
    allowAzureServicesAccess: allowAzureServicesAccess
    firewallRules: postgresFirewallRules
    azureExtensions: postgresAzureExtensions
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

// AcrPull on UAMI BEFORE Container App. Blob RBAC only when CA is deployed.
module identityRbac 'modules/identity-rbac.bicep' = {
  name: 'identity-rbac'
  params: {
    uamiPrincipalId: managedIdentity.outputs.principalId
    roleAssignmentNamePrefix: uamiName
    storageAccountName: storage.outputs.name
    containerRegistryName: containerRegistry.outputs.name
    assignBlobDataContributor: deployContainerApp
  }
}

// Depends on LAW via customerId + primarySharedKey outputs (waits for nested LAW deploy).
// Do not use listKeys(resourceId(...)) here — that can race on an empty RG.
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

// Phase 2: real FastAPI image required. Depends on AcrPull via identityRbac.
module containerApp 'modules/container-app.bicep' = if (deployContainerApp) {
  name: 'container-app'
  params: {
    location: location
    appName: containerAppName
    environmentId: containerAppEnv.outputs.id
    containerImage: containerImage
    registryLoginServer: containerRegistry.outputs.loginServer
    uamiResourceId: managedIdentity.outputs.id
    uamiClientId: managedIdentity.outputs.clientId
    appInsightsConnectionString: appInsights.outputs.connectionString
    storageBlobEndpoint: storage.outputs.blobEndpoint
    environmentName: environment
    postgresHost: postgresql.outputs.fqdn
    postgresPort: '5432'
    postgresDb: postgresql.outputs.databaseName
    postgresUser: postgresAppLogin
    postgresPassword: postgresAppPassword
    llmApiKey: llmApiKey
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

output containerRegistryName string = containerRegistry.outputs.name
output containerRegistryLoginServer string = containerRegistry.outputs.loginServer

output storageAccountName string = storage.outputs.name
output storageBlobEndpoint string = storage.outputs.blobEndpoint

output postgresServerName string = postgresql.outputs.name
output postgresFqdn string = postgresql.outputs.fqdn
output postgresDatabaseName string = postgresql.outputs.databaseName
output postgresAppLogin string = postgresAppLogin

output uamiName string = managedIdentity.outputs.name
output uamiId string = managedIdentity.outputs.id
output uamiClientId string = managedIdentity.outputs.clientId
output uamiPrincipalId string = managedIdentity.outputs.principalId

output containerAppsEnvironmentName string = containerAppEnv.outputs.name
output containerAppName string = deployContainerApp ? containerApp!.outputs.name : ''
output containerAppUrl string = deployContainerApp ? containerApp!.outputs.url : ''

output staticWebAppName string = staticWebApp.outputs.name
output staticWebAppUrl string = staticWebApp.outputs.url
