"""Rotation uses the proven bootstrap fence and never creates a Job in plan mode."""
import copy
import io
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import run_bootstrap as runner
from test_runner import Cluster, outputs


def rotation():
    return {'environment': 'dev', 'service': 'catalog', 'backend': 'mysql',
            'operation_id': '26c1f449-8db7-4d65-ae18-956c04eab842', 'ack_maintenance': False, 'rollback': False,
            'finalize': False, 'ack_consumers_reloaded': False}


class RotationRunnerTests(unittest.TestCase):
    def cli(self, directory):
        path = Path(directory) / 'outputs.json'
        path.write_text(json.dumps(outputs()), encoding='utf-8')
        spec = rotation()
        return ['--terraform-output', str(path), '--context', 'explicit-context', '--operation', 'rotate',
                *runner.rotation_arguments(spec)]

    def test_invalid_service_pairs_and_shell_arguments_are_rejected(self):
        for key, value in (('service', 'ui'), ('backend', 'rabbitmq'), ('environment', 'other'),
                           ('operation_id', 'uuid; echo bad'), ('operation_id', None), ('rollback', 'false')):
            candidate = rotation()
            candidate[key] = value
            with self.subTest(key=key, value=value), self.assertRaises(runner.RunnerError):
                runner.rotation_arguments(candidate)

    def test_plan_never_initializes_kubectl_or_creates_a_job(self):
        planned = dict(rotation(), account_id=outputs()['account_id'], status='planned',
                       maintenance_required=False, secret_count=1, plan_steps=['stage_pending_version'],
                       password='SECRET_NOT_FOR_REPORT')
        with tempfile.TemporaryDirectory() as temporary, patch.object(runner, 'Kubectl') as kubectl, \
                patch.object(runner.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, json.dumps(planned), '')) as child, \
                patch('sys.stdout', new_callable=io.StringIO) as output:
            code = runner.main(self.cli(temporary))
            self.assertEqual(code, 0)
            self.assertEqual(json.loads(output.getvalue())['status'], 'planned')
            self.assertFalse(json.loads(output.getvalue())['changed'])
            self.assertEqual(json.loads(output.getvalue())['plan']['secret_count'], 1)
            self.assertNotIn('SECRET_NOT_FOR_REPORT', output.getvalue())
            self.assertNotIn('--execute', child.call_args.args[0])
            kubectl.assert_not_called()

    def test_finalize_needs_consumer_ack_and_cannot_be_combined_with_rollback(self):
        spec = rotation()
        spec['finalize'] = True
        with self.assertRaises(runner.RunnerError):
            runner.rotation_arguments(spec)
        spec['ack_consumers_reloaded'] = True
        self.assertIn('--finalize', runner.rotation_arguments(spec))
        spec['rollback'] = True
        with self.assertRaises(runner.RunnerError):
            runner.rotation_arguments(spec)

    def test_failed_plan_never_prints_raw_process_output(self):
        with tempfile.TemporaryDirectory() as temporary, patch.object(runner, 'Kubectl') as kubectl, \
                patch.object(runner.subprocess, 'run', return_value=subprocess.CompletedProcess([], 1, 'SECRET', 'SECRET')), \
                patch('sys.stdout', new_callable=io.StringIO) as output:
            self.assertEqual(runner.main(self.cli(temporary)), 1)
            self.assertNotIn('SECRET', output.getvalue())
            kubectl.assert_not_called()

    def test_execute_shares_the_backend_account_lease(self):
        client = Cluster()
        client.logs = json.dumps(dict(rotation(), schema_version=1, operation='credential_rotation',
                                      status='success', phase='complete', old_password_retained=True, password='NEVER_PRINT_THIS'))
        report = {}
        with patch.object(runner, 'resources', wraps=runner.resources) as manifest:
            runner.run(outputs(), client, 'backend-bootstrap-fixture', 90, report, rotation=rotation())
        self.assertEqual(manifest.call_args.kwargs['rotation'], rotation())
        self.assertEqual(report['lease_name'], 'backend-bootstrap-111122223333')
        self.assertTrue(report['lease_released'])
        self.assertEqual(report['rotation_result']['phase'], 'complete')
        self.assertNotIn('NEVER_PRINT_THIS', json.dumps(report))
        calls = [call for call in client.calls if call[0][0] in ('apply', 'create', 'replace')]
        self.assertEqual(calls[0][1]['kind'], 'Lease')

    def test_empty_or_wrong_operation_evidence_cannot_report_rotation_success(self):
        for evidence in ('', json.dumps(dict(rotation(), schema_version=1, operation='credential_rotation',
                           status='success', phase='complete', operation_id='aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa'))):
            client, report = Cluster(), {}
            client.logs = evidence
            with self.subTest(evidence=evidence), self.assertRaises(runner.RunnerError) as error:
                runner.run(outputs(), client, 'backend-bootstrap-fixture', 90, report, rotation=rotation())
            self.assertEqual(error.exception.code, 'rotation_evidence_missing')
            self.assertTrue(report['lease_released'])

    def test_single_password_execution_requires_maintenance_ack_before_kubernetes(self):
        with tempfile.TemporaryDirectory() as temporary, patch.object(runner, 'Kubectl') as kubectl, \
                patch('sys.stdout', new_callable=io.StringIO):
            args = self.cli(temporary)
            args[args.index('mysql')] = 'opensearch'
            self.assertEqual(runner.main(args + ['--execute']), 1)
            kubectl.assert_not_called()

    def test_rotation_manifest_contains_only_nonsensitive_parameters_and_pinned_runtime(self):
        manifest = runner.resources(outputs(), 'backend-bootstrap-fixture', rotation=rotation())
        job = next(item for item in manifest['items'] if item['kind'] == 'Job')
        config = next(item for item in manifest['items'] if item['kind'] == 'ConfigMap')
        container = job['spec']['template']['spec']['containers'][0]
        self.assertEqual(container['image'], runner.IMAGE)
        self.assertIn('rotate_credentials.py', config['data'])
        self.assertIn('--execute', container['args'][0])
        self.assertIn(rotation()['operation_id'], container['args'][0])
        self.assertIn('/work/scripts/infra', [item['mountPath'] for item in container['volumeMounts']])
        self.assertFalse(any(item['name'].startswith('AWS_ACCESS') for item in container['env']))


if __name__ == '__main__':
    unittest.main()
