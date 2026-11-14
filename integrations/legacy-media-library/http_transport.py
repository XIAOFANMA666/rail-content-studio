"""Bounded HTTPS reads with a certificate-verified Node fallback.

Every redirect is returned to Python before another connection is made. Callers
keep their own source allowlist; model POSTs never follow redirects or retry.
No request body, authorization header, or upstream error body is logged.
"""
import base64
from email.message import Message
import json
import os
from pathlib import Path
import shutil
import socket
import ssl
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_NODE_READER = r"""
const fs = require('node:fs');
(async () => {
  const input = JSON.parse(fs.readFileSync(0, 'utf8'));
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), input.timeout_ms);
  try {
    const response = await fetch(input.url, {
      method: input.method, headers: input.headers, redirect: 'manual',
      signal: controller.signal,
      ...(input.body === null ? {} : {body: Buffer.from(input.body, 'base64')})
    });
    const type = (response.headers.get('content-type') || 'text/plain').split(';')[0].trim().toLowerCase();
    const meta = {status: response.status, type, location: response.headers.get('location') || '',
      retry_after: response.headers.get('retry-after') || ''};
    let chunks = [], length = 0;
    if (response.status >= 200 && response.status < 300) {
      if (input.mime && type !== input.mime) throw new Error('mime');
      const stated = Number(response.headers.get('content-length'));
      if (stated > input.limit) throw new Error('limit');
      if (response.body) {
        for await (const chunk of response.body) {
          length += chunk.length;
          if (length > input.limit) throw new Error('limit');
          chunks.push(Buffer.from(chunk));
        }
      }
    } else if (response.body) {
      await response.body.cancel();
    }
    process.stdout.write(JSON.stringify(meta) + '\n');
    for (const chunk of chunks) process.stdout.write(chunk);
  } catch (error) {
    const code = controller.signal.aborted ? 'timeout' :
      ['mime', 'limit'].includes(error.message) ? error.message : 'network';
    const known = new Set(['ENOTFOUND', 'ECONNREFUSED', 'ECONNRESET', 'EACCES', 'EPERM',
      'ETIMEDOUT', 'UND_ERR_CONNECT_TIMEOUT', 'CERT_HAS_EXPIRED',
      'UNABLE_TO_VERIFY_LEAF_SIGNATURE', 'SELF_SIGNED_CERT_IN_CHAIN',
      'DEPTH_ZERO_SELF_SIGNED_CERT', 'ERR_TLS_CERT_ALTNAME_INVALID']);
    const detail = known.has(error.cause?.code) ? error.cause.code : '';
    process.stdout.write(JSON.stringify({error: code, detail}) + '\n');
  } finally {
    clearTimeout(timer);
  }
})().catch(() => { process.stdout.write('{"error":"network"}\n'); });
"""


def node_binary():
    """Find Node even when a launch agent has a minimal PATH."""
    binary = shutil.which('node')
    if binary:
        return binary
    candidates = [Path('/opt/homebrew/bin/node'), Path('/usr/local/bin/node')]
    candidates += sorted((Path.home()/'.nvm/versions/node').glob('*/bin/node'), reverse=True)
    return next((str(path) for path in candidates if path.is_file() and os.access(path, os.X_OK)), None)


def _remaining(deadline):
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError('网络请求超时')
    return remaining


def _node_once(url, headers, body, limit, mime, deadline, binary):
    remaining = _remaining(deadline)
    payload = {'url': url, 'headers': headers, 'method': 'GET' if body is None else 'POST',
               'body': None if body is None else base64.b64encode(body).decode('ascii'),
               'limit': limit, 'mime': mime, 'timeout_ms': max(1, int(remaining * 1000))}
    env = dict(os.environ)
    env['NODE_TLS_REJECT_UNAUTHORIZED'] = '1'
    # Inherited preload scripts / TLS flags must not change this transport.
    env.pop('NODE_OPTIONS', None)
    try:
        result = subprocess.run([binary, '-e', _NODE_READER], input=json.dumps(payload).encode(),
                                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                timeout=remaining, check=False, env=env)
    except subprocess.TimeoutExpired:
        raise TimeoutError('网络请求超时') from None
    except OSError:
        raise urllib.error.URLError('备用 HTTPS 连接失败') from None
    if result.returncode:
        raise urllib.error.URLError('备用 HTTPS 连接失败')
    try:
        header, content = result.stdout.split(b'\n', 1)
        meta = json.loads(header)
    except (ValueError, TypeError):
        raise urllib.error.URLError('备用 HTTPS 响应无效') from None
    error = meta.get('error')
    if error == 'timeout':
        raise TimeoutError('网络请求超时')
    if error == 'limit' or len(content) > limit:
        raise ValueError('文件超过采集大小上限')
    if error == 'mime' or (mime and 200 <= meta.get('status', 0) < 300 and meta.get('type') != mime):
        raise ValueError('返回的文件类型与来源记录不同')
    if error or not isinstance(meta.get('status'), int):
        detail = meta.get('detail', '')
        suffix = '（'+detail+'）' if detail in {
            'ENOTFOUND', 'ECONNREFUSED', 'ECONNRESET', 'EACCES', 'EPERM', 'ETIMEDOUT',
            'UND_ERR_CONNECT_TIMEOUT', 'CERT_HAS_EXPIRED', 'UNABLE_TO_VERIFY_LEAF_SIGNATURE',
            'SELF_SIGNED_CERT_IN_CHAIN', 'DEPTH_ZERO_SELF_SIGNED_CERT',
            'ERR_TLS_CERT_ALTNAME_INVALID'} else ''
        raise urllib.error.URLError('备用 HTTPS 连接失败'+suffix)
    return meta, content


def _urllib_once(url, headers, body, limit, mime, deadline):
    req = urllib.request.Request(url, data=body, headers=headers)
    # A stuck connection must leave time for the alternate TLS implementation.
    timeout = min(4, _remaining(deadline) / 2) if body is None else _remaining(deadline)
    try:
        response = urllib.request.build_opener(NoRedirect()).open(req, timeout=timeout)
    except urllib.error.HTTPError as exc:
        try:
            return {'status': exc.code, 'type': exc.headers.get_content_type() if exc.headers else '',
                    'location': exc.headers.get('Location', '') if exc.headers else '',
                    'retry_after': exc.headers.get('Retry-After', '') if exc.headers else ''}, b''
        finally:
            exc.close()
    with response:
        meta = {'status': response.status, 'type': response.headers.get_content_type(),
                'location': response.headers.get('Location', ''),
                'retry_after': response.headers.get('Retry-After', '')}
        if mime and meta['type'] != mime:
            raise ValueError('返回的文件类型与来源记录不同')
        chunks, size = [], 0
        while True:
            wait = _remaining(deadline)
            # urllib's socket timeout normally resets on each read; also enforce
            # the task's total deadline while streaming a large media file.
            try:
                response.fp.raw._sock.settimeout(wait)
            except AttributeError:
                pass
            chunk = response.read1(min(65536, limit + 1 - size))
            if not chunk:
                break
            chunks.append(chunk)
            size += len(chunk)
            if size > limit:
                raise ValueError('文件超过采集大小上限')
        return meta, b''.join(chunks)


def _can_fallback(exc):
    reason = exc.reason if isinstance(exc, urllib.error.URLError) else exc
    # A certificate failure is a security error, never a reason to try another
    # trust store. All other retryable operations here are idempotent GETs.
    return not isinstance(reason, ssl.SSLCertVerificationError) and isinstance(
        reason, (ssl.SSLError, ConnectionError, socket.timeout, socket.gaierror, OSError))


def read(url, limit, *, headers=None, mime=None, timeout=15, validate_url=None,
         follow_redirects=False, body=None):
    """Return bytes; validate every URL; bound size, MIME, and total duration.

    GET: urllib first, Node fallback on a connection/TLS error only.
    POST: Node when available, otherwise urllib, with no automatic retry.
    This avoids submitting a model request twice after an ambiguous failure.
    """
    if not isinstance(limit, int) or limit < 0 or timeout <= 0:
        raise ValueError('无效的网络请求上限')
    headers = dict(headers or {})
    deadline = time.monotonic() + timeout
    binary = node_binary()
    for redirects in range(6):
        if validate_url:
            validate_url(url)
        if body is not None and binary:
            meta, content = _node_once(url, headers, body, limit, mime, deadline, binary)
        else:
            try:
                meta, content = _urllib_once(url, headers, body, limit, mime, deadline)
            except (urllib.error.URLError, ssl.SSLError, ConnectionError, socket.timeout) as exc:
                if body is not None or not binary or not _can_fallback(exc):
                    raise
                meta, content = _node_once(url, headers, body, limit, mime, deadline, binary)
        status = meta['status']
        if 200 <= status < 300:
            return content
        if status in (301, 302, 303, 307, 308) and follow_redirects and body is None and meta['location']:
            if redirects == 5:
                raise ValueError('来源链接重定向次数过多')
            url = urllib.parse.urljoin(url, meta['location'])
            continue
        response_headers = Message()
        response_headers['Content-Type'] = meta.get('type', '')
        if meta.get('retry_after'):
            response_headers['Retry-After'] = meta['retry_after']
        raise urllib.error.HTTPError(url, status, '来源请求失败', response_headers, None)
    raise ValueError('来源链接重定向次数过多')
