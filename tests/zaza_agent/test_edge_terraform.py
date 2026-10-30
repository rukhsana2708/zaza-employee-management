"""Phase 10H-A: the public edge definition stays gated off and bypass-proof.

No Terraform binary is needed: these are static checks of the .tf files."""

from __future__ import annotations

from pathlib import Path

TF = Path(__file__).resolve().parents[2] / "deploy" / "gcp" / "terraform"


def _read(name: str) -> str:
    return (TF / name).read_text(encoding="utf-8")


def test_edge_is_off_by_default_and_every_resource_is_gated():
    variables = _read("variables.tf")
    block = variables.split('variable "deploy_edge"')[1].split("\nvariable ")[0]
    assert "default     = false" in block
    edge = _read("edge.tf")
    resources = edge.count('\nresource "')
    assert resources and edge.count("count ") >= resources


def test_edge_requires_service_and_domain():
    block = _read("variables.tf").split('variable "deploy_edge"')[1].split("\nvariable ")[0]
    assert "var.deploy_service" in block and "var.edge_domain" in block


def test_cloud_run_is_only_reachable_through_the_edge_when_enabled():
    run = _read("cloud_run.tf")
    assert 'var.deploy_edge ? "INGRESS_TRAFFIC_INTERNAL_LOAD_BALANCER"' in run
    assert "default_uri_disabled = var.deploy_edge" in run


def test_edge_has_armor_policy_and_https_only_backend():
    edge = _read("edge.tf")
    assert "google_compute_security_policy" in edge
    assert "local.deploy_armor == 1 ? google_compute_security_policy.edge[0].id : null" in edge
    assert "https_redirect         = true" in edge
    assert "SERVERLESS" in edge


def test_example_tfvars_does_not_enable_the_edge():
    # terraform.tfvars is local and git-ignored: only the shipped example is checked.
    assert "deploy_edge = true" not in _read("terraform.tfvars.example")


def test_cloud_armor_is_optional_and_only_referenced_when_enabled():
    edge = _read("edge.tf")
    assert "deploy_armor = var.deploy_edge && var.enable_cloud_armor ? 1 : 0" in edge
    policy = edge.split('resource "google_compute_security_policy"')[1].split("resource \"", 1)[0]
    assert "count      = local.deploy_armor" in policy and "rate_limit_options" in policy
    # the only reference to the policy outside its own block is the guarded one
    outside = edge.replace(policy, "")
    assert outside.count("google_compute_security_policy.edge") == 1
    assert "local.deploy_armor == 1 ?" in outside
    variables = _read("variables.tf")
    assert 'variable "enable_cloud_armor"' in variables


# ─── sync rate limiting and HSTS (Terraform SOURCE only; nothing applied) ──


def test_the_load_balancer_injects_the_trusted_client_ip_header():
    edge = _read("edge.tf")
    backend = edge.split('resource "google_compute_backend_service" "edge"')[1].split("\nresource ")[0]
    assert 'custom_request_headers = ["X-Zaza-Client-IP: {client_ip_address}"]' in backend
    code = "\n".join(line.split("#")[0] for line in edge.splitlines())
    assert "X-Forwarded-For" not in code  # the client address never comes from a client-controlled header


def test_limiter_and_hsts_variables_default_to_off_and_are_validated():
    variables = _read("variables.tf")
    mode = variables.split('variable "sync_limit_mode"')[1].split("\nvariable ")[0]
    assert 'default     = "off"' in mode and '["off", "shadow", "enforce"]' in mode
    hsts = variables.split('variable "hsts_max_age_seconds"')[1].split("\nvariable ")[0]
    assert "default     = 0" in hsts and "63072000" in hsts


def test_cloud_run_receives_the_limiter_hsts_and_trusted_header_settings():
    run = _read("cloud_run.tf")
    assert "ZAZA_SYNC_LIMIT_MODE" in run and "var.sync_limit_mode" in run
    assert "ZAZA_HSTS_MAX_AGE_SECONDS" in run and "tostring(var.hsts_max_age_seconds)" in run
    assert 'var.deploy_edge ? "X-Zaza-Client-IP" : ""' in run  # trusted only when the load balancer exists
    code = "\n".join(line.split("#")[0] for line in run.splitlines())
    assert "includeSubDomains" not in code and "preload" not in code
    assert "Never includeSubDomains or preload" in _read("variables.tf")  # documented, never configurable


def test_example_tfvars_does_not_turn_the_limiter_or_hsts_on():
    example = _read("terraform.tfvars.example")
    assert "sync_limit_mode" not in example or 'sync_limit_mode = "off"' in example
    assert "hsts_max_age_seconds" not in example or "hsts_max_age_seconds = 0" in example


def test_the_limiter_code_never_reads_x_forwarded_for():
    root = Path(__file__).resolve().parents[2] / "deskmate" / "zaza_server"
    for path in ("ratelimit.py", "tenancy/saas_sync.py"):
        text = (root / path).read_text(encoding="utf-8").lower()
        assert "x-forwarded-for" not in text and "forwarded_for" not in text


def test_ip_limiters_default_to_shadow_in_terraform():
    variables = _read("variables.tf")
    ip = variables.split('variable "sync_limit_ip_mode"')[1].split("\nvariable ")[0]
    assert 'default     = "shadow"' in ip and '["off", "shadow", "enforce"]' in ip
    run = _read("cloud_run.tf")
    assert "ZAZA_SYNC_LIMIT_IP_MODE" in run and "var.sync_limit_ip_mode" in run
