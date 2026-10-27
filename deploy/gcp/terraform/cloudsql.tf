# One Cloud SQL for PostgreSQL 16 instance for the pilot. It holds the
# control database (below) and, created later by the application's
# provisioning job, one zaza_t_<id> database per customer.
#
# Tenant databases are NOT Terraform resources: customers are created
# dynamically, and their databases follow the application lifecycle
# (provisioning, migration, suspension) recorded in the control plane.
#
# Database roles (zaza_app, zaza_control_app, zaza_migrator) are created with
# SQL in Phase 10E, their passwords generated straight into Secret Manager,
# so no password ever appears in Terraform variables or state.

resource "google_sql_database_instance" "main" {
  name                = var.sql_instance_name
  region              = var.region
  database_version    = "POSTGRES_16"
  deletion_protection = var.deletion_protection

  settings {
    edition                     = "ENTERPRISE"
    tier                        = var.sql_tier
    availability_type           = var.sql_availability_type
    disk_type                   = "PD_SSD"
    disk_size                   = var.sql_disk_size_gb
    disk_autoresize             = true
    disk_autoresize_limit       = var.sql_disk_autoresize_limit_gb
    deletion_protection_enabled = var.deletion_protection
    user_labels                 = local.labels

    # Only Cloud SQL connectors (Cloud Run's built-in connection, the Auth
    # Proxy) may connect: they authenticate the caller's IAM identity and
    # encrypt the connection. No authorized networks are configured.
    connector_enforcement = "REQUIRED"

    ip_configuration {
      ipv4_enabled = true
      ssl_mode     = "ENCRYPTED_ONLY"
    }

    backup_configuration {
      enabled                        = true
      point_in_time_recovery_enabled = true
      start_time                     = var.backup_start_time
      location                       = var.region
      transaction_log_retention_days = var.pitr_log_retention_days

      backup_retention_settings {
        retained_backups = var.backup_retained_count
        retention_unit   = "COUNT"
      }
    }

    maintenance_window {
      day          = 7  # Sunday
      hour         = 21 # UTC (03:30 IST Monday)
      update_track = "stable"
    }

    insights_config {
      query_insights_enabled  = true
      record_application_tags = false
      record_client_address   = false
    }

    database_flags {
      name  = "max_connections"
      value = tostring(var.sql_max_connections)
    }
  }

  depends_on = [google_project_service.required]
}

# The SaaS control-plane database (organizations, users, memberships, the
# tenant-database directory, control audit). Schema: `control-migrate` job.
resource "google_sql_database" "control" {
  name     = "zaza_control"
  instance = google_sql_database_instance.main.name

  # never drop customer-facing data from a Terraform change
  deletion_policy = "ABANDON"
}
