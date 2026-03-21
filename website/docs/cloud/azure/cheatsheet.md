---
title: Azure CLI Cheat Sheet
sidebar_label: Cheat Sheet
sidebar_position: 5
---

# Azure CLI Cheat Sheet

Quick reference for 100+ essential Azure CLI (az) commands organized by category. PowerShell equivalents included for common commands.

## Authentication and Account

```bash
# Login to Azure
az login

# Login with specific tenant
az login --tenant <tenant-id>

# Login with service principal
az login --service-principal -u <app-id> -p <password> --tenant <tenant-id>

# List all subscriptions
az account list

# Set active subscription
az account set --subscription <subscription-id>

# Get current subscription
az account show

# List available regions
az account list-locations

# Logout
az logout
```

**PowerShell Equivalents**:
```powershell
Connect-AzAccount
Get-AzSubscription
Set-AzContext -SubscriptionId <subscription-id>
Get-AzContext
Disconnect-AzAccount
```

## Resource Groups

```bash
# Create resource group
az group create --name myResourceGroup --location eastus

# List resource groups
az group list

# Get details of a resource group
az group show --name myResourceGroup

# Delete resource group
az group delete --name myResourceGroup --yes

# List all resources in a resource group
az resource list --resource-group myResourceGroup

# List resources by type
az resource list --resource-group myResourceGroup --resource-type Microsoft.Compute/virtualMachines

# Get resource group location
az group show --name myResourceGroup --query location -o tsv
```

**PowerShell Equivalents**:
```powershell
New-AzResourceGroup -Name myResourceGroup -Location EastUS
Get-AzResourceGroup
Remove-AzResourceGroup -Name myResourceGroup
Get-AzResource -ResourceGroupName myResourceGroup
```

## Virtual Machines (VMs)

### Create and Manage

```bash
# Create VM with defaults
az vm create --name myVM --resource-group myRG --image UbuntuLTS

# Create VM with specific parameters
az vm create --name myVM --resource-group myRG --image UbuntuLTS \
  --size Standard_B2s --admin-username azureuser \
  --generate-ssh-keys --public-ip-sku Standard

# Create Windows VM
az vm create --name myWindowsVM --resource-group myRG --image Win2019Datacenter \
  --admin-username azureuser

# List VMs
az vm list --resource-group myRG

# Get VM details
az vm show --name myVM --resource-group myRG

# Start VM
az vm start --name myVM --resource-group myRG

# Stop VM (still billed)
az vm stop --name myVM --resource-group myRG

# Deallocate VM (stops billing)
az vm deallocate --name myVM --resource-group myRG

# Restart VM
az vm restart --name myVM --resource-group myRG

# Delete VM
az vm delete --name myVM --resource-group myRG --yes

# Get public IP of VM
az vm show --resource-group myRG --name myVM --show-details --query publicIps -o tsv

# Resize VM
az vm resize --resource-group myRG --name myVM --size Standard_D2s_v3

# List available VM sizes for a location
az vm list-sizes --location eastus

# Redeploy VM (move to new host)
az vm redeploy --resource-group myRG --name myVM

# Run command on VM
az vm run-command invoke --resource-group myRG --name myVM \
  --command-id RunShellScript --scripts "echo 'Hello World'"
```

**PowerShell Equivalents**:
```powershell
New-AzVM -ResourceGroupName myRG -Name myVM -Image UbuntuLTS
Get-AzVM -ResourceGroupName myRG
Start-AzVM -ResourceGroupName myRG -Name myVM
Stop-AzVM -ResourceGroupName myRG -Name myVM
Remove-AzVM -ResourceGroupName myRG -Name myVM
Restart-AzVM -ResourceGroupName myRG -Name myVM
```

### Disks

```bash
# List disks in resource group
az disk list --resource-group myRG

# Create managed disk
az disk create --name myDisk --resource-group myRG --size-gb 32

# Create disk from snapshot
az disk create --name myDiskFromSnapshot --resource-group myRG \
  --source <snapshot-id>

# Create snapshot of a disk
az snapshot create --name mySnapshot --resource-group myRG \
  --source <disk-id>

# Attach disk to VM
az vm disk attach --vm-name myVM --resource-group myRG --disk myDisk

# Detach disk from VM
az vm disk detach --vm-name myVM --resource-group myRG --disk myDisk

# Delete disk
az disk delete --name myDisk --resource-group myRG --yes

# Resize disk
az disk update --name myDisk --resource-group myRG --size-gb 64

# Grant access to disk (generate SAS URL)
az disk grant-access --name myDisk --resource-group myRG --duration-in-seconds 3600
```

## Storage

### Storage Accounts

```bash
# Create storage account
az storage account create --name mystorageaccount --resource-group myRG \
  --location eastus --sku Standard_LRS --kind StorageV2

# List storage accounts
az storage account list --resource-group myRG

# Get storage account details
az storage account show --name mystorageaccount --resource-group myRG

# Get storage account keys
az storage account keys list --name mystorageaccount --resource-group myRG

# Update storage account (e.g., change tier)
az storage account update --name mystorageaccount --resource-group myRG \
  --access-tier Hot

# Delete storage account
az storage account delete --name mystorageaccount --resource-group myRG --yes

# Enable soft delete for blobs
az storage account blob-service-properties update --account-name mystorageaccount \
  --enable-delete-retention true --delete-retention-days 7

# List storage account network rules
az storage account network-rule list --account-name mystorageaccount \
  --resource-group myRG
```

### Blob Storage

```bash
# Create container
az storage container create --account-name mystorageaccount --name mycontainer

# List containers
az storage container list --account-name mystorageaccount

# Upload blob
az storage blob upload --account-name mystorageaccount --container-name mycontainer \
  --name myblob.txt --file /path/to/file.txt

# Download blob
az storage blob download --account-name mystorageaccount --container-name mycontainer \
  --name myblob.txt --file /path/to/output.txt

# List blobs in container
az storage blob list --account-name mystorageaccount --container-name mycontainer

# Delete blob
az storage blob delete --account-name mystorageaccount --container-name mycontainer \
  --name myblob.txt

# Generate SAS token for blob
az storage blob generate-sas --account-name mystorageaccount \
  --container-name mycontainer --name myblob.txt --permissions r --expiry 2026-03-28

# Set blob access tier
az storage blob set-tier --account-name mystorageaccount --container-name mycontainer \
  --name myblob.txt --tier Cool

# Get blob properties
az storage blob show --account-name mystorageaccount --container-name mycontainer \
  --name myblob.txt
```

## Networking

### Virtual Networks

```bash
# Create virtual network
az network vnet create --name myVNet --resource-group myRG --address-prefix 10.0.0.0/16

# Create subnet
az network vnet subnet create --name mySubnet --resource-group myRG --vnet-name myVNet \
  --address-prefix 10.0.1.0/24

# List virtual networks
az network vnet list --resource-group myRG

# Get VNet details
az network vnet show --name myVNet --resource-group myRG

# List subnets in VNet
az network vnet subnet list --resource-group myRG --vnet-name myVNet

# Delete VNet
az network vnet delete --name myVNet --resource-group myRG

# Add service endpoint to subnet
az network vnet subnet update --resource-group myRG --vnet-name myVNet \
  --name mySubnet --service-endpoints Microsoft.Storage
```

**PowerShell Equivalents**:
```powershell
New-AzVirtualNetwork -Name myVNet -ResourceGroupName myRG -AddressPrefix 10.0.0.0/16
Add-AzVirtualNetworkSubnetConfig -Name mySubnet -AddressPrefix 10.0.1.0/24
Get-AzVirtualNetwork -Name myVNet -ResourceGroupName myRG
```

### Network Security Groups (NSGs)

```bash
# Create NSG
az network nsg create --name myNSG --resource-group myRG

# List NSGs
az network nsg list --resource-group myRG

# Add inbound rule (allow SSH)
az network nsg rule create --name AllowSSH --nsg-name myNSG --resource-group myRG \
  --priority 100 --direction Inbound --access Allow --protocol Tcp --destination-port-ranges 22 \
  --source-address-prefixes '*'

# Add inbound rule (allow HTTP)
az network nsg rule create --name AllowHTTP --nsg-name myNSG --resource-group myRG \
  --priority 110 --direction Inbound --access Allow --protocol Tcp --destination-port-ranges 80 \
  --source-address-prefixes '*'

# List NSG rules
az network nsg rule list --nsg-name myNSG --resource-group myRG

# Delete NSG rule
az network nsg rule delete --name AllowSSH --nsg-name myNSG --resource-group myRG

# Associate NSG with subnet
az network vnet subnet update --resource-group myRG --vnet-name myVNet \
  --name mySubnet --network-security-group myNSG

# Get NSG details
az network nsg show --name myNSG --resource-group myRG

# Delete NSG
az network nsg delete --name myNSG --resource-group myRG
```

### Public IP Addresses

```bash
# Create public IP
az network public-ip create --name myPublicIP --resource-group myRG

# List public IPs
az network public-ip list --resource-group myRG

# Get public IP details
az network public-ip show --name myPublicIP --resource-group myRG

# Delete public IP
az network public-ip delete --name myPublicIP --resource-group myRG

# Create public IP with DNS name
az network public-ip create --name myPublicIP --resource-group myRG \
  --dns-name mydnsname
```

### Load Balancers

```bash
# Create load balancer
az network lb create --name myLoadBalancer --resource-group myRG \
  --public-ip-address myPublicIP --frontend-ip-name myFrontend \
  --backend-pool-name myBackend

# Create backend address pool
az network lb address-pool create --lb-name myLoadBalancer --name myBackendPool \
  --resource-group myRG

# Create health probe
az network lb probe create --name myHealthProbe --lb-name myLoadBalancer \
  --resource-group myRG --protocol tcp --port 80 --path /

# Create load balancer rule
az network lb rule create --name myLoadBalancerRule --lb-name myLoadBalancer \
  --resource-group myRG --protocol tcp --frontend-port 80 --backend-port 80 \
  --frontend-ip-name myFrontend --backend-pool-name myBackendPool \
  --probe-name myHealthProbe

# List load balancers
az network lb list --resource-group myRG

# Delete load balancer
az network lb delete --name myLoadBalancer --resource-group myRG
```

## Web Apps and App Service

### App Service Plans

```bash
# Create App Service Plan
az appservice plan create --name myAppServicePlan --resource-group myRG \
  --sku B1 --is-linux

# List App Service Plans
az appservice plan list --resource-group myRG

# Update App Service Plan (scale up)
az appservice plan update --name myAppServicePlan --resource-group myRG --sku S1

# Delete App Service Plan
az appservice plan delete --name myAppServicePlan --resource-group myRG
```

### Web Apps

```bash
# Create web app
az webapp create --name myWebApp --resource-group myRG \
  --plan myAppServicePlan --runtime "NODE|18-lts"

# List web apps
az webapp list --resource-group myRG

# Get web app details
az webapp show --name myWebApp --resource-group myRG

# Start web app
az webapp start --name myWebApp --resource-group myRG

# Stop web app
az webapp stop --name myWebApp --resource-group myRG

# Delete web app
az webapp delete --name myWebApp --resource-group myRG

# Deploy code from Git repository
az webapp deployment source config-zip --name myWebApp --resource-group myRG \
  --src /path/to/code.zip

# Configure web app settings
az webapp config appsettings set --name myWebApp --resource-group myRG \
  --settings KEY1=value1 KEY2=value2

# List web app settings
az webapp config appsettings list --name myWebApp --resource-group myRG

# Enable HTTPS only
az webapp update --name myWebApp --resource-group myRG --https-only true

# Get web app publishing profile
az webapp deployment publish-profile --name myWebApp --resource-group myRG \
  --query publishUrl -o tsv
```

**PowerShell Equivalents**:
```powershell
New-AzAppServicePlan -Name myAppServicePlan -ResourceGroupName myRG -Tier B1
New-AzWebApp -Name myWebApp -ResourceGroupName myRG -AppServicePlanName myAppServicePlan
Get-AzWebApp -Name myWebApp -ResourceGroupName myRG
```

## Azure Kubernetes Service (AKS)

```bash
# Create AKS cluster
az aks create --name myAKSCluster --resource-group myRG --node-count 3 \
  --vm-set-type VirtualMachineScaleSets --load-balancer-sku standard

# Get AKS cluster credentials (configure kubectl)
az aks get-credentials --name myAKSCluster --resource-group myRG

# List AKS clusters
az aks list --resource-group myRG

# Get AKS cluster details
az aks show --name myAKSCluster --resource-group myRG

# Scale AKS cluster (add nodes)
az aks scale --name myAKSCluster --resource-group myRG --node-count 5

# Upgrade AKS cluster
az aks upgrade --name myAKSCluster --resource-group myRG \
  --kubernetes-version 1.28.0

# Delete AKS cluster
az aks delete --name myAKSCluster --resource-group myRG --yes

# Enable autoscaler on AKS
az aks update --name myAKSCluster --resource-group myRG \
  --enable-cluster-autoscaler --min-count 1 --max-count 5

# Get AKS cluster version
az aks show --name myAKSCluster --resource-group myRG --query kubernetesVersion -o tsv

# Add a nodepool
az aks nodepool add --name gpu --cluster-name myAKSCluster \
  --resource-group myRG --node-count 1 --node-vm-size Standard_NC6
```

## Azure Container Registry (ACR)

```bash
# Create container registry
az acr create --name myregistry --resource-group myRG --sku Basic

# List container registries
az acr list --resource-group myRG

# Get ACR details
az acr show --name myregistry --resource-group myRG

# Get ACR login server
az acr show --name myregistry --resource-group myRG --query loginServer -o tsv

# Login to ACR
az acr login --name myregistry

# Build image from Dockerfile
az acr build --registry myregistry --image myimage:latest /path/to/Dockerfile

# Push image to ACR
docker tag myimage:latest myregistry.azurecr.io/myimage:latest
docker push myregistry.azurecr.io/myimage:latest

# List repositories in ACR
az acr repository list --name myregistry

# List tags for image
az acr repository show-tags --name myregistry --repository myimage

# Delete image from ACR
az acr repository delete --name myregistry --image myimage:latest

# Delete container registry
az acr delete --name myregistry --resource-group myRG

# Enable admin user for ACR
az acr update --name myregistry --admin-enabled true

# Get admin credentials
az acr credential show --name myregistry --resource-group myRG
```

## Key Vault

```bash
# Create Key Vault
az keyvault create --name myKeyVault --resource-group myRG --location eastus

# List Key Vaults
az keyvault list --resource-group myRG

# Get Key Vault details
az keyvault show --name myKeyVault --resource-group myRG

# Set secret
az keyvault secret set --vault-name myKeyVault --name dbPassword \
  --value mySecurePassword

# Get secret
az keyvault secret show --vault-name myKeyVault --name dbPassword

# List secrets
az keyvault secret list --vault-name myKeyVault

# Delete secret
az keyvault secret delete --vault-name myKeyVault --name dbPassword

# Create key
az keyvault key create --vault-name myKeyVault --name myKey --protection software

# Encrypt data with key
az keyvault key encrypt --vault-name myKeyVault --key-name myKey \
  --algorithm RSA-OAEP --value "plaintext"

# Grant access (RBAC)
az role assignment create --assignee <user-id> --role "Key Vault Secrets Officer" \
  --scope /subscriptions/<sub-id>/resourcegroups/myRG/providers/Microsoft.KeyVault/vaults/myKeyVault

# Delete Key Vault
az keyvault delete --name myKeyVault --resource-group myRG
```

**PowerShell Equivalents**:
```powershell
New-AzKeyVault -Name myKeyVault -ResourceGroupName myRG -Location EastUS
Set-AzKeyVaultSecret -VaultName myKeyVault -Name dbPassword -SecretValue $(ConvertTo-SecureString "password" -AsPlainText)
Get-AzKeyVaultSecret -VaultName myKeyVault -Name dbPassword
```

## Azure Active Directory / Entra ID

```bash
# List users
az ad user list

# Create user
az ad user create --display-name "John Doe" --user-principal-name john@contoso.com \
  --password InitialPassword123!

# Get user details
az ad user show --id john@contoso.com

# Delete user
az ad user delete --id john@contoso.com

# List groups
az ad group list

# Create group
az ad group create --display-name "Developers" --mail-nickname developers

# Add user to group
az ad group member add --group "Developers" --member-id <user-id>

# Remove user from group
az ad group member remove --group "Developers" --member-id <user-id>

# List group members
az ad group member list --group "Developers"

# Create service principal for app
az ad sp create-for-rbac --name myApp --role Contributor \
  --scopes /subscriptions/<subscription-id>

# List role assignments
az role assignment list --resource-group myRG

# Assign role to user
az role assignment create --assignee <user-id> --role "Virtual Machine Contributor" \
  --scope /subscriptions/<subscription-id>/resourcegroups/myRG

# Remove role assignment
az role assignment delete --assignee <user-id> --role "Virtual Machine Contributor" \
  --scope /subscriptions/<subscription-id>/resourcegroups/myRG

# List available roles
az role definition list
```

## Monitoring and Diagnostics

```bash
# Create metric alert
az monitor metrics alert create --name myAlert --resource-group myRG \
  --scopes /subscriptions/<sub-id>/resourcegroups/myRG/providers/Microsoft.Compute/virtualMachines/myVM \
  --condition "avg Percentage CPU > 80" --window-size 5m --evaluation-frequency 1m

# List metric alerts
az monitor metrics alert list --resource-group myRG

# Create action group for alerts
az monitor action-group create --name myActionGroup --resource-group myRG

# Add email action to action group
az monitor action-group update --name myActionGroup --resource-group myRG \
  --add-action email admin admin@contoso.com

# Get diagnostic settings for resource
az monitor diagnostic-settings list --resource /subscriptions/<sub-id>/resourcegroups/myRG/providers/Microsoft.Compute/virtualMachines/myVM

# Create diagnostic setting
az monitor diagnostic-settings create --name myDiagSetting --resource myVM \
  --resource-group myRG --logs '[{"category": "Administrative", "enabled": true}]'

# Enable analytics in Log Analytics workspace
az monitor log-analytics workspace create --resource-group myRG --workspace-name myWorkspace
```

## SQL Database

```bash
# Create SQL server
az sql server create --name mysqlserver --resource-group myRG \
  --admin-user sqladmin --admin-password InitialPassword123!

# Create SQL database
az sql db create --server mysqlserver --name mydatabase --resource-group myRG \
  --sku Standard --capacity 10

# List SQL databases
az sql db list --server mysqlserver --resource-group myRG

# Get database details
az sql db show --server mysqlserver --name mydatabase --resource-group myRG

# Configure firewall rule (allow all Azure services)
az sql server firewall-rule create --server mysqlserver --resource-group myRG \
  --name AllowAllAzureServices --start-ip-address 0.0.0.0 --end-ip-address 0.0.0.0

# Configure firewall rule (allow specific IP)
az sql server firewall-rule create --server mysqlserver --resource-group myRG \
  --name AllowMyIP --start-ip-address 203.0.113.1 --end-ip-address 203.0.113.1

# Delete SQL database
az sql db delete --server mysqlserver --name mydatabase --resource-group myRG

# Delete SQL server
az sql server delete --name mysqlserver --resource-group myRG
```

## Cosmos DB

```bash
# Create Cosmos DB account
az cosmosdb create --name mycosmosdb --resource-group myRG \
  --locations regionName=eastus failoverPriority=0

# List Cosmos DB accounts
az cosmosdb list --resource-group myRG

# Create database
az cosmosdb sql database create --account-name mycosmosdb --name mydatabase \
  --resource-group myRG

# Create container
az cosmosdb sql container create --account-name mycosmosdb --database-name mydatabase \
  --name mycontainer --partition-key-path /partitionKey --resource-group myRG

# Get account details
az cosmosdb show --name mycosmosdb --resource-group myRG

# List connection strings
az cosmosdb keys list --name mycosmosdb --resource-group myRG

# Delete Cosmos DB account
az cosmosdb delete --name mycosmosdb --resource-group myRG
```

## Azure Functions

```bash
# Create function app
az functionapp create --name myFunctionApp --resource-group myRG \
  --storage-account mystorageaccount --runtime node --runtime-version 18

# List function apps
az functionapp list --resource-group myRG

# Get function app details
az functionapp show --name myFunctionApp --resource-group myRG

# Deploy function code
az functionapp deployment source config-zip --name myFunctionApp \
  --resource-group myRG --src /path/to/code.zip

# Get function app keys
az functionapp keys list --name myFunctionApp --resource-group myRG

# Delete function app
az functionapp delete --name myFunctionApp --resource-group myRG

# Create function
az functionapp function create --name myFunction --functionapp-name myFunctionApp \
  --resource-group myRG
```

## Key Takeaways

- Azure CLI provides scriptable command-line management of Azure resources
- Commands are organized by resource type (vm, storage, network, etc.)
- Use `--help` flag for command-specific help: `az vm create --help`
- Output formats: text (default), json, table, yaml, csv
- PowerShell offers similar capabilities with different syntax
- Combine commands with pipes and tools like `grep`, `jq`, `awk` for automation
- Use service principals for CI/CD pipeline authentication
- Batch operations with scripts for infrastructure automation
