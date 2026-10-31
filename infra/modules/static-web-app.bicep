@description('Azure region for Static Web Apps. Must be a SWA-supported region (e.g. eastasia). Not available in australiasoutheast.')
param location string

@description('Static Web App name.')
param name string

@description('SKU name. Free is cost-conscious for portfolio MVP.')
@allowed([
  'Free'
  'Standard'
])
param skuName string = 'Free'

@description('Resource tags.')
param tags object = {}

resource staticWebApp 'Microsoft.Web/staticSites@2022-09-01' = {
  name: name
  location: location
  tags: tags
  sku: {
    name: skuName
    tier: skuName
  }
  properties: {
    allowConfigFileUpdates: true
  }
}

output id string = staticWebApp.id
output name string = staticWebApp.name
output defaultHostname string = staticWebApp.properties.defaultHostname
output url string = 'https://${staticWebApp.properties.defaultHostname}'
