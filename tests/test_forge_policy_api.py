from dataclasses import replace
from io import BytesIO
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from open_sprite_pipeline.forge_api import forge_router
from open_sprite_pipeline.forge_policy import Policy
from open_sprite_pipeline.forge_store import ForgeStore
from test_forge_policy import style

MODES = ('off', 'advisory', 'enforce')


class PolicyApiTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = ForgeStore(self.tmp.name, Policy())
        app = FastAPI()
        app.state.forge_store = self.store
        app.include_router(forge_router)
        self.http = TestClient(app)
        self.addCleanup(self.http.close)
        env = patch.dict('os.environ', {'FORGE_WORKER_TOKEN': ''})
        env.start(); self.addCleanup(env.stop)
        image = BytesIO(); Image.new('RGBA', (8, 8), 'red').save(image, format='PNG')
        self.png = image.getvalue()

    def post(self, path, body=None, code=200):
        response = self.http.post('/v1/forge' + path, json=body)
        self.assertEqual(response.status_code, code, response.text)
        return response.json()

    def get(self, path):
        response = self.http.get('/v1/forge' + path)
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def match(self, mode, *, missing=False):
        self.store.policy = Policy(mode=mode)
        response = self.http.post('/v1/forge/jobs', data={'asset': 'a', 'variant': 'v',
            'canonical_views': json.dumps(['front', 'roof'] if missing else ['front'])},
            files={'files': ('a.png', self.png, 'image/png')})
        self.assertEqual(response.status_code, 201, response.text)
        job = response.json(); base = '/jobs/' + job['id']
        claim = self.post('/worker/claim', {'job_id': job['id']})
        lease = claim['lease']['lease_id']
        response = self.http.post('/v1/forge' + base + '/panels/s', content=self.png,
                                  headers={'Content-Type': 'image/png', 'X-Forge-Lease': lease})
        self.assertEqual(response.status_code, 200, response.text)
        body = {'report': {'panels': [style()], 'views_missing': ['roof'] if missing else []}, 'lease_id': lease}
        return self.post(base + '/match', body), body

    def submit(self, job):
        return self.post('/jobs/' + job['id'] + '/review', {'mode': 'submit',
            'panels': [{'panel_id': 's', 'decision': 'accept', 'view': 'front'}],
            'views_missing': job['match']['views_missing']})

    def stage(self, job):
        if not job['match'].get('submitted'):
            job = self.submit(job)
        claim = self.post('/worker/claim', {'stages': ['review'], 'job_id': job['id']})
        base = '/jobs/' + job['id']; lease = claim['lease']['lease_id']
        response = self.http.post('/v1/forge' + base + '/staged/views/front.png', content=self.png,
                                  headers={'Content-Type': 'image/png', 'X-Forge-Lease': lease})
        self.assertEqual(response.status_code, 200, response.text)
        return self.post(base + '/staged', {'lease_id': lease}), lease

    def ready(self, mode):
        job, _ = self.match(mode)
        job, _ = self.stage(job)
        if job['state'] == 'staged':
            job = self.post('/jobs/' + job['id'] + '/approve')
        claim = self.post('/worker/claim', {'stages': ['baking'], 'job_id': job['id']})
        return self.post('/worker/complete', {'job_id': job['id'], 'lease_id': claim['lease']['lease_id']})

    def verdict(self, version, status, score):
        claim = self.post('/worker/claim', {'kind': 'critic'})
        self.assertEqual(claim['job_id'], version['job_id'])
        body = {'lease_id': claim['critic_lease']['lease_id'], 'verdict': {
            'status': status, 'overall': status, 'score': score, 'issues': [], 'summary': 'fixture', 'model': 'fake'}}
        path = f"/assets/a/variants/v/versions/{version['number']}/critic"
        return self.post(path, body), path, body

    def test_review_modes_retries_and_jobs_listing(self):
        for mode in MODES:
            with self.subTest(mode=mode):
                job, body = self.match(mode)
                self.assertEqual(job['match']['submitted'], mode == 'enforce')
                listed = next(j for j in self.get('/jobs') if j['id'] == job['id'])
                self.assertEqual(listed['policy']['mode'], mode)
                self.assertIsNone(listed['attention'])
                if mode == 'off':
                    self.assertNotIn('policy', job)
                else:
                    self.assertEqual(job['policy']['review']['actor'], 'policy')
                    self.assertEqual(job['policy']['review']['applied'], mode == 'enforce')
                    if mode == 'enforce':
                        self.assertEqual(job['match']['submitted_by'], 'policy')
                    retry = self.post('/jobs/' + job['id'] + '/match', body)
                    self.assertEqual(retry, job)
                    self.post('/jobs/' + job['id'] + '/match', {**body, 'lease_id': 'wrong'}, 409)
                    changed = {**body, 'report': {'panels': []}}
                    self.post('/jobs/' + job['id'] + '/match', changed, 409)
                    listed = next(j for j in self.get('/jobs') if j['id'] == job['id'])
                    self.assertEqual(listed['policy']['mode'], mode)

    def test_uncovered_attention_and_human_submit_clear(self):
        for mode in MODES:
            with self.subTest(mode=mode):
                job, _ = self.match(mode, missing=True)
                self.assertEqual('attention' in job, mode != 'off')
                if mode != 'off':
                    self.assertEqual(job['attention']['reason'], 'review')
                self.post('/jobs/' + job['id'] + '/policy/override', {'action': 'submit', 'author': 'operator'}, 409)
                submitted = self.submit(job)
                self.assertNotIn('attention', submitted)
                self.assertEqual(submitted['match']['submitted_by'], 'human')
                self.assertEqual(submitted['policy']['review']['actor'], 'human')

    def test_staging_modes_and_idempotency(self):
        for mode in MODES:
            with self.subTest(mode=mode):
                job, _ = self.match(mode)
                staged, lease = self.stage(job)
                self.assertEqual(staged['state'], 'queued_bake' if mode == 'enforce' else 'staged')
                if mode != 'off':
                    self.assertEqual(staged['policy']['bake']['thresholds'], self.store.policy.thresholds)
                    retry = self.post('/jobs/' + job['id'] + '/staged', {'lease_id': lease})
                    self.assertEqual(retry, staged)
                    self.post('/jobs/' + job['id'] + '/staged', {'lease_id': 'wrong'}, 409)

    def test_critic_modes_flag_accept_override_and_retry(self):
        for mode in MODES:
            for status, score in [('warn', 85), ('warn', 60), ('fail', 85)]:
                with self.subTest(mode=mode, status=status, score=score):
                    version = self.ready(mode)
                    verdict, path, body = self.verdict(version, status, score)
                    version = self.get(path.removesuffix('/critic'))
                    job = self.get('/jobs/' + version['job_id'])
                    flagged = status == 'fail' or score < 70
                    self.assertEqual(version['accepted'], mode == 'enforce' and not flagged)
                    self.assertEqual('attention' in version, mode != 'off' and flagged)
                    self.assertEqual('attention' in job, mode != 'off' and flagged)
                    if mode != 'off':
                        self.assertEqual(version['policy'], job['policy']['version'])
                        self.assertEqual(self.post(path, body), verdict)
                        self.assertEqual(self.get('/jobs/' + job['id'])['policy'], job['policy'])
                        self.post(path, {**body, 'verdict': {**body['verdict'], 'summary': 'changed'}}, 409)
                        row = next(v for v in self.get('/library') if v['number'] == version['number'])
                        self.assertEqual(row['policy'], {'mode': mode, 'action': 'flag' if flagged else 'accept'})
                    overridden = self.post('/jobs/' + job['id'] + '/policy/override', {'action': 'accept', 'author': 'operator'})
                    self.assertNotIn('attention', overridden)
                    self.assertTrue(overridden['accepted'])
                    self.assertEqual(overridden['policy']['overrides'][-1]['author'], 'operator')
                    self.assertNotIn('attention', self.get(path.removesuffix('/critic')))

    def test_policy_attention_order_and_dismiss(self):
        older, _ = self.match('advisory', missing=True)
        newer, _ = self.match('enforce', missing=True)
        attention = self.get('/attention')
        self.assertEqual([j['id'] for j in attention['jobs']], [older['id'], newer['id']])
        self.assertEqual(attention['versions'], [])
        self.assertEqual(self.get('/policy'), {'mode': 'enforce', 'thresholds': Policy('enforce').thresholds})
        dismissed = self.post('/jobs/' + older['id'] + '/policy/override', {'action': 'dismiss', 'author': 'operator'})
        self.assertNotIn('attention', dismissed)
        self.assertFalse(dismissed['match']['submitted'])
        self.assertEqual(len(dismissed['policy']['overrides']), 1)
        self.assertEqual(len(self.get('/attention')['jobs']), 1)

    def test_auto_bake_disabled_human_approve_and_submit_override(self):
        job, _ = self.match('advisory')
        job = self.post('/jobs/' + job['id'] + '/policy/override', {'action': 'submit', 'author': 'operator'})
        self.assertEqual(job['match']['submitted_by'], 'human')
        self.store.policy = replace(Policy('enforce'), auto_bake=False)
        staged, _ = self.stage(job)
        self.assertEqual(staged['state'], 'staged')
        self.assertEqual(staged['attention']['reason'], 'bake')
        approved = self.post('/jobs/' + job['id'] + '/approve')
        self.assertEqual(approved['state'], 'queued_bake')
        self.assertNotIn('attention', approved)

    def test_human_accept_and_dismiss_clear_version_attention(self):
        for action in ('accept', 'dismiss'):
            version = self.ready('enforce')
            _, path, _ = self.verdict(version, 'fail', 30)
            self.assertEqual(len(self.get('/attention')['versions']), 1)
            if action == 'accept':
                self.post(path.removesuffix('/critic') + '/accept')
            else:
                self.post('/jobs/' + version['job_id'] + '/policy/override', {'action': action, 'author': 'operator'})
            self.assertEqual(self.get('/attention'), {'jobs': [], 'versions': []})

    def test_critic_rerun_records_new_evidence_once(self):
        version = self.ready('enforce')
        _, path, first = self.verdict(version, 'fail', 30)
        original = self.get('/jobs/' + version['job_id'])['policy']['version']
        self.post(path + '/rerun')
        verdict, _, second = self.verdict(version, 'warn', 85)
        job = self.get('/jobs/' + version['job_id'])
        self.assertTrue(job['accepted'])
        self.assertNotIn('attention', job)
        self.assertEqual(job['policy']['version_history'], [original])
        self.assertEqual(self.post(path, second), verdict)
        self.assertEqual(self.get('/jobs/' + job['id'])['policy'], job['policy'])
        self.post(path, first, 409)

    def test_override_approve_and_validation(self):
        job, _ = self.match('advisory')
        job, _ = self.stage(job)
        approved = self.post('/jobs/' + job['id'] + '/policy/override', {'action': 'approve', 'author': 'operator'})
        self.assertEqual(approved['state'], 'queued_bake')
        self.assertEqual(approved['policy']['overrides'][-1]['action'], 'approve')
        self.post('/jobs/' + job['id'] + '/policy/override', {'action': 'accept', 'author': 'operator'}, 409)
        for body in ({'action': 'unknown', 'author': 'operator'}, {'action': 'dismiss', 'author': ' '},
                     {'action': 'dismiss'}):
            self.post('/jobs/' + job['id'] + '/policy/override', body, 422)
        self.assertEqual(len(self.get('/jobs/' + job['id'])['policy']['overrides']), 1)

    def test_policy_cannot_bypass_staging_evidence_or_worker_progress_gate(self):
        job, _ = self.match('enforce')
        claim = self.post('/worker/claim', {'stages': ['review'], 'job_id': job['id']})
        base = '/jobs/' + job['id']; lease = claim['lease']['lease_id']
        self.post(base + '/staged', {'lease_id': lease}, 409)
        for state in ('staged', 'queued_bake'):
            self.post(base + '/progress', {'lease_id': lease, 'state': state}, 409)
        self.assertEqual(self.get(base)['state'], 'review')
        self.assertNotIn('bake', self.get(base)['policy'])
