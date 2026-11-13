"""Bounded HTTPS requests pinned to a validated public address (including TLS hostname validation)."""
import ipaddress
import json
import socket
import ssl
import time
from urllib.parse import urlsplit, urlencode
import urllib3
from .data import MAX_BYTES, json_table, parse_file

def public_target(url):
    parsed = urlsplit(url)
    if parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password or parsed.port not in (None,443):
        raise ValueError('Use a public HTTPS URL on port 443 without credentials in the URL.')
    addresses = {item[4][0] for item in socket.getaddrinfo(parsed.hostname, 443, type=socket.SOCK_STREAM)}
    if not addresses or any(not ipaddress.ip_address(a).is_global or ipaddress.ip_address(a).is_multicast for a in addresses):
        raise ValueError('Private, loopback and internal network addresses are not supported.')
    return parsed, sorted(addresses)[0]

def fetch(spec):
    parsed, address = public_target(spec.get('url', ''))
    method = spec.get('method', 'GET')
    if method not in ('GET','POST'):
        raise ValueError('Use GET, or POST for a read-only query endpoint.')
    headers, params = spec.get('headers', {}), spec.get('params', {})
    if not isinstance(headers, dict) or not isinstance(params, dict):
        raise ValueError('Headers and query parameters must be JSON objects.')
    if any(k.lower() in {'host','content-length','transfer-encoding','connection'} for k in headers):
        raise ValueError('Host and transport headers cannot be overridden.')
    headers = {str(k):str(v) for k,v in headers.items()}
    headers['Host'] = parsed.hostname
    headers.setdefault('Accept','application/json')
    path = parsed.path or '/'
    query = '&'.join(filter(None, [parsed.query, urlencode(params, doseq=True)]))
    if query: path += '?' + query
    body = json.dumps(spec.get('body', {})).encode() if method == 'POST' else None
    if body: headers.setdefault('Content-Type','application/json')
    pool = urllib3.HTTPSConnectionPool(address, port=443, server_hostname=parsed.hostname, assert_hostname=parsed.hostname, ssl_context=ssl.create_default_context(), cert_reqs='CERT_REQUIRED', timeout=urllib3.Timeout(connect=5, read=15), retries=False)
    response = None
    try:
        response = pool.urlopen(method, path, body=body, headers=headers, redirect=False, preload_content=False)
        if not 200 <= response.status < 300:
            raise ValueError(f'API returned HTTP {response.status}. Redirects are disabled; use the final URL.')
        chunks, size, start = [], 0, time.monotonic()
        for chunk in response.stream(65536):
            size += len(chunk)
            if size > MAX_BYTES or time.monotonic()-start > 45:
                raise ValueError('API response exceeds the 20 MB or 45-second limit.')
            chunks.append(chunk)
        raw = b''.join(chunks)
        if spec.get('format','json') == 'csv':
            return next(iter(parse_file('response.csv', raw).values()))
        return json_table(json.loads(raw), spec.get('path',''))
    finally:
        if response: response.close()
        pool.close()
