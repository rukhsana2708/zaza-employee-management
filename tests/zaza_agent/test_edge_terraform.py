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
    assert "security_policy       = google_compute_security_policy.edge[0].id" in edge
    assert "https_redirect         = true" in edge
    assert "SERVERLESS" in edge


def test_example_tfvars_does_not_enable_the_edge():
    # terraform.tfvars is local and git-ignored: only the shipped example is checked.
    assert "deploy_edge = true" not in _read("terraform.tfvars.example")
