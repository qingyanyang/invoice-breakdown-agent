@description('User-assigned managed identity principal ID (for role assignments).')
param uamiPrincipalId string

@description('Stable name fragment for role assignment GUIDs.')
param roleAssignmentNamePrefix string

@description('Storage account name for Blob Data Contributor (optional Phase 2).')
param storageAccountName string

@description('Azure Container Registry name for AcrPull (Phase 1 — before Container App).')
param containerRegistryName string

@description('When true, assign Storage Blob Data Contributor to the UAMI (Phase 2 with Container App).')
param assignBlobDataContributor bool = false

resource storageAccount 'Microsoft.Storage/storageAccounts@2023-05-01' existing = {
  name: storageAccountName
}

resource acr 'Microsoft.ContainerRegistry/registries@2023-07-01' existing = {
  name: containerRegistryName
}

// AcrPull MUST exist before Container App revisions that pull from ACR.
resource acrPull 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(acr.id, roleAssignmentNamePrefix, 'acr-pull-uami')
  scope: acr
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', '7f951dda-4ed3-4680-a7ca-43fe172d538d')
    principalId: uamiPrincipalId
    principalType: 'ServicePrincipal'
  }
}

resource storageBlobDataContributor 'Microsoft.Authorization/roleAssignments@2022-04-01' = if (assignBlobDataContributor) {
  name: guid(storageAccount.id, roleAssignmentNamePrefix, 'storage-blob-data-contributor-uami')
  scope: storageAccount
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', 'ba92f5b4-2d11-453d-a403-e96b0029c9fe')
    principalId: uamiPrincipalId
    principalType: 'ServicePrincipal'
  }
}
