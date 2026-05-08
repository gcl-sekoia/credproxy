"""Tests for proxy/main.py — _load_startup envelope parser."""
import io
import sys

import pytest

import main


def feed(monkeypatch, text: str):
    monkeypatch.setattr(sys, "stdin", io.StringIO(text))


# ---- Happy paths ----

def test_minimal_envelope(monkeypatch):
    feed(monkeypatch, '{"auth_token": "abc", "secrets": {}}')
    token, secrets = main._load_startup()
    assert token == "abc"
    assert secrets == {}


def test_envelope_with_secrets(monkeypatch):
    feed(monkeypatch, '{"auth_token": "abc", "secrets": {"FOO": "bar", "BAZ": "qux"}}')
    token, secrets = main._load_startup()
    assert token == "abc"
    assert secrets == {"FOO": "bar", "BAZ": "qux"}


def test_envelope_secrets_omitted_means_empty(monkeypatch):
    feed(monkeypatch, '{"auth_token": "abc"}')
    token, secrets = main._load_startup()
    assert token == "abc"
    assert secrets == {}


def test_envelope_with_multiline_value(monkeypatch):
    feed(
        monkeypatch,
        '{"auth_token": "t", "secrets": {"PEM": "-----BEGIN-----\\nbody\\n-----END-----"}}',
    )
    _, secrets = main._load_startup()
    assert secrets == {"PEM": "-----BEGIN-----\nbody\n-----END-----"}


# ---- Failure paths ----

def test_empty_stdin_exits(monkeypatch):
    feed(monkeypatch, "")
    with pytest.raises(SystemExit, match="empty stdin"):
        main._load_startup()


def test_whitespace_only_exits(monkeypatch):
    feed(monkeypatch, "\n\n  \n")
    with pytest.raises(SystemExit, match="empty stdin"):
        main._load_startup()


def test_invalid_json_exits(monkeypatch):
    feed(monkeypatch, "not json")
    with pytest.raises(SystemExit, match="invalid JSON"):
        main._load_startup()


def test_non_object_root_exits(monkeypatch):
    feed(monkeypatch, "[1, 2, 3]")
    with pytest.raises(SystemExit, match="must be an object"):
        main._load_startup()


def test_missing_auth_token_exits(monkeypatch):
    feed(monkeypatch, '{"secrets": {}}')
    with pytest.raises(SystemExit, match="auth_token"):
        main._load_startup()


def test_empty_auth_token_exits(monkeypatch):
    feed(monkeypatch, '{"auth_token": "", "secrets": {}}')
    with pytest.raises(SystemExit, match="auth_token"):
        main._load_startup()


def test_non_string_auth_token_exits(monkeypatch):
    feed(monkeypatch, '{"auth_token": 42, "secrets": {}}')
    with pytest.raises(SystemExit, match="auth_token"):
        main._load_startup()


def test_secrets_not_object_exits(monkeypatch):
    feed(monkeypatch, '{"auth_token": "abc", "secrets": [1,2,3]}')
    with pytest.raises(SystemExit, match="`secrets` must be an object"):
        main._load_startup()


def test_non_string_secret_value_exits(monkeypatch):
    feed(monkeypatch, '{"auth_token": "abc", "secrets": {"FOO": 42}}')
    with pytest.raises(SystemExit, match="must be a string"):
        main._load_startup()


def test_null_secret_value_exits(monkeypatch):
    feed(monkeypatch, '{"auth_token": "abc", "secrets": {"FOO": null}}')
    with pytest.raises(SystemExit, match="must be a string"):
        main._load_startup()


def test_empty_secret_key_exits(monkeypatch):
    feed(monkeypatch, '{"auth_token": "abc", "secrets": {"": "x"}}')
    with pytest.raises(SystemExit, match="non-empty"):
        main._load_startup()
