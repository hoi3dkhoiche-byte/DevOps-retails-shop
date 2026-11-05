"""Rotation state-machine checks without AWS or private network access."""
import copy
import io
import json
import struct
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, Mock, patch
from urllib.parse import unquote, urlsplit

import rotate_credentials as rotation
import bootstrap_backends as bootstrap
from test_runner import outputs

TOKEN = '123e4567-e89b-42d3-a456-426614174000'
OLD = 'original-password-private-0123456789'


class Store:
    def __init__(self, selection):
        self.values, self.labels, self.names = {}, {}, {}
        self.writes = []
        self.fail_promote = None
        for index, name in enumerate(rotation.identifiers(selection)):
            arn = f"arn:aws:secretsmanager:ap-southeast-1:111122223333:secret:{name}-Aa000{index}"
            self.names[name] = arn
            env, svc, backend = (selection[key] for key in ('environment', 'service', 'backend'))
            if backend == 'mysql':
                value = {'username': svc + '_' + env, 'password': OLD, 'host': outputs()['mysql_endpoint'],
                         'port': 3306, 'database': svc + '_' + env}
            elif backend == 'rabbitmq':
                value = {'username': 'orders-' + env, 'password': OLD, 'vhost': 'retail-' + env,
                         'addresses': outputs()['backend_endpoints']['RabbitMQEndpoint']}
            elif backend == 'opensearch':
                value = {'username': 'catalog-' + env, 'password': OLD,
                         'endpoint': 'https://' + outputs()['backend_endpoints']['SearchEndpoint']}
            elif index == 0:
                value = {'username': 'checkout-' + env, 'password': OLD}
            else:
                value = {'url': f"rediss://checkout-{env}:{OLD}@{outputs()['backend_endpoints']['RedisEndpoint']}:6379"}
            self.values[arn] = {'old-version-' + str(index): value}
            self.labels[arn] = {'old-version-' + str(index): ['AWSCURRENT']}

    def arn(self, name):
        return self.names.get(name, name)

    def describe_secret(self, SecretId):
        arn = self.arn(SecretId)
        return {'ARN': arn, 'VersionIdsToStages': copy.deepcopy(self.labels[arn])}

    def get_secret_value(self, SecretId, VersionId):
        return {'SecretString': json.dumps(self.values[self.arn(SecretId)][VersionId])}

    def put_secret_value(self, SecretId, ClientRequestToken, VersionStages, SecretString):
        arn = self.arn(SecretId)
        self.values[arn][ClientRequestToken] = json.loads(SecretString)
        self.labels[arn][ClientRequestToken] = list(VersionStages)
        self.writes.append(('pending', arn))

    def update_secret_version_stage(self, SecretId, VersionStage, RemoveFromVersionId=None, MoveToVersionId=None):
        arn = self.arn(SecretId)
        if self.fail_promote == arn and VersionStage == 'AWSCURRENT':
            self.fail_promote = None
            raise RuntimeError('SDK diagnostic contains NEVER_LOG_PASSWORD')
        if VersionStage == 'AWSCURRENT':
            for labels in self.labels[arn].values():
                if 'AWSPREVIOUS' in labels:
                    labels.remove('AWSPREVIOUS')
            self.labels[arn][RemoveFromVersionId].append('AWSPREVIOUS')
        if RemoveFromVersionId:
            self.labels[arn][RemoveFromVersionId].remove(VersionStage)
        if MoveToVersionId:
            self.labels[arn][MoveToVersionId].append(VersionStage)
        self.writes.append(('stage', arn))


class Adapter:
    def __init__(self):
        self.passwords = {OLD}
        self.applies = []
        self.fail_apply = False
        self.fail_check = False

    def check(self, selection, credential):
        if self.fail_check:
            raise RuntimeError('NEVER_LOG_PASSWORD')
        return credential['password'] in self.passwords

    def apply(self, selection, desired, previous):
        self.applies.append(copy.deepcopy(selection))
        if selection['backend'] in {'mysql', 'redis'}:
            self.passwords.add(desired['password'])
        else:
            self.passwords = {desired['password']}
        if self.fail_apply:
            self.fail_apply = False
            raise RuntimeError('Backend response lost NEVER_LOG_PASSWORD')

    def finalize(self, selection, current):
        self.passwords = {current['password']}

    def rollback(self, selection, original, rejected):
        self.apply(selection, original, rejected)
        if selection['backend'] in {'mysql', 'redis'}:
            self.finalize(selection, original)


class RotationTests(unittest.TestCase):
    def run_rotation(self, backend='mysql', service='catalog', env='dev', maintenance=False):
        selection = rotation.target(env, service, backend, TOKEN)
        store, adapter, report = Store(selection), Adapter(), {}
        rotation.rotate(outputs(), store, adapter, selection, execute=True, maintenance=maintenance, report=report)
        return selection, store, adapter, report

    def test_plan_is_metadata_only_without_credentials_or_backend_access(self):
        selection = rotation.target('prod', 'orders', 'rabbitmq', TOKEN)
        store = Store(selection)
        with patch.object(store, 'get_secret_value', side_effect=AssertionError('secret read')):
            report = rotation.rotate(outputs(), store, Mock(spec=[]), selection)
        self.assertEqual(report['status'], 'planned')
        self.assertFalse(report['changed'])
        self.assertTrue(report['maintenance_required'])
        self.assertEqual(store.writes, [])

    def test_mysql_stages_verifies_and_promotes_only_one_environment(self):
        selection, store, adapter, report = self.run_rotation(env='prod')
        arn = next(iter(store.values))
        self.assertIn('AWSCURRENT', store.labels[arn][TOKEN])
        self.assertNotIn('AWSPENDING', store.labels[arn][TOKEN])
        self.assertIn('AWSPREVIOUS', store.labels[arn]['old-version-0'])
        self.assertEqual(adapter.applies, [selection])
        self.assertEqual(report['status'], 'success')
        self.assertTrue(report['old_password_retained'])
        self.assertNotIn(OLD, json.dumps(report))
        self.assertTrue(all('/prod/' in name for name in store.names))

    def test_opensearch_rotation_uses_complex_password_and_reuses_it_on_resume(self):
        with patch.object(bootstrap.secrets, 'choice', side_effect=lambda alphabet: alphabet[0]), \
                patch.object(bootstrap.secrets, 'randbelow', return_value=0):
            selection, store, adapter, _ = self.run_rotation(backend='opensearch', maintenance=True)
        arn = next(iter(store.values))
        value = store.values[arn][TOKEN]['password']
        self.assertTrue(len(value) == 48)
        self.assertTrue(any(character.isupper() for character in value))
        self.assertTrue(any(character.islower() for character in value))
        self.assertTrue(any(character.isdigit() for character in value))
        self.assertTrue(any(not character.isalnum() for character in value))
        before = copy.deepcopy(store.values)
        with patch.object(rotation, 'generate_password', side_effect=AssertionError('Unexpected password regeneration')):
            rotation.rotate(outputs(), store, adapter, selection, execute=True, maintenance=True)
        self.assertTrue(before == store.values, 'Resume must keep the staged password')

    def test_all_mysql_services_are_supported_but_invalid_cross_backend_is_rejected(self):
        for service in ('catalog', 'cart', 'orders'):
            self.run_rotation(service=service)
        for service, backend in (('ui', 'mysql'), ('orders', 'redis'), ('checkout', 'mysql')):
            with self.subTest(service=service), self.assertRaises(rotation.RotationError):
                rotation.target('dev', service, backend, TOKEN)

    def test_single_password_backends_require_explicit_maintenance_before_any_write(self):
        for backend, service in (('rabbitmq', 'orders'), ('opensearch', 'catalog')):
            selection = rotation.target('dev', service, backend, TOKEN)
            store, adapter = Store(selection), Adapter()
            with self.assertRaisesRegex(rotation.RotationError, 'maintenance_ack_required'):
                rotation.rotate(outputs(), store, adapter, selection, execute=True)
            self.assertEqual(store.writes, [])
            self.assertEqual(adapter.applies, [])
            self.run_rotation(backend=backend, service=service, maintenance=True)

    def test_rerun_same_operation_does_not_generate_or_apply_a_second_password(self):
        selection, store, adapter, _ = self.run_rotation()
        before = copy.deepcopy(store.values)
        report = rotation.rotate(outputs(), store, adapter, selection, execute=True)
        self.assertEqual(before, store.values)
        self.assertEqual(len(adapter.applies), 1)
        self.assertFalse(report['changed'])

    def test_lost_backend_response_resumes_by_authentication_without_reapplying(self):
        selection = rotation.target('dev', 'catalog', 'mysql', TOKEN)
        store, adapter, report = Store(selection), Adapter(), {}
        adapter.fail_apply = True
        with self.assertRaises(RuntimeError):
            rotation.rotate(outputs(), store, adapter, selection, execute=True, report=report)
        self.assertTrue(report['backend_may_have_changed'])
        arn = next(iter(store.values))
        self.assertIn('AWSCURRENT', store.labels[arn]['old-version-0'])
        rotation.rotate(outputs(), store, adapter, selection, execute=True)
        self.assertEqual(len(adapter.applies), 1)

    def test_failed_promotion_retains_pending_for_retry_and_never_logs_secret(self):
        selection = rotation.target('dev', 'orders', 'rabbitmq', TOKEN)
        store, adapter, report = Store(selection), Adapter(), {}
        arn = next(iter(store.values))
        store.fail_promote = arn
        with self.assertRaises(RuntimeError):
            rotation.rotate(outputs(), store, adapter, selection, execute=True, maintenance=True, report=report)
        self.assertIn('AWSPENDING', store.labels[arn][TOKEN])
        self.assertTrue(report['changed'])
        self.assertNotIn('NEVER_LOG', json.dumps(report))
        rotation.rotate(outputs(), store, adapter, selection, execute=True, maintenance=True)
        self.assertEqual(len(adapter.applies), 1)

    def test_redis_partial_promotion_recovers_both_secrets_with_the_same_password(self):
        selection = rotation.target('dev', 'checkout', 'redis', TOKEN)
        store, adapter = Store(selection), Adapter()
        platform, application = list(store.values)
        store.fail_promote = application
        with self.assertRaises(RuntimeError):
            rotation.rotate(outputs(), store, adapter, selection, execute=True)
        self.assertIn('AWSCURRENT', store.labels[platform][TOKEN])
        self.assertIn('AWSCURRENT', store.labels[application]['old-version-1'])
        result = rotation.rotate(outputs(), store, adapter, selection, execute=True)
        self.assertEqual(result['status'], 'success')
        self.assertTrue(store.values[platform][TOKEN]['password'] ==
                        unquote(urlsplit(store.values[application][TOKEN]['url']).password),
                        'The Redis URI must decode to the same password as the platform secret')
        self.assertEqual(len(adapter.applies), 1)

    def test_redis_can_rollback_if_only_platform_pending_was_staged(self):
        selection = rotation.target('dev', 'checkout', 'redis', TOKEN)
        store, adapter = Store(selection), Adapter()
        platform, application = list(store.values)
        original_put = store.put_secret_value
        def fail_application(**kwargs):
            if kwargs['SecretId'] == application:
                raise RuntimeError('fixture staging failed')
            return original_put(**kwargs)
        with patch.object(store, 'put_secret_value', side_effect=fail_application), self.assertRaises(RuntimeError):
            rotation.rotate(outputs(), store, adapter, selection, execute=True)
        result = rotation.rotate(outputs(), store, adapter, selection, execute=True, rollback=True)
        self.assertEqual(result['status'], 'success')
        self.assertNotIn('AWSPENDING', store.labels[platform][TOKEN])
        self.assertNotIn(TOKEN, store.values[application])
        self.assertEqual(adapter.applies, [])
        self.assertTrue(all(any('RETAIL_ROLLED_BACK' in labels for labels in stages.values())
                            for stages in store.labels.values()))

    def test_rollback_restores_the_exact_previous_single_password_and_secret_version(self):
        selection, store, adapter, _ = self.run_rotation(backend='rabbitmq', service='orders', maintenance=True)
        result = rotation.rotate(outputs(), store, adapter, selection, execute=True, maintenance=True, rollback=True)
        arn = next(iter(store.values))
        self.assertIn('AWSCURRENT', store.labels[arn]['old-version-0'])
        self.assertEqual(adapter.passwords, {OLD})
        self.assertTrue(result['rollback'])
        with self.assertRaisesRegex(rotation.RotationError, 'operation_already_rolled_back'):
            rotation.rotate(outputs(), store, adapter, selection, execute=True, maintenance=True)

    def test_dual_password_rollback_retires_failed_password_and_cannot_be_resumed_forward(self):
        for backend, service in (('mysql', 'catalog'), ('redis', 'checkout')):
            with self.subTest(backend=backend):
                selection, store, adapter, _ = self.run_rotation(backend=backend, service=service)
                failed = store.values[next(iter(store.values))][TOKEN]['password']
                self.assertIn(OLD, adapter.passwords, 'The old secondary password is still accepted before rollback')
                result = rotation.rotate(outputs(), store, adapter, selection, execute=True, rollback=True)
                self.assertEqual(adapter.passwords, {OLD})
                self.assertNotIn(failed, adapter.passwords)
                self.assertFalse(result['old_password_retained'])
                for arn, stages in store.labels.items():
                    self.assertIn('RETAIL_ROLLED_BACK', stages[TOKEN])
                with self.assertRaisesRegex(rotation.RotationError, 'operation_already_rolled_back'):
                    rotation.rotate(outputs(), store, adapter, selection, execute=True)
                again = rotation.rotate(outputs(), store, adapter, selection, execute=True, rollback=True)
                self.assertFalse(again['changed'])

    def test_mysql_primary_is_restored_so_a_later_rotation_keeps_consumers_password(self):
        class PrimaryAdapter(Adapter):
            def __init__(self):
                super().__init__()
                self.primary, self.secondary = OLD, None

            def apply(self, selection, desired, previous):
                self.applies.append(copy.deepcopy(selection))
                self.secondary, self.primary = self.primary, desired['password']
                self.passwords = {self.primary, self.secondary}

            def finalize(self, selection, current):
                self.secondary = None
                self.passwords = {self.primary}

        selection = rotation.target('dev', 'catalog', 'mysql', TOKEN)
        store, adapter = Store(selection), PrimaryAdapter()
        rotation.rotate(outputs(), store, adapter, selection, execute=True)
        self.assertNotEqual(adapter.primary, OLD)
        rotation.rotate(outputs(), store, adapter, selection, execute=True, rollback=True)
        self.assertEqual(adapter.primary, OLD)
        next_selection = dict(selection, operation_id='123e4567-e89b-42d3-a456-426614174001')
        rotation.rotate(outputs(), store, adapter, next_selection, execute=True)
        self.assertIn(OLD, adapter.passwords, 'A later rotation must retain the password consumers use after rollback')

    def test_rollback_does_not_publish_if_failed_password_remains_authorized(self):
        selection, store, adapter, _ = self.run_rotation()
        with patch.object(adapter, 'rollback'), self.assertRaisesRegex(rotation.RotationError, 'rolled_back_password_still_accepted'):
            rotation.rotate(outputs(), store, adapter, selection, execute=True, rollback=True)
        arn = next(iter(store.values))
        self.assertIn('AWSCURRENT', store.labels[arn][TOKEN])

    def test_lost_rollback_response_resumes_without_reapplying_backend_change(self):
        selection, store, adapter, _ = self.run_rotation()
        original = adapter.rollback
        def lost_response(*args):
            original(*args)
            raise RuntimeError('fixture response lost')
        with patch.object(adapter, 'rollback', side_effect=lost_response), self.assertRaises(RuntimeError):
            rotation.rotate(outputs(), store, adapter, selection, execute=True, rollback=True)
        self.assertEqual(adapter.passwords, {OLD})
        with patch.object(adapter, 'rollback', side_effect=AssertionError('unnecessary repeated rollback')):
            report = rotation.rotate(outputs(), store, adapter, selection, execute=True, rollback=True)
        self.assertEqual(report['status'], 'success')
        self.assertIn('AWSCURRENT', next(iter(store.labels.values()))['old-version-0'])

    def test_third_party_version_conflict_is_never_overwritten(self):
        selection, store, adapter, _ = self.run_rotation()
        arn = next(iter(store.values))
        store.labels[arn][TOKEN] = ['AWSPREVIOUS']
        store.labels[arn]['third-party-version'] = ['AWSCURRENT']
        store.values[arn]['third-party-version'] = copy.deepcopy(store.values[arn][TOKEN])
        with self.assertRaisesRegex(rotation.RotationError, 'rotation_version_conflict'):
            rotation.rotate(outputs(), store, adapter, selection, execute=True)

    def test_another_pending_operation_is_not_stolen(self):
        selection = rotation.target('dev', 'catalog', 'mysql', TOKEN)
        store, adapter = Store(selection), Adapter()
        arn = next(iter(store.values))
        store.labels[arn]['other-operation'] = ['AWSPENDING']
        with self.assertRaisesRegex(rotation.RotationError, 'another_rotation_pending'):
            rotation.rotate(outputs(), store, adapter, selection, execute=True)
        self.assertEqual(store.writes, [])

    def test_published_credential_drift_fails_before_any_reapply(self):
        selection, store, adapter, _ = self.run_rotation()
        adapter.passwords = {'external-drift'}
        with self.assertRaisesRegex(rotation.RotationError, 'published_credential_rejected'):
            rotation.rotate(outputs(), store, adapter, selection, execute=True)
        self.assertEqual(len(adapter.applies), 1)

    def test_wrong_account_secret_and_secret_target_are_rejected(self):
        selection = rotation.target('dev', 'catalog', 'mysql', TOKEN)
        store, adapter = Store(selection), Adapter()
        arn = next(iter(store.values))
        store.values[arn]['old-version-0']['database'] = 'catalog_prod'
        with self.assertRaisesRegex(rotation.RotationError, 'invalid_secret_target'):
            rotation.rotate(outputs(), store, adapter, selection, execute=True)
        self.assertEqual(store.writes, [])
        with self.assertRaisesRegex(rotation.RotationError, 'wrong_secret_target'):
            rotation.metadata(store, next(iter(store.names)), '999988887777')

    def test_invalid_operation_ids_are_rejected(self):
        for operation_id in ('', 'latest', '../password', '1234567890'):
            with self.subTest(operation_id=operation_id), self.assertRaises(rotation.RotationError):
                rotation.target('dev', 'catalog', 'mysql', operation_id)

    def test_finalize_requires_consumer_ack_and_published_operation_before_writes(self):
        selection, store, adapter, _ = self.run_rotation()
        before = len(store.writes)
        with self.assertRaisesRegex(rotation.RotationError, 'consumers_reload_ack_required'):
            rotation.rotate(outputs(), store, adapter, selection, execute=True, finalize=True)
        self.assertIn(OLD, adapter.passwords)
        self.assertEqual(len(store.writes), before)
        with self.assertRaisesRegex(rotation.RotationError, 'conflicting_rotation_actions'):
            rotation.rotate(outputs(), store, adapter, selection, execute=True, finalize=True, rollback=True)

    def test_second_operation_cannot_replace_password_before_prior_handoff_is_finalized(self):
        selection, store, adapter, _ = self.run_rotation()
        writes = len(store.writes)
        next_selection = dict(selection, operation_id='123e4567-e89b-42d3-a456-426614174001')
        with self.assertRaisesRegex(rotation.RotationError, 'previous_rotation_not_finalized'):
            rotation.rotate(outputs(), store, adapter, next_selection, execute=True)
        self.assertEqual(len(store.writes), writes)
        rotation.rotate(outputs(), store, adapter, selection, execute=True, finalize=True, consumers_reloaded=True)
        result = rotation.rotate(outputs(), store, adapter, next_selection, execute=True)
        self.assertEqual(result['status'], 'success')

    def test_finalize_retires_old_password_and_cannot_rollback_or_reapply_it(self):
        for backend, service in (('mysql', 'catalog'), ('redis', 'checkout')):
            selection, store, adapter, _ = self.run_rotation(backend=backend, service=service)
            result = rotation.rotate(outputs(), store, adapter, selection, execute=True, finalize=True, consumers_reloaded=True)
            self.assertNotIn(OLD, adapter.passwords)
            self.assertFalse(result['old_password_retained'])
            self.assertEqual(result['phase'], 'finalized')
            again = rotation.rotate(outputs(), store, adapter, selection, execute=True, finalize=True, consumers_reloaded=True)
            self.assertFalse(again['changed'])
            with self.assertRaisesRegex(rotation.RotationError, 'finalized_rotation_cannot_rollback'):
                rotation.rotate(outputs(), store, adapter, selection, execute=True, rollback=True)

    def test_finalize_of_an_unpublished_new_operation_never_stages_a_password(self):
        selection = rotation.target('dev', 'catalog', 'mysql', TOKEN)
        store, adapter = Store(selection), Adapter()
        with self.assertRaisesRegex(rotation.RotationError, 'rotation_not_published'):
            rotation.rotate(outputs(), store, adapter, selection, execute=True, finalize=True, consumers_reloaded=True)
        self.assertEqual(store.writes, [])

    def test_partial_redis_finalize_blocks_next_operation_before_any_new_staging(self):
        selection, store, adapter, _ = self.run_rotation(backend='redis', service='checkout')
        platform = next(iter(store.values))
        store.labels[platform][TOKEN].append('RETAIL_FINALIZED')
        writes = len(store.writes)
        following = dict(selection, operation_id='123e4567-e89b-42d3-a456-426614174001')
        with self.assertRaisesRegex(rotation.RotationError, 'previous_rotation_not_finalized'):
            rotation.rotate(outputs(), store, adapter, following, execute=True)
        self.assertEqual(len(store.writes), writes)

    def test_finalize_never_marks_success_if_old_password_is_still_accepted(self):
        selection, store, adapter, _ = self.run_rotation()
        with patch.object(adapter, 'finalize'), self.assertRaisesRegex(rotation.RotationError, 'old_password_still_accepted'):
            rotation.rotate(outputs(), store, adapter, selection, execute=True, finalize=True, consumers_reloaded=True)
        self.assertFalse(any('RETAIL_FINALIZED' in labels for labels in next(iter(store.labels.values())).values()))

    def test_mysql_adapter_parameterizes_password_and_keeps_secondary_password(self):
        client = Mock()
        client.get_secret_value.return_value = {'SecretString': json.dumps({'username': 'admin', 'password': OLD})}
        adapter = rotation.PrivateBackends(outputs(), client, Mock(), Mock())
        connection = MagicMock()
        desired = {'username': 'catalog_dev', 'password': "private'password"}
        with patch.object(adapter, 'mysql', return_value=connection):
            adapter.apply(rotation.target('dev', 'catalog', 'mysql', TOKEN), desired, {'password': OLD})
        sql, arguments = connection.cursor.return_value.__enter__.return_value.execute.call_args.args
        self.assertIn('RETAIN CURRENT PASSWORD', sql)
        self.assertNotIn(desired['password'], sql)
        self.assertEqual(arguments, ('catalog_dev', desired['password']))
        connection.close.assert_called_once()

    def test_mysql_rollback_restores_primary_then_discards_the_rejected_secondary(self):
        client = Mock()
        client.get_secret_value.return_value = {'SecretString': json.dumps({'username': 'admin', 'password': OLD})}
        adapter = rotation.PrivateBackends(outputs(), client, Mock(), Mock())
        reset, discard = MagicMock(), MagicMock()
        original = {'username': 'catalog_dev', 'password': OLD}
        with patch.object(adapter, 'mysql', side_effect=[reset, discard]), patch.object(adapter, 'check', return_value=True):
            adapter.rollback(rotation.target('dev', 'catalog', 'mysql', TOKEN), original, {'password': 'failed-new'})
        first = reset.cursor.return_value.__enter__.return_value.execute.call_args.args
        last = discard.cursor.return_value.__enter__.return_value.execute.call_args.args
        self.assertIn('RETAIN CURRENT PASSWORD', first[0])
        self.assertEqual(first[1], ('catalog_dev', OLD))
        self.assertIn('DISCARD OLD PASSWORD', last[0])
        self.assertEqual(last[1], ('catalog_dev',))
        self.assertNotIn(OLD, first[0])

    def test_redis_rollback_replaces_password_set_with_original_only(self):
        elasticache = Mock()
        elasticache.describe_users.return_value = {'Users': [{'Status': 'active'}]}
        adapter = rotation.PrivateBackends(outputs(), Mock(), elasticache, Mock())
        adapter.rollback(rotation.target('dev', 'checkout', 'redis', TOKEN), {'password': OLD}, {'password': 'failed-new'})
        elasticache.modify_user.assert_called_once_with(UserId='retail-checkout-dev', Passwords=[OLD])

    def test_search_password_patch_preserves_role_mapping(self):
        client = Mock()
        client.get_secret_value.return_value = {'SecretString': json.dumps({'username': 'admin', 'password': OLD})}
        adapter = rotation.PrivateBackends(outputs(), client, Mock(), Mock())
        desired = {'username': 'catalog-dev', 'password': 'new-private-password', 'endpoint': 'https://search.invalid'}
        with patch.object(rotation, 'http', return_value=200) as request:
            adapter.apply(rotation.target('dev', 'catalog', 'opensearch', TOKEN), desired, {'password': OLD})
        self.assertEqual(request.call_args.args[0], 'PATCH')
        self.assertEqual(request.call_args.args[-1], [{'op': 'replace', 'path': '/password', 'value': desired['password']}])

    def test_redis_api_uses_two_passwords_and_only_the_selected_environment_user(self):
        elasticache = Mock()
        elasticache.describe_users.return_value = {'Users': [{'Status': 'active'}]}
        adapter = rotation.PrivateBackends(outputs(), Mock(), elasticache, Mock())
        adapter.apply(rotation.target('prod', 'checkout', 'redis', TOKEN), {'password': 'new-private-password'}, {'password': OLD})
        elasticache.modify_user.assert_called_once_with(UserId='retail-checkout-prod', Passwords=[OLD, 'new-private-password'])
        elasticache.describe_users.assert_called_once_with(UserId='retail-checkout-prod')

    def test_cli_failure_never_forwards_sdk_exception_secret_values(self):
        identity = Mock()
        identity.get_caller_identity.return_value = {'Account': '111122223333'}
        client = Mock()
        client.describe_secret.side_effect = RuntimeError('password=NEVER_LOG_PASSWORD')
        sdk = SimpleNamespace(client=lambda name, **kwargs: identity if name == 'sts' else client)
        with patch.dict(sys.modules, {'boto3': sdk, 'botocore.config': SimpleNamespace(Config=lambda **kwargs: None)}), \
                patch.object(rotation, 'load_outputs', return_value=outputs()), patch('sys.stdout', new_callable=io.StringIO) as output:
            code = rotation.main(['--terraform-output', 'unused.json', '--environment', 'dev', '--service', 'catalog',
                                  '--backend', 'mysql', '--operation-id', TOKEN])
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(output.getvalue())['error_code'], 'rotation_failed')
        self.assertNotIn('NEVER_LOG_PASSWORD', output.getvalue())


class Wire:
    def __init__(self, data):
        self.input = io.BytesIO(data)
        self.sent = []

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def settimeout(self, value):
        pass

    def recv(self, size):
        return self.input.read(size)

    def sendall(self, value):
        self.sent.append(value)


class ProtocolTests(unittest.TestCase):
    @staticmethod
    def frame(method, data=b''):
        payload = struct.pack('!HH', *method) + data
        return struct.pack('!BHI', 1, 0, len(payload)) + payload + b'\xce'

    def test_amqp_authentication_and_vhost_are_checked_without_declaring_resources(self):
        wire = Wire(self.frame((10, 10)) + self.frame((10, 30), struct.pack('!HIH', 2047, 131072, 60)) + self.frame((10, 41), b'\0'))
        context = Mock()
        context.wrap_socket.return_value = wire
        with patch.object(rotation.socket, 'create_connection', return_value=wire), \
                patch.object(rotation.ssl, 'create_default_context', return_value=context):
            self.assertTrue(rotation.amqp_check('broker.invalid', {'username': 'orders-dev', 'password': OLD, 'vhost': 'retail-dev'}))
        self.assertEqual(wire.sent[0], b'AMQP\x00\x00\x09\x01')
        self.assertIn(b'authentication_failure_close', wire.sent[1])
        self.assertIn(b'retail-dev', wire.sent[3])
        self.assertEqual(len(wire.sent), 5)  # Header, start-ok, tune-ok, open, close; no channels/queues.
        context.wrap_socket.assert_called_once_with(wire, server_hostname='broker.invalid')

    def test_amqp_access_refused_is_a_known_auth_failure_not_network_success(self):
        wire = Wire(self.frame((10, 10)) + self.frame((10, 50), struct.pack('!H', 403) + b'\0\0\0\0\0'))
        context = Mock()
        context.wrap_socket.return_value = wire
        with patch.object(rotation.socket, 'create_connection', return_value=wire), \
                patch.object(rotation.ssl, 'create_default_context', return_value=context):
            self.assertFalse(rotation.amqp_check('broker.invalid', {'username': 'orders-dev', 'password': OLD, 'vhost': 'retail-dev'}))

    def test_closed_socket_is_not_misclassified_as_bad_password_before_a_write(self):
        wire = Wire(b'')
        with self.assertRaisesRegex(rotation.RotationError, 'backend_protocol_closed'):
            rotation.recv_exact(wire, 7)

    def test_redis_auth_and_ping_use_tls_without_mutating_keys(self):
        wire = Wire(b'+OK\r\n+PONG\r\n')
        context = Mock()
        context.wrap_socket.return_value = wire
        with patch.object(rotation.socket, 'create_connection', return_value=wire), \
                patch.object(rotation.ssl, 'create_default_context', return_value=context):
            self.assertTrue(rotation.redis_check('cache.invalid', {'username': 'checkout-dev', 'password': OLD}))
        self.assertEqual(len(wire.sent), 2)
        self.assertIn(b'AUTH', wire.sent[0])
        self.assertIn(b'PING', wire.sent[1])
        context.wrap_socket.assert_called_once_with(wire, server_hostname='cache.invalid')


if __name__ == '__main__':
    unittest.main()
