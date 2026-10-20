"""Webhook targets are checked when they are sent, not only when they are entered."""

from __future__ import annotations

import json

import click
import pytest
from click.testing import CliRunner

from audible_deals import wishlist as wishlist_mod
from audible_deals.cli import cli
from audible_deals.validation import validate_webhook_url
from audible_deals.webhook_client import WebhookClient, WebhookDeliveryError
from audible_deals.webhook_guard import (
    AddressGuard,
    BlockedAddressError,
    is_public_address,
)
from tests.conftest import make_product
from tests.webhook_server import RecordingServer

PUBLIC = "93.184.216.34"


@pytest.fixture
def server():
    recorder = RecordingServer()
    yield recorder
    recorder.close()


class Rebinder:
    """A resolver whose answer changes after the first lookup (DNS rebinding)."""

    def __init__(self, first, later):
        self.first, self.later = first, later
        self.calls: list[tuple[str, int]] = []

    def __call__(self, host, port):
        self.calls.append((host, port))
        return list(self.first if len(self.calls) == 1 else self.later)


def post(client, url, body=b'{"ok": true}'):
    client.post(url, body, {"Content-Type": "application/json"})


class TestWhichAddressesArePublic:
    @pytest.mark.parametrize(
        "address",
        [
            "127.0.0.1",
            "10.1.2.3",
            "172.16.0.9",
            "192.168.1.20",
            "169.254.169.254",  # cloud metadata
            "100.64.0.1",  # carrier-grade NAT
            "0.0.0.0",
            "::1",
            "fe80::1",
            "fc00::1",
            "::ffff:127.0.0.1",  # IPv4 loopback written as IPv6
            "::ffff:10.0.0.1",
            "fe80::1%eth0",
        ],
    )
    def test_private_and_special_addresses_are_refused(self, address):
        assert not is_public_address(address)

    @pytest.mark.parametrize("address", [PUBLIC, "8.8.8.8", "2606:4700:4700::1111"])
    def test_public_addresses_pass(self, address):
        assert is_public_address(address)


class TestAddressGuard:
    def test_one_private_answer_among_public_ones_blocks(self):
        guard = AddressGuard(resolver=lambda host, port: [PUBLIC, "10.0.0.5"])

        with pytest.raises(BlockedAddressError, match="10.0.0.5"):
            guard.addresses("mixed.example", 443)

    def test_the_opt_in_lets_private_addresses_through(self):
        guard = AddressGuard(
            allow_private=True, resolver=lambda host, port: ["192.168.1.7"]
        )

        assert guard.addresses("ntfy.lan", 80) == ["192.168.1.7"]

    def test_no_answer_is_an_error_not_a_pass(self):
        guard = AddressGuard(resolver=lambda host, port: [])

        with pytest.raises(BlockedAddressError, match="Cannot resolve"):
            guard.addresses("nothing.example", 80)

    def test_the_refusal_says_how_to_allow_a_lan_target(self):
        guard = AddressGuard(resolver=lambda host, port: ["192.168.1.7"])

        with pytest.raises(BlockedAddressError, match="webhook-allow-private"):
            guard.addresses("ntfy.lan", 80)


class TestSendTimeCheck:
    def test_loopback_is_refused_by_default_and_nothing_is_sent(self, server):
        sleeps = []
        client = WebhookClient(sleep=sleeps.append, proxies={})

        with pytest.raises(WebhookDeliveryError, match="refused.*non-public"):
            post(client, server.url)

        assert server.requests == []
        assert sleeps == []  # a policy refusal is not retried

    def test_the_opt_in_delivers_to_a_local_server(self, server):
        client = WebhookClient(allow_private=True, proxies={})

        post(client, server.url)

        assert server.bodies() == [{"ok": True}]
        assert server.requests[0]["headers"]["Content-Type"] == "application/json"

    def test_dns_rebinding_is_refused(self, server):
        """Public when the URL is entered, 127.0.0.1 when the webhook is sent."""
        resolver = Rebinder([PUBLIC], ["127.0.0.1"])
        url = f"http://rebind.example:{server.port}/hook"

        validate_webhook_url(url, resolver=resolver)  # entering it: fine
        sleeps = []
        client = WebhookClient(sleep=sleeps.append, resolver=resolver, proxies={})

        with pytest.raises(WebhookDeliveryError, match="refused"):
            post(client, url)

        assert len(resolver.calls) == 2  # validation, then the send
        assert server.requests == []
        assert sleeps == []

    def test_the_connection_goes_to_the_address_that_was_checked(self, server):
        """One lookup per connection: the checked address is the one connected to,
        and the Host header still carries the name."""
        resolver = Rebinder(["127.0.0.1"], ["10.9.9.9"])
        url = f"http://webhook.test:{server.port}/hook"
        client = WebhookClient(allow_private=True, resolver=resolver, proxies={})

        post(client, url)

        assert len(resolver.calls) == 1
        assert [r["headers"]["Host"] for r in server.requests] == [
            f"webhook.test:{server.port}"
        ]

    def test_tls_still_checks_the_certificate_against_the_name(self, monkeypatch):
        """Only the socket is pinned: the TLS server name stays the host name."""
        import ssl

        seen = {}

        class StubContext(ssl.SSLContext):
            def wrap_socket(self, sock, *args, server_hostname=None, **kwargs):
                seen["hostname"] = server_hostname
                seen["peer"] = sock.getpeername()[0]
                raise ssl.SSLError("stop before the handshake")

        server = RecordingServer()
        try:
            client = WebhookClient(
                allow_private=True,
                resolver=lambda host, port: ["127.0.0.1"],
                proxies={},
                sleep=lambda seconds: None,
            )
            # Route the https opener's connections to the plain local server.
            monkeypatch.setattr(
                "audible_deals.webhook_guard.socket.create_connection",
                _redirect_to(server.port),
            )
            handler = next(
                h
                for h in client._opener.handlers
                if h.__class__.__name__ == "_GuardedHTTPSHandler"
            )
            handler._tls_context = StubContext(ssl.PROTOCOL_TLS_CLIENT)
            with pytest.raises(WebhookDeliveryError):
                post(client, "https://secure.example/hook")
        finally:
            server.close()

        assert seen == {"hostname": "secure.example", "peer": "127.0.0.1"}

    def test_a_redirect_to_a_private_address_is_not_followed(self):
        target = RecordingServer()
        redirecting = RecordingServer(status=302, location=target.url)
        try:
            client = WebhookClient(
                allow_private=True, proxies={}, sleep=lambda seconds: None
            )
            with pytest.raises(WebhookDeliveryError, match="Webhook failed"):
                post(client, redirecting.url)
            assert target.requests == []
        finally:
            redirecting.close()
            target.close()


def _redirect_to(port):
    import socket

    real = socket.create_connection

    def connect(address, timeout=None, source_address=None):
        return real(("127.0.0.1", port), timeout, source_address)

    return connect


class TestThroughAProxy:
    @pytest.fixture(autouse=True)
    def _no_proxy_environment(self, monkeypatch):
        """Which hosts bypass the proxy comes from the environment (this machine
        may set NO_PROXY=localhost,...); the tests pass ``proxies=`` themselves."""
        for name in (
            "http_proxy",
            "https_proxy",
            "all_proxy",
            "no_proxy",
            "HTTP_PROXY",
            "HTTPS_PROXY",
            "ALL_PROXY",
            "NO_PROXY",
        ):
            monkeypatch.delenv(name, raising=False)

    @pytest.mark.parametrize(
        "url",
        [
            "http://10.0.0.5/hook",
            "http://169.254.169.254/latest",
            "http://[::1]/hook",
            "http://localhost/hook",
            "http://hooks.localhost/hook",
        ],
    )
    def test_a_target_that_is_private_by_its_name_is_refused_before_the_proxy(
        self, server, url
    ):
        calls = []

        def resolver(host, port):
            calls.append(host)
            return [PUBLIC]

        client = WebhookClient(
            resolver=resolver,
            proxies={"http": f"http://127.0.0.1:{server.port}"},
            sleep=lambda seconds: None,
        )

        with pytest.raises(WebhookDeliveryError, match="refused"):
            post(client, url)

        assert server.requests == []
        assert calls == []  # decided from the name alone, no lookup

    def test_a_proxied_host_is_left_for_the_proxy_to_resolve(self, server):
        """Behind a proxy the proxy does the lookup, often the only one that can."""
        import socket

        calls = []

        def resolver(host, port):
            calls.append(host)
            raise socket.gaierror(-2, "Name or service not known")

        client = WebhookClient(
            resolver=resolver,
            proxies={"http": f"http://127.0.0.1:{server.port}"},
            sleep=lambda seconds: None,
        )

        post(client, "http://only-the-proxy-can-resolve.example/hook")

        assert calls == []
        assert [r["path"] for r in server.requests] == [
            "http://only-the-proxy-can-resolve.example/hook"
        ]

    def test_private_literals_pass_the_proxy_when_the_setting_is_on(self, server):
        client = WebhookClient(
            allow_private=True,
            proxies={"http": f"http://127.0.0.1:{server.port}"},
        )

        post(client, "http://192.168.1.20/ntfy")

        assert [r["path"] for r in server.requests] == ["http://192.168.1.20/ntfy"]

    def test_hosts_in_no_proxy_are_still_resolved_and_checked_here(
        self, server, monkeypatch
    ):
        monkeypatch.setenv("no_proxy", "internal.example")
        monkeypatch.setenv("NO_PROXY", "internal.example")
        client = WebhookClient(
            resolver=lambda host, port: ["10.0.0.5"],
            proxies={"http": f"http://127.0.0.1:{server.port}"},
            sleep=lambda seconds: None,
        )

        with pytest.raises(WebhookDeliveryError, match="non-public address 10.0.0.5"):
            post(client, "http://internal.example/hook")

        assert server.requests == []

    def test_a_public_target_goes_through_the_proxy(self, server):
        client = WebhookClient(
            resolver=lambda host, port: [PUBLIC],
            proxies={"http": f"http://127.0.0.1:{server.port}"},
        )

        post(client, "http://hooks.example/path")

        # An HTTP proxy receives the absolute URL of the target.
        assert [r["path"] for r in server.requests] == ["http://hooks.example/path"]

    def test_no_proxy_hosts_are_not_proxied(self, server, monkeypatch):
        monkeypatch.setenv("no_proxy", "hooks.example")
        monkeypatch.setenv("NO_PROXY", "hooks.example")
        resolver = Rebinder(["127.0.0.1"], ["127.0.0.1"])
        client = WebhookClient(
            allow_private=True,
            resolver=resolver,
            proxies={"http": "http://proxy.invalid:3128"},
        )

        post(client, f"http://hooks.example:{server.port}/hook")

        assert len(server.requests) == 1


class TestDefaultsFromTheConfig:
    def _wishlist_hit(self, mock_client):
        wishlist_mod.save_wishlist(
            [{"asin": "W0HOOK0001", "title": "T", "max_price": 5.0, "added": ""}]
        )
        mock_client.get_products_batch.return_value = [
            make_product(asin="W0HOOK0001", price=3.0)
        ]

    def test_entering_a_loopback_webhook_is_refused_with_the_remedy(self, tmp_config):
        result = CliRunner().invoke(
            cli, ["config", "set", "webhook", "http://127.0.0.1:9/hook"]
        )

        assert result.exit_code == 2
        assert "non-public address 127.0.0.1" in result.stderr
        assert "deals config set webhook-allow-private true" in result.stderr

    def test_config_set_names_the_key_not_a_flag_that_does_not_exist(self, tmp_config):
        runner = CliRunner()

        config = runner.invoke(
            cli, ["config", "set", "webhook", "http://127.0.0.1:9/hook"]
        )
        port = runner.invoke(
            cli, ["config", "set", "webhook", "https://example.com:0/x"]
        )
        scheme = runner.invoke(cli, ["config", "set", "webhook", "ftp://example.com/x"])
        flag = runner.invoke(cli, ["notify", "--webhook", "http://127.0.0.1:9/hook"])

        for result in (config, port, scheme):
            assert result.exit_code == 2
            assert "Invalid value for 'webhook':" in result.stderr
            assert "--webhook" not in result.stderr
        # where it really is an option, the option is named
        assert "Invalid value for '--webhook':" in flag.stderr

    def test_the_setting_allows_it_and_says_what_it_means(self, tmp_config):
        runner = CliRunner()

        first = runner.invoke(cli, ["config", "set", "webhook-allow-private", "true"])
        second = runner.invoke(
            cli, ["config", "set", "webhook", "http://192.168.1.20/ntfy/deals"]
        )

        assert first.exit_code == 0, first.output
        assert "Note: Webhooks may now reach servers on your own network" in (
            first.stderr
        )
        assert second.exit_code == 0, second.output

    def test_notify_delivers_to_a_local_server_only_with_the_setting(
        self, tmp_config, mock_client, server
    ):
        self._wishlist_hit(mock_client)
        runner = CliRunner()

        refused = runner.invoke(cli, ["notify", "--webhook", server.url])
        assert refused.exit_code == 2
        assert server.requests == []

        runner.invoke(cli, ["config", "set", "webhook-allow-private", "true"])
        sent = runner.invoke(cli, ["notify", "--webhook", server.url])

        assert sent.exit_code == 0, sent.output
        body = json.loads(server.requests[0]["body"])
        assert body["count"] == 1 and body["deals"][0]["asin"] == "W0HOOK0001"

    @pytest.mark.parametrize(
        "url",
        [
            "https://example.com:99999/x",
            "https://example.com:abc/x",
            "https://example.com:0/x",
            "https://example.com:-1/x",
        ],
    )
    def test_a_bad_port_is_a_usage_error_not_an_unexpected_one(
        self, tmp_config, mock_client, url
    ):
        with pytest.raises(click.BadParameter, match="invalid port"):
            validate_webhook_url(url)

        runner = CliRunner()
        notify = runner.invoke(cli, ["notify", "--webhook", url])
        config = runner.invoke(cli, ["config", "set", "webhook", url])

        for result in (notify, config):
            assert result.exit_code == 2, result.output
            assert "invalid port" in result.stderr
            assert "Unexpected" not in result.stderr
            assert "ValueError" not in result.stderr

    def test_the_default_is_off(self, tmp_config):
        with pytest.raises(click.BadParameter, match="non-public"):
            validate_webhook_url("http://127.0.0.1/hook")
