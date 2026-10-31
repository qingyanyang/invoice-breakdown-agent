@description('Azure region for the workspace.')
param location string

@description('Log Analytics workspace name.')
param name string

@description('Retention in days.')
param retentionInDays int = 30

@description('Resource tags.')
param tags object = {}

resource workspace 'Microsoft.OperationalInsights/workspaces@2023-09-01' = {
  name: name
  location: location
  tags: tags
  properties: {
    sku: {
      name: 'PerGB2018'
    }
    retentionInDays: retentionInDays
  }
}

output id string = workspace.id
output name string = workspace.name
output customerId string = workspace.properties.customerId

// Emit the shared key from THIS resource so listKeys runs only after the workspace exists.
// Parent templates must use this output (not listKeys(resourceId(...))) for greenfield CAE deploys.
@secure()
output primarySharedKey string = workspace.listKeys().primarySharedKey
