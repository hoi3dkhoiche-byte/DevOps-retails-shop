#!/usr/bin/env python3
"""Offline proof that generated Argo CD AWS secret values are reusable and valid."""
import copy
from datetime import datetime, timedelta, timezone
import io
from contextlib import redirect_stdout
import bcrypt
from cryptography import x509
from cryptography.hazmat.primitives import serialization
import bootstrap_backends
from test_bootstrap import SecretStore

store = SecretStore()
store.values['/retail/platform/argocd/admin'] = None
store.values['/retail/platform/argocd/redis'] = None
capture = io.StringIO()
with redirect_stdout(capture):
    bootstrap_backends.argocd_credentials(store)
    first = copy.deepcopy(store.values)
    bootstrap_backends.argocd_credentials(store)
assert capture.getvalue() == ''
assert first == store.values, 'Repeated bootstrap unexpectedly rotated platform credentials'
admin = store.values['/retail/platform/argocd/admin']
assert bcrypt.checkpw(admin['password'].encode(), admin['password_hash'].encode())
certificate = x509.load_pem_x509_certificate(admin['tls_certificate'].encode())
private_key = serialization.load_pem_private_key(admin['tls_private_key'].encode(), password=None)
assert certificate.public_key().public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo) == private_key.public_key().public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
assert 'argocd-server.argocd.svc' in certificate.extensions.get_extension_for_class(x509.SubjectAlternativeName).value.get_values_for_type(x509.DNSName)
assert certificate.not_valid_after_utc > datetime.now(timezone.utc) + timedelta(days=300)
assert len(store.values['/retail/platform/argocd/redis']['password']) >= 32
print('Argo CD bcrypt, TLS key/certificate, secret stability and no-plaintext-output checks passed.')
