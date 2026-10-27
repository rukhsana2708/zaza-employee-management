# ZaZa on Google Cloud (Phase 10D definition; Phase 10E-A remote state).
#
# The main infrastructure has NOT been applied. Phase 10E-A created only the
# state bucket below (private: public access prevention enforced, uniform
# bucket-level access, versioning on, asia-south1) and produced a reviewed
# plan. `terraform apply` runs deliberately in a later step (see README.md).
#
# Never commit *.tfstate files or plans (they can contain sensitive values);
# .gitignore excludes them. The bucket name is not a secret.

terraform {
  required_version = ">= 1.6.0"

  required_providers {
    google = {
      source  = "hashicorp/google"
      version = ">= 7.0.0, < 8.0.0" # validated with 7.46.2 (see .terraform.lock.hcl)
    }
  }

  # Credentials come from the operator's gcloud login at run time — never
  # from this file.
  backend "gcs" {
    bucket = "zaza-employee-monitor-tfstate-e33ac2a6"
    prefix = "zaza/production"
  }
}

provider "google" {
  project = var.project_id
  region  = var.region

  default_labels = local.labels
}
