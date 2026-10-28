# Non-secret identifiers only.

output "instance_connection_name" {
  description = "Value for ZAZA_CLOUDSQL_INSTANCE_CONNECTION_NAME."
  value       = google_sql_database_instance.main.connection_name
}

output "control_database" {
  value = google_sql_database.control.name
}

output "image_repository" {
  description = "Push images here; deploy them by digest."
  value       = "${var.region}-docker.pkg.dev/${var.project_id}/${google_artifact_registry_repository.images.repository_id}"
}

output "runtime_service_account" {
  value = google_service_account.runtime.email
}

output "migrator_service_account" {
  value = google_service_account.migrator.email
}

output "password_secrets" {
  description = "Secret IDs that need a version (added out of band, Phase 10E)."
  value       = { for k, s in google_secret_manager_secret.db : k => s.secret_id }
}

output "service_uri" {
  value = var.deploy_service ? google_cloud_run_v2_service.server[0].uri : null
}

output "build_service_account" {
  value = google_service_account.build.email
}

output "build_source_bucket" {
  value = google_storage_bucket.build_source.name
}

output "migration_job" {
  value = var.deploy_migration_job ? google_cloud_run_v2_job.migrate[0].name : null
}
