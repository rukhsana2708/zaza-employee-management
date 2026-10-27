# ZaZa on Google Cloud (Phase 10D) - infrastructure DEFINITION ONLY.
#
# Nothing in this directory has been applied. Phase 10E runs `terraform plan`
# and `terraform apply` deliberately, after review (see README.md).
#
# State: Phase 10E creates a dedicated, versioned GCS bucket for the state and
# enables the backend below. Never commit *.tfstate files (they can contain
# sensitive values); .gitignore excludes them.

terraform {
  required_version = ">= 1.6.0"

  required_providers {
    google = {
      source  = "hashicorp/google"
      version = ">= 7.0.0, < 8.0.0" # validated with 7.46.2 (see .terraform.lock.hcl)
    }
  }

  # backend "gcs" {
  #   bucket = "<state bucket created in Phase 10E>"
  #   prefix = "zaza/production"
  # }
}

provider "google" {
  project = var.project_id
  region  = var.region

  default_labels = local.labels
}
