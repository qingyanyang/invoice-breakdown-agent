@description('Azure region for the Container App.')
param location string

@description('Container App name.')
param appName string

@description('Resource ID of the Container Apps Environment.')
param environmentId string

@description('Full container image reference including registry and tag.')
param containerImage string

@description('ACR login server, e.g. myacr.azurecr.io.')
param registryLoginServer string

@description('User-assigned managed identity resource ID (used for ACR pull and app Azure auth).')
param uamiResourceId string

@description('User-assigned managed identity client ID (AZURE_CLIENT_ID for DefaultAzureCredential).')
param uamiClientId string

@description('Application Insights connection string (non-secret telemetry endpoint config).')
param appInsightsConnectionString string

@description('Blob service endpoint URL for DefaultAzureCredential access.')
param storageBlobEndpoint string

@description('Environment name passed to the app as ENVIRONMENT (e.g. demo).')
param environmentName string

@description('PostgreSQL FQDN host.')
param postgresHost string

@description('PostgreSQL port.')
param postgresPort string = '5432'

@description('PostgreSQL database name.')
param postgresDb string

@description('Least-privilege application DB login (not the server admin).')
param postgresUser string

@secure()
@description('Least-privilege application DB password.')
param postgresPassword string

@secure()
@description('External LLM API key. Declared in Bicep secrets (IaC-owned) so redeploys do not wipe it. Supply via --parameters / env.')
param llmApiKey string

@description('Container listen port.')
param containerPort int = 8000

@description('Minimum replicas (0 enables scale-to-zero on Consumption).')
param minReplicas int = 0

@description('Maximum replicas.')
param maxReplicas int = 2

@description('Liveness probe path.')
param livenessPath string = '/health'

@description('Readiness probe path.')
param readinessPath string = '/health'

@description('Container CPU cores.')
param containerCpu string = '0.25'

@description('Container memory.')
param containerMemory string = '0.5Gi'

@description('CORS allowed origins for the API (comma-separated).')
param corsAllowedOrigins string = 'http://localhost:5173'

@description('Resource tags.')
param tags object = {}

// Option A — single secret owner: Bicep declares ALL app secrets used by the CA.
// Do not also manage llm-api-key via CLI/portal; a secrets array is replace-semantics.
// Do not build DATABASE_URL in Bicep (password URL-encoding). App builds URL via URL.create().
resource containerApp 'Microsoft.App/containerApps@2024-03-01' = {
  name: appName
  location: location
  tags: tags
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: {
      '${uamiResourceId}': {}
    }
  }
  properties: {
    managedEnvironmentId: environmentId
    configuration: {
      activeRevisionsMode: 'Single'
      ingress: {
        external: true
        targetPort: containerPort
        transport: 'auto'
        allowInsecure: false
        traffic: [
          {
            weight: 100
            latestRevision: true
          }
        ]
      }
      registries: [
        {
          server: registryLoginServer
          identity: uamiResourceId
        }
      ]
      secrets: [
        {
          name: 'postgres-password'
          value: postgresPassword
        }
        {
          name: 'llm-api-key'
          value: llmApiKey
        }
      ]
    }
    template: {
      containers: [
        {
          name: 'api'
          image: containerImage
          resources: {
            cpu: json(containerCpu)
            memory: containerMemory
          }
          probes: [
            {
              type: 'Liveness'
              failureThreshold: 3
              httpGet: {
                path: livenessPath
                port: containerPort
              }
              initialDelaySeconds: 30
              periodSeconds: 20
              timeoutSeconds: 5
            }
            {
              type: 'Readiness'
              failureThreshold: 3
              httpGet: {
                path: readinessPath
                port: containerPort
              }
              initialDelaySeconds: 10
              periodSeconds: 10
              timeoutSeconds: 5
            }
          ]
          env: [
            {
              name: 'ENVIRONMENT'
              value: environmentName
            }
            {
              name: 'APPLICATIONINSIGHTS_CONNECTION_STRING'
              value: appInsightsConnectionString
            }
            {
              name: 'POSTGRES_HOST'
              value: postgresHost
            }
            {
              name: 'POSTGRES_PORT'
              value: postgresPort
            }
            {
              name: 'POSTGRES_DB'
              value: postgresDb
            }
            {
              name: 'POSTGRES_USER'
              value: postgresUser
            }
            {
              name: 'POSTGRES_PASSWORD'
              secretRef: 'postgres-password'
            }
            {
              name: 'LLM_API_KEY'
              secretRef: 'llm-api-key'
            }
            {
              name: 'AZURE_CLIENT_ID'
              value: uamiClientId
            }
            {
              name: 'AZURE_STORAGE_BLOB_ENDPOINT'
              value: storageBlobEndpoint
            }
            {
              name: 'AZURE_STORAGE_DOCUMENTS_CONTAINER'
              value: 'documents'
            }
            {
              name: 'AZURE_STORAGE_PROCESSED_DOCUMENTS_CONTAINER'
              value: 'processed-documents'
            }
            {
              name: 'CORS_ALLOWED_ORIGINS'
              value: corsAllowedOrigins
            }
          ]
        }
      ]
      scale: {
        minReplicas: minReplicas
        maxReplicas: maxReplicas
      }
    }
  }
}

output id string = containerApp.id
output name string = containerApp.name
output fqdn string = containerApp.properties.configuration.ingress.fqdn
output url string = 'https://${containerApp.properties.configuration.ingress.fqdn}'
