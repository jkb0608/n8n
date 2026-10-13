#!/usr/bin/env python3
"""TEST DOUBLE ONLY - not part of the pipeline, never invoked in production.

Why it exists
-------------
Module 03 of the workflow fetches the Jenkins console log through the Jenkins MCP
server (`getBuildLog`). On this instance that call needs an `httpHeaderAuth`
credential that does not exist yet: the MCP handshake succeeds anonymously, but
`tools/call` comes back as **HTTP 200 with `result.isError=true`**
(`AccessDeniedException3: anonymous is missing the Overall/Read permission`).
That is enough to exercise the blocked path, but not the diagnosis path.

This server speaks just enough MCP streamable-HTTP (initialize / notifications /
tools/list / tools/call) for the workflow's MCP Client node to complete, and
replays the **real** console log of Jenkins build 255 read from the Jenkins home
directory. Nothing is fabricated: the payload is the byte-exact build log, so the
probe counts, the `[stress]` summary and the context-gate tiers measured against
it are real numbers.

Usage
-----
    python tools/mcp_testdouble/mock_jenkins_mcp.py 8899
    # then temporarily point the workflow's MCP node at http://127.0.0.1:8899/mcp
    # and RESTORE it to http://localhost:8080/mcp-server/mcp afterwards,
    # verifying the restore by re-exporting the workflow and reading it back.

Rules
-----
* High port only; never bind 8080 (Jenkins) or 5678 (n8n).
* Stop it when done (`Get-NetTCPConnection -LocalPort 8899 -State Listen` must be
  empty) - a job-wrapper cancellation can leave the python child orphaned.
* `--log` selects the build log to replay; it defaults to build 255.
"""
import argparse
import json
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

JENKINS_HOME = Path(r'D:\Users\Administrator\Documents\tools\jenkins\home')
LINES: list = []


class Handler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'

    def log_message(self, *args):
        pass

    def _send(self, code, body, ctype, sid=None):
        raw = body.encode('utf-8')
        self.send_response(code)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(raw)))
        if sid:
            self.send_header('mcp-session-id', sid)
        self.end_headers()
        self.wfile.write(raw)

    def _sse(self, obj, sid):
        self._send(200, 'event: message\ndata: ' + json.dumps(obj, ensure_ascii=False) + '\n\n',
                   'text/event-stream', sid)

    def do_POST(self):
        n = int(self.headers.get('Content-Length') or 0)
        raw = self.rfile.read(n).decode('utf-8', 'replace')
        try:
            msg = json.loads(raw)
        except Exception:
            msg = {}
        sid = self.headers.get('mcp-session-id')
        method = msg.get('method')
        if method == 'initialize':
            self._sse({'jsonrpc': '2.0', 'id': msg.get('id'), 'result': {
                'protocolVersion': '2024-11-05',
                'capabilities': {'tools': {}},
                'serverInfo': {'name': 'mock-jenkins-mcp', 'version': '0.1'},
            }}, str(uuid.uuid4()))
            return
        if method == 'notifications/initialized':
            self._send(202, '', 'text/plain')
            return
        if method == 'tools/list':
            self._sse({'jsonrpc': '2.0', 'id': msg.get('id'), 'result': {'tools': [
                {'name': 'getBuildLog', 'description': 'replay of a real Jenkins build log',
                 'inputSchema': {'type': 'object',
                                 'properties': {'jobFullName': {'type': 'string'},
                                                'buildNumber': {'type': 'integer'},
                                                'limit': {'type': 'integer'}}}}
            ]}}, sid)
            return
        if method == 'tools/call':
            payload = {
                'message': 'Data retrieved successfully.',
                'status': 'COMPLETED',
                'result': {
                    'lines': LINES,
                    'hasMoreContent': False,
                    'startLine': 0,
                    'endLine': len(LINES) - 1,
                    'totalLines': len(LINES),
                    'nextCursor': None,
                },
            }
            self._sse({'jsonrpc': '2.0', 'id': msg.get('id'), 'result': {
                'content': [{'type': 'text', 'text': json.dumps(payload, ensure_ascii=False)}],
                'isError': False,
            }}, sid)
            return
        self._sse({'jsonrpc': '2.0', 'id': msg.get('id'),
                   'error': {'code': -32601, 'message': 'method not found: ' + str(method)}}, sid)

    def do_GET(self):
        self._send(405, 'no get stream', 'text/plain')


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('port', nargs='?', type=int, default=8899)
    ap.add_argument('--log', default=str(JENKINS_HOME / 'jobs' / 'gr5526-ci-v2' / 'builds' / '255' / 'log'))
    args = ap.parse_args()
    path = Path(args.log)
    global LINES
    LINES = path.read_text(encoding='utf-8', errors='replace').split('\n')
    print('mock mcp on 127.0.0.1:%d replaying %d lines from %s' % (args.port, len(LINES), path), flush=True)
    ThreadingHTTPServer(('127.0.0.1', args.port), Handler).serve_forever()


if __name__ == '__main__':
    main()
