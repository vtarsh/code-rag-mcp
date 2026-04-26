"""Tests for scripts/runpod/{pod_lifecycle,cost_guard}.py.

All tests mock urllib / network so the suite stays offline.

Coverage:
- key-missing aborts (cost_guard + pod_lifecycle)
- bad key format aborts
- single-run-cap and daily-cap enforcement in cost_guard
- atexit handler stops every started pod, even when one stop fails or
  the surrounding code raises
- teardown is idempotent
- --dry-run mode: never POSTs, returns 0/2/3 codes
- start_pod requires --secure-cloud + valid GPU + cost-guard pass
- start_pod records pod ids for teardown
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from scripts.runpod import cost_guard, full_pipeline, pod_lifecycle

# ----- Fixtures --------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_started_pods():
    pod_lifecycle._started_pod_ids.clear()
    pod_lifecycle._teardown_running = False
    yield
    pod_lifecycle._started_pod_ids.clear()
    pod_lifecycle._teardown_running = False


@pytest.fixture
def env_with_key(monkeypatch):
    monkeypatch.setenv("RUNPOD_API_KEY", "rpa_test_xxx")
    yield


@pytest.fixture
def env_no_key(monkeypatch):
    monkeypatch.delenv("RUNPOD_API_KEY", raising=False)
    yield


# ----- key-missing -----------------------------------------------------------


def test_cost_guard_aborts_when_key_missing(env_no_key):
    with pytest.raises(cost_guard.CostGuardError, match="RUNPOD_API_KEY missing"):
        cost_guard.assert_can_spend(1.0)


def test_pod_lifecycle_aborts_when_key_missing(env_no_key):
    with pytest.raises(pod_lifecycle.PodLifecycleError, match="RUNPOD_API_KEY missing"):
        pod_lifecycle._read_api_key()


def test_cost_guard_aborts_on_bad_key_format(monkeypatch):
    monkeypatch.setenv("RUNPOD_API_KEY", "wrongprefix_xxx")
    with pytest.raises(cost_guard.CostGuardError, match="format unexpected"):
        cost_guard._read_api_key()


def test_pod_lifecycle_aborts_on_bad_key_format(monkeypatch):
    monkeypatch.setenv("RUNPOD_API_KEY", "wrongprefix_xxx")
    with pytest.raises(pod_lifecycle.PodLifecycleError, match="format unexpected"):
        pod_lifecycle._read_api_key()


# ----- cost-over-limit -------------------------------------------------------


def test_cost_guard_blocks_when_today_plus_estimated_exceeds_cap(env_with_key):
    # today=4.5, estimated=1.0, cap=5 -> 5.5 > 5 -> blocked
    with pytest.raises(cost_guard.CostGuardError, match="would exceed daily cap"):
        cost_guard.assert_can_spend(
            estimated_run_usd=1.0,
            daily_cap_usd=5.0,
            single_run_cap_usd=5.0,
            today_spend_fn=lambda: 4.5,
        )


def test_cost_guard_allows_when_under_cap(env_with_key):
    # today=2.0, estimated=1.5, cap=5 -> 3.5 <= 5 -> ok
    cost_guard.assert_can_spend(
        estimated_run_usd=1.5,
        daily_cap_usd=5.0,
        single_run_cap_usd=5.0,
        today_spend_fn=lambda: 2.0,
    )


def test_cost_guard_blocks_oversized_single_run(env_with_key):
    with pytest.raises(cost_guard.CostGuardError, match="single-run cap"):
        cost_guard.assert_can_spend(
            estimated_run_usd=10.0,
            daily_cap_usd=20.0,
            single_run_cap_usd=5.0,
            today_spend_fn=lambda: 0.0,
        )


def test_cost_guard_skips_today_fetch_on_oversized_run(env_with_key):
    """Single-run cap fires before today_spend_fn is consulted."""
    today_called = []

    def _spy() -> float:
        today_called.append(1)
        return 0.0

    with pytest.raises(cost_guard.CostGuardError, match="single-run cap"):
        cost_guard.assert_can_spend(
            estimated_run_usd=10.0,
            daily_cap_usd=20.0,
            single_run_cap_usd=5.0,
            today_spend_fn=_spy,
        )
    assert today_called == []


# ----- atexit / signal teardown ---------------------------------------------


def test_teardown_stops_all_started_pods(env_with_key):
    pod_lifecycle._started_pod_ids[:] = ["pod_a", "pod_b"]
    with patch.object(pod_lifecycle, "stop_pod") as mock_stop:
        pod_lifecycle._teardown()
    assert [c.args[0] for c in mock_stop.call_args_list] == ["pod_a", "pod_b"]


def test_teardown_continues_even_when_one_pod_stop_fails(env_with_key):
    pod_lifecycle._started_pod_ids[:] = ["pod_a", "pod_b", "pod_c"]
    with patch.object(pod_lifecycle, "stop_pod") as mock_stop:
        mock_stop.side_effect = [
            {"ok": True},
            pod_lifecycle.PodLifecycleError("network"),
            {"ok": True},
        ]
        pod_lifecycle._teardown()  # MUST NOT raise
    assert mock_stop.call_count == 3


def test_teardown_is_idempotent(env_with_key):
    pod_lifecycle._started_pod_ids[:] = ["pod_a"]
    with patch.object(pod_lifecycle, "stop_pod") as mock_stop:
        pod_lifecycle._teardown()
        pod_lifecycle._teardown()
    assert mock_stop.call_count == 1


def test_atexit_runs_teardown_even_on_exception(env_with_key):
    """Simulate (start_pod -> work raises -> atexit fires) lifecycle."""
    pod_lifecycle._started_pod_ids[:] = ["pod_x"]
    with patch.object(pod_lifecycle, "stop_pod") as mock_stop:
        try:
            raise RuntimeError("simulated training failure")
        except RuntimeError:
            pod_lifecycle._teardown()
    mock_stop.assert_called_once_with("pod_x")


# ----- --dry-run mode --------------------------------------------------------


def test_dry_run_succeeds_when_auth_ok(env_with_key):
    with patch.object(pod_lifecycle, "list_pods", return_value=[]):
        rc = pod_lifecycle.cmd_dry_run()
    assert rc == 0


def test_dry_run_returns_2_when_key_missing(env_no_key, capsys):
    rc = pod_lifecycle.cmd_dry_run()
    assert rc == 2
    assert "MISSING" in capsys.readouterr().out


def test_dry_run_returns_3_when_api_call_fails(env_with_key, capsys):
    with patch.object(
        pod_lifecycle,
        "list_pods",
        side_effect=pod_lifecycle.PodLifecycleError("HTTP 401"),
    ):
        rc = pod_lifecycle.cmd_dry_run()
    assert rc == 3
    assert "FAILED" in capsys.readouterr().out


def test_dry_run_does_not_call_pod_create(env_with_key):
    """Crucial invariant: --dry-run never POSTs /pods."""
    with patch.object(pod_lifecycle, "list_pods", return_value=[]), patch.object(pod_lifecycle, "_request") as mock_req:
        rc = pod_lifecycle.cmd_dry_run()
    assert rc == 0
    # _request was bypassed via list_pods mock so it shouldn't have been called
    assert mock_req.call_count == 0


# ----- start_pod safeguards --------------------------------------------------


def test_start_pod_rejects_unknown_gpu(env_with_key):
    with pytest.raises(pod_lifecycle.PodLifecycleError, match="Unknown --gpu"):
        pod_lifecycle.start_pod(
            gpu="rtx9999",
            secure_cloud=True,
            time_limit_min=60,
            spending_cap_usd=5.0,
        )


def test_start_pod_requires_secure_cloud(env_with_key):
    with pytest.raises(pod_lifecycle.PodLifecycleError, match="Secure Cloud is required"):
        pod_lifecycle.start_pod(
            gpu="rtx4090",
            secure_cloud=False,
            time_limit_min=60,
            spending_cap_usd=5.0,
        )


def test_start_pod_runs_cost_guard_before_creating(env_with_key):
    """If the cost guard raises, start_pod must NOT have called _request."""
    with (
        patch.object(
            pod_lifecycle,
            "assert_can_spend",
            side_effect=cost_guard.CostGuardError("over cap"),
        ),
        patch.object(pod_lifecycle, "_request") as mock_req,
        pytest.raises(cost_guard.CostGuardError),
    ):
        pod_lifecycle.start_pod(
            gpu="a100-80g",
            secure_cloud=True,
            time_limit_min=60,
            spending_cap_usd=5.0,
        )
    assert mock_req.call_count == 0


def test_start_pod_records_id_for_teardown(env_with_key):
    fake_pod = {"id": "pod_abc", "status": "STARTING"}
    with (
        patch.object(pod_lifecycle, "assert_can_spend", return_value=None),
        patch.object(pod_lifecycle, "_request", return_value=fake_pod),
    ):
        pod_lifecycle.start_pod(
            gpu="rtx4090",
            secure_cloud=True,
            time_limit_min=15,
            spending_cap_usd=2.0,
        )
    assert pod_lifecycle._started_pod_ids == ["pod_abc"]


# ----- redaction guard -------------------------------------------------------


def test_pod_print_redacts_env_secrets():
    pod = {
        "id": "abc",
        "env": {"HF_TOKEN": "hf_secret123", "RUNPOD_API_KEY": "rpa_xyz"},
    }
    safe = pod_lifecycle._redact_pod_for_print(pod)
    assert safe["env"] == {"HF_TOKEN": "***", "RUNPOD_API_KEY": "***"}
    # original untouched
    assert pod["env"]["HF_TOKEN"] == "hf_secret123"


# ----- B1: atexit split (cmd_start must not self-terminate) ------------------


def _make_start_args(**overrides):
    """Build an argparse.Namespace matching `cmd_start`'s expectations."""
    import argparse

    defaults = dict(
        gpu="rtx4090",
        secure_cloud=True,
        time_limit="60m",
        spending_cap=5.0,
        hold=False,
    )
    defaults.update(overrides)
    return argparse.Namespace(**defaults)


def test_start_pod_does_not_register_atexit_by_default(env_with_key):
    """B1: `--start` (no --hold) must NOT bind atexit — otherwise the pod is
    stopped the moment main() returns. SIGTERM/SIGINT still install."""
    with (
        patch.object(pod_lifecycle.atexit, "register") as mock_register,
        patch.object(pod_lifecycle.signal, "signal") as mock_signal,
        patch.object(pod_lifecycle, "start_pod", return_value={"id": "pod_x"}),
    ):
        rc = pod_lifecycle.cmd_start(_make_start_args(hold=False))
    assert rc == 0
    assert mock_register.call_count == 0, "cmd_start in default mode must NOT atexit.register — see B1."
    # SIGTERM + SIGINT handlers are still installed (2 calls).
    signals_bound = {c.args[0] for c in mock_signal.call_args_list}
    assert pod_lifecycle.signal.SIGTERM in signals_bound
    assert pod_lifecycle.signal.SIGINT in signals_bound


def test_start_pod_with_hold_registers_atexit(env_with_key):
    """B1: `--start --hold` opts back in to atexit teardown for Mac-driven
    workflows where the launcher is expected to stay alive."""
    with (
        patch.object(pod_lifecycle.atexit, "register") as mock_register,
        patch.object(pod_lifecycle.signal, "signal"),
        patch.object(pod_lifecycle, "start_pod", return_value={"id": "pod_y"}),
    ):
        rc = pod_lifecycle.cmd_start(_make_start_args(hold=True))
    assert rc == 0
    assert mock_register.call_count == 1
    assert mock_register.call_args.args[0] is pod_lifecycle._teardown


def test_install_signal_handlers_default_skips_atexit():
    """Direct unit test on install_signal_handlers()."""
    with patch.object(pod_lifecycle.atexit, "register") as mock_register, patch.object(pod_lifecycle.signal, "signal"):
        pod_lifecycle.install_signal_handlers()
    assert mock_register.call_count == 0


def test_install_signal_handlers_hold_registers_atexit():
    with patch.object(pod_lifecycle.atexit, "register") as mock_register, patch.object(pod_lifecycle.signal, "signal"):
        pod_lifecycle.install_signal_handlers(register_atexit=True)
    assert mock_register.call_count == 1


# ----- B6 + B7: POST body shape ---------------------------------------------


def test_start_pod_sends_ports_as_array(env_with_key):
    """B6: RunPod OpenAPI requires `ports` to be array<string>."""
    captured = {}

    def _fake_request(method, path, body=None):
        captured["method"] = method
        captured["path"] = path
        captured["body"] = body
        return {"id": "pod_z"}

    with (
        patch.object(pod_lifecycle, "assert_can_spend", return_value=None),
        patch.object(pod_lifecycle, "_request", side_effect=_fake_request),
    ):
        pod_lifecycle.start_pod(
            gpu="rtx4090",
            secure_cloud=True,
            time_limit_min=60,
            spending_cap_usd=5.0,
        )
    body = captured["body"]
    assert isinstance(body["ports"], list), f"ports must be list, got {type(body['ports']).__name__}"
    assert all(isinstance(p, str) for p in body["ports"])
    assert "22/tcp" in body["ports"]
    assert "8888/http" in body["ports"]


def test_start_pod_body_omits_idle_timeout(env_with_key):
    """B7 update 2026-04-25: RunPod REST PodCreateInput rejects unknown keys
    (HTTP 400 on `additionalProperties`). idleTimeoutInMin must NOT be in the
    body — safety net is dashboard `Auto-terminate after: 1h` + cost-guard cap.
    """
    captured = {}

    def _fake_request(method, path, body=None):
        captured["body"] = body
        return {"id": "pod_w"}

    with (
        patch.object(pod_lifecycle, "assert_can_spend", return_value=None),
        patch.object(pod_lifecycle, "_request", side_effect=_fake_request),
    ):
        pod_lifecycle.start_pod(
            gpu="rtx4090",
            secure_cloud=True,
            time_limit_min=60,
            spending_cap_usd=5.0,
        )
    body = captured["body"]
    assert "idleTimeoutInMin" not in body, (
        "B7 re-verification 2026-04-25: live RunPod 400'd on this key. Pod-side safety lives in dashboard + cost-guard."
    )


def test_start_pod_body_omits_termination_time(env_with_key):
    """B7 update 2026-04-25: terminationTime also rejected by RunPod REST."""
    captured = {}

    def _fake_request(method, path, body=None):
        captured["body"] = body
        return {"id": "pod_v"}

    with (
        patch.object(pod_lifecycle, "assert_can_spend", return_value=None),
        patch.object(pod_lifecycle, "_request", side_effect=_fake_request),
    ):
        pod_lifecycle.start_pod(
            gpu="rtx4090",
            secure_cloud=True,
            time_limit_min=60,
            spending_cap_usd=5.0,
        )
    body = captured["body"]
    assert "terminationTime" not in body, "B7 re-verification 2026-04-25: RunPod 400'd on this key too."


def test_start_pod_body_omits_minvcpu_minmemory(env_with_key):
    """B7 update 2026-04-25: minVcpuCount / minMemoryInGb also rejected."""
    captured = {}

    def _fake_request(method, path, body=None):
        captured["body"] = body
        return {"id": "pod_u"}

    with (
        patch.object(pod_lifecycle, "assert_can_spend", return_value=None),
        patch.object(pod_lifecycle, "_request", side_effect=_fake_request),
    ):
        pod_lifecycle.start_pod(
            gpu="rtx4090",
            secure_cloud=True,
            time_limit_min=30,
            spending_cap_usd=2.0,
        )
    body = captured["body"]
    assert "minVcpuCount" not in body
    assert "minMemoryInGb" not in body
    # B6 still present:
    assert isinstance(body["ports"], list)
    assert body["ports"] == ["22/tcp", "8888/http"]


# ----- Bug 3: TRAIN purpose requires persistent volume -----------------------


def test_start_pod_train_purpose_rejects_zero_volume(env_with_key):
    """Bug 3 (NEXT_SESSION_PROMPT.md §2): purpose=train + volume_gb<20 must
    refuse to create — last cycle 6 ephemeral pods wiped /workspace on stop."""
    with (
        patch.object(pod_lifecycle, "assert_can_spend", return_value=None),
        patch.object(pod_lifecycle, "_request") as mock_req,
        pytest.raises(
            pod_lifecycle.PodLifecycleError,
            match=r"TRAIN purpose requires --volume-gb >= 20",
        ),
    ):
        pod_lifecycle.start_pod(
            gpu="rtx4090",
            secure_cloud=True,
            time_limit_min=60,
            spending_cap_usd=5.0,
            purpose="train",
            volume_gb=0,
        )
    # ephemeral wipe danger means we must NOT have hit the create endpoint.
    assert mock_req.call_count == 0


def test_start_pod_train_purpose_rejects_below_threshold_volume(env_with_key):
    """Boundary: volume_gb=19 still rejected (threshold is 20)."""
    with (
        patch.object(pod_lifecycle, "assert_can_spend", return_value=None),
        patch.object(pod_lifecycle, "_request") as mock_req,
        pytest.raises(
            pod_lifecycle.PodLifecycleError,
            match=r"TRAIN purpose requires --volume-gb >= 20",
        ),
    ):
        pod_lifecycle.start_pod(
            gpu="rtx4090",
            secure_cloud=True,
            time_limit_min=60,
            spending_cap_usd=5.0,
            purpose="train",
            volume_gb=19,
        )
    assert mock_req.call_count == 0


def test_start_pod_train_purpose_accepts_20gb_volume(env_with_key):
    """Bug 3: purpose=train with volume_gb>=20 must proceed."""
    captured = {}

    def _fake_request(method, path, body=None):
        captured["body"] = body
        return {"id": "pod_train"}

    with (
        patch.object(pod_lifecycle, "assert_can_spend", return_value=None),
        patch.object(pod_lifecycle, "_request", side_effect=_fake_request),
    ):
        pod_lifecycle.start_pod(
            gpu="rtx4090",
            secure_cloud=True,
            time_limit_min=60,
            spending_cap_usd=5.0,
            purpose="train",
            volume_gb=20,
        )
    assert captured["body"]["volumeInGb"] == 20
    assert captured["body"]["volumeMountPath"] == "/workspace"


def test_start_pod_bench_purpose_allows_zero_volume(env_with_key):
    """Bug 3: only 'train' demands a volume — bench/smoke pods may stay
    ephemeral (no FT artifacts to persist)."""
    captured = {}

    def _fake_request(method, path, body=None):
        captured["body"] = body
        return {"id": "pod_bench"}

    with (
        patch.object(pod_lifecycle, "assert_can_spend", return_value=None),
        patch.object(pod_lifecycle, "_request", side_effect=_fake_request),
    ):
        pod_lifecycle.start_pod(
            gpu="rtx4090",
            secure_cloud=True,
            time_limit_min=60,
            spending_cap_usd=5.0,
            purpose="bench",
            volume_gb=0,
        )
    assert captured["body"]["volumeInGb"] == 0
    # No volumeMountPath when volume_gb=0 (existing behavior).
    assert "volumeMountPath" not in captured["body"]


def test_start_pod_smoke_purpose_allows_zero_volume(env_with_key):
    """Bug 3: smoke pods may also stay ephemeral."""
    with (
        patch.object(pod_lifecycle, "assert_can_spend", return_value=None),
        patch.object(pod_lifecycle, "_request", return_value={"id": "pod_s"}),
    ):
        pod_lifecycle.start_pod(
            gpu="rtx4090",
            secure_cloud=True,
            time_limit_min=60,
            spending_cap_usd=5.0,
            purpose="smoke",
            volume_gb=0,
        )


def test_main_start_train_zero_volume_exits_nonzero(env_with_key, capsys, tmp_path):
    """Bug 3 acceptance criterion (NEXT_SESSION_PROMPT.md §2):
    `pod_lifecycle --start --purpose=train --volume-gb=0` must exit non-zero
    with a clear error message. Run via main() so the argparse + cmd_start
    plumbing is exercised end-to-end."""
    # Provide a real-looking SSH key so the volume check is the *first* gate
    # that fails, not the SSH format/existence check.
    fake_key = tmp_path / "fake.pub"
    fake_key.write_text("ssh-ed25519 AAAA test@host\n")
    # Stub assert_can_spend so the cost guard doesn't try to read /history.
    with (
        patch.object(pod_lifecycle, "assert_can_spend", return_value=None),
        patch.object(pod_lifecycle, "_request") as mock_req,
    ):
        rc = pod_lifecycle.main(
            [
                "--start",
                "--purpose=train",
                "--secure-cloud",
                "--gpu=rtx4090",
                "--time-limit=60m",
                "--volume-gb=0",
                f"--ssh-public-key-file={fake_key}",
            ]
        )
    assert rc == 4
    err = capsys.readouterr().err
    assert "TRAIN purpose requires --volume-gb >= 20" in err
    # /pods POST must NOT have fired.
    assert mock_req.call_count == 0


def test_main_start_bench_zero_volume_exits_ok(env_with_key, tmp_path):
    """Bug 3 acceptance criterion: `--purpose=bench --volume-gb=0` must
    succeed (only train demands a volume)."""
    # Provide a real file so the SSH fail-closed check passes.
    fake_key = tmp_path / "fake_key.pub"
    fake_key.write_text("ssh-ed25519 AAAA test@host\n")
    with (
        patch.object(pod_lifecycle, "assert_can_spend", return_value=None),
        patch.object(pod_lifecycle, "_request", return_value={"id": "pod_bench"}),
    ):
        rc = pod_lifecycle.main(
            [
                "--start",
                "--purpose=bench",
                "--secure-cloud",
                "--gpu=rtx4090",
                "--time-limit=60m",
                "--volume-gb=0",
                f"--ssh-public-key-file={fake_key}",
            ]
        )
    assert rc == 0


# ----- Bug 4: SSH key default + fail-closed ----------------------------------


def test_main_start_missing_ssh_key_exits_nonzero(env_with_key, capsys, tmp_path):
    """Bug 4 acceptance criterion: `--ssh-public-key-file=<missing>` must
    fail-closed before any pod create. Last cycle agents picked random
    keys from disk; cross-session pod access broke for half the pods."""
    bogus = tmp_path / "does_not_exist.pub"
    assert not bogus.exists()
    with (
        patch.object(pod_lifecycle, "assert_can_spend", return_value=None),
        patch.object(pod_lifecycle, "_request") as mock_req,
    ):
        rc = pod_lifecycle.main(
            [
                "--start",
                "--purpose=bench",
                "--secure-cloud",
                "--gpu=rtx4090",
                "--time-limit=60m",
                f"--ssh-public-key-file={bogus}",
            ]
        )
    assert rc == 4
    err = capsys.readouterr().err
    assert "SSH public key not found" in err
    assert str(bogus) in err
    assert mock_req.call_count == 0


def test_main_start_default_ssh_key_resolves_to_runpod_key_go(env_with_key, monkeypatch, tmp_path):
    """Bug 4 acceptance criterion: spawning a pod without
    `--ssh-public-key-file` should auto-resolve to
    `~/.runpod/ssh/RunPod-Key-Go.pub` when present."""
    # Stage a fake home with the canonical key path populated.
    fake_home = tmp_path / "home"
    runpod_key = fake_home / ".runpod" / "ssh" / "RunPod-Key-Go.pub"
    runpod_key.parent.mkdir(parents=True)
    runpod_key.write_text("ssh-ed25519 AAAA RunPod-Key-Go\n")
    monkeypatch.setattr(pod_lifecycle.Path, "home", classmethod(lambda cls: fake_home))

    captured = {}

    def _fake_cmd_start(args):
        captured["ssh_public_key_file"] = args.ssh_public_key_file
        captured["purpose"] = args.purpose
        return 0

    with patch.object(pod_lifecycle, "cmd_start", side_effect=_fake_cmd_start):
        rc = pod_lifecycle.main(
            [
                "--start",
                "--purpose=train",
                "--secure-cloud",
                "--gpu=rtx4090",
                "--time-limit=60m",
                "--volume-gb=20",
            ]
        )
    assert rc == 0
    assert captured["ssh_public_key_file"] == str(runpod_key)
    assert captured["purpose"] == "train"


def test_main_start_requires_purpose(env_with_key, capsys, tmp_path):
    """Bug 3 corollary: --purpose must be supplied with --start, otherwise
    the volume-gate cannot fire and we'd silently allow an ephemeral train
    pod again."""
    fake_key = tmp_path / "key.pub"
    fake_key.write_text("ssh-ed25519 AAAA test\n")
    rc = pod_lifecycle.main(
        [
            "--start",
            "--secure-cloud",
            "--gpu=rtx4090",
            "--time-limit=60m",
            f"--ssh-public-key-file={fake_key}",
        ]
    )
    assert rc == 4
    err = capsys.readouterr().err
    assert "--purpose=(train|bench|smoke) is required" in err


# ============================================================================
# Bug 5: full_pipeline.py — single-candidate end-to-end runner
# ============================================================================
#
# Contract under test (per NEXT_SESSION_PROMPT.md §2 Bug 5):
#   - run_pipeline() owns ONE pod from spawn to stop in a linear flow;
#   - pod is ALWAYS stopped (try/finally) before the function returns;
#   - function does NOT return until the bench JSON sentinel exists on Mac
#     OR an explicit non-zero exit code is set;
#   - on smoke failure: pod stops, no train attempted, exit=1;
#   - on train failure: pod stops, exit=2;
#   - on bench failure: pod stops, exit=3 (HF push of the train artifact may
#     have already happened so the model survives);
#   - if the pod is still RUNNING after stop_pod: exit=4 (orphan).


def _ok_proc(stdout: str = "", stderr: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(
        args=[],
        returncode=0,
        stdout=stdout,
        stderr=stderr,
    )


def _fail_proc(stderr: str = "boom") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(
        args=[],
        returncode=42,
        stdout="",
        stderr=stderr,
    )


@pytest.fixture
def fake_pod_dict():
    """A redacted pod dict matching pod_lifecycle._redact_pod_for_print()."""
    return {
        "pod_id": "pod_xyz",
        "id": "pod_xyz",
        "ssh_host": "1.2.3.4",
        "ssh_port": 12345,
        "env": {"PUBLIC_KEY": "***"},
    }


@pytest.fixture
def smoke_inputs(tmp_path):
    """Minimal valid inputs for run_pipeline()."""
    train = tmp_path / "train.jsonl"
    train.write_text('{"query":"q","positive":"p"}\n')
    eval_ = tmp_path / "eval.jsonl"
    eval_.write_text('{"query":"q","expected":[]}\n')
    bench_dir = tmp_path / "bench_out"
    bench_dir.mkdir()
    return SimpleNamespace(
        train=train,
        eval=eval_,
        bench_json=bench_dir / "tag_bench.json",
        ssh_key=tmp_path / "fake_id",
        ssh_pub=tmp_path / "fake_id.pub",
    )


# --- happy path -------------------------------------------------------------


def test_full_pipeline_happy_path_returns_zero_with_bench_json(
    smoke_inputs,
    fake_pod_dict,
    monkeypatch,
):
    """Spawn → provision → smoke → train → HF push → bench → scp_back → stop.

    Bench JSON is materialized by the scp_back mock so the poll-loop
    guarantee is satisfied immediately (file exists when poll starts).
    """
    spawn_calls: list[dict] = []

    def _spawn(**kwargs):
        spawn_calls.append(kwargs)
        return fake_pod_dict

    ssh_calls: list[str] = []

    def _ssh(*, host, port, cmd, **_):
        ssh_calls.append(cmd)
        return _ok_proc()

    def _scp_back(*, host, port, remote_path, local_path, **_):
        # Materialize the sentinel so the poll loop sees it on first check.
        Path(local_path).write_text('{"model_key":"tag-smoke"}')
        return _ok_proc()

    def _scp_to(**_):
        return _ok_proc()

    def _tar_overlay(**_):
        return _ok_proc()

    monkeypatch.setattr(full_pipeline.pod_lifecycle, "stop_pod", lambda pod_id: {"ok": True})
    monkeypatch.setattr(full_pipeline.pod_lifecycle, "get_pod", lambda pod_id: {"desiredStatus": "EXITED"})

    result = full_pipeline.run_pipeline(
        candidate_tag="tag",
        kind="docs",
        base_model="nomic-ai/nomic-embed-text-v1.5",
        train_data=smoke_inputs.train,
        eval_data=smoke_inputs.eval,
        gpu="4090",
        max_steps=100,
        hf_repo="Tarshevskiy/tag",
        bench_json_path=smoke_inputs.bench_json,
        spawn_fn=_spawn,
        ssh_fn=_ssh,
        scp_back_fn=_scp_back,
        scp_to_fn=_scp_to,
        tar_overlay_fn=_tar_overlay,
        sleep_fn=lambda _s: None,
    )

    assert result.exit_code == full_pipeline.EXIT_OK
    assert smoke_inputs.bench_json.exists(), "Bug 5 contract: bench JSON sentinel must exist on Mac on success."
    assert result.pod_stopped is True
    assert result.smoke_passed is True
    assert result.train_pushed_to_hf is True
    # Spawn must have requested a TRAIN pod with volume_gb >= 20 (Bug 3).
    assert spawn_calls[0]["volume_gb"] >= 20
    # SSH commands must include provision + smoke + train + bench (in order).
    setup_idx = next(i for i, c in enumerate(ssh_calls) if "setup_env.sh" in c)
    smoke_idx = next(i for i, c in enumerate(ssh_calls) if "FT did NOT change weights" in c)
    train_idx = next(
        i for i, c in enumerate(ssh_calls) if "train_docs_embedder.py" in c and "--steps=100" in c and "_full" in c
    )
    bench_idx = next(i for i, c in enumerate(ssh_calls) if "benchmark_doc_intent.py" in c and "_bench.json" in c)
    assert setup_idx < smoke_idx < train_idx < bench_idx


# --- pod always stopped ------------------------------------------------------


def test_full_pipeline_stops_pod_in_finally_even_on_unexpected_exception(
    smoke_inputs,
    fake_pod_dict,
    monkeypatch,
):
    """Bug 5 invariant: pod_lifecycle.stop_pod must be called before
    run_pipeline returns, even if an unhandled exception propagates."""
    stop_called: list[str] = []

    def _spawn(**_):
        return fake_pod_dict

    def _ssh(**_):
        # Simulate an unexpected runtime error mid-pipeline.
        raise RuntimeError("network blew up")

    monkeypatch.setattr(
        full_pipeline.pod_lifecycle,
        "stop_pod",
        lambda pod_id: stop_called.append(pod_id) or {"ok": True},
    )
    monkeypatch.setattr(full_pipeline.pod_lifecycle, "get_pod", lambda pod_id: {"desiredStatus": "EXITED"})

    with pytest.raises(RuntimeError, match="network blew up"):
        full_pipeline.run_pipeline(
            candidate_tag="tag",
            kind="docs",
            base_model="base",
            train_data=smoke_inputs.train,
            eval_data=smoke_inputs.eval,
            gpu="4090",
            max_steps=100,
            hf_repo="Tarshevskiy/tag",
            bench_json_path=smoke_inputs.bench_json,
            spawn_fn=_spawn,
            ssh_fn=_ssh,
            scp_back_fn=lambda **_: _ok_proc(),
            scp_to_fn=lambda **_: _ok_proc(),
            tar_overlay_fn=lambda **_: _ok_proc(),
            sleep_fn=lambda _s: None,
        )

    assert stop_called == ["pod_xyz"], "Bug 5: stop_pod MUST run in finally even when ssh_fn raises."


# --- smoke failure path ------------------------------------------------------


def test_full_pipeline_smoke_failure_skips_train_and_returns_one(
    smoke_inputs,
    fake_pod_dict,
    monkeypatch,
):
    """Smoke failure → exit=1, no train SSH command issued, pod stopped.

    Provision (setup_env + sanity-check, 2 ssh calls) succeeds; the smoke
    one-liner is the next ssh call and we fail it. After failure no further
    ssh should fire (no train, no bench, no hf push).
    """
    ssh_calls: list[str] = []

    def _spawn(**_):
        return fake_pod_dict

    def _ssh(*, host, port, cmd, **_):
        ssh_calls.append(cmd)
        # Provision (setup_env + sanity import) must pass; smoke fails.
        if "FT did NOT change weights" in cmd:
            return _fail_proc(stderr="cosine 1.0 — FT didn't move")
        return _ok_proc()

    monkeypatch.setattr(full_pipeline.pod_lifecycle, "stop_pod", lambda pod_id: {"ok": True})
    monkeypatch.setattr(full_pipeline.pod_lifecycle, "get_pod", lambda pod_id: {"desiredStatus": "EXITED"})

    result = full_pipeline.run_pipeline(
        candidate_tag="tag",
        kind="docs",
        base_model="base",
        train_data=smoke_inputs.train,
        eval_data=smoke_inputs.eval,
        gpu="4090",
        max_steps=100,
        hf_repo="Tarshevskiy/tag",
        bench_json_path=smoke_inputs.bench_json,
        spawn_fn=_spawn,
        ssh_fn=_ssh,
        scp_back_fn=lambda **_: _ok_proc(),
        scp_to_fn=lambda **_: _ok_proc(),
        tar_overlay_fn=lambda **_: _ok_proc(),
        sleep_fn=lambda _s: None,
    )

    assert result.exit_code == full_pipeline.EXIT_SMOKE_FAILED
    assert result.smoke_passed is False
    assert result.pod_stopped is True
    # No train/hf-push/bench commands fired after smoke failed.
    assert not any("train_docs_embedder.py --base" in c and "--steps=100" in c and "_full" in c for c in ssh_calls)
    assert not any("_bench.json" in c for c in ssh_calls)
    assert any("FT did NOT change weights" in c for c in ssh_calls)
    assert not smoke_inputs.bench_json.exists()


def test_full_pipeline_train_failure_returns_two_and_pod_stopped(
    smoke_inputs,
    fake_pod_dict,
    monkeypatch,
):
    """Train failure → exit=2, pod stopped, partial logs scp attempt made.

    Provision + smoke succeed; the full-train ssh fails with OOM-style stderr.
    Match by command substring so we don't depend on ssh call ordering.
    """
    scp_back_calls: list[dict] = []

    def _spawn(**_):
        return fake_pod_dict

    def _ssh(*, host, port, cmd, **_):
        # Train command is the unique one that has --steps=100 + _full out dir
        # but does NOT contain the smoke-only "FT did NOT change weights".
        if (
            "train_docs_embedder.py" in cmd
            and "--steps=100" in cmd
            and "_full" in cmd
            and "FT did NOT change weights" not in cmd
        ):
            return _fail_proc(stderr="OOM at step 423")
        return _ok_proc()

    def _scp_back(**kwargs):
        scp_back_calls.append(kwargs)
        return _ok_proc()

    monkeypatch.setattr(full_pipeline.pod_lifecycle, "stop_pod", lambda pod_id: {"ok": True})
    monkeypatch.setattr(full_pipeline.pod_lifecycle, "get_pod", lambda pod_id: {"desiredStatus": "EXITED"})

    result = full_pipeline.run_pipeline(
        candidate_tag="tag",
        kind="docs",
        base_model="base",
        train_data=smoke_inputs.train,
        eval_data=smoke_inputs.eval,
        gpu="4090",
        max_steps=100,
        hf_repo="Tarshevskiy/tag",
        bench_json_path=smoke_inputs.bench_json,
        spawn_fn=_spawn,
        ssh_fn=_ssh,
        scp_back_fn=_scp_back,
        scp_to_fn=lambda **_: _ok_proc(),
        tar_overlay_fn=lambda **_: _ok_proc(),
        sleep_fn=lambda _s: None,
    )

    assert result.exit_code == full_pipeline.EXIT_TRAIN_FAILED
    assert result.smoke_passed is True
    assert result.pod_stopped is True
    # Partial-log scp was attempted (best-effort).
    assert any("training.log" in c.get("remote_path", "") for c in scp_back_calls)


def test_full_pipeline_bench_failure_returns_three_with_hf_pushed(
    smoke_inputs,
    fake_pod_dict,
    monkeypatch,
):
    """Bench failure → exit=3; train artifact already pushed to HF Hub.

    Match by command substring (not call number) so the test is stable to
    future provisioning-step changes.
    """

    def _spawn(**_):
        return fake_pod_dict

    def _ssh(*, host, port, cmd, **_):
        # Bench command: contains the bench JSON path AND benchmark_doc_intent
        # but NOT the smoke-only "FT did NOT change weights".
        if "_bench.json" in cmd and "benchmark_doc_intent.py" in cmd and "FT did NOT change weights" not in cmd:
            return _fail_proc(stderr="benchmark_doc_intent.py crashed")
        return _ok_proc()

    monkeypatch.setattr(full_pipeline.pod_lifecycle, "stop_pod", lambda pod_id: {"ok": True})
    monkeypatch.setattr(full_pipeline.pod_lifecycle, "get_pod", lambda pod_id: {"desiredStatus": "EXITED"})

    result = full_pipeline.run_pipeline(
        candidate_tag="tag",
        kind="docs",
        base_model="base",
        train_data=smoke_inputs.train,
        eval_data=smoke_inputs.eval,
        gpu="4090",
        max_steps=100,
        hf_repo="Tarshevskiy/tag",
        bench_json_path=smoke_inputs.bench_json,
        spawn_fn=_spawn,
        ssh_fn=_ssh,
        scp_back_fn=lambda **_: _ok_proc(),
        scp_to_fn=lambda **_: _ok_proc(),
        tar_overlay_fn=lambda **_: _ok_proc(),
        sleep_fn=lambda _s: None,
    )

    assert result.exit_code == full_pipeline.EXIT_BENCH_FAILED
    assert result.train_pushed_to_hf is True
    assert result.pod_stopped is True


# --- pod orphan detection ----------------------------------------------------


def test_full_pipeline_marks_orphan_when_pod_still_running_after_stop(
    smoke_inputs,
    fake_pod_dict,
    monkeypatch,
):
    """If get_pod reports RUNNING after stop_pod was called, exit=4."""

    def _spawn(**_):
        return fake_pod_dict

    def _ssh(**_):
        return _ok_proc()

    def _scp_back(*, local_path, **_):
        Path(local_path).write_text("{}")
        return _ok_proc()

    monkeypatch.setattr(full_pipeline.pod_lifecycle, "stop_pod", lambda pod_id: {"ok": True})
    # The orphan signal: pod is still RUNNING after our stop_pod call.
    monkeypatch.setattr(full_pipeline.pod_lifecycle, "get_pod", lambda pod_id: {"desiredStatus": "RUNNING"})

    result = full_pipeline.run_pipeline(
        candidate_tag="tag",
        kind="docs",
        base_model="base",
        train_data=smoke_inputs.train,
        eval_data=smoke_inputs.eval,
        gpu="4090",
        max_steps=100,
        hf_repo="Tarshevskiy/tag",
        bench_json_path=smoke_inputs.bench_json,
        spawn_fn=_spawn,
        ssh_fn=_ssh,
        scp_back_fn=_scp_back,
        scp_to_fn=lambda **_: _ok_proc(),
        tar_overlay_fn=lambda **_: _ok_proc(),
        sleep_fn=lambda _s: None,
    )

    assert result.exit_code == full_pipeline.EXIT_ORPHAN_DETECTED


# --- poll-until-bench-json-exists guarantee ---------------------------------


def test_full_pipeline_poll_loop_waits_until_sentinel_appears(
    smoke_inputs,
    fake_pod_dict,
    monkeypatch,
):
    """The function MUST NOT return until the bench JSON exists on Mac.

    We delete the sentinel right after scp_back so the poll loop has to spin.
    `sleep_fn` is a counter that materialises the file on the 3rd tick.
    """
    tick = [0]
    bench_path = smoke_inputs.bench_json

    def _spawn(**_):
        return fake_pod_dict

    def _ssh(**_):
        return _ok_proc()

    def _scp_back(*, local_path, **_):
        # Simulate scp returning success but the file flushing late.
        return _ok_proc()

    def _sleep(_secs):
        tick[0] += 1
        if tick[0] == 3:
            bench_path.write_text("{}")

    monkeypatch.setattr(full_pipeline.pod_lifecycle, "stop_pod", lambda pod_id: {"ok": True})
    monkeypatch.setattr(full_pipeline.pod_lifecycle, "get_pod", lambda pod_id: {"desiredStatus": "EXITED"})

    result = full_pipeline.run_pipeline(
        candidate_tag="tag",
        kind="docs",
        base_model="base",
        train_data=smoke_inputs.train,
        eval_data=smoke_inputs.eval,
        gpu="4090",
        max_steps=100,
        hf_repo="Tarshevskiy/tag",
        bench_json_path=bench_path,
        spawn_fn=_spawn,
        ssh_fn=_ssh,
        scp_back_fn=_scp_back,
        scp_to_fn=lambda **_: _ok_proc(),
        tar_overlay_fn=lambda **_: _ok_proc(),
        sleep_fn=_sleep,
        poll_interval_sec=1,
        poll_max_wait_sec=10,
    )

    assert result.exit_code == full_pipeline.EXIT_OK
    assert bench_path.exists()
    assert tick[0] >= 3, "Bug 5: poll loop must keep ticking until the sentinel exists."


def test_full_pipeline_poll_loop_times_out_to_orphan_when_sentinel_never_lands(
    smoke_inputs,
    fake_pod_dict,
    monkeypatch,
):
    """If the bench JSON sentinel never appears within poll_max_wait_sec, the
    function must return EXIT_ORPHAN_DETECTED (not a false-positive OK)."""
    bench_path = smoke_inputs.bench_json
    if bench_path.exists():
        bench_path.unlink()

    def _spawn(**_):
        return fake_pod_dict

    def _ssh(**_):
        return _ok_proc()

    def _scp_back(**_):
        # Simulate scp success but the local file never materializes (e.g.
        # remote returned 0 bytes, race with disk-full, etc).
        return _ok_proc()

    fake_now = [0.0]

    def _now():
        return fake_now[0]

    def _sleep(secs):
        fake_now[0] += secs

    monkeypatch.setattr(full_pipeline.pod_lifecycle, "stop_pod", lambda pod_id: {"ok": True})
    monkeypatch.setattr(full_pipeline.pod_lifecycle, "get_pod", lambda pod_id: {"desiredStatus": "EXITED"})

    result = full_pipeline.run_pipeline(
        candidate_tag="tag",
        kind="docs",
        base_model="base",
        train_data=smoke_inputs.train,
        eval_data=smoke_inputs.eval,
        gpu="4090",
        max_steps=100,
        hf_repo="Tarshevskiy/tag",
        bench_json_path=bench_path,
        spawn_fn=_spawn,
        ssh_fn=_ssh,
        scp_back_fn=_scp_back,
        scp_to_fn=lambda **_: _ok_proc(),
        tar_overlay_fn=lambda **_: _ok_proc(),
        sleep_fn=_sleep,
        now_fn=_now,
        poll_interval_sec=10,
        poll_max_wait_sec=30,
    )

    assert result.exit_code == full_pipeline.EXIT_ORPHAN_DETECTED
    assert result.failure_step == "poll"
    assert not bench_path.exists()


# --- spawn rejection paths --------------------------------------------------


def test_full_pipeline_spawn_failure_returns_one_no_pod_to_orphan(
    smoke_inputs,
    monkeypatch,
):
    """If pod creation itself fails, no orphan possible — return SMOKE_FAILED.

    Exercises the negative path where pod_lifecycle.start_pod raises (e.g.
    cost-guard breach, RunPod 4xx, missing API key)."""

    def _spawn(**_):
        raise full_pipeline.PodLifecycleError("TRAIN purpose requires --volume-gb >= 20")

    monkeypatch.setattr(full_pipeline.pod_lifecycle, "stop_pod", lambda pod_id: {"ok": True})

    result = full_pipeline.run_pipeline(
        candidate_tag="tag",
        kind="docs",
        base_model="base",
        train_data=smoke_inputs.train,
        eval_data=smoke_inputs.eval,
        gpu="4090",
        max_steps=100,
        hf_repo="Tarshevskiy/tag",
        bench_json_path=smoke_inputs.bench_json,
        spawn_fn=_spawn,
        sleep_fn=lambda _s: None,
    )

    assert result.exit_code == full_pipeline.EXIT_SMOKE_FAILED
    assert result.failure_step == "spawn"
    assert result.pod_id is None


# --- helper-function unit tests (no I/O at all) -----------------------------


def test_make_smoke_command_includes_cos_check_and_probe_10_for_docs():
    """Smoke command for `kind=docs` must verify cos<0.999 + probe=10 +
    model_key in the bench JSON."""
    cmd = full_pipeline._make_smoke_command(
        kind="docs",
        candidate_tag="docs-nomic-ft-v2",
        base_model="nomic-ai/nomic-embed-text-v1.5",
        train_data_remote="/workspace/train.jsonl",
        eval_data_remote="/workspace/eval.jsonl",
    )
    assert "FT did NOT change weights" in cmd
    assert "cos < 0.999" in cmd
    assert "--probe=10" in cmd
    assert "docs-nomic-ft-v2-smoke" in cmd
    assert ".model_key ==" in cmd


def test_make_smoke_command_uses_rerank_model_path_for_reranker():
    """Smoke command for `kind=reranker` must invoke --rerank-model-path
    (Bug 1 fix) and assert the path contains the smoke tag."""
    cmd = full_pipeline._make_smoke_command(
        kind="reranker",
        candidate_tag="rerank-l6-ft",
        base_model="cross-encoder/ms-marco-MiniLM-L-6-v2",
        train_data_remote="/workspace/train.jsonl",
        eval_data_remote="/workspace/eval.jsonl",
    )
    assert "--rerank-model-path=/workspace/rerank-l6-ft_smoke" in cmd
    assert ".rerank_model | contains" in cmd
    assert "rerank-l6-ft_smoke" in cmd


def test_gpu_alias_resolution_maps_friendly_tags():
    """CLI takes 4090/a40/a100; pod_lifecycle expects rtx4090/a40/a100-80g."""
    assert full_pipeline.GPU_ALIASES["4090"] == "rtx4090"
    assert full_pipeline.GPU_ALIASES["a40"] == "a40"
    assert full_pipeline.GPU_ALIASES["a100"] == "a100-80g"


def test_make_train_command_routes_docs_kind_to_bi_encoder_trainer():
    """`kind=docs` must invoke train_docs_embedder.py (the bi-encoder trainer)."""
    cmd = full_pipeline._make_train_command(
        kind="docs",
        base_model="nomic-ai/nomic-embed-text-v1.5",
        train_data_remote="/workspace/train.jsonl",
        out_dir_remote="/workspace/tag_full",
        max_steps=500,
    )
    assert "scripts/runpod/train_docs_embedder.py" in cmd
    assert "scripts/runpod/train_reranker_ce.py" not in cmd
    assert "--steps=500" in cmd


def test_make_train_command_routes_reranker_kind_to_ce_trainer():
    """`kind=reranker` must invoke train_reranker_ce.py (the cross-encoder trainer).

    This is the contract that lets full_pipeline run a reranker candidate with
    the same orchestrator as a docs candidate — only the trainer script path
    differs."""
    cmd = full_pipeline._make_train_command(
        kind="reranker",
        base_model="cross-encoder/ms-marco-MiniLM-L-6-v2",
        train_data_remote="/workspace/combined_train.jsonl",
        out_dir_remote="/workspace/rerank-l6_full",
        max_steps=300,
    )
    assert "scripts/runpod/train_reranker_ce.py" in cmd
    assert "scripts/runpod/train_docs_embedder.py" not in cmd
    assert "--steps=300" in cmd
    assert "--base=cross-encoder/ms-marco-MiniLM-L-6-v2" in cmd


def test_run_pipeline_rejects_unknown_kind(smoke_inputs):
    with pytest.raises(ValueError, match="kind"):
        full_pipeline.run_pipeline(
            candidate_tag="tag",
            kind="bogus",
            base_model="base",
            train_data=smoke_inputs.train,
            eval_data=smoke_inputs.eval,
            gpu="4090",
            max_steps=100,
            hf_repo="Tarshevskiy/tag",
        )


def test_poll_until_bench_json_exists_returns_immediately_when_present(tmp_path):
    sentinel = tmp_path / "x.json"
    sentinel.write_text("{}")
    ticks = []
    ok = full_pipeline._poll_until_bench_json_exists(
        sentinel,
        interval_sec=1,
        max_wait_sec=5,
        sleep_fn=lambda s: ticks.append(s),
        now_fn=lambda: 0.0,
    )
    assert ok is True
    assert ticks == [], "should not sleep when sentinel already present"


def test_poll_until_bench_json_exists_times_out_when_absent(tmp_path):
    sentinel = tmp_path / "never.json"
    fake_now = [0.0]
    ticks = []

    def _sleep(s):
        ticks.append(s)
        fake_now[0] += s

    ok = full_pipeline._poll_until_bench_json_exists(
        sentinel,
        interval_sec=1,
        max_wait_sec=3,
        sleep_fn=_sleep,
        now_fn=lambda: fake_now[0],
    )
    assert ok is False
    assert sum(ticks) >= 3


# ============================================================================
# Step 1.5: pod provisioning (setup_env.sh + Phase-0 tar overlay)
# ============================================================================
#
# Without this step the pod has only the stock runpod/pytorch image — no
# scripts/, no src/. The contract:
#   1. scp setup_env.sh to /workspace/setup_env.sh
#   2. ssh `bash /workspace/setup_env.sh` (installs deps + git clone)
#   3. tar-stream local scripts/ + src/ → /workspace/code-rag-mcp/ (Phase-0
#      overlay so unpushed local code beats stale origin/main)
#   4. ssh `python3 -c "import scripts.benchmark_doc_intent"` sanity check
# Any sub-step failure → EXIT_SMOKE_FAILED + failure_step="provision".


def _stage_repo_root_with_setup_env(tmp_path):
    """Build a fake repo root with the minimum files _provision_pod inspects.

    `_provision_pod` reads `<repo_root>/scripts/runpod/setup_env.sh` to scp
    over and validates `scripts/` + `src/` exist on disk for the tar overlay
    pre-flight check. The Bug-6a fix added a fourth requirement:
    `<repo_root>/db/knowledge.db` is scp'd to the pod for build_docs_vectors
    + benchmark_doc_intent to read chunks/FTS metadata from.
    """
    fake_root = tmp_path / "fake_repo"
    (fake_root / "scripts" / "runpod").mkdir(parents=True)
    (fake_root / "src").mkdir(parents=True)
    (fake_root / "db").mkdir(parents=True)
    (fake_root / "db" / "vectors.lance.docs").mkdir(parents=True)  # Bug 6e overlay target
    (fake_root / "scripts" / "runpod" / "setup_env.sh").write_text("#!/bin/bash\necho stub\n")
    (fake_root / "db" / "knowledge.db").write_bytes(b"SQLite stub")
    return fake_root


def test_full_pipeline_provision_happy_path_then_smoke_runs(
    smoke_inputs,
    fake_pod_dict,
    monkeypatch,
    tmp_path,
):
    """Provision succeeds (setup_env scp + bash + overlay + sanity), so smoke
    + train + bench all fire afterward and the run hits EXIT_OK."""
    fake_root = _stage_repo_root_with_setup_env(tmp_path)

    scp_to_calls: list[dict] = []
    ssh_calls: list[str] = []
    overlay_calls: list[dict] = []

    def _spawn(**_):
        return fake_pod_dict

    def _ssh(*, host, port, cmd, **_):
        ssh_calls.append(cmd)
        return _ok_proc()

    def _scp_to(**kwargs):
        scp_to_calls.append(kwargs)
        return _ok_proc()

    def _scp_back(*, local_path, **_):
        Path(local_path).write_text('{"model_key":"tag-smoke"}')
        return _ok_proc()

    def _tar_overlay(**kwargs):
        overlay_calls.append(kwargs)
        return _ok_proc()

    monkeypatch.setattr(full_pipeline.pod_lifecycle, "stop_pod", lambda pod_id: {"ok": True})
    monkeypatch.setattr(full_pipeline.pod_lifecycle, "get_pod", lambda pod_id: {"desiredStatus": "EXITED"})

    result = full_pipeline.run_pipeline(
        candidate_tag="tag",
        kind="docs",
        base_model="base",
        train_data=smoke_inputs.train,
        eval_data=smoke_inputs.eval,
        gpu="4090",
        max_steps=100,
        hf_repo="Tarshevskiy/tag",
        bench_json_path=smoke_inputs.bench_json,
        spawn_fn=_spawn,
        ssh_fn=_ssh,
        scp_back_fn=_scp_back,
        scp_to_fn=_scp_to,
        tar_overlay_fn=_tar_overlay,
        repo_root=fake_root,
        sleep_fn=lambda _s: None,
    )

    assert result.exit_code == full_pipeline.EXIT_OK
    # setup_env.sh must have been scp'd to /workspace/setup_env.sh.
    assert any(c["remote_path"] == "/workspace/setup_env.sh" for c in scp_to_calls), scp_to_calls
    # tar overlay must target the cloned repo dir with scripts/ + src/.
    assert overlay_calls, "tar_overlay_fn was not invoked"
    overlay = overlay_calls[0]
    assert overlay["remote_root"] == "/workspace/code-rag-mcp"
    # Bug 6e: db/vectors.lance.docs included if locally present.
    assert overlay["local_dirs"] == ["scripts", "src", "db/vectors.lance.docs"]
    assert overlay["repo_root"] == fake_root
    # Bug 6a: db/knowledge.db must have been scp'd to the cloned repo's db dir.
    assert any(
        c["remote_path"] == "/workspace/code-rag-mcp/db/knowledge.db"
        and c["local_path"] == fake_root / "db" / "knowledge.db"
        for c in scp_to_calls
    ), f"knowledge.db not scp'd; scp_to_calls={scp_to_calls}"
    # Provision ssh sequence: bash setup_env.sh, then sanity import, then mkdir
    # remote db/, all must precede the smoke / train / bench commands.
    setup_idx = next(i for i, c in enumerate(ssh_calls) if "bash /workspace/setup_env.sh" in c)
    sanity_idx = next(i for i, c in enumerate(ssh_calls) if "import scripts.benchmark_doc_intent" in c)
    mkdir_idx = next(i for i, c in enumerate(ssh_calls) if "mkdir -p /workspace/code-rag-mcp/db" in c)
    smoke_idx = next(i for i, c in enumerate(ssh_calls) if "FT did NOT change weights" in c)
    assert setup_idx < sanity_idx < mkdir_idx < smoke_idx
    # All scripts run under the cloned + overlaid repo dir.
    assert any("cd /workspace/code-rag-mcp" in c for c in ssh_calls)


def test_full_pipeline_provision_fails_when_setup_env_returns_nonzero(
    smoke_inputs,
    fake_pod_dict,
    monkeypatch,
    tmp_path,
):
    """setup_env.sh exits non-zero → exit=EXIT_SMOKE_FAILED, no train, pod
    stopped, failure_step='provision'."""
    fake_root = _stage_repo_root_with_setup_env(tmp_path)
    ssh_calls: list[str] = []

    def _spawn(**_):
        return fake_pod_dict

    def _ssh(*, host, port, cmd, **_):
        ssh_calls.append(cmd)
        if "bash /workspace/setup_env.sh" in cmd:
            return _fail_proc(stderr="apt-get update failed: 503")
        return _ok_proc()

    monkeypatch.setattr(full_pipeline.pod_lifecycle, "stop_pod", lambda pod_id: {"ok": True})
    monkeypatch.setattr(full_pipeline.pod_lifecycle, "get_pod", lambda pod_id: {"desiredStatus": "EXITED"})

    result = full_pipeline.run_pipeline(
        candidate_tag="tag",
        kind="docs",
        base_model="base",
        train_data=smoke_inputs.train,
        eval_data=smoke_inputs.eval,
        gpu="4090",
        max_steps=100,
        hf_repo="Tarshevskiy/tag",
        bench_json_path=smoke_inputs.bench_json,
        spawn_fn=_spawn,
        ssh_fn=_ssh,
        scp_back_fn=lambda **_: _ok_proc(),
        scp_to_fn=lambda **_: _ok_proc(),
        tar_overlay_fn=lambda **_: _ok_proc(),
        repo_root=fake_root,
        sleep_fn=lambda _s: None,
    )

    assert result.exit_code == full_pipeline.EXIT_SMOKE_FAILED
    assert result.failure_step == "provision"
    assert "setup_env.sh" in (result.failure_reason or "")
    assert result.pod_stopped is True
    # Smoke / train / bench must NOT have fired after provision blew up.
    assert not any("FT did NOT change weights" in c for c in ssh_calls)
    assert not any("_bench.json" in c for c in ssh_calls)
    assert not smoke_inputs.bench_json.exists()


def test_full_pipeline_provision_fails_when_tar_overlay_returns_nonzero(
    smoke_inputs,
    fake_pod_dict,
    monkeypatch,
    tmp_path,
):
    """Tar overlay exits non-zero (e.g. broken pipe to remote tar) → same
    contract: exit=EXIT_SMOKE_FAILED, failure_step='provision', pod stopped,
    no train/bench attempted."""
    fake_root = _stage_repo_root_with_setup_env(tmp_path)
    ssh_calls: list[str] = []

    def _spawn(**_):
        return fake_pod_dict

    def _ssh(*, host, port, cmd, **_):
        ssh_calls.append(cmd)
        return _ok_proc()  # setup_env succeeds

    def _tar_overlay(**_):
        return _fail_proc(stderr="tar: Broken pipe")

    monkeypatch.setattr(full_pipeline.pod_lifecycle, "stop_pod", lambda pod_id: {"ok": True})
    monkeypatch.setattr(full_pipeline.pod_lifecycle, "get_pod", lambda pod_id: {"desiredStatus": "EXITED"})

    result = full_pipeline.run_pipeline(
        candidate_tag="tag",
        kind="docs",
        base_model="base",
        train_data=smoke_inputs.train,
        eval_data=smoke_inputs.eval,
        gpu="4090",
        max_steps=100,
        hf_repo="Tarshevskiy/tag",
        bench_json_path=smoke_inputs.bench_json,
        spawn_fn=_spawn,
        ssh_fn=_ssh,
        scp_back_fn=lambda **_: _ok_proc(),
        scp_to_fn=lambda **_: _ok_proc(),
        tar_overlay_fn=_tar_overlay,
        repo_root=fake_root,
        sleep_fn=lambda _s: None,
    )

    assert result.exit_code == full_pipeline.EXIT_SMOKE_FAILED
    assert result.failure_step == "provision"
    assert (
        "tar overlay" in (result.failure_reason or "").lower() or "broken pipe" in (result.failure_reason or "").lower()
    )
    assert result.pod_stopped is True
    # Sanity-import + smoke must NOT have fired (overlay never landed).
    assert not any("import scripts.benchmark_doc_intent" in c for c in ssh_calls)
    assert not any("FT did NOT change weights" in c for c in ssh_calls)


def test_full_pipeline_provision_fails_when_knowledge_db_missing(
    smoke_inputs,
    fake_pod_dict,
    monkeypatch,
    tmp_path,
):
    """Bug 6a: db/knowledge.db is required on the local repo root (the
    provision step scp's it to the pod). If it's missing locally, fail
    fast at provision rather than late at bench time."""
    fake_root = _stage_repo_root_with_setup_env(tmp_path)
    # Delete the staged knowledge.db to simulate a fresh checkout that
    # never ran build_index.
    (fake_root / "db" / "knowledge.db").unlink()

    monkeypatch.setattr(full_pipeline.pod_lifecycle, "stop_pod", lambda pod_id: {"ok": True})
    monkeypatch.setattr(full_pipeline.pod_lifecycle, "get_pod", lambda pod_id: {"desiredStatus": "EXITED"})

    result = full_pipeline.run_pipeline(
        candidate_tag="tag",
        kind="docs",
        base_model="base",
        train_data=smoke_inputs.train,
        eval_data=smoke_inputs.eval,
        gpu="4090",
        max_steps=100,
        hf_repo="Tarshevskiy/tag",
        bench_json_path=smoke_inputs.bench_json,
        spawn_fn=lambda **_: fake_pod_dict,
        ssh_fn=lambda **_: _ok_proc(),
        scp_back_fn=lambda **_: _ok_proc(),
        scp_to_fn=lambda **_: _ok_proc(),
        tar_overlay_fn=lambda **_: _ok_proc(),
        repo_root=fake_root,
        sleep_fn=lambda _s: None,
    )

    assert result.exit_code == full_pipeline.EXIT_SMOKE_FAILED
    assert result.failure_step == "provision"
    assert "knowledge.db" in (result.failure_reason or "")
    assert result.pod_stopped is True


def test_full_pipeline_provision_fails_when_scp_knowledge_db_returns_nonzero(
    smoke_inputs,
    fake_pod_dict,
    monkeypatch,
    tmp_path,
):
    """Bug 6a: scp of knowledge.db from Mac → pod fails (e.g. disk full,
    network blip). Same contract as the other provision failures: clean
    abort, no train, pod stopped."""
    fake_root = _stage_repo_root_with_setup_env(tmp_path)
    scp_to_calls: list[dict] = []

    def _scp_to(**kwargs):
        scp_to_calls.append(kwargs)
        if kwargs.get("remote_path", "").endswith("/db/knowledge.db"):
            return _fail_proc(stderr="No space left on device")
        return _ok_proc()

    monkeypatch.setattr(full_pipeline.pod_lifecycle, "stop_pod", lambda pod_id: {"ok": True})
    monkeypatch.setattr(full_pipeline.pod_lifecycle, "get_pod", lambda pod_id: {"desiredStatus": "EXITED"})

    result = full_pipeline.run_pipeline(
        candidate_tag="tag",
        kind="docs",
        base_model="base",
        train_data=smoke_inputs.train,
        eval_data=smoke_inputs.eval,
        gpu="4090",
        max_steps=100,
        hf_repo="Tarshevskiy/tag",
        bench_json_path=smoke_inputs.bench_json,
        spawn_fn=lambda **_: fake_pod_dict,
        ssh_fn=lambda **_: _ok_proc(),
        scp_back_fn=lambda **_: _ok_proc(),
        scp_to_fn=_scp_to,
        tar_overlay_fn=lambda **_: _ok_proc(),
        repo_root=fake_root,
        sleep_fn=lambda _s: None,
    )

    assert result.exit_code == full_pipeline.EXIT_SMOKE_FAILED
    assert result.failure_step == "provision"
    assert "knowledge.db" in (result.failure_reason or "")
    assert "No space left" in (result.failure_reason or "") or "rc=" in (result.failure_reason or "")
    assert result.pod_stopped is True


def test_tar_overlay_to_returns_nonzero_when_local_dir_missing(tmp_path):
    """`_tar_overlay_to` does its own pre-flight: a missing local dir must
    map to rc != 0 + a clear stderr instead of silently succeeding via tar's
    rc=0 + warning."""
    cp = full_pipeline._tar_overlay_to(
        host="1.2.3.4",
        port=22,
        repo_root=tmp_path,  # empty — no scripts/ or src/
        local_dirs=["scripts"],
        remote_root="/workspace/code-rag-mcp",
        key_path=tmp_path / "fake_key",
        runner=lambda *a, **kw: _ok_proc(),  # would-be success if reached
    )
    assert cp.returncode != 0
    assert "missing local dir" in cp.stderr


# ============================================================================
# wait_for_ssh_ready: post-spawn SSH-endpoint polling guard
# ============================================================================
#
# RunPod's POST /pods returns desiredStatus="RUNNING" immediately, but
# publicIp="" and portMappings={} for ~30-90 seconds while the container
# actually boots. Without polling, _spawn_pod raced into
# "pod missing ssh_host/ssh_port" → exit 1 every time. The contract:
#   - returns immediately if SSH info is already populated;
#   - polls until publicIp + portMappings.22 land;
#   - raises PodLifecycleError after timeout_sec if they never land.


def test_wait_for_ssh_ready_returns_immediately_when_endpoint_populated():
    """If the very first GET /pods/<id> already has publicIp + portMappings,
    we must NOT sleep or poll a second time."""
    sleep_calls: list[float] = []
    poll_calls: list[str] = []

    def _get_pod(pod_id):
        poll_calls.append(pod_id)
        # Schema confirmed against live RunPod 2026-04-26 — both running pods
        # had this exact shape (string-keyed portMappings, str publicIp).
        return {
            "id": pod_id,
            "desiredStatus": "RUNNING",
            "publicIp": "63.141.33.57",
            "portMappings": {"22": 22130},
        }

    out = pod_lifecycle.wait_for_ssh_ready(
        "pod_ready",
        timeout_sec=300,
        sleep_sec=10,
        get_pod_fn=_get_pod,
        sleep_fn=lambda s: sleep_calls.append(s),
        now_fn=lambda: 0.0,
    )
    assert out == {
        "ssh_host": "63.141.33.57",
        "ssh_port": 22130,
        "pod_id": "pod_ready",
    }
    assert poll_calls == ["pod_ready"], "exactly one GET when info is already there"
    assert sleep_calls == [], "must not sleep when SSH info is present on first poll"


def test_wait_for_ssh_ready_polls_until_endpoint_appears():
    """Mock get_pod to return empty publicIp twice, then the populated dict."""
    fake_now = [0.0]
    sleep_calls: list[float] = []

    def _sleep(s):
        sleep_calls.append(s)
        fake_now[0] += s

    responses = [
        # tick 0: pod created, no IP yet (the bug-causing race)
        {"id": "p", "desiredStatus": "RUNNING", "publicIp": "", "portMappings": {}},
        # tick 1: still booting
        {"id": "p", "desiredStatus": "RUNNING", "publicIp": "", "portMappings": {}},
        # tick 2: container up, SSH endpoint published
        {
            "id": "p",
            "desiredStatus": "RUNNING",
            "publicIp": "194.68.245.198",
            "portMappings": {"22": 22145},
        },
    ]
    polls = [0]

    def _get_pod(pod_id):
        i = polls[0]
        polls[0] += 1
        return responses[i]

    out = pod_lifecycle.wait_for_ssh_ready(
        "p",
        timeout_sec=300,
        sleep_sec=10,
        get_pod_fn=_get_pod,
        sleep_fn=_sleep,
        now_fn=lambda: fake_now[0],
    )
    assert out == {"ssh_host": "194.68.245.198", "ssh_port": 22145, "pod_id": "p"}
    assert polls[0] == 3, "should poll until endpoint appears"
    assert sleep_calls == [10, 10], "two sleeps before the third (successful) poll"


def test_wait_for_ssh_ready_raises_after_timeout():
    """If publicIp + portMappings.22 never appear, raise PodLifecycleError
    with the last seen pod info attached for postmortem."""
    fake_now = [0.0]

    def _sleep(s):
        fake_now[0] += s

    def _get_pod(pod_id):
        # Stuck in the boot race forever.
        return {
            "id": pod_id,
            "desiredStatus": "RUNNING",
            "publicIp": "",
            "portMappings": {},
            "templateId": "",
        }

    with pytest.raises(pod_lifecycle.PodLifecycleError) as exc_info:
        pod_lifecycle.wait_for_ssh_ready(
            "stuck_pod",
            timeout_sec=30,
            sleep_sec=10,
            get_pod_fn=_get_pod,
            sleep_fn=_sleep,
            now_fn=lambda: fake_now[0],
        )
    msg = str(exc_info.value)
    assert "stuck_pod" in msg
    assert "30s" in msg
    # Error message should embed the last known pod state for postmortem.
    assert "publicIp" in msg or "portMappings" in msg


# --- _spawn_pod integration: ensures wait_for_ssh_ready is wired in ---------


def test_spawn_pod_calls_wait_for_ssh_ready_and_returns_populated_endpoint(
    monkeypatch,
    tmp_path,
):
    """`_spawn_pod` must invoke `wait_for_ssh_ready` so the returned dict
    always has non-empty ssh_host/ssh_port — even when start_pod's response
    has them blank (the actual RunPod race condition)."""
    # Empty ssh pubkey path so _spawn_pod skips the read step.
    fake_pub = tmp_path / "missing.pub"

    def _start_pod(**_):
        # Simulate the real-world race: pod created, but no IP/port yet.
        return {
            "id": "pod_race",
            "desiredStatus": "RUNNING",
            "publicIp": "",
            "portMappings": {},
            "env": {"PUBLIC_KEY": "ssh-ed25519 AAAA test"},
        }

    wait_calls: list[dict] = []

    def _wait(pod_id, **kwargs):
        wait_calls.append({"pod_id": pod_id, **kwargs})
        return {
            "ssh_host": "1.2.3.4",
            "ssh_port": 22130,
            "pod_id": pod_id,
        }

    monkeypatch.setattr(full_pipeline.pod_lifecycle, "start_pod", _start_pod)
    monkeypatch.setattr(full_pipeline.pod_lifecycle, "wait_for_ssh_ready", _wait)

    out = full_pipeline._spawn_pod(
        candidate_tag="t",
        gpu="4090",
        volume_gb=20,
        time_limit_min=60,
        ssh_public_key_path=fake_pub,
        spending_cap_usd=1.0,
    )
    assert out["ssh_host"] == "1.2.3.4"
    assert out["ssh_port"] == 22130
    assert out["pod_id"] == "pod_race"
    assert len(wait_calls) == 1
    assert wait_calls[0]["pod_id"] == "pod_race"
    # env still redacted in the merged dict.
    assert out["env"] == {"PUBLIC_KEY": "***"}


def test_spawn_pod_propagates_ssh_ready_timeout(monkeypatch, tmp_path):
    """If `wait_for_ssh_ready` times out, `_spawn_pod` must propagate the
    PodLifecycleError so run_pipeline maps it to EXIT_SMOKE_FAILED with
    failure_step='spawn' (instead of pretending the pod is reachable)."""
    fake_pub = tmp_path / "missing.pub"

    monkeypatch.setattr(
        full_pipeline.pod_lifecycle,
        "start_pod",
        lambda **_: {"id": "pod_stuck", "publicIp": "", "portMappings": {}},
    )

    def _wait(pod_id, **_):
        raise pod_lifecycle.PodLifecycleError(f"Pod {pod_id} did not become SSH-ready in 300s. Last info: ...")

    monkeypatch.setattr(full_pipeline.pod_lifecycle, "wait_for_ssh_ready", _wait)

    with pytest.raises(pod_lifecycle.PodLifecycleError, match="SSH-ready"):
        full_pipeline._spawn_pod(
            candidate_tag="t",
            gpu="4090",
            volume_gb=20,
            time_limit_min=60,
            ssh_public_key_path=fake_pub,
            spending_cap_usd=1.0,
        )
