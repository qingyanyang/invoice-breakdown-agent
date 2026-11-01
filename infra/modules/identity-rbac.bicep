@description('User-assigned managed identity principal ID (for role assignments).')
param uamiPrincipalId string

@description('Stable name fragment for role assignment GUIDs.')
param roleAssignmentNamePrefix string

@description('Storage account name for Blob Data Contributor.')
param storageAccountName string

@description('When true, assign Storage Blob Data Contributor to the UAMI (typically with Container App).')
param assignBlobDataContributor bool = false

resource storageAccount 'Microsoft.Storage/storageAccounts@2023-05-01' existing = {
  name: storageAccountName
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
