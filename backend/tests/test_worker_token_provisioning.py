"""Worker token auto-provisioning: resolves a shared, persistent secret from
the model-data volume so api and inference worker agree without manual config.

This is what fixes `GET /api/internal/active-model -> 503` when ZMK_WORKER_TOKEN
is not set in .env (e.g. running `docker compose --profile inference up`).
"""
from app.main import provision_worker_token


def test_provisions_shared_token_file(tmp_path):
    token_file = tmp_path / "models" / ".worker-token"
    token = provision_worker_token(token_file, env_token="")
    assert len(token) == 64  # secrets.token_hex(32)
    assert token_file.exists()
    assert token_file.read_text(encoding="utf-8").strip() == token
    # stable across calls (persistent, not regenerated)
    token2 = provision_worker_token(token_file, env_token="")
    assert token2 == token


def test_prefers_explicit_env_token(tmp_path):
    token_file = tmp_path / "models" / ".worker-token"
    token = provision_worker_token(token_file, env_token="operator-set-secret")
    assert token == "operator-set-secret"
    assert not token_file.exists()  # no file needed when env is provided
    # empty / whitespace env still provisions a secret file
    token2 = provision_worker_token(token_file, env_token="   ")
    assert len(token2) == 64 and token_file.exists()


def test_inference_worker_resolves_the_same_shared_token(tmp_path):
    # The api provisions the secret; the inference worker must read the SAME
    # value from the shared volume path.
    token_file = tmp_path / "models" / ".worker-token"
    shared = provision_worker_token(token_file, env_token="")

    # Emulate the worker's resolver (the file is read, not regenerated).
    assert token_file.is_file()
    worker_read = token_file.read_text(encoding="utf-8").strip()
    assert worker_read == shared
