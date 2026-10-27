# Project services, the image registry, service identities and secret
# CONTAINERS. Secret VALUES are never written by Terraform (README.md).

locals {
  labels = {
    app         = "zaza"
    environment = var.environment
    managed-by  = "terraform"
  }

  instance_connection_name = "${var.project_id}:${var.region}:${var.sql_instance_name}"

  # Secret Manager secrets (containers only). The service mounts each as a
  # file; the application reads ZAZA_*_PASSWORD_FILE.
  secrets = {
    db_app      = "zaza-db-app-password"         # var.db_app_user      (tenant runtime)
    db_control  = "zaza-db-control-app-password" # var.db_control_user  (control runtime)
    db_migrator = "zaza-db-migrator-password"    # var.db_migrator_user (migration job)
  }
}

resource "google_project_service" "required" {
  for_each = toset([
    "run.googleapis.com",
    "sqladmin.googleapis.com",
    "secretmanager.googleapis.com",
    "artifactregistry.googleapis.com",
    "iam.googleapis.com",
  ])

  service            = each.value
  disable_on_destroy = false
}

# ─── image registry ──────────────────────────────────────────────────────────

resource "google_artifact_registry_repository" "images" {
  location      = var.region
  repository_id = "zaza"
  format        = "DOCKER"
  description   = "ZaZa central server images (deployed by digest)"

  docker_config {
    immutable_tags = true
  }

  depends_on = [google_project_service.required]
}

# ─── service identities (no keys are ever created for them) ─────────────────

resource "google_service_account" "runtime" {
  account_id   = "zaza-run"
  display_name = "ZaZa Cloud Run web service"
  description  = "Runtime identity of the web service: Cloud SQL client and its own database secrets only."
}

resource "google_service_account" "migrator" {
  account_id   = "zaza-migrate"
  display_name = "ZaZa migration job"
  description  = "Runs control/tenant migrations and tenant provisioning as an explicit job."
}

resource "google_project_iam_member" "runtime_sql_client" {
  project = var.project_id
  role    = "roles/cloudsql.client"
  member  = "serviceAccount:${google_service_account.runtime.email}"
}

resource "google_project_iam_member" "migrator_sql_client" {
  project = var.project_id
  role    = "roles/cloudsql.client"
  member  = "serviceAccount:${google_service_account.migrator.email}"
}

# ─── secret containers (values are added out of band in Phase 10E) ─────────

resource "google_secret_manager_secret" "db" {
  for_each  = local.secrets
  secret_id = each.value

  replication {
    user_managed {
      replicas {
        location = var.region
      }
    }
  }

  depends_on = [google_project_service.required]
}

# Each identity may read only its own secrets.
resource "google_secret_manager_secret_iam_member" "runtime" {
  for_each  = toset(["db_app", "db_control"])
  secret_id = google_secret_manager_secret.db[each.value].id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.runtime.email}"
}

resource "google_secret_manager_secret_iam_member" "migrator" {
  secret_id = google_secret_manager_secret.db["db_migrator"].id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.migrator.email}"
}
