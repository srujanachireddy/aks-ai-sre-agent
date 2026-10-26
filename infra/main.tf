# Azure infrastructure for the AI SRE agent platform.
#
#   AKS cluster ─── runs the shop, the observability stack, Argo CD and the agent
#   Azure OpenAI ── the model the agent reasons with (keyless: Entra ID only)
#   Agent identity ─ a managed identity the agent pod uses through workload identity
#
# Everything else (Prometheus, Argo CD apps, policies...) is installed by Argo CD from Git.

terraform {
  required_version = ">= 1.6"
  required_providers {
    azurerm = {
      source  = "hashicorp/azurerm"
      version = ">= 4.10, < 5.0"
    }
    random = {
      source  = "hashicorp/random"
      version = "~> 3.6"
    }
  }
  backend "azurerm" {} # filled in by scripts/up.sh
}

provider "azurerm" {
  features {
    resource_group {
      prevent_deletion_if_contains_resources = false
    }
    cognitive_account {
      purge_soft_delete_on_destroy = true # lab: lets you destroy and recreate with the same name
    }
  }
  subscription_id = var.subscription_id
}

data "azurerm_client_config" "current" {}

locals {
  name = "${var.prefix}-${var.environment}"
  tags = { project = "aks-ai-sre-agent", environment = var.environment, managed_by = "terraform" }
}

resource "azurerm_resource_group" "main" {
  name     = "rg-${local.name}"
  location = var.location
  tags     = local.tags
}

# ------------------------------------------------------------------ AKS

resource "azurerm_kubernetes_cluster" "main" {
  name                = "aks-${local.name}"
  resource_group_name = azurerm_resource_group.main.name
  location            = azurerm_resource_group.main.location
  dns_prefix          = local.name
  node_resource_group = "rg-${local.name}-nodes"
  sku_tier            = "Free"

  automatic_upgrade_channel = "patch"
  node_os_upgrade_channel   = "NodeImage"

  # Workload identity: pods prove who they are with a Kubernetes token, Entra ID trusts it.
  oidc_issuer_enabled       = true
  workload_identity_enabled = true

  # kubectl access through Entra ID + Azure RBAC; no shared admin certificate.
  local_account_disabled            = true
  role_based_access_control_enabled = true
  azure_active_directory_role_based_access_control {
    azure_rbac_enabled = true
    tenant_id          = data.azurerm_client_config.current.tenant_id
  }

  # The Kubernetes API only answers your Cloud Shell's IP.
  api_server_access_profile {
    authorized_ip_ranges = ["${var.operator_ip}/32"]
  }

  default_node_pool {
    name                        = "system"
    vm_size                     = var.node_size
    auto_scaling_enabled        = true
    min_count                   = 1
    max_count                   = var.max_nodes
    os_disk_size_gb             = 64
    max_pods                    = 110
    temporary_name_for_rotation = "systemtmp"
    tags                        = local.tags

    upgrade_settings {
      max_surge = "33%"
    }
  }

  identity {
    type = "SystemAssigned"
  }

  network_profile {
    network_plugin      = "azure"
    network_plugin_mode = "overlay" # pod IPs come from a private overlay range
    network_data_plane  = "cilium"  # eBPF networking + NetworkPolicy enforcement
    network_policy      = "cilium"
    pod_cidr            = "192.168.0.0/16"
    load_balancer_sku   = "standard"
  }

  tags = local.tags

  lifecycle {
    ignore_changes = [default_node_pool[0].node_count, kubernetes_version]
  }
}

# You can run kubectl.
resource "azurerm_role_assignment" "admin_aks" {
  scope                = azurerm_kubernetes_cluster.main.id
  role_definition_name = "Azure Kubernetes Service RBAC Cluster Admin"
  principal_id         = var.admin_object_id
}

# ------------------------------------------------------------------ Azure OpenAI

resource "random_string" "suffix" {
  length  = 5
  upper   = false
  special = false
}

resource "azurerm_cognitive_account" "openai" {
  name                  = "aoai-${local.name}-${random_string.suffix.result}"
  resource_group_name   = azurerm_resource_group.main.name
  location              = var.openai_location
  kind                  = "OpenAI"
  sku_name              = "S0"
  custom_subdomain_name = "aoai-${local.name}-${random_string.suffix.result}" # required for Entra ID auth
  local_auth_enabled    = false                                               # no API keys at all
  tags                  = local.tags
}

resource "azurerm_cognitive_deployment" "model" {
  name                 = var.openai_model
  cognitive_account_id = azurerm_cognitive_account.openai.id

  model {
    format  = "OpenAI"
    name    = var.openai_model
    version = var.openai_model_version
  }

  sku {
    name     = "GlobalStandard" # pay per token, no idle cost
    capacity = 20               # thousands of tokens per minute
  }
}

# ------------------------------------------------------------------ Agent identity

resource "azurerm_user_assigned_identity" "agent" {
  name                = "id-${local.name}-sre-agent"
  resource_group_name = azurerm_resource_group.main.name
  location            = azurerm_resource_group.main.location
  tags                = local.tags
}

# Trust: "the Kubernetes service account sre-agent/sre-agent in THIS cluster is this identity".
resource "azurerm_federated_identity_credential" "agent" {
  name                = "aks-sre-agent"
  resource_group_name = azurerm_resource_group.main.name
  parent_id           = azurerm_user_assigned_identity.agent.id
  issuer              = azurerm_kubernetes_cluster.main.oidc_issuer_url
  subject             = "system:serviceaccount:sre-agent:sre-agent"
  audience            = ["api://AzureADTokenExchange"]
}

# The agent may call the model, nothing else in Azure.
resource "azurerm_role_assignment" "agent_openai" {
  scope                = azurerm_cognitive_account.openai.id
  role_definition_name = "Cognitive Services OpenAI User"
  principal_id         = azurerm_user_assigned_identity.agent.principal_id
}

# You can call the model too, so you can run the evals from Cloud Shell without a key.
resource "azurerm_role_assignment" "admin_openai" {
  scope                = azurerm_cognitive_account.openai.id
  role_definition_name = "Cognitive Services OpenAI User"
  principal_id         = var.admin_object_id
}
