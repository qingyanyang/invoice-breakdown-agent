using '../main.bicep'

param location = 'australiasoutheast'
param staticWebAppLocation = 'eastasia'
param environment = 'demo'
param prefix = 'eki'

param tags = {
  project: 'enterprise-knowledge-intelligence'
  environment: 'demo'
  managedBy: 'bicep'
}

// Dual-path secrets (empty defaults are for CI file-eval only — do not deploy with empties).
// Local: export vars, then run infra/scripts/validate-deploy-secrets.sh before az deploy.
// CI:    infra-cd.yml validates, then --parameters overrides.
// Option A: Bicep owns postgres-password + llm-api-key. Do not CLI/portal-manage them.
// Gates: always require POSTGRES_ADMIN_PASSWORD; when deployContainerApp=true also require
//        POSTGRES_APP_PASSWORD + LLM_API_KEY (and a non-empty containerImage).
param postgresAdminLogin = 'pgadmin'
param postgresAdminPassword = readEnvironmentVariable('POSTGRES_ADMIN_PASSWORD', '')

param postgresAppLogin = 'eki_app'
param postgresAppPassword = readEnvironmentVariable('POSTGRES_APP_PASSWORD', '')

param llmApiKey = readEnvironmentVariable('LLM_API_KEY', '')

param postgresVersion = '16'
param postgresSkuName = 'Standard_B1ms'
param postgresSkuTier = 'Burstable'
param postgresStorageSizeGb = 32
param postgresBackupRetentionDays = 7
param postgresDatabaseName = 'knowledge_intelligence_db'
param postgresAzureExtensions = 'vector'

// WARNING (public demo only): allows any Azure IP to attempt Postgres auth.
// Mitigations: TLS (sslmode=require), strong password, no committed credentials.
// Future staging/production parameter files MUST set allowAzureServicesAccess = false
// and use IP allowlists or private networking instead.
param allowAzureServicesAccess = true

param postgresFirewallRules = []

// Phase 1: false — UAMI + AcrPull still created; no Container App until image exists.
// Phase 2: after first FastAPI image is in ACR, set true and set containerImage.
param deployContainerApp = false
param containerImage = ''

param containerCpu = '0.25'
param containerMemory = '0.5Gi'
param minReplicas = 0
param maxReplicas = 2

param corsAllowedOrigins = [
  'http://localhost:5173'
]

param staticWebAppSkuName = 'Free'
param acrSkuName = 'Basic'
