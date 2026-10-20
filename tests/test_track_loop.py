"""``track run --every`` and ``watch --every``: the loop, its lock and its backoff."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import textwrap
from functools import partial

import pytest
from click.testing import CliRunner

from audible_deals import wishlist as wishlist_mod
from audible_deals.automation_models import TrackRunRequest
from audible_deals.cli import cli
from audible_deals.locking import LockHeldError, run_lock
from audible_deals.state_store import load_track_state
from audible_deals.track_loop import (
    BACKOFF_CAP_SECONDS,
    LoopClock,
    backoff_seconds,
    human_seconds,
    run_every,
    stop_on_sigterm,
)
from audible_deals.track_service import run_track
from tests.conftest import make_product
from tests.test_track_service import Client, Webhook, runtime


class FakeClock(LoopClock):
    """Records every sleep instead of waiting; jitter is a fixed fraction."""

    def __init__(self, jitter: float = 1.0, on_sleep=None):
        self.sleeps: list[float] = []
        self._jitter = jitter
        self._on_sleep = on_sleep
        super().__init__(
            sleep=self._sleep, uniform=lambda low, high: self._jitter_for(low, high)
        )

    def _jitter_for(self, low, high):
        return min(max(self._jitter, low), high)

    def _sleep(self, seconds):
        self.sleeps.append(seconds)
        if self._on_sleep:
            self._on_sleep(len(self.sleeps))


def script(*outcomes):
    """A run_once that returns or raises the next outcome each call."""
    remaining = list(outcomes)

    def run_once():
        outcome = remaining.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    return run_once


class TestBackoff:
    @pytest.mark.parametrize(
        ("interval", "expected"),
        [
            (60, [60, 120, 240, 480, 960, 1920, 3600, 3600, 3600]),
            (1800, [1800, 3600, 3600]),
            (7200, [7200, 7200, 7200]),  # never shorter than the interval
            (0.01, [0.01, 0.02, 0.04]),  # tiny intervals, as the tests use them
        ],
    )
    def test_the_wait_doubles_up_to_the_cap(self, interval, expected):
        waits = [backoff_seconds(n, interval) for n in range(1, len(expected) + 1)]

        assert waits == pytest.approx(expected)

    def test_it_stays_capped_after_many_failures(self):
        assert backoff_seconds(500, 60) == BACKOFF_CAP_SECONDS

    def test_no_failures_waits_the_interval(self):
        assert backoff_seconds(0, 90) == 90


class TestLoop:
    def test_successes_wait_the_interval(self):
        clock = FakeClock()
        results = []

        runs = run_every(
            script("a", "b", "c"),
            interval_s=600,
            clock=clock,
            on_result=results.append,
            max_runs=3,
        )

        assert runs == 3 and results == ["a", "b", "c"]
        assert clock.sleeps == [600, 600]  # no wait after the last of max_runs

    def test_failures_back_off_and_a_success_resets(self):
        clock = FakeClock()
        failures = []

        run_every(
            script(OSError("x"), OSError("y"), OSError("z"), "ok", OSError("w")),
            interval_s=100,
            clock=clock,
            on_failure=lambda error, n, wait: failures.append((str(error), n, wait)),
            max_runs=5,
        )

        assert failures == [("x", 1, 100), ("y", 2, 200), ("z", 3, 400), ("w", 1, 100)]
        assert clock.sleeps == [
            100,
            200,
            400,
            100,
        ]  # the success waits the plain interval

    def test_jitter_only_shortens_the_wait(self):
        for jitter, low in ((0.0, 0.8), (1.0, 1.0)):
            clock = FakeClock(jitter=jitter)
            run_every(
                script(*[OSError("x")] * 12),
                interval_s=60,
                clock=clock,
                max_runs=12,
            )

            assert all(w <= BACKOFF_CAP_SECONDS for w in clock.sleeps)
            assert clock.sleeps[-1] == pytest.approx(BACKOFF_CAP_SECONDS * low)

    def test_the_cap_holds_over_a_long_outage(self):
        clock = FakeClock()

        run_every(
            script(*[OSError("x")] * 40), interval_s=900, clock=clock, max_runs=40
        )

        assert max(clock.sleeps) == BACKOFF_CAP_SECONDS

    def test_a_held_lock_skips_the_run_and_is_not_a_failure(self):
        clock = FakeClock()
        events = []

        run_every(
            script(LockHeldError("busy"), OSError("x"), LockHeldError("busy"), "ok"),
            interval_s=100,
            clock=clock,
            on_busy=lambda error: events.append("busy"),
            on_failure=lambda error, n, wait: events.append(("failed", n)),
            on_result=lambda result: events.append(result),
            max_runs=4,
        )

        # The busy run neither backs off nor counts: the failure after it is still #1.
        assert events == ["busy", ("failed", 1), "busy", "ok"]
        assert clock.sleeps == [100, 100, 100]

    def test_ctrl_c_during_a_run_ends_the_loop_cleanly(self):
        runs = run_every(
            script("ok", KeyboardInterrupt()),
            interval_s=1,
            clock=FakeClock(),
        )

        assert runs == 1

    def test_ctrl_c_while_waiting_ends_the_loop_cleanly(self):
        def interrupt(count):
            raise KeyboardInterrupt

        clock = FakeClock(on_sleep=interrupt)

        assert run_every(script("ok", "never"), interval_s=1, clock=clock) == 1

    def test_sigterm_stops_it_like_ctrl_c_and_the_handler_is_restored(self):
        if not hasattr(signal, "SIGTERM") or sys.platform == "win32":
            pytest.skip("POSIX signals")
        before = signal.getsignal(signal.SIGTERM)

        def terminate(count):
            os.kill(os.getpid(), signal.SIGTERM)

        with stop_on_sigterm():
            runs = run_every(
                script("ok", "never"), interval_s=1, clock=FakeClock(on_sleep=terminate)
            )

        assert runs == 1
        assert signal.getsignal(signal.SIGTERM) is before

    def test_on_wait_reports_whether_it_is_backing_off(self):
        waits = []

        run_every(
            script("ok", OSError("x"), "ok"),
            interval_s=100,
            clock=FakeClock(),
            on_wait=lambda seconds, backing_off: waits.append((seconds, backing_off)),
            max_runs=3,
        )

        assert waits == [(100, False), (100, True)]

    @pytest.mark.parametrize(
        ("seconds", "text"),
        [
            (0, "0s"),
            (45, "45s"),
            (60, "1m"),
            (1800, "30m"),
            (3600, "1h"),
            (3900, "1h 5m"),
        ],
    )
    def test_durations_are_short_and_human(self, seconds, text):
        assert human_seconds(seconds) == text


def request(webhook=None):
    return TrackRunRequest("us", "$", None, 1, webhook, "generic", {})


class TestSharesTheRunLockWithScheduledRuns:
    def _tracking(self, client):
        return runtime(client, Webhook(), wishlist=[])

    def test_a_scheduled_run_in_progress_makes_the_loop_skip(self, tmp_config):
        busy = []
        track_runtime = self._tracking(Client())

        with run_lock():  # what a scheduled `deals track run` holds
            runs = run_every(
                lambda: run_track(request(), track_runtime),
                interval_s=1,
                clock=FakeClock(),
                on_busy=busy.append,
                max_runs=1,
            )

        assert runs == 1 and len(busy) == 1
        assert load_track_state().runs == []  # nothing ran, nothing was recorded

    def test_while_a_loop_run_works_a_scheduled_run_is_refused(self, tmp_config):
        seen = []

        class Probe(Client):
            def get_products_batch(self, asins):
                try:
                    with run_lock():
                        seen.append("acquired")
                except LockHeldError:
                    seen.append("refused")
                return super().get_products_batch(asins)

        track_runtime = runtime(
            Probe(products=[make_product(asin="B0LOCKTEST", price=3.0)]),
            Webhook(),
            wishlist=[{"asin": "B0LOCKTEST", "max_price": 5.0}],
        )

        run_every(
            lambda: run_track(request(), track_runtime),
            interval_s=1,
            clock=FakeClock(),
            max_runs=1,
        )

        assert seen == ["refused"]
        with run_lock():  # and the lock is free again afterwards
            pass

    def test_a_separate_process_holding_the_lock_makes_the_loop_skip(self, tmp_config):
        holder = subprocess.Popen(
            [
                sys.executable,
                "-c",
                textwrap.dedent(
                    """
                    import sys
                    from audible_deals.locking import run_lock
                    with run_lock():
                        print("held", flush=True)
                        sys.stdin.readline()
                    """
                ),
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            text=True,
            env={**os.environ, "DEALS_HOME": str(tmp_config)},
        )
        try:
            assert holder.stdout.readline().strip() == "held"
            busy = []
            run_every(
                lambda: run_track(request(), self._tracking(Client())),
                interval_s=1,
                clock=FakeClock(),
                on_busy=busy.append,
                max_runs=1,
            )
        finally:
            holder.stdin.close()
            holder.wait(timeout=10)

        assert len(busy) == 1


class TestTrackRunEveryCommand:
    @pytest.fixture
    def looped(self, monkeypatch):
        """`track run --every` with a fake clock and a bounded number of runs."""
        import audible_deals.cli.track as track_cli

        clock = FakeClock()

        def bounded(max_runs):
            monkeypatch.setattr(
                track_cli,
                "run_every",
                partial(run_every, clock=clock, max_runs=max_runs),
            )
            return clock

        return bounded

    def test_it_runs_repeatedly_and_prints_each_summary(
        self, tmp_config, mock_client, looped
    ):
        clock = looped(3)

        result = CliRunner().invoke(cli, ["track", "run", "--every", "30m"])

        assert result.exit_code == 0, result.output
        assert result.stdout.count("Refreshed 0 wishlist") == 3
        assert "Running every 30m" in result.stdout
        assert "Next run in 30m" in result.stdout
        assert clock.sleeps == [1800, 1800]
        assert len(load_track_state().runs) == 3

    def test_a_failing_run_is_reported_recorded_and_retried_with_backoff(
        self, tmp_config, mock_client, looped
    ):
        wishlist_mod.save_wishlist(
            [{"asin": "B0LOOPFAIL", "title": "T", "max_price": 5.0, "added": ""}]
        )
        mock_client.get_products_batch.side_effect = RuntimeError("catalog down")
        clock = looped(4)

        result = CliRunner().invoke(cli, ["track", "run", "--every", "30m"])

        assert result.exit_code == 0, result.output
        warnings = [ln for ln in result.stderr.splitlines() if "run failed" in ln]
        assert len(warnings) == 4
        assert "(1 in a row)" in warnings[0] and "(4 in a row)" in warnings[3]
        assert "catalog down" in warnings[0]
        assert "trying again in" in warnings[0]
        assert clock.sleeps == [1800, 3600, 3600]
        errors = [run.get("error") for run in load_track_state().runs]
        assert len(errors) == 4 and all("catalog down" in e for e in errors)

    def test_it_recovers_after_a_failure(self, tmp_config, mock_client, looped):
        wishlist_mod.save_wishlist(
            [{"asin": "B0LOOPFAIL", "title": "T", "max_price": 5.0, "added": ""}]
        )
        outcomes = [RuntimeError("blip"), [make_product(asin="B0LOOPFAIL", price=9.0)]]

        def batch(asins):
            outcome = outcomes.pop(0) if outcomes else []
            if isinstance(outcome, Exception):
                raise outcome
            return outcome

        mock_client.get_products_batch.side_effect = batch
        clock = looped(3)

        result = CliRunner().invoke(cli, ["track", "run", "--every", "30m"])

        assert "run failed (1 in a row)" in result.stderr
        assert result.stdout.count("Refreshed 1 wishlist") == 2
        assert clock.sleeps == [1800, 1800]  # the retry, then the plain interval

    def test_a_held_lock_is_a_note_not_an_error(self, tmp_config, mock_client, looped):
        clock = looped(2)

        with run_lock():
            result = CliRunner().invoke(cli, ["track", "run", "--every", "30m"])

        assert result.exit_code == 0, result.output
        assert result.stderr.count("another run is in progress, skipped this one") == 2
        assert "run failed" not in result.stderr
        assert clock.sleeps == [1800]

    def test_ctrl_c_stops_it_with_status_zero(
        self, tmp_config, mock_client, monkeypatch
    ):
        import audible_deals.cli.track as track_cli

        def interrupt(count):
            raise KeyboardInterrupt

        monkeypatch.setattr(
            track_cli,
            "run_every",
            partial(run_every, clock=FakeClock(on_sleep=interrupt)),
        )

        result = CliRunner().invoke(cli, ["track", "run", "--every", "30m"])

        assert result.exit_code == 0, result.output
        assert "Note: Stopped." in result.stderr

    def test_it_will_not_hammer_the_api(self, tmp_config, mock_client):
        result = CliRunner().invoke(cli, ["track", "run", "--every", "5m"])

        assert result.exit_code == 2
        assert "Minimum interval is 10m" in result.stderr

    def test_the_config_is_reread_between_runs(
        self, tmp_config, mock_client, looped, monkeypatch
    ):
        import audible_deals.cli.track as track_cli
        from audible_deals.config_store import save_config

        requests = []
        real = track_cli.run_track

        def spy(request, runtime):
            requests.append(request)
            return real(request, runtime)

        monkeypatch.setattr(track_cli, "run_track", spy)
        clock = looped(2)
        clock._on_sleep = lambda count: save_config({"credit_price": 7.5})

        CliRunner().invoke(cli, ["track", "run", "--every", "30m"])

        assert [r.credit_price for r in requests] == [None, 7.5]

    def test_a_single_run_is_unchanged(self, tmp_config, mock_client):
        result = CliRunner().invoke(cli, ["track", "run"])

        assert result.exit_code == 0, result.output
        assert "Refreshed 0 wishlist" in result.stdout
        assert "Running every" not in result.stdout


class TestWatchEvery:
    @pytest.fixture
    def looped(self, monkeypatch):
        import audible_deals.cli.wishlist as wishlist_cli

        def bounded(max_runs):
            clock = FakeClock()
            monkeypatch.setattr(
                wishlist_cli,
                "run_every",
                partial(run_every, clock=clock, max_runs=max_runs),
            )
            return clock

        return bounded

    def _wishlist(self, mock_client):
        wishlist_mod.save_wishlist(
            [{"asin": "B0WATCHEVR", "title": "T", "max_price": 5.0, "added": ""}]
        )
        mock_client.get_products_batch.return_value = [
            make_product(asin="B0WATCHEVR", price=3.0)
        ]

    def test_it_is_deprecated_but_still_works(self, tmp_config, mock_client, looped):
        self._wishlist(mock_client)
        clock = looped(2)

        result = CliRunner().invoke(cli, ["watch", "--every", "30m"])

        assert result.exit_code == 0, result.output
        assert (
            "Warning: 'deals watch --every' is deprecated; use "
            "'deals track run --every 30m'"
        ) in result.stderr
        assert result.stdout.count("Wishlist Price Check") == 2
        assert "Next check in 30m" in result.stdout
        assert clock.sleeps == [1800]

    def test_it_takes_the_run_lock_and_skips_while_a_scheduled_run_works(
        self, tmp_config, mock_client, looped
    ):
        self._wishlist(mock_client)
        looped(2)

        with run_lock():
            result = CliRunner().invoke(cli, ["watch", "--every", "30m"])

        assert result.exit_code == 0, result.output
        assert result.stderr.count("another deals run is in progress") == 2
        assert "Wishlist Price Check" not in result.stdout

    def test_a_failure_backs_off_instead_of_ending_the_loop(
        self, tmp_config, mock_client, looped
    ):
        self._wishlist(mock_client)
        mock_client.get_products_batch.side_effect = RuntimeError("catalog down")
        clock = looped(3)

        result = CliRunner().invoke(cli, ["watch", "--every", "30m"])

        assert result.exit_code == 0, result.output
        assert "check failed (1 in a row): RuntimeError: catalog down" in result.stderr
        assert "check failed (3 in a row)" in result.stderr
        assert clock.sleeps == [1800, 3600]

    def test_a_single_check_is_not_deprecated(self, tmp_config, mock_client):
        self._wishlist(mock_client)

        result = CliRunner().invoke(cli, ["watch"])

        assert result.exit_code == 0
        assert "deprecated" not in result.output
