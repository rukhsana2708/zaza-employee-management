# Public edge (Phase 10H-A, DEFINITION ONLY, gated OFF): global external
# Application Load Balancer -> Cloud Armor -> serverless NEG -> Cloud Run.
#
# deploy_edge = true requires deploy_service = true and edge_domain. With the
# edge on, the Cloud Run service accepts traffic only from the load balancer
# (ingress INTERNAL_LOAD_BALANCER) and its default run.app URL is disabled,
# so Cloud Armor cannot be bypassed. Not applied: review the plan first.

locals {
  deploy_edge = var.deploy_edge ? 1 : 0
}

resource "google_project_service" "compute" {
  count              = local.deploy_edge
  service            = "compute.googleapis.com"
  disable_on_destroy = false
}

resource "google_compute_global_address" "edge" {
  count      = local.deploy_edge
  name       = "${var.service_name}-edge-ip"
  depends_on = [google_project_service.compute]
}

resource "google_compute_managed_ssl_certificate" "edge" {
  count      = local.deploy_edge
  depends_on = [google_project_service.compute]
  name       = "${var.service_name}-edge-cert"

  managed {
    domains = [var.edge_domain]
  }
}

resource "google_compute_region_network_endpoint_group" "edge" {
  count                 = local.deploy_edge
  depends_on            = [google_project_service.compute]
  name                  = "${var.service_name}-neg"
  region                = var.region
  network_endpoint_type = "SERVERLESS"

  cloud_run {
    service = google_cloud_run_v2_service.server[0].name
  }
}

resource "google_compute_security_policy" "edge" {
  count      = local.deploy_edge
  depends_on = [google_project_service.compute]
  name       = "${var.service_name}-edge-policy"

  # Per-client-IP throttle; the API still authenticates every route itself.
  rule {
    action   = "throttle"
    priority = 1000
    match {
      versioned_expr = "SRC_IPS_V1"
      config {
        src_ip_ranges = ["*"]
      }
    }
    rate_limit_options {
      conform_action = "allow"
      exceed_action  = "deny(429)"
      enforce_on_key = "IP"
      rate_limit_threshold {
        count        = var.edge_rate_limit_per_minute
        interval_sec = 60
      }
    }
  }

  rule {
    action   = "allow"
    priority = 2147483647
    match {
      versioned_expr = "SRC_IPS_V1"
      config {
        src_ip_ranges = ["*"]
      }
    }
  }
}

resource "google_compute_backend_service" "edge" {
  count                 = local.deploy_edge
  depends_on            = [google_project_service.compute]
  name                  = "${var.service_name}-backend"
  protocol              = "HTTPS"
  load_balancing_scheme = "EXTERNAL_MANAGED"
  security_policy       = google_compute_security_policy.edge[0].id

  backend {
    group = google_compute_region_network_endpoint_group.edge[0].id
  }

  log_config {
    enable      = true
    sample_rate = 1.0
  }
}

resource "google_compute_url_map" "edge" {
  count           = local.deploy_edge
  depends_on      = [google_project_service.compute]
  name            = "${var.service_name}-urlmap"
  default_service = google_compute_backend_service.edge[0].id
}

resource "google_compute_target_https_proxy" "edge" {
  count            = local.deploy_edge
  depends_on       = [google_project_service.compute]
  name             = "${var.service_name}-https-proxy"
  url_map          = google_compute_url_map.edge[0].id
  ssl_certificates = [google_compute_managed_ssl_certificate.edge[0].id]
}

resource "google_compute_global_forwarding_rule" "https" {
  count                 = local.deploy_edge
  name                  = "${var.service_name}-https"
  ip_address            = google_compute_global_address.edge[0].address
  port_range            = "443"
  target                = google_compute_target_https_proxy.edge[0].id
  load_balancing_scheme = "EXTERNAL_MANAGED"
}

# Plain http only redirects to https.
resource "google_compute_url_map" "redirect" {
  count      = local.deploy_edge
  depends_on = [google_project_service.compute]
  name       = "${var.service_name}-http-redirect"

  default_url_redirect {
    https_redirect         = true
    strip_query            = false
    redirect_response_code = "MOVED_PERMANENTLY_DEFAULT"
  }
}

resource "google_compute_target_http_proxy" "redirect" {
  count      = local.deploy_edge
  depends_on = [google_project_service.compute]
  name       = "${var.service_name}-http-proxy"
  url_map    = google_compute_url_map.redirect[0].id
}

resource "google_compute_global_forwarding_rule" "http" {
  count                 = local.deploy_edge
  name                  = "${var.service_name}-http"
  ip_address            = google_compute_global_address.edge[0].address
  port_range            = "80"
  target                = google_compute_target_http_proxy.redirect[0].id
  load_balancing_scheme = "EXTERNAL_MANAGED"
}
