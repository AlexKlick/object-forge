from __future__ import annotations

import base64
from contextlib import redirect_stdout
from datetime import timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import BytesIO, StringIO
import json
import os
from pathlib import Path
import socket
import sys
import tempfile
from threading import Event, Thread
import unittest
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from open_sprite_pipeline.forge_api import forge_router
from open_sprite_pipeline.forge_critic import CriticClient
from open_sprite_pipeline.forge_store import ForgeStore
from open_sprite_pipeline.forge_worker import ForgeWorker

VERDICT = {'overall': 'pass', 'score': 91, 'issues': [
    {'frame': 0, 'severity': 'low', 'kind': 'texture', 'note': 'Minor seam.'}],
    'summary': 'Consistent geometry and coverage.'}


class ApiClient:
    def __init__(self, http):
        self.http = http

    def request(self, method, path, body=None):
        response = self.http.request(method, '/v1/forge' + path, json=body,
                                     headers={'X-Forge-Worker': 'critic-test'})
        response.raise_for_status()
        if response.status_code == 204:
            return None
        return response.json() if 'application/json' in response.headers.get('content-type', '') else response.content


class CriticTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='forge-p5-test-')
        self.addCleanup(self.tmp.cleanup)
        self.store = ForgeStore(Path(self.tmp.name) / 'store')
        app = FastAPI()
        app.state.forge_store = self.store
        app.include_router(forge_router)
        self.http = TestClient(app)
        self.addCleanup(self.http.close)
        env = patch.dict(os.environ, {'FORGE_WORKER_TOKEN': 'critic-test'})
        env.start()
        self.addCleanup(env.stop)
        self.client = ApiClient(self.http)
        self.worker = ForgeWorker(self.client, Path(self.tmp.name) / 'spike')
        self.version = self.ready()
        self.path = '/assets/a/variants/v/versions/1'
        self.original_job = self.store.get_job(self.version['job_id'])

    def ready(self, frames=8):
        job = self.store.create_job('a', 'v')
        for state in ('matching', 'review', 'staged', 'queued_bake', 'baking'):
            self.store.set_state(job['id'], state)
        stream = BytesIO()
        Image.new('RGBA', (1600, 900), (40, 100, 130, 255)).save(stream, format='PNG')
        return self.store.complete(job['id'], artifacts={
            f'turntable/tt_{i:02d}.png': stream.getvalue() for i in range(frames)}, metrics={
                'selfcheck': {'views': {'front': .99}}, 'coverage': {'front': .8},
                'bleed_faces': 0, 'fallback_split': {'gap': .1}, 'part_layers': {'core': 4}})

    def stub(self, outputs=None, *, probe_status=200, redirect=None, hold=None):
        outputs = list(outputs if outputs is not None else [json.dumps(VERDICT)])
        requests = []
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                payload = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                requests.append(payload)
                is_probe = payload['max_tokens'] == 8
                if hold is not None and not is_probe:
                    hold[0].set()
                    hold[1].wait(5)
                if redirect:
                    self.send_response(307)
                    self.send_header('Location', redirect)
                    self.end_headers()
                    return
                self.send_response(probe_status if is_probe else 200)
                self.send_header('Content-Type', 'application/json')
                self.end_headers()
                content = 'OK' if is_probe else outputs.pop(0)
                self.wfile.write(json.dumps({'choices': [{'message': {'content': content}}]}).encode())
        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        def stop():
            server.shutdown()
            thread.join(5)
            server.server_close()
        self.addCleanup(stop)
        self.worker.critic = CriticClient(f'http://127.0.0.1:{server.server_port}/v1')
        return requests

    def assert_ready(self):
        self.assertEqual(self.store.get_job(self.version['job_id']), self.original_job)
        self.assertEqual(self.client.request('GET', '/library')[0]['state'], 'ready')

    def test_happy_path_updates_version_file_and_library(self):
        requests = self.stub()
        verdict = self.worker.run_critic_next()
        self.assertEqual(verdict['status'], 'pass')
        self.assertEqual(verdict['score'], 91)
        self.assertEqual(verdict['issues'], VERDICT['issues'])
        self.assertEqual(self.client.request('GET', self.path + '/critic'), verdict)
        record = self.client.request('GET', self.path)['critic']
        self.assertEqual(record['issues'], 1)
        self.assertEqual(record['status'], 'pass')
        self.assertIn('at', record)
        self.assertEqual(self.client.request('GET', '/library')[0]['critic'], record)
        self.assertEqual(self.client.request('GET', '/assets/a/variants/v/versions')[0]['critic'], record)
        retained = self.store.root / 'assets/a/variants/v/versions/v1/critic/critic.json'
        self.assertEqual(json.loads(retained.read_text()), verdict)
        self.assertEqual(len(requests), 2)
        self.assert_ready()

    def test_valid_fences_are_stripped_without_retry(self):
        requests = self.stub(['```json\n' + json.dumps(VERDICT) + '\n```'])
        self.assertEqual(self.worker.run_critic_next()['status'], 'pass')
        self.assertEqual(len(requests), 2)

    def test_malformed_fenced_json_then_plain_json_repairs_once(self):
        requests = self.stub(['```json\n{"overall": "pass",}\n```', json.dumps(VERDICT)])
        self.assertEqual(self.worker.run_critic_next()['status'], 'pass')
        self.assertEqual(len(requests), 3)
        self.assertIn('reply with JSON only, no prose', requests[-1]['messages'][-1]['content'])

    def test_garbage_twice_is_error_and_does_not_fail_version(self):
        requests = self.stub(['garbage', 'x' * 800])
        verdict = self.worker.run_critic_next()
        self.assertEqual(verdict['status'], 'error')
        self.assertEqual(verdict['excerpt'], 'x' * 400)
        self.assertEqual(self.client.request('GET', self.path + '/critic'), verdict)
        self.assertIsNone(self.worker.run_critic_next())
        self.assertEqual(len(requests), 3)
        self.assert_ready()

    def test_connection_refused_probe_once_skips_even_after_rerun(self):
        sock = socket.socket()
        sock.bind(('127.0.0.1', 0))  # Reserved but not listening: deterministic refusal.
        self.addCleanup(sock.close)
        self.worker.critic = CriticClient(f'http://127.0.0.1:{sock.getsockname()[1]}/v1')
        log = StringIO()
        with patch.object(self.worker.critic.opener, 'open', wraps=self.worker.critic.opener.open) as opened, redirect_stdout(log):
            first = self.worker.run_critic_next()
            self.client.request('POST', self.path + '/critic/rerun', {})
            second = self.worker.run_critic_next()
        self.assertEqual(opened.call_count, 1)
        self.assertEqual(first['status'], 'skipped')
        self.assertEqual(second['status'], 'skipped')
        self.assertEqual(log.getvalue().count('CRITIC disabled for process lifetime:'), 1)
        self.assertFalse(first['probe']['http_200'])
        self.assert_ready()

    def test_request_images_jpeg_downscaled_four_evenly_sampled_frames_and_metrics(self):
        requests = self.stub()
        self.worker.run_critic_next()
        content = requests[1]['messages'][1]['content']
        images = [item for item in content if item['type'] == 'image_url']
        self.assertEqual(len(images), 4)
        for item in images:
            prefix, encoded = item['image_url']['url'].split(',')
            self.assertEqual(prefix, 'data:image/jpeg;base64')
            with Image.open(BytesIO(base64.b64decode(encoded))) as frame:
                self.assertLessEqual(max(frame.size), 768)
                self.assertEqual(frame.size, (768, 432))
                self.assertEqual(frame.format, 'JPEG')
        self.assertEqual([item['text'].split(':')[0] for item in content[1:] if item['type'] == 'text'],
                         ['Frame 0', 'Frame 2', 'Frame 4', 'Frame 6'])
        for key in ('selfcheck', 'coverage', 'bleed_faces', 'fallback_split', 'part_layers'):
            self.assertIn(key, content[0]['text'])
        for request in requests:
            self.assertEqual(request['chat_template_kwargs'], {'enable_thinking': False})
        self.assertTrue(self.worker.critic.probe_result['kwarg_accepted'])
        self.assertFalse(self.worker.critic.probe_result['kwarg_echoed'])

    def test_rerun_pending_invalidates_stale_lease_and_hides_old_verdict(self):
        self.stub([json.dumps(VERDICT), json.dumps(VERDICT)])
        self.worker.run_critic_next()
        self.assertEqual(self.client.request('POST', self.path + '/critic/rerun', {}), {'status': 'pending'})
        self.assertEqual(self.client.request('GET', self.path + '/critic'), {'status': 'pending'})
        old = self.client.request('POST', '/worker/claim', {'kind': 'critic'})
        self.assertIsNone(self.client.request('POST', '/worker/claim', {'kind': 'critic'}))
        self.client.request('POST', self.path + '/critic/rerun', {})
        response = self.http.post('/v1/forge' + self.path + '/critic', headers={'X-Forge-Worker': 'critic-test'}, json={
            'lease_id': old['critic_lease']['lease_id'], 'verdict': {**VERDICT, 'status': 'pass', 'model': 'stub'}})
        self.assertEqual(response.status_code, 409)
        self.assertEqual(self.worker.run_critic_next()['status'], 'pass')
        self.assert_ready()

    def test_newest_first_and_expired_lease_reclaimed(self):
        newer = self.ready()
        claimed = self.store.claim_job('critic')
        self.assertEqual(claimed['number'], newer['number'])
        now = self.store._now()
        with patch.object(self.store, '_now', return_value=now + timedelta(seconds=301)):
            reclaimed = self.store.claim_job('critic')
        self.assertEqual(reclaimed['number'], newer['number'])
        self.assertNotEqual(reclaimed['critic_lease']['lease_id'], claimed['critic_lease']['lease_id'])

    def test_url_refusal_and_localhost_numeric_pinning(self):
        for url in ('https://example.org/v1', 'http://192.168.1.2/v1', 'http://127.0.0.2/v1',
                    'http://localhost.example.org/v1', 'http://user@127.0.0.1/v1',
                    'http://127.0.0.1/v1?redirect=external', 'http://[::ffff:127.0.0.1]/v1'):
            with self.subTest(url=url), self.assertRaises(ValueError):
                CriticClient(url)
        self.assertEqual(CriticClient('http://localhost:18001/v1').url, 'http://127.0.0.1:18001/v1/chat/completions')
        self.assertEqual(CriticClient('http://[::1]:18001/v1').url, 'http://[::1]:18001/v1/chat/completions')

    def test_redirect_refused_no_follow_and_probe_not_retried(self):
        requests = self.stub(redirect='http://external.invalid/chat/completions')
        self.assertEqual(self.worker.run_critic_next()['status'], 'skipped')
        self.worker.critic.probe()
        self.assertEqual(len(requests), 1)

    def test_configuration_disabled_does_not_open_network(self):
        with patch.dict(os.environ, {'FORGE_CRITIC_ENABLED': '0'}):
            self.worker.init_critic()
        with patch.object(self.worker.critic.opener, 'open', side_effect=AssertionError('Network forbidden')) as opened:
            self.assertEqual(self.worker.run_critic_next()['status'], 'skipped')
        opened.assert_not_called()
        self.assert_ready()

    def test_invalid_model_schema_repairs_then_errors(self):
        bad = {**VERDICT, 'score': True}
        self.stub([json.dumps(bad), json.dumps({**VERDICT, 'overall': 'maybe'})])
        self.assertEqual(self.worker.run_critic_next()['status'], 'error')
        self.assert_ready()

    def test_fail_verdict_is_advisory_and_job_remains_ready(self):
        self.stub([json.dumps({**VERDICT, 'overall': 'fail', 'score': 10})])
        self.assertEqual(self.worker.run_critic_next()['status'], 'fail')
        self.assert_ready()

    def test_missing_frames_skips(self):
        self.version = self.ready(frames=0)
        self.stub()
        self.assertEqual(self.worker.run_critic_next()['status'], 'skipped')

    def test_probe_http_rejection_disables_for_lifetime(self):
        requests = self.stub(probe_status=400)
        self.assertEqual(self.worker.run_critic_next()['status'], 'skipped')
        self.worker.critic.probe()
        self.assertEqual(len(requests), 1)

    def test_slow_critic_does_not_block_pipeline_claim_and_bake(self):
        entered, release, stopped = Event(), Event(), Event()
        self.stub(hold=(entered, release))
        thread = Thread(target=self.worker.critic_loop, args=(stopped, .01), daemon=True)
        thread.start()
        try:
            self.assertTrue(entered.wait(5))
            job = self.store.create_job('b', 'v')
            for state in ('matching', 'review', 'staged', 'queued_bake'):
                self.store.set_state(job['id'], state)
            def complete(job, **kwargs):
                self.assertEqual(job['state'], 'baking')
                return self.store.complete(job['id'], lease_id=job['lease']['lease_id'])
            with patch.object(self.worker, 'process', side_effect=complete):
                self.assertEqual(self.worker.run_next()['job_id'], job['id'])
            self.assertFalse(release.is_set())
        finally:
            stopped.set()
            release.set()
            thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assert_ready()

    def test_critic_write_requires_worker_token(self):
        response = self.http.post('/v1/forge' + self.path + '/critic', json={'lease_id': 'x', 'verdict': {}})
        self.assertEqual(response.status_code, 403)


if __name__ == '__main__':
    unittest.main()
