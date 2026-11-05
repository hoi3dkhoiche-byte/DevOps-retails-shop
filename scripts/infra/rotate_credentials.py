#!/usr/bin/env python3
"""Stage, test and publish one application's credential without printing values."""
import argparse
import base64
import copy
import json
import os
from pathlib import Path
import re
import socket
import ssl
import struct
import sys
import time
from urllib.parse import quote, urlsplit
import urllib.error
import urllib.request
import uuid

from run_bootstrap import load_outputs, REGION
from bootstrap_backends import generate_password

TARGETS = {('catalog', 'mysql'), ('cart', 'mysql'), ('orders', 'mysql'),
           ('orders', 'rabbitmq'), ('catalog', 'opensearch'), ('checkout', 'redis')}


class RotationError(RuntimeError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def require(condition, code):
    if not condition:
        raise RotationError(code)


def target(environment, service, backend, operation_id):
    require(environment in {'dev', 'prod'} and (service, backend) in TARGETS, 'unsupported_target')
    try:
        require(str(uuid.UUID(operation_id)) == operation_id, 'invalid_operation_id')
    except (ValueError, TypeError, AttributeError):
        raise RotationError('invalid_operation_id') from None
    return {'environment': environment, 'service': service, 'backend': backend, 'operation_id': operation_id}


def identifiers(selection):
    env, svc, backend = (selection[key] for key in ('environment', 'service', 'backend'))
    app = f'/retail/{env}/{svc}/{backend}'
    return [f'/retail/platform/redis/{env}', app] if backend == 'redis' else [app]


def metadata(client, name, account):
    value = client.describe_secret(SecretId=name)
    require(not value.get('DeletedDate'), 'secret_scheduled_for_deletion')
    require(value.get('ARN', '').startswith(f'arn:aws:secretsmanager:{REGION}:{account}:secret:{name}-'), 'wrong_secret_target')
    stages = value.get('VersionIdsToStages', {})
    current = [key for key, labels in stages.items() if 'AWSCURRENT' in labels]
    require(len(current) == 1, 'missing_current_version')
    return value['ARN'], stages, current[0]


def read(client, arn, version):
    value = json.loads(client.get_secret_value(SecretId=arn, VersionId=version)['SecretString'])
    require(isinstance(value, dict), 'invalid_secret_schema')
    return value


def validate_value(selection, value, outputs, platform=False):
    env, svc, backend = (selection[key] for key in ('environment', 'service', 'backend'))
    endpoints = outputs['backend_endpoints']
    if backend == 'redis' and not platform:
        address = urlsplit(value.get('url', ''))
        require(address.scheme == 'rediss' and address.hostname == endpoints['RedisEndpoint']
                and address.port == 6379 and address.username == f'checkout-{env}'
                and bool(address.password) and not (address.path or address.query or address.fragment), 'invalid_secret_target')
        return
    username = f'{svc}_{env}' if backend == 'mysql' else f'{svc}-{env}'
    require(value.get('username') == username and isinstance(value.get('password'), str)
            and len(value['password']) >= 16, 'invalid_secret_identity')
    if backend == 'mysql':
        require(value.get('host') == outputs['mysql_endpoint'] and value.get('database') == username
                and value.get('port') == 3306, 'invalid_secret_target')
    elif backend == 'rabbitmq':
        require(value.get('addresses') == endpoints['RabbitMQEndpoint'] and value.get('vhost') == f'retail-{env}', 'invalid_secret_target')
    elif backend == 'opensearch':
        require(value.get('endpoint') == 'https://' + endpoints['SearchEndpoint'], 'invalid_secret_target')


def candidate(selection, original, previous, password):
    value = copy.deepcopy(original)
    value['_rotation'] = dict(selection, previous_version=previous)
    if selection['backend'] == 'redis' and 'url' in value:
        address = urlsplit(value['url'])
        value['url'] = f'rediss://{quote(address.username, safe="")}:{quote(password, safe="")}@{address.hostname}:6379'
    else:
        value['password'] = password
    return value


def rotate(outputs, client, adapter, selection, execute=False, maintenance=False, rollback=False, report=None,
           finalize=False, consumers_reloaded=False):
    report = report if report is not None else {}
    report.update(schema_version=1, operation='credential_rotation', **selection, changed=False,
                  status='planned', phase='metadata', account_id=outputs['account_id'], cluster_name=outputs['cluster_name'])
    names = identifiers(selection)
    known = [metadata(client, name, outputs['account_id']) for name in names]
    token = selection['operation_id']
    report.update(secret_count=len(names), maintenance_required=selection['backend'] in {'rabbitmq', 'opensearch'})
    require(not (rollback and finalize), 'conflicting_rotation_actions')
    report.update(finalize=finalize, rollback=rollback)
    report['secret_names'] = names
    report['other_pending_operation'] = any('AWSPENDING' in labels and version != token
                                            for _, stages, _ in known for version, labels in stages.items())
    report['plan_steps'] = ['stage_pending_version', 'apply_one_backend_identity', 'verify_new_authentication',
                            'promote_exact_current_versions', 'wait_for_eso_then_gitops_rollout']
    if not execute:
        return report
    require(not finalize or consumers_reloaded, 'consumers_reload_ack_required')
    require(not report['maintenance_required'] or maintenance, 'maintenance_ack_required')
    require(all(not any('AWSPENDING' in labels and version != token for version, labels in stages.items())
                for _, stages, _ in known), 'another_rotation_pending')
    require(rollback or not any('RETAIL_ROLLED_BACK' in stages.get(token, []) for _, stages, _ in known),
            'operation_already_rolled_back')
    fresh_values = {}
    for index, (arn, stages, current) in enumerate(known):
        if token not in stages and not rollback:
            require(not finalize, 'rotation_not_published')
            value = read(client, arn, current)
            validate_value(selection, value, outputs, platform=index == 0 and len(names) == 2)
            if '_rotation' in value:
                require(isinstance(value['_rotation'], dict)
                        and value['_rotation'].get('operation_id') == current, 'prior_rotation_metadata_invalid')
                require('RETAIL_FINALIZED' in stages[current], 'previous_rotation_not_finalized')
            fresh_values[arn] = value
    report['phase'] = 'stage'
    entries = []
    new_password = None
    for index, (arn, stages, current) in enumerate(known):
        if token in stages:
            require(not rollback or 'RETAIL_FINALIZED' not in stages[token], 'finalized_rotation_cannot_rollback')
            pending = read(client, arn, token)
            info = pending.get('_rotation', {})
            require(all(info.get(key) == value for key, value in selection.items()), 'operation_target_mismatch')
            previous = info.get('previous_version')
            require(isinstance(previous, str) and previous in stages and current in {previous, token}, 'rotation_version_conflict')
            require('AWSPENDING' in stages[token] or 'AWSCURRENT' in stages[token] or rollback, 'operation_already_rolled_back')
            original = read(client, arn, previous)
        else:
            require(not finalize, 'rotation_not_published')
            require(not rollback or (len(names) == 2 and index == 1 and bool(entries)), 'rotation_not_found')
            previous = current
            original = fresh_values.get(arn) or read(client, arn, current)
            validate_value(selection, original, outputs, platform=index == 0 and len(names) == 2)
            if '_rotation' in original and not rollback:
                require(isinstance(original['_rotation'], dict)
                        and original['_rotation'].get('operation_id') == current, 'prior_rotation_metadata_invalid')
                require('RETAIL_FINALIZED' in stages[current], 'previous_rotation_not_finalized')
            if new_password is None:
                new_password = generate_password()
            pending = candidate(selection, original, previous, new_password)
            if not rollback:
                report['changed'] = True
                client.put_secret_value(SecretId=arn, ClientRequestToken=token,
                                        VersionStages=['AWSPENDING'], SecretString=json.dumps(pending, separators=(',', ':')))
        validate_value(selection, original, outputs, platform=index == 0 and len(names) == 2)
        validate_value(selection, pending, outputs, platform=index == 0 and len(names) == 2)
        if index == 0:
            new_password = pending.get('password')
        entries.append({'name': names[index], 'arn': arn, 'original': original, 'pending': pending,
                        'previous': previous, 'current': current})
    original, pending = entries[0]['original'], entries[0]['pending']
    if len(entries) == 2:
        old_url = candidate(selection, entries[1]['original'], entries[1]['previous'], original['password'])['url']
        require(entries[1]['original']['url'] == old_url, 'redis_original_versions_disagree')
        expected = candidate(selection, entries[1]['original'], entries[1]['previous'], pending['password'])['url']
        require(entries[1]['pending']['url'] == expected, 'redis_pending_versions_disagree')
    desired = original if rollback else pending
    for entry in entries:
        _, stages, current = metadata(client, entry['name'], outputs['account_id'])
        require(current in {entry['previous'], token}, 'rotation_version_conflict')
        require(token in stages or (rollback and selection['backend'] == 'redis'), 'pending_version_lost')
        if not rollback:
            require('AWSCURRENT' in stages.get(token, []) or 'AWSPENDING' in stages.get(token, []), 'pending_version_lost')
    if finalize:
        report['phase'] = 'finalize'
        require(all(entry['current'] == token for entry in entries), 'rotation_not_published')
        require(all('AWSPENDING' not in stages.get(token, []) for _, stages, _ in known), 'rotation_not_published')
        require(adapter.check(selection, pending), 'published_credential_rejected')
        if selection['backend'] in {'mysql', 'redis'} and adapter.check(selection, original):
            report.update(changed=True, backend_may_have_changed=True)
            adapter.finalize(selection, pending)
        require(adapter.check(selection, pending), 'backend_verification_failed')
        require(not adapter.check(selection, original), 'old_password_still_accepted')
        for entry in entries:
            _, stages, current = metadata(client, entry['name'], outputs['account_id'])
            require(current == token, 'rotation_version_conflict')
            old_markers = [version for version, labels in stages.items() if 'RETAIL_FINALIZED' in labels]
            if token not in old_markers:
                arguments = {'SecretId': entry['arn'], 'VersionStage': 'RETAIL_FINALIZED', 'MoveToVersionId': token}
                if old_markers:
                    require(len(old_markers) == 1, 'invalid_finalized_stage')
                    arguments['RemoveFromVersionId'] = old_markers[0]
                report['changed'] = True
                client.update_secret_version_stage(**arguments)
        report.update(status='success', phase='finalized', old_password_retained=False,
                      next_step='Old backend password is retired; preserve the operation report for audit.')
        return report
    report['phase'] = 'backend'
    if rollback:
        # An old secondary password still authenticating is not a completed
        # rollback. Restore it as primary and revoke the failed pending password.
        if not adapter.check(selection, original) or adapter.check(selection, pending):
            report.update(changed=True, backend_may_have_changed=True)
            adapter.rollback(selection, original, pending)
        require(adapter.check(selection, original), 'backend_verification_failed')
        require(not adapter.check(selection, pending), 'rolled_back_password_still_accepted')
    elif not adapter.check(selection, desired):
        require(rollback or not any(entry['current'] == token for entry in entries), 'published_credential_rejected')
        report.update(changed=True, backend_may_have_changed=True)
        adapter.apply(selection, desired, pending if rollback else original)
    require(adapter.check(selection, desired), 'backend_verification_failed')
    report['phase'] = 'publish'
    # Re-read every current stage before a move; never replace a third party's version.
    for entry in entries:
        arn, stages, current = metadata(client, entry['name'], outputs['account_id'])
        require(arn == entry['arn'] and current in {token, entry['previous']}, 'rotation_version_conflict')
        desired_id = entry['previous'] if rollback else token
        if current != desired_id:
            report['changed'] = True
            client.update_secret_version_stage(SecretId=arn, VersionStage='AWSCURRENT',
                                              MoveToVersionId=desired_id, RemoveFromVersionId=current)
    for entry in entries:
        stages = client.describe_secret(SecretId=entry['arn']).get('VersionIdsToStages', {})
        if 'AWSPENDING' in stages.get(token, []):
            report['changed'] = True
            client.update_secret_version_stage(SecretId=entry['arn'], VersionStage='AWSPENDING', RemoveFromVersionId=token)
    if rollback:
        for entry in entries:
            _, stages, current = metadata(client, entry['name'], outputs['account_id'])
            require(current == entry['previous'], 'rotation_version_conflict')
            # If staging stopped before the second Redis version existed,
            # record the rollback on its unchanged original version instead.
            marker_version = token if token in stages else current
            markers = [version for version, labels in stages.items() if 'RETAIL_ROLLED_BACK' in labels]
            require(len(markers) <= 1, 'invalid_rollback_stage')
            if marker_version not in markers:
                arguments = {'SecretId': entry['arn'], 'VersionStage': 'RETAIL_ROLLED_BACK',
                             'MoveToVersionId': marker_version}
                if markers:
                    arguments['RemoveFromVersionId'] = markers[0]
                report['changed'] = True
                client.update_secret_version_stage(**arguments)
    report.update(status='success', phase='complete', rollback=rollback,
                  old_password_retained=selection['backend'] in {'mysql', 'redis'} and not rollback,
                  next_step='Wait for ESO refresh, review a GitOps secretRevision change, sync and run dev acceptance.')
    return report


def http(method, endpoint, path, credential, body=None):
    token = base64.b64encode((credential['username'] + ':' + credential['password']).encode()).decode()
    request = urllib.request.Request(endpoint + path, data=None if body is None else json.dumps(body).encode(),
                                     method=method, headers={'Authorization': 'Basic ' + token, 'Content-Type': 'application/json'})
    try:
        with urllib.request.urlopen(request, context=ssl.create_default_context(), timeout=20) as response:
            response.read(1024 * 1024)
            return response.status
    except urllib.error.HTTPError as error:
        if error.code == 401:
            return 401
        raise RotationError('backend_http_failed') from None


def recv_exact(connection, size):
    result = b''
    while len(result) < size:
        chunk = connection.recv(size - len(result))
        require(bool(chunk), 'backend_protocol_closed')
        result += chunk
    return result


def amqp_check(host, credential):
    def short(value):
        raw = value.encode()
        require(len(raw) < 256, 'backend_protocol_input')
        return bytes([len(raw)]) + raw
    with socket.create_connection((host, 5671), timeout=15) as raw:
        with ssl.create_default_context().wrap_socket(raw, server_hostname=host) as connection:
            connection.settimeout(15)
            def receive(expected):
                frame_type, channel, size = struct.unpack('!BHI', recv_exact(connection, 7))
                require(frame_type == 1 and channel == 0 and 4 <= size <= 131072, 'backend_protocol_frame')
                payload = recv_exact(connection, size)
                require(recv_exact(connection, 1) == b'\xce', 'backend_protocol_frame')
                method = struct.unpack('!HH', payload[:4])
                if method == (10, 50) and len(payload) >= 6 and struct.unpack('!H', payload[4:6])[0] == 403:
                    return None
                require(method == expected, 'backend_protocol_method')
                return payload
            def send(method, value):
                payload = struct.pack('!HH', *method) + value
                connection.sendall(struct.pack('!BHI', 1, 0, len(payload)) + payload + b'\xce')
            connection.sendall(b'AMQP\x00\x00\x09\x01')
            require(receive((10, 10)) is not None, 'backend_protocol_start')
            plain = ('\0' + credential['username'] + '\0' + credential['password']).encode()
            capability = short('authentication_failure_close') + b't\x01'
            properties = short('capabilities') + b'F' + struct.pack('!I', len(capability)) + capability
            send((10, 11), struct.pack('!I', len(properties)) + properties + short('PLAIN')
                 + struct.pack('!I', len(plain)) + plain + short('en_US'))
            tune = receive((10, 30))
            if tune is None:
                return False
            send((10, 31), tune[4:])
            send((10, 40), short(credential['vhost']) + b'\0\0')
            if receive((10, 41)) is None:
                return False
            send((10, 50), struct.pack('!H', 200) + short('rotation-check') + b'\0\0\0\0')
            return True


def redis_check(host, credential):
    def command(connection, *arguments):
        parts = [argument.encode() for argument in arguments]
        connection.sendall(b'*' + str(len(parts)).encode() + b'\r\n' + b''.join(
            b'$' + str(len(part)).encode() + b'\r\n' + part + b'\r\n' for part in parts))
        response = bytearray()
        while not response.endswith(b'\r\n'):
            response.extend(recv_exact(connection, 1))
            require(len(response) <= 4096, 'backend_protocol_frame')
        return bytes(response)
    with socket.create_connection((host, 6379), timeout=15) as raw:
        with ssl.create_default_context().wrap_socket(raw, server_hostname=host) as connection:
            connection.settimeout(15)
            response = command(connection, 'AUTH', credential['username'], credential['password'])
            if response.startswith(b'-WRONGPASS'):
                return False
            require(response == b'+OK\r\n', 'redis_authentication_failed')
            require(command(connection, 'PING') == b'+PONG\r\n', 'redis_ping_failed')
            return True


class PrivateBackends:
    def __init__(self, outputs, client, elasticache, ca_file):
        self.outputs, self.client, self.elasticache, self.ca_file = outputs, client, elasticache, ca_file

    def mysql(self, credential):
        import pymysql
        require(self.ca_file.is_file(), 'rds_ca_required')
        return pymysql.connect(host=self.outputs['mysql_endpoint'], port=3306, user=credential['username'],
                               password=credential['password'], ssl_ca=str(self.ca_file), ssl_verify_cert=True,
                               ssl_verify_identity=True, connect_timeout=10, read_timeout=20, write_timeout=20,
                               autocommit=True)

    def admin(self, key):
        arn = self.outputs['platform_secret_arns'][key]
        response = self.client.get_secret_value(SecretId=arn, VersionStage='AWSCURRENT')
        return json.loads(response['SecretString'])

    def check(self, selection, credential):
        backend = selection['backend']
        if backend == 'mysql':
            import pymysql
            try:
                connection = self.mysql(credential)
                try:
                    with connection.cursor() as cursor:
                        cursor.execute('SELECT 1')
                        require(cursor.fetchone()[0] == 1, 'mysql_verification_failed')
                    return True
                finally:
                    connection.close()
            except pymysql.err.OperationalError as error:
                if error.args and error.args[0] == 1045:
                    return False
                raise RotationError('mysql_connection_failed') from None
        if backend == 'rabbitmq':
            return amqp_check(urlsplit(self.outputs['backend_endpoints']['RabbitMQEndpoint']).hostname, credential)
        if backend == 'opensearch':
            return http('GET', credential['endpoint'], '/_plugins/_security/authinfo', credential) == 200
        return redis_check(self.outputs['backend_endpoints']['RedisEndpoint'], credential)

    def apply(self, selection, desired, previous):
        backend = selection['backend']
        if backend == 'mysql':
            master = json.loads(self.client.get_secret_value(SecretId=self.outputs['mysql_master_secret_arn'],
                                                            VersionStage='AWSCURRENT')['SecretString'])
            connection = self.mysql(master)
            try:
                with connection.cursor() as cursor:
                    cursor.execute("ALTER USER %s@'%%' IDENTIFIED BY %s RETAIN CURRENT PASSWORD",
                                   (desired['username'], desired['password']))
            finally:
                connection.close()
        elif backend == 'rabbitmq':
            host = urlsplit(self.outputs['backend_endpoints']['RabbitMQEndpoint']).hostname
            require(http('PUT', 'https://' + host, '/api/users/' + quote(desired['username'], safe=''),
                         self.admin('mq_admin'), {'password': desired['password'], 'tags': ''}) in {200, 201, 204},
                    'rabbit_password_update_failed')
        elif backend == 'opensearch':
            require(http('PATCH', desired['endpoint'], '/_plugins/_security/api/internalusers/' + quote(desired['username'], safe=''),
                         self.admin('search_admin'), [{'op': 'replace', 'path': '/password', 'value': desired['password']}]) in {200, 201},
                    'search_password_update_failed')
        else:
            user_id = 'retail-checkout-' + selection['environment']
            self.elasticache.modify_user(UserId=user_id, Passwords=list(dict.fromkeys([previous['password'], desired['password']])))
            deadline = time.monotonic() + 180
            while time.monotonic() < deadline:
                users = self.elasticache.describe_users(UserId=user_id).get('Users', [])
                if len(users) == 1 and users[0].get('Status') == 'active':
                    return
                time.sleep(5)
            raise RotationError('redis_user_update_timeout')

    def finalize(self, selection, current):
        if selection['backend'] == 'mysql':
            master = json.loads(self.client.get_secret_value(SecretId=self.outputs['mysql_master_secret_arn'],
                                                            VersionStage='AWSCURRENT')['SecretString'])
            connection = self.mysql(master)
            try:
                with connection.cursor() as cursor:
                    cursor.execute("ALTER USER %s@'%%' DISCARD OLD PASSWORD", (current['username'],))
            finally:
                connection.close()
        elif selection['backend'] == 'redis':
            user_id = 'retail-checkout-' + selection['environment']
            self.elasticache.modify_user(UserId=user_id, Passwords=[current['password']])
            deadline = time.monotonic() + 180
            while time.monotonic() < deadline:
                users = self.elasticache.describe_users(UserId=user_id).get('Users', [])
                if len(users) == 1 and users[0].get('Status') == 'active':
                    return
                time.sleep(5)
            raise RotationError('redis_user_update_timeout')

    def rollback(self, selection, original, rejected):
        if selection['backend'] == 'redis':
            # Replace the entire password set rather than leaving both values.
            self.finalize(selection, original)
        else:
            # MySQL apply establishes the desired primary before DISCARD OLD;
            # single-password backends simply replace their rejected password.
            self.apply(selection, original, rejected)
            require(self.check(selection, original), 'backend_verification_failed')
            if selection['backend'] == 'mysql':
                self.finalize(selection, original)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--terraform-output', type=Path, required=True)
    parser.add_argument('--environment', choices=('dev', 'prod'), required=True)
    parser.add_argument('--service', required=True)
    parser.add_argument('--backend', required=True)
    parser.add_argument('--operation-id', required=True)
    parser.add_argument('--execute', action='store_true')
    parser.add_argument('--ack-maintenance', action='store_true')
    parser.add_argument('--rollback', action='store_true')
    parser.add_argument('--finalize', action='store_true')
    parser.add_argument('--ack-consumers-reloaded', action='store_true')
    parser.add_argument('--rds-ca', type=Path, default=Path('/config/rds-ca.pem'))
    args = parser.parse_args(argv)
    report = {'schema_version': 1, 'status': 'failed', 'changed': False, 'phase': 'input'}
    try:
        selection = target(args.environment, args.service, args.backend, args.operation_id)
        outputs = load_outputs(args.terraform_output)
        import boto3
        from botocore.config import Config
        config = Config(connect_timeout=10, read_timeout=20, retries={'max_attempts': 2, 'mode': 'standard'})
        identity = boto3.client('sts', region_name=REGION, config=config).get_caller_identity()
        require(identity.get('Account') == outputs['account_id'], 'wrong_aws_account')
        if args.execute:
            require(re.fullmatch(r'backend-bootstrap-[0-9]{14}-[0-9a-f]{12}', os.environ.get('RETAIL_ROTATION_JOB', ''))
                    is not None, 'bounded_rotation_job_required')
            cluster = boto3.client('eks', region_name=REGION, config=config).describe_cluster(name=outputs['cluster_name'])['cluster']
            require(cluster.get('arn') == f"arn:aws:eks:{REGION}:{outputs['account_id']}:cluster/{outputs['cluster_name']}"
                    and cluster.get('name') == outputs['cluster_name'] and cluster.get('status') == 'ACTIVE'
                    and cluster.get('endpoint') == outputs['cluster_endpoint'], 'wrong_eks_target')
        client = boto3.client('secretsmanager', region_name=REGION, config=config)
        adapter = PrivateBackends(outputs, client, boto3.client('elasticache', region_name=REGION, config=config), args.rds_ca)
        rotate(outputs, client, adapter, selection, args.execute, args.ack_maintenance, args.rollback, report,
               finalize=args.finalize, consumers_reloaded=args.ack_consumers_reloaded)
        print(json.dumps(report, sort_keys=True))
        return 0
    except Exception as error:
        report.update(status='failed', error_code=error.code if isinstance(error, RotationError) else 'rotation_failed')
        print(json.dumps(report, sort_keys=True))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
