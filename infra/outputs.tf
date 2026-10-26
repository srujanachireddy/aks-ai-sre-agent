output "resource_group" {
  value = azurerm_resource_group.main.name
}

output "aks_name" {
  value = azurerm_kubernetes_cluster.main.name
}

output "openai_endpoint" {
  value = azurerm_cognitive_account.openai.endpoint
}

output "openai_deployment" {
  value = azurerm_cognitive_deployment.model.name
}

output "agent_client_id" {
  value = azurerm_user_assigned_identity.agent.client_id
}

output "tenant_id" {
  value = data.azurerm_client_config.current.tenant_id
}
