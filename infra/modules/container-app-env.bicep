@description('Azure region for the Container Apps Environment.')
param location string

@description('Container Apps Environment name.')
param name string

@description('Log Analytics workspace customer ID.')
param logAnalyticsCustomerId string

@secure()
@description('Log Analytics workspace shared key.')
param logAnalyticsSharedKey string

@description('Resource tags.')
param tags object = {}

// Consumption environment without custom VNet (MVP). Outbound IPs are not static.
// Database is external Supabase (session pooler); no Azure Postgres firewall in this stack.
resource managedEnvironment 'Microsoft.App/managedEnvironments@2024-03-01' = {
  name: name
  location: location
  tags: tags
  properties: {
    appLogsConfiguration: {
      destination: 'log-analytics'
      logAnalyticsConfiguration: {
        customerId: logAnalyticsCustomerId
        sharedKey: logAnalyticsSharedKey
      }
    }
    zoneRedundant: false
  }
}

output id string = managedEnvironment.id
output name string = managedEnvironment.name
