# Production image builds (Phase 10E-C).
#
# Images are built by Cloud Build running as the DEDICATED identity zaza-build,
# never as the legacy default Cloud Build service account (whose project-wide
# roles/cloudbuild.builds.builder includes read/write/delete on every bucket in
# the project, the Terraform state bucket included).
#
# zaza-build can only:
#   - read build sources from the private build-source bucket below;
#   - push images to the Artifact Registry repository `zaza`;
#   - write build logs.
# It has no database secret, no Cloud SQL, no Terraform-state and no IAM
# administration access. No key is ever created for it.
#
# Build source = `git archive` of an exact, pushed commit, uploaded by the
# operator to the source bucket; see deploy/gcp/README.md.

resource "google_service_account" "build" {
  account_id   = "zaza-build"
  display_name = "ZaZa image build"
  description  = "Cloud Build identity for the ZaZa server image: reads build sources, pushes to Artifact Registry zaza, writes build logs."
}

resource "google_project_iam_member" "build_log_writer" {
  project = var.project_id
  role    = "roles/logging.logWriter"
  member  = "serviceAccount:${google_service_account.build.email}"
}

# Repository-scoped: push (and read) images in `zaza` only.
resource "google_artifact_registry_repository_iam_member" "build_writer" {
  location   = google_artifact_registry_repository.images.location
  repository = google_artifact_registry_repository.images.name
  role       = "roles/artifactregistry.writer"
  member     = "serviceAccount:${google_service_account.build.email}"
}

# Private bucket for build-source archives (exact git commits). Not the
# Terraform state bucket; nothing else is stored here.
resource "google_storage_bucket" "build_source" {
  name                        = "${var.project_id}-build-source"
  location                    = var.region
  storage_class               = "STANDARD"
  uniform_bucket_level_access = true
  public_access_prevention    = "enforced"
  force_destroy               = false

  lifecycle_rule {
    condition {
      age = 30
    }
    action {
      type = "Delete"
    }
  }

  depends_on = [google_project_service.required]
}

resource "google_storage_bucket_iam_member" "build_source_reader" {
  bucket = google_storage_bucket.build_source.name
  role   = "roles/storage.objectViewer"
  member = "serviceAccount:${google_service_account.build.email}"
}
