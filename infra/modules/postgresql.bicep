@description('Azure region for PostgreSQL Flexible Server.')
param location string

@description('PostgreSQL Flexible Server name (globally unique).')
param serverName string

@description('Administrator login name (not azure_superuser / azure_pg_admin / admin / administrator / root / guest).')
param administratorLogin string

@secure()
@description('Administrator password. Supply via environment variable or CI --parameters override; never commit.')
param administratorPassword string

@description('Database name. Application schemas are managed by Alembic, not Bicep.')
param databaseName string = 'knowledge_intelligence_db'

@description('PostgreSQL major version.')
param postgresVersion string = '16'

@description('Compute SKU name, e.g. Standard_B1ms for cost-conscious dev.')
param skuName string = 'Standard_B1ms'

@description('Compute tier.')
@allowed([
  'Burstable'
  'GeneralPurpose'
  'MemoryOptimized'
])
param skuTier string = 'Burstable'

@description('Storage size in GB.')
param storageSizeGb int = 32

@description('Backup retention in days.')
param backupRetentionDays int = 7

@description('Enable public network access. Required for non-VNet Container Apps MVP.')
@allowed([
  'Enabled'
  'Disabled'
])
param publicNetworkAccess string = 'Enabled'

@description('When true, adds firewall rule 0.0.0.0-0.0.0.0 (Allow Azure services). Module default is false; enable only intentionally (e.g. low-cost dev MVP). Keep false for production.')
param allowAzureServicesAccess bool = false

@description('Additional firewall rules: array of { name, startIpAddress, endIpAddress }.')
param firewallRules array = []

@description('Comma-separated azure.extensions allowlist. Use vector (not pgvector) for Azure PostgreSQL. CREATE EXTENSION remains an Alembic/app step.')
param azureExtensions string = 'vector'

@description('Resource tags.')
param tags object = {}

resource postgresServer 'Microsoft.DBforPostgreSQL/flexibleServers@2024-08-01' = {
  name: serverName
  location: location
  tags: tags
  sku: {
    name: skuName
    tier: skuTier
  }
  properties: {
    version: postgresVersion
    administratorLogin: administratorLogin
    administratorLoginPassword: administratorPassword
    storage: {
      storageSizeGB: storageSizeGb
    }
    backup: {
      backupRetentionDays: backupRetentionDays
      geoRedundantBackup: 'Disabled'
    }
    highAvailability: {
      mode: 'Disabled'
    }
    network: {
      publicNetworkAccess: publicNetworkAccess
    }
    authConfig: {
      activeDirectoryAuth: 'Disabled'
      passwordAuth: 'Enabled'
    }
  }
}

// Allowlist pgvector under its Azure extension name "vector". Does NOT run CREATE EXTENSION.
resource azureExtensionsConfig 'Microsoft.DBforPostgreSQL/flexibleServers/configurations@2024-08-01' = {
  parent: postgresServer
  name: 'azure.extensions'
  properties: {
    value: azureExtensions
    source: 'user-override'
  }
}

resource azureServicesFirewall 'Microsoft.DBforPostgreSQL/flexibleServers/firewallRules@2024-08-01' = if (allowAzureServicesAccess && publicNetworkAccess == 'Enabled') {
  parent: postgresServer
  name: 'AllowAllAzureServicesAndResourcesWithinAzureIps'
  properties: {
    startIpAddress: '0.0.0.0'
    endIpAddress: '0.0.0.0'
  }
}

resource extraFirewallRules 'Microsoft.DBforPostgreSQL/flexibleServers/firewallRules@2024-08-01' = [
  for rule in firewallRules: if (publicNetworkAccess == 'Enabled') {
    parent: postgresServer
    name: rule.name
    properties: {
      startIpAddress: rule.startIpAddress
      endIpAddress: rule.endIpAddress
    }
  }
]

resource database 'Microsoft.DBforPostgreSQL/flexibleServers/databases@2024-08-01' = {
  parent: postgresServer
  name: databaseName
  properties: {
    charset: 'UTF8'
    collation: 'en_US.utf8'
  }
}

output id string = postgresServer.id
output name string = postgresServer.name
output fqdn string = postgresServer.properties.fullyQualifiedDomainName
output databaseName string = database.name
