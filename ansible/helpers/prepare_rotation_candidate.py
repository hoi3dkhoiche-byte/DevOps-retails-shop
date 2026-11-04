#!/usr/bin/env python3
"""Prepare one secretRevision change after a successful fenced rotation Job."""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import sys
import subprocess
import time

import yaml
import prepare_candidate as candidate

PAIRS = {('catalog', 'mysql'), ('cart', 'mysql'), ('orders', 'mysql'),
         ('orders', 'rabbitmq'), ('catalog', 'opensearch'), ('checkout', 'redis')}


def timestamp(value):
    result = datetime.fromisoformat(value.replace('Z', '+00:00'))
    candidate.require(result.tzinfo is not None, 'Rotation and ESO timestamps require timezone information')
    return result.astimezone(timezone.utc)


def refreshed(resource, report):
    spec = selection(report)
    metadata = resource.get('metadata', {})
    candidate.require(metadata.get('namespace') == 'retail-' + spec['environment']
                      and metadata.get('name') == spec['service'] + '-' + spec['backend'],
                      'ExternalSecret does not match the rotated target')
    status = resource.get('status', {})
    ready = any(item.get('type') == 'Ready' and item.get('status') == 'True' for item in status.get('conditions', []))
    return ready and bool(status.get('refreshTime')) and timestamp(status['refreshTime']) >= timestamp(report['finished_at'])


def wait_for_refresh(report, context, kubeconfig, timeout):
    spec = selection(report)
    candidate.require(bool(context) and kubeconfig is not None and 30 <= timeout <= 900,
                      'ESO wait requires explicit context/kubeconfig and a bounded timeout')
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = subprocess.run(['kubectl', '--context', context, '--kubeconfig', str(kubeconfig),
                                 '--request-timeout=20s', '-n', 'retail-' + spec['environment'],
                                 'get', 'externalsecret', spec['service'] + '-' + spec['backend'], '-o', 'json'],
                                capture_output=True, text=True, check=False, timeout=30)
        candidate.require(result.returncode == 0, 'Cannot read the rotated ExternalSecret status')
        if refreshed(json.loads(result.stdout), report):
            return
        time.sleep(min(5, max(0, deadline - time.monotonic())))
    raise candidate.CandidateError('ESO did not become Ready with a refresh after rotation; retry this helper after inspecting ESO')


def selection(report):
    candidate.require(isinstance(report, dict) and report.get('schema_version') == 1
                      and report.get('operation') == 'credential_rotation' and report.get('status') == 'success',
                      'A successful credential rotation runner report is required')
    spec = report.get('rotation', {})
    candidate.require(spec.get('environment') in ('dev', 'prod') and
                      (spec.get('service'), spec.get('backend')) in PAIRS,
                      'Rotation report selects an unsupported environment or service')
    candidate.require(isinstance(spec.get('operation_id'), str) and
                      re.fullmatch(r'[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}', spec['operation_id']) is not None,
                      'Rotation report needs a valid operation UUID')
    candidate.require(type(spec.get('rollback')) is bool, 'Rotation report needs an explicit rollback boolean')
    return spec


def update_revision(root, gitops, local, report):
    spec = selection(report)
    owner = candidate.existing_manifest(root, gitops)
    candidate.require(owner is not None, 'Prepare an owned GitOps candidate first')
    config = yaml.safe_load((root / 'config.yaml').read_text(encoding='utf-8-sig'))
    candidate.require(str(config.get('accountId')) == report.get('account_id')
                      and config.get('clusterName') == report.get('cluster_name')
                      and config.get('deployment', {}).get('enabled') is True,
                      'Candidate and successful rotation must identify the same active AWS deployment')
    relative = 'environments/' + spec['environment'] + '/' + spec['service'] + '.yaml'
    path = candidate.no_links(root / relative)
    candidate.require(relative in owner['files'] and path.is_relative_to(root), 'Service values are absent from the owned candidate')
    values = yaml.safe_load(path.read_text(encoding='utf-8-sig'))
    candidate.require(isinstance(values, dict) and 'secretRevision' in values, 'Service chart must support secretRevision')
    revision = ('rollback-' if spec['rollback'] else '') + spec['operation_id']
    changed = values['secretRevision'] != revision
    if changed:
        values['secretRevision'] = revision
        payload = yaml.safe_dump(values, sort_keys=False, allow_unicode=True).encode('utf-8')
        candidate.existing_manifest(root, gitops)
        candidate.write_atomic(path, payload, local)
        owner['files'] = candidate.file_hashes(root)
    owner['rotation'] = {key: spec[key] for key in ('environment', 'service', 'backend', 'operation_id', 'rollback')}
    candidate.write_atomic(root / candidate.MANIFEST, (json.dumps(owner, indent=2, sort_keys=True) + '\n').encode(), local)
    return {'schema_version': 1, 'status': 'prepared', 'changed': changed,
            'candidate_dir': str(root), 'values_file': relative, 'secret_revision': revision}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('source-dir', 'gitops-dir', 'local-dir', 'candidate-dir', 'terraform-output', 'runner-report'):
        parser.add_argument('--' + name, type=Path, required=True)
    parser.add_argument('--wait-eso', action='store_true')
    parser.add_argument('--context')
    parser.add_argument('--kubeconfig', type=Path)
    parser.add_argument('--timeout', type=int, default=420)
    args = parser.parse_args()
    try:
        report = json.loads(args.runner_report.read_text(encoding='utf-8-sig'))
        spec = selection(report)
        source, gitops, local, root = (candidate.no_links(path) for path in
                                        (args.source_dir, args.gitops_dir, args.local_dir, args.candidate_dir))
        candidate.require(root != local and root.is_relative_to(local) and local != source and local.is_relative_to(source),
                          'Rotation candidate must be below the ignored source local directory')
        candidate.require(not root.is_relative_to(gitops) and not gitops.is_relative_to(root), 'Candidate cannot alias the GitOps checkout')
        config = yaml.safe_load((gitops / 'config.yaml').read_text(encoding='utf-8-sig'))
        candidate.require(config.get('deployment', {}).get('enabled') is True,
                          'Rotation requires an already activated canonical GitOps deployment')
        if args.wait_eso:
            wait_for_refresh(report, args.context, args.kubeconfig, args.timeout)
        owner = candidate.existing_manifest(root, gitops)
        if owner is None:
            candidate.prepare_candidate(source, gitops, local, root, args.terraform_output, activate=True)
        else:
            candidate.require(owner.get('rotation', {}).get('operation_id') == spec['operation_id']
                              and owner.get('terraform_output_sha256') == candidate.digest(args.terraform_output),
                              'Use a fresh candidate directory for a different rotation or deployment output')
        print(json.dumps(update_revision(root, gitops, local, report), sort_keys=True))
        return 0
    except (candidate.CandidateError, OSError, ValueError, TypeError, KeyError, yaml.YAMLError, subprocess.SubprocessError):
        print(json.dumps({'schema_version': 1, 'status': 'failed', 'changed': False,
                          'error': 'Rotation candidate rejected; verify the successful report, ownership and active GitOps target'}))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
