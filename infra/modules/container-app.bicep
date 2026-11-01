@description('Azure region for the Container App.')
param location string

@description('Container App name.')
param appName string

@description('Resource ID of the Container Apps Environment.')
param environmentId string

@description('Full public container image reference (e.g. ghcr.io/<user>/<image>:<tag>). No registry credentials — image must be public.')
param containerImage string

@description('User-assigned managed identity resource ID (Azure Blob access via DefaultAzureCredential).')
param uamiResourceId string

@description('User-assigned managed identity client ID (AZURE_CLIENT_ID for DefaultAzureCredential).')
param uamiClientId string

@description('Application Insights connection string (non-secret).')
param appInsightsConnectionString string

@description('Blob service endpoint URL.')
param storageBlobEndpoint string

@description('Environment name passed to the app as ENVIRONMENT (e.g. demo).')
param environmentName string

@secure()
@description('Supabase PostgreSQL connection URL (prefer session pooler for IPv4). IaC-owned ACA secret.')
param databaseUrl string

@description('PostgreSQL schema used by the application (search_path / Alembic).')
param postgresSchema string = 'knowledge_intelligence'

@secure()
@description('External LLM API key. Only used when enableLlmApiKey is true.')
param llmApiKey string = ''

@description('When true, declare ACA secret llm-api-key and env LLM_API_KEY. Do not use empty() on @secure() params — ARM cannot reliably test them.')
param enableLlmApiKey bool = false

@description('Container listen port.')
param containerPort int = 8000

@description('Minimum replicas.')
param minReplicas int = 0

@description('Maximum replicas.')
param maxReplicas int = 2

@description('Liveness probe path (MVP: single /health endpoint).')
param livenessPath string = '/health'

@description('Readiness probe path (MVP: same as liveness).')
param readinessPath string = '/health'

@description('Startup probe path (MVP: same as liveness).')
param startupPath string = '/health'

@description('Container CPU cores.')
param containerCpu string = '0.25'

@description('Container memory.')
param containerMemory string = '0.5Gi'

@description('CORS allowed origins (comma-separated).')
param corsAllowedOrigins string = 'http://localhost:5173'

@description('Resource tags.')
param tags object = {}

// ACA rejects empty secret values (ContainerAppSecretInvalid). Gate on non-secure bool, not empty(secure).
var databaseSecret = [
  {
    name: 'database-url'
    value: databaseUrl
  }
]

var llmSecret = enableLlmApiKey ? [
  {
    name: 'llm-api-key'
    value: llmApiKey
  }
] : []

var baseEnv = [
  {
    name: 'ENVIRONMENT'
    value: environmentName
  }
  {
    name: 'APPLICATIONINSIGHTS_CONNECTION_STRING'
    value: appInsightsConnectionString
  }
  {
    name: 'AZURE_CLIENT_ID'
    value: uamiClientId
  }
  {
    name: 'DATABASE_URL'
    secretRef: 'database-url'
  }
  {
    name: 'POSTGRES_SCHEMA'
    value: postgresSchema
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

var llmEnv = enableLlmApiKey ? [
  {
    name: 'LLM_API_KEY'
    secretRef: 'llm-api-key'
  }
] : []

// Option A — single secret owner: Bicep declares ALL app secrets that are in use.
// Public GHCR: no registries[] credentials (image must be pullable anonymously).
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
      secrets: concat(databaseSecret, llmSecret)
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
              type: 'Startup'
              failureThreshold: 30
              httpGet: {
                path: startupPath
                port: containerPort
              }
              initialDelaySeconds: 5
              periodSeconds: 10
              timeoutSeconds: 5
            }
            {
              type: 'Liveness'
              failureThreshold: 3
              httpGet: {
                path: livenessPath
                port: containerPort
              }
              initialDelaySeconds: 10
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
              initialDelaySeconds: 5
              periodSeconds: 10
              timeoutSeconds: 5
            }
          ]
          env: concat(baseEnv, llmEnv)
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
