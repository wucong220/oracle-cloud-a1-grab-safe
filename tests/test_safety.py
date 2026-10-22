import ast
import base64
import copy
import datetime as dt
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from fixtures import AD, CHILD, INSTANCE, NOW, ROOT, TENANT, approve, cfg, defaults, instance, write

spec = importlib.util.spec_from_file_location("a1grab", ROOT / "a1grab.py")
a = importlib.util.module_from_spec(spec)
spec.loader.exec_module(a)


def deny_network(*args, **kwargs):
    raise AssertionError("TEST NETWORK ACCESS FORBIDDEN")


class Safety(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="a1grab-mock-")
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name)
        if os.name != "nt":
            self.path.chmod(0o700)
        self.cfg = cfg(self.path)
        self.fixture = defaults()
        self.now = NOW
        approve(a, self.cfg)
        self.api = a.OCI(self.cfg, self.path, command=[sys.executable, "-I", str(ROOT / "tests" / "mock_oci.py")])
        # Synthetic Python-based CLI is not a real installed OCI venv.
        self.api.verify_dependency_policy = lambda: None
        self.engine = a.Engine(self.cfg, self.api, self.path, lambda: self.now)
        for target in ("socket.socket.connect", "socket.socket.connect_ex", "socket.create_connection", "urllib.request.urlopen"):
            guard = patch(target, deny_network)
            guard.start()
            self.addCleanup(guard.stop)
        self.flush()

    def flush(self):
        self.fixture["now"] = self.now
        write(self.path / "fixture.json", self.fixture)
        text = "\n".join(f'{key}="{value.replace(chr(92), "/")}"' for key, value in self.cfg.items()) + "\n"
        (self.path / "config.sh").write_text(text, encoding="utf-8")

    def run_engine(self, create=True, resume=False):
        self.flush()
        return self.engine.run(create, resume)

    def run_with_preflight_seconds(self, elapsed, create=True, resume=False):
        """Simulate a preflight that takes `elapsed` wall seconds.

        The first clock() read is run() entry; after that the engine clock advances so
        that the launch-time re-check sees entry_time + elapsed. Lets us exercise the
        PREFLIGHT_MAX_SECONDS boundary and the launch-instant approval/month re-check.
        """
        reads = {"n": 0}
        base = self.now
        def clock():
            n = reads["n"]
            reads["n"] += 1
            # First read (entry) = base; every later read = base + elapsed.
            return base if n == 0 else base + elapsed
        engine = a.Engine(self.cfg, self.api, self.path, clock)
        self.flush()
        return engine.run(create, resume)

    def stopped_with_preflight(self, reason, elapsed):
        self.flush()
        with self.assertRaises(a.Stop) as caught:
            self.run_with_preflight_seconds(elapsed)
        self.assertIn(reason, str(caught.exception))
        return str(caught.exception)

    def stopped(self, reason=None, **kwargs):
        with self.assertRaises(a.Stop) as caught:
            self.run_engine(**kwargs)
        if reason:
            self.assertIn(reason, str(caught.exception))
        return str(caught.exception)

    def remote(self):
        path = self.path / "mock-remote.json"
        return json.loads(path.read_text()) if path.exists() else {"launch_calls": 0, "resources": {}, "history": []}

    def journal(self):
        return a.load_json(self.path / "journal.json")

    def calls(self):
        path = self.path / "mock-calls.jsonl"
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def worker(self, *options):
        self.flush()
        return subprocess.run([sys.executable, "-I", str(ROOT / "tests" / "worker.py"), str(self.path), *options], capture_output=True, text=True, timeout=30)

    def test_dry_run_never_writes_even_with_enabled_config(self):
        self.assertEqual(self.run_engine(create=False)["state"], "DRY_RUN")
        self.assertEqual(self.remote()["launch_calls"], 0)
        self.assertFalse((self.path / "journal.json").exists())

    def test_dry_run_cannot_call_launch_boundary(self):
        with self.assertRaisesRegex(a.Stop, "CLOUD_WRITES_DISABLED"):
            self.api.launch({}, "not-an-auth-token")

    def test_dry_run_ignores_absent_approval(self):
        Path(self.cfg["REVIEW_FILE"]).unlink()
        self.assertEqual(self.run_engine(create=False)["state"], "DRY_RUN")

    def test_default_safety_latches(self):
        for key, value in (("DRY_RUN", "1"), ("ALLOW_CREATE", "0")):
            with self.subTest(key=key):
                old = self.cfg[key]
                self.cfg[key] = value
                self.stopped("CREATION_NOT_EXPLICITLY_ENABLED")
                self.cfg[key] = old
        self.assertEqual(self.remote()["launch_calls"], 0)

    def test_dry_run_env_is_absolute_veto(self):
        with patch.dict(os.environ, {"DRY_RUN": "1"}):
            self.stopped("CREATION_NOT_EXPLICITLY_ENABLED")
        self.assertEqual(self.remote()["launch_calls"], 0)

    def test_success_persists_before_returning(self):
        self.assertEqual(self.run_engine()["state"], "ACCEPTED")
        self.assertEqual(self.journal()["instance_id"], INSTANCE)
        self.assertEqual(len(self.remote()["resources"]), 1)

    def test_success_restart_never_launches_again(self):
        self.run_engine()
        self.assertEqual(self.run_engine()["state"], "SUCCEEDED")
        self.assertEqual(self.remote()["launch_calls"], 1)

    def test_success_with_list_invisibility_uses_get(self):
        self.run_engine()
        self.fixture["hide_accepted"] = True
        self.assertEqual(self.run_engine()["state"], "ACCEPTED")
        self.assertEqual(self.remote()["launch_calls"], 1)
        self.assertIn("instance", [c["action"] for c in self.calls()])

    def test_capacity_retries_identical_body_and_token_after_recheck(self):
        self.fixture["launch_sequence"] = ["capacity", "success"]
        self.assertEqual(self.run_engine()["state"], "CAPACITY")
        self.now += 601
        self.assertEqual(self.run_engine()["state"], "ACCEPTED")
        history = self.remote()["history"]
        self.assertEqual(history[0], history[1])
        self.assertEqual(len(self.remote()["resources"]), 1)
        self.assertEqual([c["action"] for c in self.calls()].count("instances"), 2)

    def test_capacity_backoff_survives_restart(self):
        self.fixture["launch_sequence"] = ["capacity"]
        self.run_engine()
        self.stopped("PERSISTED_BACKOFF_ACTIVE")
        self.assertEqual(self.remote()["launch_calls"], 1)

    def test_rate_limit_variants_and_retry_after(self):
        for outcome in ("throttled", "throttled_words"):
            with self.subTest(outcome=outcome):
                api = a.OCI(self.cfg, self.path, runner=lambda *args, **kwargs: subprocess.CompletedProcess(args, 1, "", json.dumps({"status": 429, "code": "TooManyRequests" if outcome == "throttled" else "Other", "message": "Too many requests for the user", "headers": {"Retry-After": "1800"}})))
                api.writes_enabled = True
                self.assertEqual(api.launch({}, "synthetic"), ("THROTTLED", None, 1800))

    def test_rate_limit_is_persistent(self):
        self.fixture["launch_sequence"] = ["throttled"]
        self.assertEqual(self.run_engine()["state"], "THROTTLED")
        self.stopped("PERSISTED_BACKOFF_ACTIVE")

    def test_timeout_no_automatic_retry(self):
        self.fixture["launch_sequence"] = ["timeout"]
        self.stopped("LAUNCH_UNKNOWN")
        self.assertEqual(self.journal()["state"], "UNKNOWN")
        self.stopped("EXPLICIT_RESUME_REQUIRED")
        self.assertEqual(self.remote()["launch_calls"], 1)

    def test_timeout_after_accept_reconciles_pending_instance(self):
        self.fixture["launch_sequence"] = ["timeout_after_accept"]
        self.stopped("LAUNCH_UNKNOWN")
        self.assertEqual(self.run_engine()["state"], "SUCCEEDED")
        self.assertEqual(self.remote()["launch_calls"], 1)

    def test_unknown_explicit_resume_reuses_token_with_stale_list(self):
        self.fixture.update(launch_sequence=["timeout_after_accept", "success"], hide_accepted=True)
        self.stopped("LAUNCH_UNKNOWN")
        self.assertEqual(self.run_engine(resume=True)["state"], "ACCEPTED")
        self.assertEqual(len(self.remote()["resources"]), 1)
        self.assertEqual(self.remote()["history"][0], self.remote()["history"][1])

    def test_duplicate_and_invalidated_token_stop(self):
        for outcome in ("duplicate", "invalidated", "server_error", "invalid_json"):
            with self.subTest(outcome=outcome):
                self.fixture["launch_sequence"] = [outcome]
                self.stopped("LAUNCH_UNKNOWN")
                self.assertEqual(self.journal()["state"], "UNKNOWN")
                (self.path / "journal.json").unlink()  # synthetic fixture reset only
                (self.path / "mock-remote.json").unlink()

    def test_duplicate_after_accept_reconciles(self):
        self.fixture["launch_sequence"] = ["duplicate_after_accept"]
        self.stopped("LAUNCH_UNKNOWN")
        self.assertEqual(self.run_engine()["state"], "SUCCEEDED")
        self.assertEqual(self.remote()["launch_calls"], 1)

    def test_expired_token_is_never_rotated(self):
        self.fixture["launch_sequence"] = ["capacity"]
        self.run_engine()
        self.now += a.TOKEN_SAFE_SECONDS + 1
        approve(a, self.cfg, now=self.now)
        self.stopped("RETRY_TOKEN_EXPIRED")
        self.assertEqual(self.remote()["launch_calls"], 1)

    def test_request_change_with_existing_intent_is_blocked(self):
        self.fixture["launch_sequence"] = ["capacity"]
        self.run_engine()
        self.cfg["NAME"] = "another"
        self.now += 601
        approve(a, self.cfg, now=self.now)
        self.stopped("REQUEST_CHANGED")

    def test_tampered_journal_token_is_blocked(self):
        self.fixture["launch_sequence"] = ["capacity"]
        self.run_engine()
        state = self.journal()
        state["retry_token"] = "tampered"
        a.atomic_json(self.path / "journal.json", state)
        self.stopped("JOURNAL_RETRY_TOKEN_CHANGED")

    def test_corrupt_journal_never_replaced_with_new_intent(self):
        (self.path / "journal.json").write_text("{")
        (self.path / "journal.json").chmod(0o600)
        self.stopped("INVALID_JSON_FILE")
        self.assertEqual(self.remote()["launch_calls"], 0)

    def test_limit_exceeded_is_terminal(self):
        self.fixture["launch_sequence"] = ["limit"]
        self.assertEqual(self.run_engine()["state"], "REJECTED")
        self.stopped("PERSISTENT_TERMINAL_STATE")
        self.assertEqual(self.remote()["launch_calls"], 1)

    def test_all_existing_a1_lifecycles_are_counted(self):
        for state in ("PROVISIONING", "STARTING", "RUNNING", "STOPPED", "STOPPING", "TERMINATING", "MOVING", "CREATING_IMAGE"):
            with self.subTest(state=state):
                self.fixture["instances"] = [instance(state)]
                self.stopped("A1_ALREADY_EXISTS")
        self.assertEqual(self.remote()["launch_calls"], 0)

    def test_terminated_instances_do_not_erase_historical_billing(self):
        self.fixture["instances"] = [instance("TERMINATED")]
        self.fixture["usage"] = [{"sku-part-number": "B93297", "unit": "OCPU HOURS", "computed-quantity": 1499}]
        self.stopped("BILLING_EXCEEDS_REVIEWED")

    def test_existing_a1_in_child_compartment_blocks_root_creation(self):
        self.fixture["compartments"] = [{"id": CHILD, "lifecycle-state": "ACTIVE"}]
        self.fixture["child_instances"] = [instance("PROVISIONING")]
        self.stopped("A1_ALREADY_EXISTS")
        self.assertIn(CHILD, [c["compartment"] for c in self.calls()])

    def test_any_read_failure_fails_closed(self):
        for action in ("regions", "tenancy", "ads", "compartments", "instances", "boot", "block", "boot_backups", "block_backups", "containers", "pools", "reservations", "subnet", "images", "availability", "subscriptions", "usage"):
            with self.subTest(action=action):
                self.fixture["read_error"] = action
                self.stopped("READ_FAILED")
        self.assertEqual(self.remote()["launch_calls"], 0)

    def test_unknown_resource_lifecycle_blocks(self):
        self.fixture["instances"] = [instance("UNKNOWN_ENUM_VALUE")]
        self.stopped("UNKNOWN_RESOURCE_LIFECYCLE")

    def test_missing_a1_usage_blocks(self):
        self.fixture["instances"] = [instance()]
        self.fixture["instances"][0]["shape-config"]["ocpus"] = None
        self.stopped("UNKNOWN_A1_CPU_USAGE")

    def test_boot_and_block_volumes_share_200_gb(self):
        self.fixture["boot"] = [{"id": "boot1", "lifecycle-state": "AVAILABLE", "size-in-gbs": 100, "vpus-per-gb": 10}]
        self.fixture["block"] = [{"id": "block1", "lifecycle-state": "PROVISIONING", "size-in-gbs": 51, "vpus-per-gb": 10}]
        self.stopped("STORAGE_FREE_ALLOCATION_EXCEEDED")

    def test_exactly_200_gb_with_new_boot_is_allowed(self):
        self.fixture["boot"] = [{"id": "boot1", "lifecycle-state": "AVAILABLE", "size-in-gbs": 150, "vpus-per-gb": 10}]
        self.assertEqual(self.run_engine()["state"], "ACCEPTED")

    def test_missing_volume_size_blocks(self):
        self.fixture["boot"] = [{"id": "boot1", "lifecycle-state": "PROVISIONING", "vpus-per-gb": 10}]
        self.stopped("UNKNOWN_VOLUME_SIZE")

    def test_high_performance_volumes_block(self):
        self.fixture["boot"] = [{"id": "boot1", "lifecycle-state": "AVAILABLE", "size-in-gbs": 50, "vpus-per-gb": 20}]
        self.stopped("PAID_VOLUME_PERFORMANCE")

    def test_replication_and_autotuning_block(self):
        for field in ("block-volume-replicas", "boot-volume-replicas", "autotune-policies", "is-auto-tune-enabled"):
            with self.subTest(field=field):
                self.fixture["boot"] = [{"id": "boot1", "lifecycle-state": "AVAILABLE", "size-in-gbs": 50, "vpus-per-gb": 10, field: True}]
                self.stopped("VOLUME_REPLICATION_OR_AUTOTUNE")

    def test_shared_containers_and_automated_controllers_block(self):
        for name in ("containers", "pools", "reservations"):
            with self.subTest(name=name):
                old = self.fixture[name]
                self.fixture[name] = [{"lifecycle-state": "CREATING"}]
                self.stopped("OTHER_COMPUTE_OR_CONTROLLER")
                self.fixture[name] = old

    def test_backup_limit_blocks(self):
        self.fixture["boot_backups"] = [{"lifecycle-state": "AVAILABLE"}] * 6
        self.stopped("STORAGE_FREE_ALLOCATION_EXCEEDED")

    def test_pending_other_shape_blocks_unknown_boot_storage(self):
        self.fixture["instances"] = [instance("PROVISIONING")]
        self.fixture["instances"][0]["shape"] = "VM.Standard.E2.1.Micro"
        self.stopped("OTHER_INSTANCE_PENDING_STORAGE_UNCERTAIN")

    def test_wrong_home_region_blocks(self):
        self.fixture["regions"][0]["region-name"] = "us-phoenix-1"
        self.stopped("HOME_REGION_OR_REGION_SCOPE")

    def test_multiple_regions_allowed_when_tokyo_is_home(self):
        # An account may subscribe to several regions; extra READY regions must not block.
        self.fixture["regions"].append({"region-name": "us-phoenix-1", "is-home-region": False, "status": "READY"})
        self.assertEqual(self.run_engine()["state"], "ACCEPTED")

    def test_home_region_must_be_tokyo_even_with_multiple_regions(self):
        # Multi-region is fine, but the single Home Region must still be Tokyo.
        self.fixture["regions"] = [
            {"region-name": "ap-tokyo-1", "is-home-region": False, "status": "READY"},
            {"region-name": "us-phoenix-1", "is-home-region": True, "status": "READY"},
        ]
        self.stopped("HOME_REGION_OR_REGION_SCOPE")

    def test_region_with_non_ready_or_unknown_status_blocks(self):
        # OCI RegionSubscription.status documents only READY / IN_PROGRESS. Fail closed:
        # accept only READY; IN_PROGRESS, the non-official INACTIVE, and any unknown
        # enum value must all stop.
        for status in ("IN_PROGRESS", "INACTIVE", "UNKNOWN_ENUM_VALUE", None):
            with self.subTest(status=status):
                self.fixture["regions"] = [{"region-name": "ap-tokyo-1", "is-home-region": True, "status": status}]
                self.stopped("REGION_SUBSCRIPTION_STATE_UNKNOWN")

    def test_extra_region_with_non_ready_status_blocks(self):
        # A non-READY secondary subscription must still stop even though Tokyo is READY.
        self.fixture["regions"].append({"region-name": "eu-frankfurt-1", "is-home-region": False, "status": "IN_PROGRESS"})
        self.stopped("REGION_SUBSCRIPTION_STATE_UNKNOWN")

    def test_wrong_or_multiple_ad_blocks(self):
        self.fixture["ads"] = [{"name": "TEST:AP-TOKYO-1-AD-2"}]
        self.stopped("SINGLE_TOKYO_AD1")
        self.fixture["ads"] = [{"name": AD}, {"name": AD}]
        self.stopped("SINGLE_TOKYO_AD1")

    def test_paid_trial_or_unknown_account_blocks(self):
        for plan in ("PAYG", "TRIAL", None, "UNKNOWN_ENUM_VALUE"):
            with self.subTest(plan=plan):
                self.fixture["subscriptions"]["items"][0]["plan-type"] = plan
                self.stopped("FREE_TIER_ACCOUNT_NOT_CONFIRMED")

    def test_upgrade_pending_blocks(self):
        self.fixture["subscriptions"]["items"][0]["upgrade-state"] = "SUBMITTED"
        self.stopped("FREE_TIER_ACCOUNT_NOT_CONFIRMED")

    def test_empty_or_ambiguous_subscription_blocks(self):
        self.fixture["subscriptions"] = {"items": []}
        self.stopped("FREE_TIER_ACCOUNT_NOT_CONFIRMED")

    def test_quota_is_not_free_entitlement(self):
        self.fixture["availability"].update(available=10000, **{"fractional-availability": 10000})
        approve(a, self.cfg, a1_ocpu_hours_upper_bound=1499)
        self.stopped("MONTHLY_ALWAYS_FREE_BUDGET_NOT_PROVEN")

    def test_unknown_or_inconsistent_quota_usage_blocks(self):
        self.fixture["availability"]["fractional-usage"] = 1.0
        self.stopped("QUOTA_COUNTERS_DISAGREE")
        self.fixture["availability"]["fractional-usage"] = None
        self.fixture["availability"]["used"] = None
        self.stopped("UNKNOWN_USED_QUOTA")

    def test_marketplace_custom_or_unknown_image_blocks(self):
        for value in (TENANT, "other", ""):
            with self.subTest(compartment=value):
                self.fixture["images"][0]["compartment-id"] = value
                self.stopped("IMAGE_NOT_FREE_OFFICIAL_PLATFORM_IMAGE")

    def test_too_large_image_blocks(self):
        self.fixture["images"][0]["size-in-mbs"] = 60 * 1024
        self.stopped("IMAGE_EXCEEDS_BOOT_VOLUME")

    def test_missing_image_size_blocks(self):
        del self.fixture["images"][0]["size-in-mbs"]
        self.stopped("UNKNOWN_IMAGE_SIZE")

    def test_wrong_subnet_or_ad_blocks(self):
        self.fixture["subnet"]["availability-domain"] = "OTHER"
        self.stopped("SUBNET_NOT_CONFIRMED")

    def test_empty_usage_is_not_automatic_free_confirmation(self):
        Path(self.cfg["REVIEW_FILE"]).unlink()
        self.stopped("MISSING_OR_UNSAFE_FILE")
        self.assertEqual(self.remote()["launch_calls"], 0)

    def test_missing_any_human_confirmation_blocks(self):
        for key in ("approved", "all_compartments_visible", "exclusive_writer", "no_unresolved_requests", "official_image_free", "balanced_boot_free", "unreported_usage_accounted_for", "local_state_preserved"):
            with self.subTest(key=key):
                approve(a, self.cfg, **{key: False})
                self.stopped()
        self.assertEqual(self.remote()["launch_calls"], 0)

    def test_stale_or_unbound_approval_blocks(self):
        for changes in ({"request_sha256": "wrong"}, {"tenancy_sha256": "wrong"}, {"policy_date": "2000-01-01"}, {"billing_month": "2026-09"}, {"expires_at": a.iso(NOW - 1)}, {"reviewed_at": a.iso(NOW + 1)}, {"a1_ocpu_hours_upper_bound": None}):
            with self.subTest(fields=list(changes)):
                approve(a, self.cfg, **changes)
                self.stopped()

    def test_preflight_within_window_launches(self):
        # 299s preflight is within PREFLIGHT_MAX_SECONDS (300): approval still valid.
        self.assertEqual(self.run_with_preflight_seconds(299)["state"], "ACCEPTED")

    def test_preflight_beyond_window_stops(self):
        # 301s preflight exceeds PREFLIGHT_MAX_SECONDS (300): fail closed before launch.
        self.stopped_with_preflight("PREFLIGHT_TOO_OLD", 301)
        self.assertEqual(self.remote()["launch_calls"], 0)

    def test_approval_expired_during_preflight_stops(self):
        # Approval valid at entry but expired by launch time must stop at launch recheck.
        approve(a, self.cfg, expires_at=a.iso(NOW + 200))
        self.stopped_with_preflight("HUMAN_REVIEW_STALE", 299)

    def test_preflight_crossing_month_rechecks_usage_and_approval(self):
        # Entry on 2026-10-31 23:58 UTC; a 299s preflight reaches 2026-11-01, so the
        # launch-instant re-check must re-read usage for the new month; the October
        # approval no longer matches the billing month and the launch stops.
        base = dt.datetime(2026, 10, 31, 23, 58, tzinfo=dt.timezone.utc).timestamp()
        self.now = base
        approve(a, self.cfg, now=base)
        self.stopped_with_preflight("PRICING_OR_ACCOUNT_REVIEW_REQUIRED", 299)
        self.assertEqual(self.remote()["launch_calls"], 0)

    def test_billing_usage_pages_are_all_consumed(self):
        self.fixture["usage_pages"] = [[], [{"sku-part-number": "B93297", "unit": "OCPU HOURS", "computed-quantity": 1499}]]
        self.stopped("BILLING_EXCEEDS_REVIEWED")

    def test_billing_corrections_or_missing_quantity_stop(self):
        row = {"sku-part-number": "B93297", "unit": "OCPU HOURS", "computed-quantity": None}
        self.fixture["usage"] = [row]
        self.stopped("UNKNOWN_MONTHLY_A1_USAGE")
        row.update(**{"computed-quantity": 1, "is-correction": True})
        self.stopped("BILLING_CORRECTION_REQUIRES_REVIEW")

    def test_nonfinite_or_negative_usage_stops(self):
        for quantity in (-1, float("nan"), float("inf"), True):
            with self.subTest(quantity=str(quantity)):
                self.fixture["usage"] = [{"sku-part-number": "B93297", "unit": "OCPU HOURS", "computed-quantity": quantity}]
                self.stopped("UNKNOWN_MONTHLY_A1_USAGE")

    def test_request_has_no_paid_addons_or_public_ip(self):
        self.run_engine()
        body = self.journal()["body"]
        self.assertEqual(body["shapeConfig"], {"ocpus": 2, "memoryInGBs": 12})
        self.assertEqual(body["sourceDetails"]["bootVolumeSizeInGBs"], 50)
        self.assertEqual(body["sourceDetails"]["bootVolumeVpusPerGB"], 10)
        self.assertIs(body["createVnicDetails"]["assignPublicIp"], False)
        self.assertEqual(body["capacityReservationId"], "")
        self.assertNotIn("userData", json.dumps(body))
        self.assertEqual(set(body["metadata"]), {"ssh_authorized_keys"})

    def test_tokens_not_in_process_args_and_temp_files_removed(self):
        captured = []
        def runner(command, **kwargs):
            captured.append(command)
            return subprocess.CompletedProcess(command, 0, json.dumps({"status": "200 OK", "data": {"id": INSTANCE}}), "")
        api = a.OCI(self.cfg, self.path, runner=runner)
        api.writes_enabled = True
        api.launch({}, "SYNTHETIC_SECRET_SENTINEL")
        self.assertNotIn("SYNTHETIC_SECRET_SENTINEL", str(captured))
        self.assertFalse((self.path / "request-headers.json").exists())
        self.assertFalse((self.path / "request-body.json").exists())

    def test_subprocess_has_no_shell_or_unsafe_environment(self):
        def runner(command, **kwargs):
            self.assertIs(kwargs["shell"], False)
            self.assertEqual(kwargs["timeout"], 75)
            for key in ("OCI_CLI_ENDPOINT", "OCI_CLI_AUTH", "PYTHONPATH", "HTTPS_PROXY", "REQUESTS_CA_BUNDLE"):
                self.assertNotIn(key, kwargs["env"])
            return subprocess.CompletedProcess(command, 0, '{"data":[]}', "")
        with patch.dict(os.environ, {"OCI_CLI_ENDPOINT": "https://attacker.invalid", "PYTHONPATH": "bad", "HTTPS_PROXY": "bad"}):
            api = a.OCI(self.cfg, self.path, runner=runner)
            api.read("instances", "--all")

    def test_hard_process_timeout_is_unknown(self):
        def runner(*args, **kwargs):
            raise subprocess.TimeoutExpired("MOCK", 75, output="secret-sentinel")
        self.api.runner = runner
        self.stopped("OCI_TIMEOUT_RESULT_UNKNOWN")
        self.assertEqual(self.remote()["launch_calls"], 0)

    def test_hard_process_timeout_during_launch_keeps_unknown_intent(self):
        original = self.api.runner
        def runner(command, **kwargs):
            if "raw-request" in command:
                raise subprocess.TimeoutExpired("MOCK", 75, output="secret-sentinel")
            return original(command, **kwargs)
        self.api.runner = runner
        self.stopped("LAUNCH_UNKNOWN")
        self.assertEqual(self.journal()["state"], "UNKNOWN")
        self.assertEqual(self.journal()["attempts"], 1)

    def test_failed_durable_intent_write_prevents_launch(self):
        with patch.object(a.os, "fsync", side_effect=OSError("synthetic disk failure")):
            with self.assertRaises(OSError):
                self.run_engine()
        self.assertEqual(self.remote()["launch_calls"], 0)
        self.assertFalse((self.path / "journal.json").exists())

    def test_untagged_external_instance_after_capacity_stops(self):
        self.fixture["launch_sequence"] = ["capacity"]
        self.run_engine()
        self.fixture["instances"] = [instance("PROVISIONING")]
        self.fixture["instances"][0]["freeform-tags"] = None
        self.stopped("A1_ALREADY_EXISTS")
        self.assertEqual(self.remote()["launch_calls"], 1)

    def test_unknown_cli_commands_rejected(self):
        with self.assertRaisesRegex(a.Stop, "API_NOT_ALLOWLISTED"):
            self.api.read("delete_everything")

    def test_read_pagination_must_be_complete(self):
        self.api.runner = lambda *args, **kwargs: subprocess.CompletedProcess(args, 0, '{"data":[],"opc-next-page":"more"}', "")
        with self.assertRaisesRegex(a.Stop, "UNCONSUMED_PAGINATION"):
            self.api.read("instances", "--all")

    def test_config_constraints(self):
        for key, bad in (("MODEL", "VM.Standard.E4.Flex"), ("OCPUS", "4"), ("MEMGB", "24"), ("BOOT_GB", "200"), ("BOOT_VPUS_PER_GB", "20"), ("REGION", "us-phoenix-1"), ("AD", "AD-2")):
            with self.subTest(key=key):
                original = self.cfg[key]
                self.cfg[key] = bad
                self.flush()
                with self.assertRaisesRegex(a.Stop, "CONFIG_OUTSIDE_APPROVED"):
                    a.config(self.path / "config.sh")
                self.cfg[key] = original

    def test_config_command_injection_never_executes(self):
        for text in ('NAME="$(touch exploited)"', 'NAME="`touch exploited`"', 'NAME="safe"; touch exploited', 'ADS=("bad")', 'source=/tmp/evil', 'NAME="a"\nNAME="b"'):
            with self.subTest(text=text):
                (self.path / "config.sh").write_text(text)
                with self.assertRaises(a.Stop):
                    a.config(self.path / "config.sh")
        self.assertFalse((self.path / "exploited").exists())

    def test_private_key_cannot_be_injected_as_public_key(self):
        Path(self.cfg["SSH_PUBKEY_FILE"]).write_text("PRIVATE MATERIAL MUST NOT BE READ AS PUBLIC")
        self.stopped("ONLY_ONE_SSH_PUBLIC_KEY")

    def test_cli_hash_and_version_are_pinned(self):
        self.api.verify()
        self.cfg["OCI_CLI_SHA256"] = "0" * 64
        with self.assertRaisesRegex(a.Stop, "CLI_HASH_CHANGED"):
            self.api.verify()

    def test_auth_config_tenancy_mismatch_stops(self):
        Path(self.cfg["OCI_CONFIG_FILE"]).write_text("[DEFAULT]\ntenancy=wrong\nregion=ap-tokyo-1\n")
        with self.assertRaisesRegex(a.Stop, "AUTH_TENANCY_OR_REGION_MISMATCH"):
            self.api.verify()

    def test_secret_stderr_does_not_escape(self):
        self.fixture["read_error"] = "regions"
        result = self.worker("--create")
        self.assertEqual(result.returncode, 2)
        self.assertNotIn("secret-sentinel", result.stdout + result.stderr)

    def test_process_restart_after_accept_before_result_save(self):
        self.fixture["crash_phase"] = "after_accept"
        result = self.worker("--create")
        self.assertEqual(result.returncode, 77, result.stderr)
        self.assertEqual(self.journal()["state"], "IN_FLIGHT")
        del self.fixture["crash_phase"]
        result = self.worker("--create")
        self.assertEqual(json.loads(result.stdout)["state"], "SUCCEEDED", result.stderr)
        self.assertEqual(self.remote()["launch_calls"], 1)

    def test_process_restart_after_write_ahead_before_send(self):
        self.fixture["crash_phase"] = "after_intent"
        self.assertEqual(self.worker("--create").returncode, 77)
        del self.fixture["crash_phase"]
        self.assertEqual(self.worker("--create").returncode, 2)
        self.assertEqual(self.remote()["launch_calls"], 0)
        self.assertEqual(self.worker("--create", "--resume-unknown").returncode, 0)
        self.assertEqual(self.remote()["launch_calls"], 1)

    def test_real_multiprocess_lock_allows_only_one_launch(self):
        self.fixture["pause_before_launch"] = 1.0
        self.flush()
        command = [sys.executable, "-I", str(ROOT / "tests" / "worker.py"), str(self.path), "--create"]
        first = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.addCleanup(lambda: first.kill() if first.poll() is None else None)
        deadline = time.monotonic() + 10
        while not (self.path / "pause-ready").exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertTrue((self.path / "pause-ready").exists())
        second = subprocess.run(command, capture_output=True, text=True, timeout=10)
        self.assertEqual(second.returncode, 2, second.stderr)
        self.assertIn("ANOTHER_PROCESS", second.stdout)
        output, error = first.communicate(timeout=15)
        self.assertEqual(first.returncode, 0, error)
        self.assertEqual(self.remote()["launch_calls"], 1)
        self.assertEqual(len(self.remote()["resources"]), 1)

    def test_state_path_does_not_depend_on_checkout(self):
        first = a.state_directory(TENANT)
        with patch("os.getcwd", return_value="different-checkout"), patch.dict(os.environ, {"HOME": "different-home", "LOCALAPPDATA": "different-state"}):
            self.assertEqual(first, a.state_directory(TENANT))
        self.assertNotIn(str(ROOT), str(first))

    def test_existing_journal_success_tombstone_blocks_recreation(self):
        self.run_engine()
        self.run_engine()
        remote = self.remote()
        remote["resources"] = {}
        write(self.path / "mock-remote.json", remote)
        self.stopped("OCI_READ_FAILED_INSTANCE")
        self.assertEqual(self.remote()["launch_calls"], 1)

    def test_setup_is_local_and_does_not_overwrite(self):
        target = self.path / "new-config.sh"
        self.assertEqual(a.local_setup(target)["state"], "LOCAL_TEMPLATE_CREATED")
        with self.assertRaisesRegex(a.Stop, "CONFIG_ALREADY_EXISTS"):
            a.local_setup(target)
        self.assertEqual(self.remote()["launch_calls"], 0)

    def test_service_is_read_only_and_does_not_restart(self):
        text = (ROOT / "a1grab.service").read_text()
        self.assertIn("--dry-run", text)
        self.assertIn("Environment=DRY_RUN=1", text)
        self.assertIn("Restart=no", text)
        self.assertNotIn("--create", text)

    def test_no_executable_config_external_notifications_or_timer_changes(self):
        for name in ("setup.sh", "grab.sh", "status.sh", "notify.sh"):
            text = (ROOT / name).read_text()
            self.assertNotIn("systemctl", text)
            self.assertNotIn("curl ", text)
            self.assertNotIn("eval ", text)
            self.assertNotIn('. "$BASE/config.sh"', text)
        source = (ROOT / "a1grab.py").read_text()
        self.assertNotIn("dnf-makecache.timer", source)

    def test_no_shell_true_or_eval_in_python(self):
        tree = ast.parse((ROOT / "a1grab.py").read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                self.assertNotIn(getattr(node.func, "id", ""), ("eval", "exec"))
                self.assertFalse(any(k.arg == "shell" and isinstance(k.value, ast.Constant) and k.value.value is True for k in node.keywords))


if __name__ == "__main__":
    unittest.main()
