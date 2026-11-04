import copy
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest

import yaml

HELPERS = Path(__file__).resolve().parents[1] / 'helpers'
sys.path.insert(0, str(HELPERS))
import prepare_candidate as candidate
import prepare_rotation_candidate as rotation


def report():
    return {'schema_version': 1, 'operation': 'credential_rotation', 'status': 'success',
            'account_id': '111122223333', 'cluster_name': 'retail-lab-eks',
            'finished_at': '2026-11-04T10:00:00+00:00',
            'rotation': {'environment': 'dev', 'service': 'catalog', 'backend': 'mysql',
                         'operation_id': '26c1f449-8db7-4d65-ae18-956c04eab842', 'rollback': False}}


class RotationCandidateTests(unittest.TestCase):
    def test_ready_eso_before_rotation_is_insufficient(self):
        resource = {'metadata': {'namespace': 'retail-dev', 'name': 'catalog-mysql'},
                    'status': {'conditions': [{'type': 'Ready', 'status': 'True'}],
                               'refreshTime': '2026-11-04T09:59:59Z'}}
        self.assertFalse(rotation.refreshed(resource, report()))
        resource['status']['refreshTime'] = '2026-11-04T10:00:01Z'
        self.assertTrue(rotation.refreshed(resource, report()))
        resource['status']['conditions'][0]['status'] = 'False'
        self.assertFalse(rotation.refreshed(resource, report()))
        resource['metadata']['namespace'] = 'retail-prod'
        with self.assertRaises(candidate.CandidateError):
            rotation.refreshed(resource, report())

    def fixture(self, directory):
        local = Path(directory).resolve()
        root, gitops = local / 'candidate', local / 'gitops'
        root.mkdir()
        gitops.mkdir()
        (root / 'config.yaml').write_text(yaml.safe_dump({'accountId': '111122223333',
            'clusterName': 'retail-lab-eks', 'deployment': {'enabled': True}}), encoding='utf-8')
        for env in ('dev', 'prod'):
            for service in ('catalog', 'cart'):
                path = root / 'environments' / env / (service + '.yaml')
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text('secretRevision: initial\nreplicaCount: 1\n', encoding='utf-8')
        owner = {'schema_version': 1, 'source_dir': str(gitops), 'candidate_dir': str(root), 'files': candidate.file_hashes(root)}
        (root / candidate.MANIFEST).write_text(json.dumps(owner), encoding='utf-8')
        return root, gitops, local

    def test_only_selected_service_environment_changes_and_rerun_is_unchanged(self):
        with tempfile.TemporaryDirectory() as directory:
            root, gitops, local = self.fixture(directory)
            before = candidate.file_hashes(root)
            result = rotation.update_revision(root, gitops, local, report())
            after = candidate.file_hashes(root)
            self.assertTrue(result['changed'])
            self.assertEqual([key for key in before if before[key] != after[key]], ['environments/dev/catalog.yaml'])
            self.assertFalse(rotation.update_revision(root, gitops, local, report())['changed'])
            candidate.existing_manifest(root, gitops)

    def test_wrong_account_failed_plan_and_wrong_service_cannot_generate_rollout(self):
        for field, bad in (('account_id', '999988887777'), ('status', 'planned'), ('status', 'failed')):
            with self.subTest(field=field, bad=bad), tempfile.TemporaryDirectory() as directory:
                root, gitops, local = self.fixture(directory)
                value = report()
                value[field] = bad
                before = candidate.file_hashes(root)
                with self.assertRaises(candidate.CandidateError):
                    rotation.update_revision(root, gitops, local, value)
                self.assertEqual(before, candidate.file_hashes(root))

    def test_dirty_candidate_is_never_overwritten(self):
        with tempfile.TemporaryDirectory() as directory:
            root, gitops, local = self.fixture(directory)
            (root / 'environments/dev/catalog.yaml').write_text('secretRevision: operator-edit\n', encoding='utf-8')
            with self.assertRaises(candidate.CandidateError):
                rotation.update_revision(root, gitops, local, report())

    def test_rollback_revision_is_distinct_from_the_forward_rotation(self):
        with tempfile.TemporaryDirectory() as directory:
            root, gitops, local = self.fixture(directory)
            rotation.update_revision(root, gitops, local, report())
            value = report()
            value['rotation']['rollback'] = True
            result = rotation.update_revision(root, gitops, local, value)
            self.assertTrue(result['changed'])
            self.assertTrue(result['secret_revision'].startswith('rollback-'))


if __name__ == '__main__':
    unittest.main()
