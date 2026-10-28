# The Cloud Run web service and the explicit migration job, gated
# INDEPENDENTLY (Phase 10E-C):
#   deploy_migration_job = true  -> only the zaza-migrate job (no HTTP endpoint)
#   deploy_service       = true  -> the zaza-server web service (+ public
#                                   invoker only if allow_unauthenticated)
# The job can exist long before the web service is safe to expose.
#
# The web service NEVER migrates: its entrypoint only runs `serve`. Schema
# changes run as the separate job below, with the migrator identity.

# Project number for the service's deterministic run.app host name (read-only).
data "google_project" "this" {
  project_id = var.project_id
}

locals {
  # Host names the web service answers to (Host-header validation). Default:
  # https://<service>-<project number>.<region>.run.app
  public_host = var.public_host != "" ? var.public_host : "${var.service_name}-${data.google_project.this.number}.${var.region}.run.app"

  deploy_service = var.deploy_service ? 1 : 0
  deploy_job     = var.deploy_migration_job ? 1 : 0

  # Non-secret settings shared by the service and the job.
  common_env = {
    ZAZA_SERVER_BACKEND                            = "postgres"
    ZAZA_CLOUDSQL_INSTANCE_CONNECTION_NAME         = local.instance_connection_name
    ZAZA_CONTROL_CLOUDSQL_INSTANCE_CONNECTION_NAME = local.instance_connection_name
    ZAZA_CONTROL_DB_NAME                           = google_sql_database.control.name
  }

  service_env = merge(local.common_env, {
    ZAZA_DB_USER                  = var.db_app_user
    ZAZA_DB_PASSWORD_FILE         = "/secrets/db-app/password"
    ZAZA_CONTROL_DB_USER          = var.db_control_user
    ZAZA_CONTROL_DB_PASSWORD_FILE = "/secrets/db-control/password"
    ZAZA_DB_POOL_MIN              = "0"
    ZAZA_DB_POOL_MAX              = tostring(var.db_pool_max)
    ZAZA_TENANT_POOL_MAX          = tostring(var.tenant_pool_max)
    ZAZA_TENANT_POOL_CACHE_MAX    = tostring(var.tenant_pool_cache_max)
    ZAZA_TENANT_POOL_IDLE_SECONDS = "300"
    ZAZA_CLOUD_RUN_MAX_INSTANCES  = tostring(var.max_instances)
    ZAZA_DB_CONNECTION_BUDGET     = tostring(var.db_connection_budget)
    ZAZA_DASHBOARD_COOKIE_SECURE  = "true"
    ZAZA_HTTP_DEPLOYMENT_MODE     = "cloud-run"
    ZAZA_TENANCY_MODE             = "saas"
    ZAZA_PUBLIC_HOST              = local.public_host
  })

  job_env = merge(local.common_env, {
    ZAZA_DB_USER                  = var.db_migrator_user
    ZAZA_DB_PASSWORD_FILE         = "/secrets/db-migrator/password"
    ZAZA_CONTROL_DB_USER          = var.db_migrator_user
    ZAZA_CONTROL_DB_PASSWORD_FILE = "/secrets/db-migrator/password"
    ZAZA_DB_POOL_MIN              = "0"
    ZAZA_DB_POOL_MAX              = "1"
  })
}

resource "google_cloud_run_v2_service" "server" {
  count               = local.deploy_service
  name                = var.service_name
  location            = var.region
  ingress             = "INGRESS_TRAFFIC_ALL"
  deletion_protection = var.deletion_protection

  template {
    service_account                  = google_service_account.runtime.email
    max_instance_request_concurrency = var.request_concurrency
    timeout                          = "60s"

    scaling {
      min_instance_count = 0
      max_instance_count = var.max_instances # part of the DB connection budget
    }

    volumes {
      name = "cloudsql"
      cloud_sql_instance {
        instances = [google_sql_database_instance.main.connection_name]
      }
    }

    volumes {
      name = "db-app"
      secret {
        secret = google_secret_manager_secret.db["db_app"].secret_id
        items {
          version = "latest"
          path    = "password"
        }
      }
    }

    volumes {
      name = "db-control"
      secret {
        secret = google_secret_manager_secret.db["db_control"].secret_id
        items {
          version = "latest"
          path    = "password"
        }
      }
    }

    containers {
      image = var.container_image

      ports {
        container_port = 8080
      }

      dynamic "env" {
        for_each = { for k, v in local.service_env : k => v if v != "" }
        content {
          name  = env.key
          value = env.value
        }
      }

      volume_mounts {
        name       = "cloudsql"
        mount_path = "/cloudsql"
      }
      volume_mounts {
        name       = "db-app"
        mount_path = "/secrets/db-app"
      }
      volume_mounts {
        name       = "db-control"
        mount_path = "/secrets/db-control"
      }

      resources {
        limits = {
          cpu    = "1"
          memory = "512Mi"
        }
      }

      # Public health is minimal: no database, tenant or Cloud SQL details.
      startup_probe {
        http_get {
          path = "/api/v1/sync/health"
        }
        period_seconds    = 5
        failure_threshold = 12
      }

      liveness_probe {
        http_get {
          path = "/api/v1/sync/health"
        }
        period_seconds = 30
      }
    }
  }

  lifecycle {
    precondition {
      condition     = var.container_image != ""
      error_message = "Set container_image (by digest) before deploy_service = true."
    }
  }
}

resource "google_cloud_run_v2_service_iam_member" "public" {
  count    = var.deploy_service && var.allow_unauthenticated ? 1 : 0
  name     = google_cloud_run_v2_service.server[0].name
  location = var.region
  role     = "roles/run.invoker"
  member   = "allUsers"
}

# Explicit migration job: `gcloud run jobs execute zaza-migrate --args=...`
# e.g. control-migrate | tenant-migrate --all-ready | db-status.
# Default arguments only report status.
resource "google_cloud_run_v2_job" "migrate" {
  count               = local.deploy_job
  name                = "zaza-migrate"
  location            = var.region
  deletion_protection = var.deletion_protection

  template {
    task_count = 1

    template {
      service_account = google_service_account.migrator.email
      max_retries     = 0
      timeout         = "1800s"

      # Block order matches what the Cloud Run API returns (secret first), so
      # plans show no spurious reordering diff.
      volumes {
        name = "db-migrator"
        secret {
          secret = google_secret_manager_secret.db["db_migrator"].secret_id
          items {
            version = "latest"
            path    = "password"
          }
        }
      }

      volumes {
        name = "cloudsql"
        cloud_sql_instance {
          instances = [google_sql_database_instance.main.connection_name]
        }
      }

      containers {
        image   = var.container_image
        command = ["python", "-E", "-s", "-u", "-m", "deskmate.zaza_server"]
        args    = ["control-db-status"]

        dynamic "env" {
          for_each = local.job_env
          content {
            name  = env.key
            value = env.value
          }
        }

        volume_mounts {
          name       = "db-migrator"
          mount_path = "/secrets/db-migrator"
        }
        volume_mounts {
          name       = "cloudsql"
          mount_path = "/cloudsql"
        }

        resources {
          limits = {
            cpu    = "1"
            memory = "512Mi"
          }
        }
      }
    }
  }
  lifecycle {
    precondition {
      condition     = var.container_image != ""
      error_message = "Set container_image (by digest) before deploy_migration_job = true."
    }
  }
}
