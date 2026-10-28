# Inputs. NONE of these is a secret: database passwords are created directly
# in Secret Manager (README.md, Phase 10E) and never pass through Terraform.

variable "project_id" {
  description = "Google Cloud project ID."
  type        = string
  default     = "zaza-employee-monitor"
}

variable "region" {
  description = "Primary region for every regional resource."
  type        = string
  default     = "asia-south1"
}

variable "environment" {
  description = "Environment label (production, staging, ...)."
  type        = string
  default     = "production"
}

# ─── Cloud SQL ───────────────────────────────────────────────────────────────

variable "sql_instance_name" {
  description = "Cloud SQL instance ID (chosen in Phase 10E; deliberately no default)."
  type        = string

  validation {
    condition     = can(regex("^[a-z]([a-z0-9-]{0,61}[a-z0-9])?$", var.sql_instance_name))
    error_message = "Instance ID: lower-case letters, digits and hyphens, starting with a letter, at most 63 characters."
  }
}

variable "sql_tier" {
  description = "Machine tier. A dedicated-core tier (no shared-core) for production; 1 vCPU / 3.75 GB for the pilot."
  type        = string
  default     = "db-custom-1-3840"
}

variable "sql_availability_type" {
  description = "ZONAL for the pilot; REGIONAL (high availability) once paying customers depend on it."
  type        = string
  default     = "ZONAL"

  validation {
    condition     = contains(["ZONAL", "REGIONAL"], var.sql_availability_type)
    error_message = "ZONAL or REGIONAL."
  }
}

variable "sql_disk_size_gb" {
  description = "Initial SSD size (grows automatically up to sql_disk_autoresize_limit_gb)."
  type        = number
  default     = 20
}

variable "sql_disk_autoresize_limit_gb" {
  description = "Upper bound for automatic disk growth (0 = unlimited)."
  type        = number
  default     = 200
}

variable "sql_max_connections" {
  description = "PostgreSQL max_connections. The application's connection budget must stay well below it."
  type        = number
  default     = 100
}

variable "backup_start_time" {
  description = "Daily backup window start, UTC (20:30 UTC = 02:00 IST)."
  type        = string
  default     = "20:30"
}

variable "backup_retained_count" {
  description = "Number of automated daily backups to keep."
  type        = number
  default     = 14

  validation {
    condition     = var.backup_retained_count >= 7 && var.backup_retained_count <= 365
    error_message = "Keep 7..365 backups."
  }
}

variable "pitr_log_retention_days" {
  description = "Point-in-time recovery window in days (transaction log retention)."
  type        = number
  default     = 7

  validation {
    condition     = var.pitr_log_retention_days >= 1 && var.pitr_log_retention_days <= 7
    error_message = "1..7 days (Enterprise edition)."
  }
}

variable "deletion_protection" {
  description = "Protect the instance and service from deletion (Terraform and Google Cloud API level)."
  type        = bool
  default     = true
}

# ─── Cloud Run ───────────────────────────────────────────────────────────────

variable "deploy_migration_job" {
  description = "Create ONLY the zaza-migrate Cloud Run job (no HTTP endpoint). Needs container_image."
  type        = bool
  default     = false
}

variable "deploy_service" {
  description = "Create the zaza-server Cloud Run WEB service. Stays false until the Cloud Run proxy/origin/dashboard work is done."
  type        = bool
  default     = false
}

variable "service_name" {
  description = "Cloud Run service name."
  type        = string
  default     = "zaza-server"
}

variable "container_image" {
  description = "Image reference by digest, e.g. asia-south1-docker.pkg.dev/<project>/zaza/zaza-server@sha256:... (required when deploy_service or deploy_migration_job is true)."
  type        = string
  default     = ""

  validation {
    condition     = var.container_image == "" || can(regex("@sha256:[0-9a-f]{64}$", var.container_image))
    error_message = "Deploy images by digest (@sha256:...), never by a moving tag."
  }
}

variable "max_instances" {
  description = "Cloud Run maximum instances. Part of the database connection budget: every instance can open its own pools."
  type        = number
  default     = 3

  validation {
    condition     = var.max_instances >= 1 && var.max_instances <= 20
    error_message = "1..20 for now (raise together with the connection budget)."
  }
}

variable "request_concurrency" {
  description = "Concurrent requests per instance (small pools: requests wait for a connection)."
  type        = number
  default     = 20
}

variable "tenant_pool_max" {
  description = "ZAZA_TENANT_POOL_MAX: connections per tenant pool."
  type        = number
  default     = 2
}

variable "tenant_pool_cache_max" {
  description = "ZAZA_TENANT_POOL_CACHE_MAX: open tenant pools per instance."
  type        = number
  default     = 8
}

variable "db_pool_max" {
  description = "ZAZA_DB_POOL_MAX: the server's own (non-tenant) pool."
  type        = number
  default     = 2
}

variable "db_connection_budget" {
  description = "ZAZA_DB_CONNECTION_BUDGET: connections the web service may use in total (max_connections minus the reserve for admin, migrations and Cloud SQL)."
  type        = number
  default     = 70
}

variable "public_host" {
  description = "ZAZA_PUBLIC_HOST override (comma-separated host names). Empty: the service's deterministic run.app host."
  type        = string
  default     = ""
}

variable "allow_unauthenticated" {
  description = "Let the internet reach the service (the employee agents must). Every API route still authenticates itself."
  type        = bool
  default     = true
}

variable "db_app_user" {
  description = "Runtime role for tenant databases (no SUPERUSER/CREATEDB/CREATEROLE)."
  type        = string
  default     = "zaza_app"
}

variable "db_control_user" {
  description = "Runtime role for the control database."
  type        = string
  default     = "zaza_control_app"
}

variable "db_migrator_user" {
  description = "Migration/provisioning role (CREATEDB; used only by the migration job)."
  type        = string
  default     = "zaza_migrator"
}
