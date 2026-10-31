targetScope = 'subscription'

@description('Azure region for the resource group.')
param location string = 'australiasoutheast'

@description('Name of the application resource group.')
param resourceGroupName string = 'rg-knowledge-intelligence-demo'

@description('Tags applied to the resource group.')
param tags object = {
  project: 'enterprise-knowledge-intelligence'
  environment: 'demo'
  managedBy: 'bicep'
}

resource rg 'Microsoft.Resources/resourceGroups@2024-03-01' = {
  name: resourceGroupName
  location: location
  tags: tags
}

output resourceGroupName string = rg.name
output resourceGroupId string = rg.id
output location string = rg.location
