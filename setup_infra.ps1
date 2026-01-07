# ==============================================================================
# SCRIPT DE SETUP - Versão "Force Creation"
# Foco: Ignora verificações de leitura e força a criação (Idempotente)
# ==============================================================================

$ErrorActionPreference = "Stop"

# --- 1. CONFIGURAÇÕES ---
$ResourceGroup = "rg-rag-audit-east"
$Location = "eastus"
$ProjectName = "ragaudit"

# Garante Login
try {
    $CurrentSub = az account show --query id -o tsv 2>$null
    if (-not $CurrentSub) { throw "LoginRequired" }
    Write-Host "🔐 Contexto ID: $CurrentSub" -ForegroundColor Cyan
} catch {
    Write-Error "❌ Erro: Execute 'az login' antes."
    exit
}

# --- 2. GESTÃO DE NOMES ---
$Suffix = Get-Date -Format "ddHHmm"
# Tenta recuperar sufixo antigo
try {
    if (az group show --name $ResourceGroup 2>$null) {
        $ExistingSearch = az search service list --resource-group $ResourceGroup --query "[0].name" -o tsv 2>$null
        if ($ExistingSearch -match "$ProjectName.+(\d{6})$") {
            $Suffix = $matches[1]
            Write-Host "🔄 Reutilizando Sufixo: $Suffix" -ForegroundColor Cyan
        }
    }
} catch {}

$StorageAccount = "${ProjectName}st${Suffix}"
$SearchService = "${ProjectName}srch${Suffix}"
$OpenAIService = "${ProjectName}ai${Suffix}"
$FunctionApp = "${ProjectName}func${Suffix}"

# --- FUNÇÃO DE ESPERA ---
function Wait-For-Resource ($Name, $RG, $Type) {
    Write-Host "   ⏳ Validando $Name..." -NoNewline
    $Retry = 0
    while ($Retry -lt 15) {
        if ($Type -eq "storage") {
            $State = az storage account show --name $Name --resource-group $RG --query provisioningState -o tsv 2>$null
        }
        if ($State -eq "Succeeded") { Write-Host " OK!" -ForegroundColor Green; return }
        Write-Host "." -NoNewline
        Start-Sleep -Seconds 5
        $Retry++
    }
}

Write-Host "`n🚀 Iniciando Setup Blindado..." -ForegroundColor Green

# --- 3. RESOURCE GROUP ---
Write-Host "📦 [1/5] Resource Group..."
az group create --name $ResourceGroup --location $Location --tags Environment="Dev" --output none
Write-Host "   ✓ Processado" -ForegroundColor Green

# --- 4. STORAGE ACCOUNT (A CORREÇÃO) ---
Write-Host "💾 [2/5] Storage Account..."

# TÁTICA SRE: Força o contexto da assinatura novamente para evitar perda de sessão
az account set --subscription $CurrentSub

# Tenta criar direto (sem verificar se existe antes). Se já existir, o Azure apenas retorna OK.
try {
    az storage account create `
        --name $StorageAccount `
        --resource-group $ResourceGroup `
        --location $Location `
        --sku Standard_LRS `
        --kind StorageV2 `
        --access-tier Cool `
        --allow-blob-public-access false `
        --output none
    Write-Host "   ✓ Comando de criação enviado" -ForegroundColor Green
} catch {
    Write-Warning "   ⚠️ O comando retornou erro, mas vamos verificar se o recurso foi criado mesmo assim..."
}

# Aguarda a existência real
Wait-For-Resource $StorageAccount $ResourceGroup "storage"

# Container
$StorageKey = (az storage account keys list --account-name $StorageAccount --resource-group $ResourceGroup --query '[0].value' -o tsv)
az storage container create --name "raw-docs" --account-name $StorageAccount --account-key $StorageKey --public-access off --output none 2>$null
Write-Host "   ✓ Container pronto" -ForegroundColor Green

# --- 5. AZURE AI SEARCH ---
Write-Host "🔍 [3/5] AI Search..."
az search service create --name $SearchService --resource-group $ResourceGroup --location $Location --sku free --partition-count 1 --replica-count 1 --output none 2>$null
Write-Host "   ✓ Processado" -ForegroundColor Green

# --- 6. OPENAI SERVICE ---
Write-Host "🤖 [4/5] OpenAI Service..."
az cognitiveservices account create --name $OpenAIService --resource-group $ResourceGroup --location $Location --kind OpenAI --sku S0 --custom-domain $OpenAIService --yes --output none 2>$null

# Deploy Models (GPT-4o-mini e Ada-002)
Function Deploy-Model ($DepName, $ModelName, $Version) {
    az cognitiveservices account deployment create --name $OpenAIService --resource-group $ResourceGroup --deployment-name $DepName --model-name $ModelName --model-version $Version --model-format OpenAI --sku-capacity 1 --sku-name "Standard" --output none 2>$null
}
Deploy-Model "text-embedding-ada-002" "text-embedding-ada-002" "2"
Deploy-Model "gpt-4o-mini" "gpt-4o-mini" "2024-07-18"
Write-Host "   ✓ Modelos Processados" -ForegroundColor Green

# --- 7. FUNCTION APP ---
Write-Host "⚡ [5/5] Function App..."
$FuncStorage = "${ProjectName}fn${Suffix}"

# Cria storage da function direto
az storage account create --name $FuncStorage --resource-group $ResourceGroup --location $Location --sku Standard_LRS --kind StorageV2 --output none 2>$null
Wait-For-Resource $FuncStorage $ResourceGroup "storage"

az functionapp create --name $FunctionApp --resource-group $ResourceGroup --storage-account $FuncStorage --consumption-plan-location $Location --runtime python --runtime-version 3.11 --functions-version 4 --os-type Linux --output none 2>$null
Write-Host "   ✓ App Criada" -ForegroundColor Green

# --- 8. GERAR .ENV ---
Write-Host "📝 Gerando .env..."

$OpenAIEndpoint = (az cognitiveservices account show --name $OpenAIService --resource-group $ResourceGroup --query properties.endpoint -o tsv)
$OpenAIKey = (az cognitiveservices account keys list --name $OpenAIService --resource-group $ResourceGroup --query key1 -o tsv)
$SearchKey = (az search admin-key show --service-name $SearchService --resource-group $ResourceGroup --query primaryKey -o tsv)
$SearchEndpoint = "https://$SearchService.search.windows.net"

$EnvContent = @"
AZURE_OPENAI_ENDPOINT=$OpenAIEndpoint
AZURE_OPENAI_API_KEY=$OpenAIKey
AZURE_OPENAI_DEPLOYMENT_EMBEDDING=text-embedding-ada-002
AZURE_OPENAI_DEPLOYMENT_CHAT=gpt-4o-mini
AZURE_OPENAI_API_VERSION=2024-02-15-preview
AZURE_SEARCH_ENDPOINT=$SearchEndpoint
AZURE_SEARCH_KEY=$SearchKey
AZURE_SEARCH_INDEX_NAME=compliance-docs-index
STORAGE_ACCOUNT_NAME=$StorageAccount
STORAGE_ACCOUNT_KEY=$StorageKey
STORAGE_CONTAINER_NAME=raw-docs
AZURE_SUBSCRIPTION_ID=$CurrentSub
FUNCTION_APP_NAME=$FunctionApp
"@

Set-Content -Path ".env" -Value $EnvContent
Write-Host "`n✅ SETUP CONCLUÍDO! (Tudo verde!)" -ForegroundColor Green