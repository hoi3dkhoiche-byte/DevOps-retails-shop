"""Accept dev through the deployed Service port, including GitOps port 8080."""
import json
import unittest
from unittest import mock

import accept_dev


CONTEXT = "arn:aws:eks:ap-southeast-1:123456789012:cluster/test-cluster"


class ServicePortTests(unittest.TestCase):
    def service(self, ports):
        return json.dumps({"spec": {"ports": ports}}).encode()

    def test_named_http_port_takes_precedence_over_other_tcp_ports(self):
        response = self.service([{"name": "metrics", "port": 9090},
                                 {"name": "http", "port": 8080, "protocol": "TCP"}])
        with mock.patch.object(accept_dev, "run", return_value=response) as run:
            self.assertEqual(accept_dev.service_http_port(CONTEXT, "ui-active"), 8080)
        run.assert_called_once_with(accept_dev.kubectl(CONTEXT, "get", "service/ui-active", "-o", "json"))

    def test_a_unique_tcp_port_supports_both_legacy_and_gitops_services(self):
        for port in (80, 8080):
            with self.subTest(port=port), mock.patch.object(
                    accept_dev, "run", return_value=self.service([{"port": port}])):
                self.assertEqual(accept_dev.service_http_port(CONTEXT, "catalog"), port)

    def test_missing_ambiguous_non_tcp_and_invalid_ports_fail(self):
        responses = [b"{}", self.service([]), self.service({"port": 8080}),
                     self.service([{"port": 8080}, {"port": 9090}]),
                     self.service([{"name": "http", "port": 8080}, {"name": "http", "port": 8081}]),
                     self.service([{"port": 8080, "protocol": "UDP"}]),
                     self.service([{"name": "http", "port": 8080, "protocol": "UDP"},
                                   {"name": "metrics", "port": 9090}])]
        responses.extend(self.service([{"name": "http", "port": port}])
                         for port in (None, True, 0, 65536, "8080"))
        for response in responses:
            with self.subTest(response=response), mock.patch.object(accept_dev, "run", return_value=response):
                with self.assertRaises(ValueError):
                    accept_dev.service_http_port(CONTEXT, "orders")

    def test_forward_uses_active_ui_and_carts_service_ports_and_stops_the_process(self):
        for service, name in (("ui", "ui-active"), ("cart", "carts")):
            with self.subTest(service=service), \
                    mock.patch.object(accept_dev, "run", return_value=self.service([{"name": "http", "port": 8080}])), \
                    mock.patch.object(accept_dev.socket, "socket") as listener, \
                    mock.patch.object(accept_dev.socket, "create_connection"), \
                    mock.patch.object(accept_dev.subprocess, "Popen") as popen:
                listener.return_value.__enter__.return_value.getsockname.return_value = ("127.0.0.1", 43210)
                process = popen.return_value
                process.poll.return_value = None
                with accept_dev.forward(CONTEXT, service) as url:
                    self.assertEqual(url, "http://127.0.0.1:43210")
                    expected = accept_dev.kubectl(CONTEXT, "port-forward", "--address", "127.0.0.1",
                                                  "service/" + name, "43210:8080")
                    self.assertEqual(popen.call_args.args[0], expected)
                process.terminate.assert_called_once_with()
                process.wait.assert_called_once_with(timeout=10)

    def test_invalid_service_does_not_start_a_forward_or_bind_a_local_port(self):
        with mock.patch.object(accept_dev, "run", return_value=self.service([])), \
                mock.patch.object(accept_dev.socket, "socket") as listener, \
                mock.patch.object(accept_dev.subprocess, "Popen") as popen:
            with self.assertRaises(ValueError):
                with accept_dev.forward(CONTEXT, "ui"):
                    self.fail("Invalid Service must not yield a forward")
        listener.assert_not_called()
        popen.assert_not_called()


if __name__ == "__main__":
    unittest.main()
