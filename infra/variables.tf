variable "subscription_id" {
  type = string
}

variable "admin_object_id" {
  description = "Your Entra object ID (az ad signed-in-user show --query id -o tsv)."
  type        = string
}

variable "operator_ip" {
  description = "Public IP allowed to reach the Kubernetes API (your Cloud Shell)."
  type        = string

  validation {
    condition     = can(regex("^(\\d{1,3}\\.){3}\\d{1,3}$", var.operator_ip))
    error_message = "operator_ip must be a plain IPv4 address."
  }
}

variable "prefix" {
  type    = string
  default = "sre"
}

variable "environment" {
  type    = string
  default = "lab"
}

variable "location" {
  type    = string
  default = "eastus2"
}

variable "openai_location" {
  description = "Region for Azure OpenAI (must offer the model below)."
  type        = string
  default     = "eastus2"
}

variable "openai_model" {
  description = "Model name. Check what your region offers: az cognitiveservices model list -l eastus2 -o table"
  type        = string
  default     = "gpt-4.1-mini"
}

variable "openai_model_version" {
  type    = string
  default = "2025-04-14"
}

variable "node_size" {
  description = "Standard_B4ms = 4 vCPU, 16 GiB, about $0.17/hour."
  type        = string
  default     = "Standard_B4ms"
}

variable "max_nodes" {
  type    = number
  default = 2
}
