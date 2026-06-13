"""Tests for proxy/config.py — load_resolved schema validation.

The proxy receives an already-resolved config dict (bindings wire format);
secret resolution happens client-side (bin/credproxy). config.load_resolved
validates the shape and produces a BindingCredentials.
"""
import pytest

import config


# ---- Failure paths (one test per _fail() branch in load_resolved) ----

def test_non_dict_input():
    with pytest.raises(config.ConfigError, match="missing top-level"):
        config.load_resolved("not a dict")


def test_missing_top_level_bindings():
    with pytest.raises(config.ConfigError, match="missing top-level"):
        config.load_resolved({"other": {}})


def test_bindings_not_array():
    with pytest.raises(config.ConfigError, match="`bindings` must be an array"):
        config.load_resolved({"bindings": {"not": "an-array"}})


def test_binding_entry_not_object():
    with pytest.raises(config.ConfigError, match=r"bindings\[0\] must be an object"):
        config.load_resolved({"bindings": ["wrong"]})


def test_name_missing():
    with pytest.raises(config.ConfigError, match=r"bindings\[0\].name must be a non-empty string"):
        config.load_resolved({"bindings": [
            {"hosts": ["api.github.com"], "header": "Authorization",
             "placeholder": "ph", "real": "r"}
        ]})


def test_name_empty():
    with pytest.raises(config.ConfigError, match=r"bindings\[0\].name must be a non-empty string"):
        config.load_resolved({"bindings": [
            {"name": "", "hosts": ["api.github.com"], "header": "Authorization",
             "placeholder": "ph", "real": "r"}
        ]})


def test_name_duplicate():
    entry = {"name": "dup", "hosts": ["api.github.com"], "header": "Authorization",
             "placeholder": "ph", "real": "r"}
    entry2 = {"name": "dup", "hosts": ["api.example.com"], "header": "X-Key",
               "placeholder": "ph2", "real": "r2"}
    with pytest.raises(config.ConfigError, match="duplicate binding name 'dup'"):
        config.load_resolved({"bindings": [entry, entry2]})


def test_hosts_missing():
    with pytest.raises(config.ConfigError, match=r"bindings\[0\].hosts must be a non-empty array"):
        config.load_resolved({"bindings": [
            {"name": "b", "header": "Authorization",
             "placeholder": "ph", "real": "r"}
        ]})


def test_hosts_empty_array():
    with pytest.raises(config.ConfigError, match=r"bindings\[0\].hosts must be a non-empty array"):
        config.load_resolved({"bindings": [
            {"name": "b", "hosts": [], "header": "Authorization",
             "placeholder": "ph", "real": "r"}
        ]})


def test_hosts_not_array():
    with pytest.raises(config.ConfigError, match=r"bindings\[0\].hosts must be a non-empty array"):
        config.load_resolved({"bindings": [
            {"name": "b", "hosts": "api.github.com", "header": "Authorization",
             "placeholder": "ph", "real": "r"}
        ]})


def test_header_missing():
    with pytest.raises(config.ConfigError, match=r"bindings\[0\].header must be a non-empty string"):
        config.load_resolved({"bindings": [
            {"name": "b", "hosts": ["api.github.com"],
             "placeholder": "ph", "real": "r"}
        ]})


def test_header_empty():
    with pytest.raises(config.ConfigError, match=r"bindings\[0\].header must be a non-empty string"):
        config.load_resolved({"bindings": [
            {"name": "b", "hosts": ["api.github.com"], "header": "",
             "placeholder": "ph", "real": "r"}
        ]})


@pytest.mark.parametrize("placeholder", [
    pytest.param("", id="empty"),
    pytest.param(None, id="missing"),
    pytest.param(42, id="non-string"),
])
def test_placeholder_invalid(placeholder):
    entry = {"name": "b", "hosts": ["api.github.com"], "header": "Authorization",
             "real": "x"}
    if placeholder is not None:
        entry["placeholder"] = placeholder
    with pytest.raises(config.ConfigError, match=r"bindings\[0\].placeholder must be a non-empty string"):
        config.load_resolved({"bindings": [entry]})


@pytest.mark.parametrize("real", [
    pytest.param("", id="empty"),
    pytest.param(None, id="missing"),
    pytest.param(42, id="non-string"),
])
def test_real_invalid(real):
    entry = {"name": "b", "hosts": ["api.github.com"], "header": "Authorization",
             "placeholder": "ph"}
    if real is not None:
        entry["real"] = real
    with pytest.raises(config.ConfigError, match=r"bindings\[0\].real must be a non-empty string"):
        config.load_resolved({"bindings": [entry]})


def test_unresolved_secret_reference_in_real_rejected():
    """Unresolved ${secret:NAME} in real -> ConfigError. Caller resolves client-side."""
    with pytest.raises(config.ConfigError, match=r"unresolved \$\{secret:GITHUB_PAT\}"):
        config.load_resolved({"bindings": [
            {"name": "b", "hosts": ["api.github.com"], "header": "Authorization",
             "placeholder": "ph", "real": "Bearer ${secret:GITHUB_PAT}"}
        ]})


def test_unresolved_secret_reference_in_placeholder_rejected():
    """Unresolved ${secret:NAME} in placeholder -> ConfigError."""
    with pytest.raises(config.ConfigError, match=r"unresolved \$\{secret:GITHUB_PH\}"):
        config.load_resolved({"bindings": [
            {"name": "b", "hosts": ["api.github.com"], "header": "Authorization",
             "placeholder": "${secret:GITHUB_PH}", "real": "real_val"}
        ]})


def test_env_empty_string_rejected():
    with pytest.raises(config.ConfigError, match=r"bindings\[0\].env must be a non-empty string or absent/null"):
        config.load_resolved({"bindings": [
            {"name": "b", "hosts": ["api.github.com"], "header": "Authorization",
             "placeholder": "ph", "real": "r", "env": ""}
        ]})


def test_host_header_uniqueness_violated():
    """Two bindings claiming the same (host, header) -> ConfigError."""
    b1 = {"name": "b1", "hosts": ["api.github.com"], "header": "Authorization",
          "placeholder": "ph1", "real": "r1"}
    b2 = {"name": "b2", "hosts": ["api.github.com"], "header": "Authorization",
          "placeholder": "ph2", "real": "r2"}
    with pytest.raises(config.ConfigError, match="both claim header 'Authorization' on host"):
        config.load_resolved({"bindings": [b1, b2]})


def test_validation_uses_source_label():
    with pytest.raises(config.ConfigError, match="POST /admin/config"):
        config.load_resolved({"not-bindings": {}}, source="POST /admin/config")


def test_default_source_label():
    with pytest.raises(config.ConfigError, match="<resolved>"):
        config.load_resolved({"not-bindings": {}})


# ---- Happy paths ----

def test_minimal_config():
    creds = config.load_resolved({"bindings": [
        {"name": "github-env", "hosts": ["api.github.com"],
         "header": "Authorization", "placeholder": "ph", "real": "real_value"}
    ]})
    assert creds.intercept_hosts() == {"api.github.com"}
    [sub] = creds.substitutions_for("api.github.com")
    assert (sub.header, sub.placeholder, sub.real) == ("Authorization", "ph", "real_value")


def test_env_optional_null():
    """env=null is accepted (treated as absent)."""
    creds = config.load_resolved({"bindings": [
        {"name": "b", "hosts": ["api.github.com"], "header": "Authorization",
         "placeholder": "ph", "real": "r", "env": None}
    ]})
    [ib] = creds.inward_bindings()
    assert ib.env is None


def test_env_optional_absent():
    """env key entirely absent -> InwardBinding.env is None."""
    creds = config.load_resolved({"bindings": [
        {"name": "b", "hosts": ["api.github.com"], "header": "Authorization",
         "placeholder": "ph", "real": "r"}
    ]})
    [ib] = creds.inward_bindings()
    assert ib.env is None


def test_env_present():
    creds = config.load_resolved({"bindings": [
        {"name": "b", "hosts": ["api.github.com"], "header": "Authorization",
         "placeholder": "ph", "real": "r", "env": "GITHUB_TOKEN"}
    ]})
    [ib] = creds.inward_bindings()
    assert ib.env == "GITHUB_TOKEN"


def test_multiple_hosts_in_one_binding():
    """A binding covering two hosts shows up in both host lookups."""
    creds = config.load_resolved({"bindings": [
        {"name": "b", "hosts": ["api.github.com", "uploads.github.com"],
         "header": "Authorization", "placeholder": "ph", "real": "r"}
    ]})
    assert creds.intercept_hosts() == {"api.github.com", "uploads.github.com"}
    assert len(creds.substitutions_for("api.github.com")) == 1
    assert len(creds.substitutions_for("uploads.github.com")) == 1


def test_multiple_bindings_different_hosts():
    creds = config.load_resolved({"bindings": [
        {"name": "gh", "hosts": ["api.github.com"], "header": "Authorization",
         "placeholder": "ph1", "real": "r1"},
        {"name": "ex", "hosts": ["api.example.com"], "header": "X-API-Key",
         "placeholder": "ph2", "real": "r2"},
    ]})
    assert creds.intercept_hosts() == {"api.github.com", "api.example.com"}
    [sub1] = creds.substitutions_for("api.github.com")
    assert sub1.placeholder == "ph1"
    [sub2] = creds.substitutions_for("api.example.com")
    assert sub2.placeholder == "ph2"


def test_same_host_different_headers_allowed():
    """Two bindings on the same host with different headers is valid."""
    creds = config.load_resolved({"bindings": [
        {"name": "b1", "hosts": ["api.github.com"], "header": "Authorization",
         "placeholder": "ph1", "real": "r1"},
        {"name": "b2", "hosts": ["api.github.com"], "header": "X-Extra-Token",
         "placeholder": "ph2", "real": "r2"},
    ]})
    subs = creds.substitutions_for("api.github.com")
    assert len(subs) == 2
    headers = {s.header for s in subs}
    assert headers == {"Authorization", "X-Extra-Token"}


def test_substitutions_for_unknown_host_returns_empty():
    creds = config.load_resolved({"bindings": [
        {"name": "b", "hosts": ["api.github.com"], "header": "Authorization",
         "placeholder": "ph", "real": "r"}
    ]})
    assert creds.substitutions_for("not-configured.com") == []


def test_empty_bindings_no_intercepts():
    """Empty bindings array is the legitimate startup state."""
    creds = config.load_resolved({"bindings": []})
    assert creds.intercept_hosts() == set()
    assert creds.substitutions_for("anything.example") == []
    assert creds.inward_bindings() == []


def test_binding_credentials_empty_default():
    """BindingCredentials({}) is the legitimate startup state when no
    config has been pushed yet."""
    creds = config.BindingCredentials({})
    assert creds.intercept_hosts() == set()
    assert creds.substitutions_for("anything.example") == []
    assert creds.inward_bindings() == []


def test_inward_bindings_excludes_real():
    """inward_bindings() must not expose the real credential value."""
    creds = config.load_resolved({"bindings": [
        {"name": "b", "hosts": ["api.github.com"], "header": "Authorization",
         "placeholder": "ph", "real": "super_secret_value", "env": "GH_TOKEN"}
    ]})
    [ib] = creds.inward_bindings()
    assert ib.name == "b"
    assert ib.placeholder == "ph"
    assert ib.env == "GH_TOKEN"
    assert ib.header == "Authorization"
    assert ib.hosts == ["api.github.com"]
    # Verify InwardBinding has no 'real' attribute
    assert not hasattr(ib, "real")
