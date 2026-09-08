import base64
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from contextlib import redirect_stdout
from io import BytesIO, StringIO
import json
import os
from pathlib import Path
import sys
from threading import Thread
import unittest
from unittest.mock import Mock, patch

import numpy as np
from PIL import Image
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from open_sprite_pipeline.forge_worker import ForgeWorker, png, load_tool, main
from open_sprite_pipeline.style_client import StyleClient, StyleBusy, StyleError
from open_sprite_pipeline.forge_policy import Policy
import test_forge_generate_worker as fixture


class FakeStyle:
    def __init__(self, busy=0, identity=True):
        self.busy = busy
        self.identity = identity
        self.requests = []

    def render(self, request):
        self.requests.append(request)
        if self.busy:
            self.busy -= 1
            raise StyleBusy(100, 4500)
        init = Image.open(BytesIO(base64.b64decode(request['init_png']))).convert('RGBA')
        candidates = []
        for i, seed in enumerate(request['seeds']):
            candidate = init.copy()
            if not (self.identity and i == 0):
                hsv = np.array(init.convert('RGB').resize((512, 512)).convert('HSV'))
                hsv[:, :, 0] = ((hsv[:, :, 0].astype(int) + 12) % 256).astype('uint8')
                rgb = np.array(Image.fromarray(hsv, 'HSV').convert('RGB')).astype(np.int16)
                noise = np.random.default_rng(seed).integers(-20, 21, size=(512, 512, 1))
                candidate = Image.fromarray(np.clip(rgb + noise, 0, 255).astype('uint8')).resize(init.size)
                candidate.putalpha(init.getchannel('A'))
            candidates.append({'seed': seed, 'png': base64.b64encode(png(candidate)).decode(),
                               'working_size': [768, 768], 'seconds': 0.01})
        return {'candidates': candidates, 'prompt_tokens': 51, 'truncated': True,
                'model': 'fake-style', 'box': [400, 400, 1648, 1648], 'scale': 1, 'peak_mb': None}


class StyleWorkerTests(unittest.TestCase):
    snapshot = fixture.GenerateWorkerTests.snapshot

    def setUp(self):
        fixture.GenerateWorkerTests.setUp(self)
        self.fake = FakeStyle()
        self.worker = ForgeWorker(self.client, self.assets, style=self.fake)
        # Keep full SE1 frames for metrics/checks, while using compressible noise.
        render = fixture.BLOCKOUT.replace("image.save(out / (view + '.png'))", """
    image = Image.new('RGBA', (2048, 2048), (0, 0, 0, 0))
    ImageDraw.Draw(image).rectangle((400, 400, 1647, 1647), fill='#778899')
    image.save(out / (view + '.png'))""")
        render = render.replace("variant = sys.argv[3] if len(sys.argv) > 3 else 'default'",
            "variant = sys.argv[3] if len(sys.argv) > 3 else 'default'\nspec['palette'].update(spec['variants'][variant].get('palette', {}))")
        (self.root / 'fake blockout.py').write_text(render)
        bake = (self.root / 'fake bake.py').read_text()
        bake = bake.replace('assert sorted(p.name for p in views.glob("*.png")) in ([], ["front_left.png"])',
                            'assert all(Image.open(p).mode == "RGBA" for p in views.glob("*.png"))')
        (self.root / 'fake bake.py').write_text(bake)

    def create(self, *, intent='from_spec', style=None, **fields):
        files = []
        if intent == 'generate':
            files.append(('files', ('photo.png', png(self.image), 'image/png')))
        if style:
            files.append(('style_refs[]', ('ref.png', png(self.image), 'image/png')))
        response = self.http.post('/v1/forge/jobs', data={'asset': 'a', 'variant': 'v',
            'intent': intent, 'params': '{"turntable": 1}', 'style': json.dumps(style), **fields}, files=files)
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()

    def submit(self, job):
        panels = [{key: p[key] for key in ('panel_id', 'decision', 'view')}
                  for p in job['match']['panels']]
        self.client.request('POST', f"/jobs/{job['id']}/review", {
            'mode': 'submit', 'panels': panels, 'views_missing': job['match']['views_missing']})
        return self.worker.run_next()

    def bake(self, job):
        self.client.request('POST', f"/jobs/{job['id']}/approve")
        ready = self.worker.run_next()
        self.assertEqual(ready['state'], 'ready')
        return self.store.get_version('a', 'v', ready['version_number'])

    def test_enforce_style_runs_unattended_to_accepted_version(self):
        self.store.policy = Policy('enforce')
        self.worker.style = FakeStyle(identity=False)
        created = self.create(style={'enabled': True, 'seeds_per_view': 1})
        self.worker.critic = Mock()
        self.worker.critic.review.return_value = {
            'status': 'warn', 'overall': 'warn', 'score': 85, 'issues': [],
            'summary': 'Fake critic: acceptable texture warning.', 'model': 'fake-critic'}
        with patch.object(self.client, 'request', wraps=self.client.request) as requests:
            review = self.worker.run_next()
            self.assertEqual(review['state'], 'review')
            self.assertTrue(review['match']['submitted'])
            self.assertEqual(review['match']['submitted_by'], 'policy')
            queued = self.worker.run_next()
            self.assertEqual(queued['state'], 'queued_bake')
            ready = self.worker.run_next()
            self.assertEqual(ready['state'], 'ready')
            self.worker.run_critic_next()
        self.assertFalse(any(call.args[0] == 'POST' and call.args[1].endswith(('/review', '/approve'))
                             for call in requests.call_args_list))
        version = self.store.get_version('a', 'v', ready['version_number'])
        self.assertTrue(version['accepted'])
        self.assertEqual(version['policy']['action'], 'accept')
        job = self.store.get_job(created['id'])
        self.assertNotIn('attention', job)
        self.assertEqual(job['policy']['version'], version['policy'])

    def test_style_checks_inherit_the_render_verdict(self):
        # A roof view the camera frames off-centre fails style_check's `centered`
        # rule in the render itself; candidates keep that alpha byte-for-byte, so
        # the failure is inherited and must not escalate the review (set round
        # finding: city_hall/public roof, 2026-09-07).
        self.store.policy = Policy('enforce')
        self.worker.style = FakeStyle(identity=False)
        script = self.root / 'fake blockout.py'
        script.write_text(script.read_text().replace('(400, 400, 1647, 1647)', '(400, 600, 1647, 1847)'))
        created = self.create(style={'enabled': True, 'seeds_per_view': 2})
        review = self.worker.run_next()
        self.assertEqual(review['state'], 'review')
        self.assertTrue(review['match']['submitted'], review.get('attention'))
        self.assertEqual(review['match']['submitted_by'], 'policy')
        self.assertNotIn('attention', review)
        style_panels = [p for p in review['match']['panels'] if p.get('source') == 'style']
        self.assertEqual(len(style_panels), 2 * len(review['canonical_views']))
        for panel in style_panels:
            self.assertEqual(panel['checks']['inherited'], ['centered'])
            self.assertFalse(panel['checks']['render_pass'])
            self.assertFalse(panel['checks']['rules']['centered']['pass'])
            self.assertTrue(panel['checks']['pass'])
        self.assertIn('STYLE-CHECK', json.dumps(self.store.get_job(created['id'])))
        self.assertEqual(self.snapshot(), self.before, 'SPIKE TREE CHANGED')

    def test_enforce_identity_style_escalates_with_attention(self):
        self.store.policy = Policy('enforce')
        self.worker.style = FakeStyle(identity=True)
        self.create(style={'enabled': True, 'seeds_per_view': 1})
        job = self.worker.run_next()
        self.assertEqual(job['state'], 'review')
        self.assertFalse(job['match']['submitted'])
        self.assertEqual(job['attention']['reason'], 'review')
        self.assertEqual(len(job['policy']['review']['views_missing']), len(job['canonical_views']))
        self.assertTrue(all('failed metrics' in reason for reason in job['attention']['detail']))
        attention = self.http.get('/v1/forge/attention').json()
        self.assertEqual([j['id'] for j in attention['jobs']], [job['id']])
        self.assertIsNone(self.worker.run_next())

    def test_set_palette_only_child_keeps_enforce_policy_hooks(self):
        self.store.policy = Policy('enforce')
        self.worker.publish_catalog()
        response = self.http.post('/v1/forge/sets', data={'manifest':
            'palette_only: true\nrequires: [{asset: a, variant: v}]'})
        self.assertEqual(response.status_code, 201, response.text)
        set_id = response.json()['id']
        launch = self.http.post(f'/v1/forge/sets/{set_id}/launch')
        self.assertEqual(launch.status_code, 200, launch.text)
        child = launch.json()['launched'][0]
        job = self.worker.run_next()
        self.assertEqual(job['id'], child['job_id'])
        self.assertEqual(job['set_id'], set_id)
        self.assertEqual(job['state'], 'queued_bake')
        self.assertEqual(job['match']['submitted_by'], 'policy')
        self.assertEqual(self.store.get_set(set_id)['pairs']['a/v']['policy_mode'], 'enforce')
        self.assertEqual(self.snapshot(), self.before, 'SPIKE TREE CHANGED')

    def test_enforce_palette_only_skips_worker_double_submit(self):
        self.store.policy = Policy('enforce')
        self.create(palette_only='true')
        with patch.object(self.client, 'request', wraps=self.client.request) as requests:
            job = self.worker.run_next()
        self.assertEqual(job['state'], 'queued_bake')
        self.assertEqual(job['match']['submitted_by'], 'policy')
        self.assertFalse(any(call.args[0] == 'POST' and call.args[1].endswith('/review')
                             for call in requests.call_args_list))

    def test_catalog_startup_and_after_bake_read_only_with_bad_yaml(self):
        (self.assets / 'specs/broken.yaml').write_text('asset: [unterminated')
        (self.assets / 'prompts/broken.yaml').write_text('prompt: [unterminated')
        # Fixture setup is finished; every worker action must preserve this tree.
        self.before = self.snapshot()
        output = StringIO()
        with patch.dict(os.environ, {'FORGE_ONCE': '1', 'FORGE_CRITIC_ENABLED': '0'}), \
                patch('open_sprite_pipeline.forge_worker.ForgeClient', return_value=self.client), \
                patch('open_sprite_pipeline.forge_worker.ForgeWorker', return_value=self.worker), \
                patch('open_sprite_pipeline.forge_worker.signal.signal'), redirect_stdout(output):
            self.assertEqual(main(), 0)
        catalog = self.store.get_catalog()
        self.assertEqual([spec['asset'] for spec in catalog['specs']], ['a', 'prop'])
        self.assertEqual(catalog['specs'][0]['variants'], ['default', 'v'])
        self.assertEqual(len(catalog['specs'][0]['views']), 7)
        self.assertEqual(catalog['specs'][0]['views'], sorted(catalog['specs'][0]['views']))
        self.assertEqual(catalog['prompts'], ['a'])
        self.assertEqual(catalog['assets_root'], str(self.assets))
        self.assertEqual([error['file'] for error in catalog['errors']], ['prompts/broken.yaml', 'specs/broken.yaml'])
        self.assertTrue(all(error['error'] for error in catalog['errors']))
        self.assertIn('CATALOG specs=2 prompts=1', output.getvalue())
        self.create(palette_only='true')
        staged = self.worker.run_next()
        with redirect_stdout(output):
            self.bake(staged)
        self.assertEqual(output.getvalue().count('CATALOG specs=2 prompts=1'), 2)
        refreshed = self.store.get_catalog()
        self.assertNotEqual(refreshed['published_at'], catalog['published_at'])
        self.assertEqual(refreshed['specs'], catalog['specs'])
        self.assertEqual(self.snapshot(), self.before, 'SPIKE TREE CHANGED')

    def test_catalog_publish_failure_is_nonfatal_at_startup_and_completion(self):
        request = self.client.request
        def unavailable(method, path, *args, **kwargs):
            if path == '/worker/catalog':
                raise OSError('API restarting')
            return request(method, path, *args, **kwargs)
        output = StringIO()
        with patch.object(self.client, 'request', side_effect=unavailable), redirect_stdout(output):
            with patch.dict(os.environ, {'FORGE_ONCE': '1', 'FORGE_CRITIC_ENABLED': '0'}), \
                    patch('open_sprite_pipeline.forge_worker.ForgeClient', return_value=self.client), \
                    patch('open_sprite_pipeline.forge_worker.ForgeWorker', return_value=self.worker), \
                    patch('open_sprite_pipeline.forge_worker.signal.signal'):
                self.assertEqual(main(), 0)
            self.create(palette_only='true')
            version = self.bake(self.worker.run_next())
        self.assertEqual(version['number'], 1)
        self.assertEqual(output.getvalue().count('CATALOG publish failed: OSError: API restarting'), 2)
        self.assertEqual(self.snapshot(), self.before, 'SPIKE TREE CHANGED')

    def test_palette_only_seven_views_auto_staged_and_baked(self):
        created = self.create(palette_only='true')
        staged = self.worker.run_next()
        self.assertEqual(staged['state'], 'staged')
        self.assertEqual(staged['match']['panels'], [])
        self.assertEqual(len(staged['match']['views_missing']), 7)
        self.assertEqual(staged['match']['views_missing'], staged['canonical_views'])
        self.assertIn('MATCH skipped: palette-only authored spec', staged['worker_log'])
        self.assertEqual(staged['generate']['blockout']['params']['height'], 12)
        self.assertEqual(staged['generate']['blockout']['params']['plinth'], {'floors': 8, 'floor_height': 1.5})
        self.assertEqual(staged['generate']['blockout']['palette']['body']['hex'], '8899aa')
        ws = self.worker.workspace_root(created)
        self.assertFalse((ws / 'synth_count').exists())
        with patch.dict(os.environ, {'FORGE_BAKE_CMD': ''}):
            self.assertIn('--allow-palette-only', self.worker.bake_command(staged))
        version = self.bake(staged)
        self.assertEqual(version['inputs']['staged_views'], [])
        self.assertTrue({'blockout/spec.yaml', 'blockout/build_plan.json'} <= set(version['artifacts']))
        spec = yaml.safe_load(self.store.artifact_file('a', 'v', version['number'], 'blockout/spec.yaml').read_bytes())
        self.assertEqual(spec['variants']['v']['palette']['body']['hex'], '8899aa')
        self.assertEqual(len(spec['views']), 7)

    def test_style_seven_views_review_exact_staging_and_version_custody(self):
        self.create(style={'enabled': True})
        job = self.worker.run_next()
        self.assertEqual(job['state'], 'review')
        self.assertFalse(job['match']['submitted'])
        self.assertEqual(len(job['match']['panels']), 21)
        self.assertEqual(job['match']['views_missing'], [])
        self.assertEqual(job['match']['extras'], [])
        self.assertEqual(len(self.fake.requests), 7)
        report = self.store.get_style_report(job['id'])
        ws = self.worker.workspace_root(job)
        original = {}
        for i, view in enumerate(job['canonical_views']):
            req = self.fake.requests[i]
            self.assertEqual(req['seeds'], [1000+i, 1100+i, 1200+i])
            self.assertEqual(len(req['refs']), 1)
            self.assertEqual(req['long_side'], 768)
            self.assertIn('Painted stone workshop', req['prompt'])
            self.assertIn('body', req['prompt'])
            entry = report['views'][view]
            self.assertNotEqual(entry['chosen'], 1000+i)
            self.assertFalse(entry['metrics'][str(1000+i)]['metrics']['pass'])
            self.assertIn('change<min', entry['metrics'][str(1000+i)]['metrics']['reasons'])
            self.assertTrue(entry['metrics'][str(entry['chosen'])]['metrics']['pass'])
            findings = [p for p in job['match']['panels'] if p['style_view'] == view]
            self.assertEqual(sum(p['decision'] == 'accept' for p in findings), 1)
            self.assertTrue(all(p['reason'] == 'alternate' for p in findings if p['decision'] == 'reject'))
            name = f"style/{view}/{entry['chosen']}.png"
            original[name] = (ws / name).read_bytes()
            (ws / name).write_bytes(b'other job overwrote workspace')
        self.assertTrue(any('STYLE-TRUNCATED' in line for line in job['worker_log']))
        staged = self.submit(job)
        self.assertEqual(staged['state'], 'staged')
        for view, entry in report['views'].items():
            name = f"style/{view}/{entry['chosen']}.png"
            self.assertEqual(self.store.staged_view_file(job['id'], view).read_bytes(), original[name])
            self.assertEqual((ws / name).read_bytes(), original[name])
            (ws / name).write_bytes(b'overwritten again before bake')
        version = self.bake(staged)
        self.assertIn('style/report.json', version['artifacts'])
        self.assertEqual(version['metrics']['style']['model'], 'fake-style')
        self.assertEqual(version['metrics']['style']['refs'], 1)
        for view, summary in version['metrics']['style']['views'].items():
            self.assertEqual(set(summary), {'seed', 'auto_seed', 'staged', 'pass', 'palette_drift', 'change', 'detail_gain'})
            self.assertEqual((summary['seed'], summary['auto_seed'], summary['staged']),
                             (report['views'][view]['chosen'], report['views'][view]['chosen'], True))
            self.assertTrue(summary['pass'])
            self.assertGreaterEqual(summary['change'], 4.0)
        self.assertTrue(any(line.startswith('STYLE front') and ' change=' in line and ' pass=True' in line
                            for line in job['worker_log']))
        for name, data in original.items():
            self.assertEqual(self.store.artifact_file('a', 'v', version['number'], name).read_bytes(), data)

    def test_human_seed_choice_drives_staging_and_version_custody(self):
        # SE3 live round defect: the version retained the worker's ranked seed
        # although the review accepted (and the bake consumed) another one.
        self.create(style={'enabled': True, 'seeds_per_view': 2})
        job = self.worker.run_next()
        self.assertEqual(job['state'], 'review')
        report = self.store.get_style_report(job['id'])
        view, other = job['canonical_views'][0], job['canonical_views'][1]
        auto = report['views'][view]['chosen']
        alternate = next(seed for seed in report['views'][view]['seeds'] if seed != auto)
        panels = []
        for p in job['match']['panels']:
            decision = p['decision']
            if p['style_view'] == view:
                decision = 'accept' if p['seed'] == alternate else 'reject'
            panels.append({'panel_id': p['panel_id'], 'decision': decision, 'view': p['view']})
        self.client.request('POST', f"/jobs/{job['id']}/review", {'mode': 'submit', 'panels': panels, 'views_missing': []})
        staged = self.worker.run_next()
        self.assertEqual(staged['state'], 'staged')
        alt_bytes = self.store.style_candidate_file(job['id'], view, alternate).read_bytes()
        self.assertEqual(self.store.staged_view_file(job['id'], view).read_bytes(), alt_bytes)
        ws = self.worker.workspace_root(job)
        (ws / f'style/{view}/{alternate}.png').write_bytes(b'other job overwrote workspace')
        version = self.bake(staged)
        self.assertIn(f'style/{view}/{alternate}.png', version['artifacts'])
        self.assertNotIn(f'style/{view}/{auto}.png', version['artifacts'])
        self.assertEqual(self.store.artifact_file('a', 'v', version['number'], f'style/{view}/{alternate}.png').read_bytes(), alt_bytes)
        summary = version['metrics']['style']['views'][view]
        self.assertEqual((summary['seed'], summary['auto_seed'], summary['staged']), (alternate, auto, True))
        self.assertEqual(summary['palette_drift'], report['views'][view]['metrics'][str(alternate)]['metrics']['palette_drift'])
        self.assertEqual(summary['pass'], report['views'][view]['metrics'][str(alternate)]['metrics']['pass'])
        untouched = version['metrics']['style']['views'][other]
        self.assertEqual((untouched['seed'], untouched['auto_seed']), (report['views'][other]['chosen'],) * 2)

    def test_photo_style_depth_copy_busy_retry_and_overrides(self):
        self.fake.busy = 2
        self.create(intent='generate', style={'enabled': True, 'seeds_per_view': 2,
                    'prompt_override': 'Metal toy workshop', 'negative_override': 'blurry', 'long_side': 512})
        with patch('open_sprite_pipeline.forge_worker.time.sleep') as sleep:
            job = self.worker.run_next()
        self.assertEqual(sleep.call_count, 2)
        self.assertEqual(len(self.fake.requests), 7)
        self.assertEqual(job['state'], 'review')
        self.assertEqual(job['match']['views_missing'], [])
        self.assertEqual(len([p for p in job['match']['panels'] if p['decision'] == 'accept']), 5)
        self.assertTrue(all(p['source'] == 'style' for p in job['match']['panels'] if p['decision'] == 'accept'))
        self.assertTrue(any('STYLE deferred: gpu busy free=100 needed=4500' in s for s in job['worker_log']))
        for req in self.fake.requests:
            self.assertIn('Metal toy workshop', req['prompt'])
            self.assertIn('hand-painted', req['prompt'])
            self.assertEqual(req['negative'], 'blurry')
            self.assertEqual(req['long_side'], 512)
        ws = self.worker.workspace_root(job)
        for view in job['canonical_views']:
            self.assertEqual((ws / f'renders/a/v/passes/{view}.depth.png').read_bytes(),
                             (ws / f'renders/a/default/passes/{view}.depth.png').read_bytes())

    def test_missing_depth_clears_stale_pass_and_fails(self):
        job = self.create(style={'enabled': True})
        ws = self.worker.workspace_root(job)
        (ws / 'renders/a/v/passes').mkdir()
        (ws / 'renders/a/v/passes/back.depth.png').write_bytes(b'stale')
        script = self.root / 'fake blockout.py'
        script.write_text(script.read_text().replace("Image.new('I;16', image.size, 32000).save(out / 'passes' / (view + '.depth.png'))", 'pass'))
        with self.assertRaisesRegex(ValueError, 'No depth pass for back; regenerate the blockout with the current tools'):
            self.worker.run_next()
        self.assertEqual(self.store.get_job(job['id'])['state'], 'failed')

    def test_undeclared_variant_without_prompt_fails_before_rendering(self):
        job = self.create(variant='new_variant', style={'enabled': True})
        with self.assertRaisesRegex(ValueError, "No prompt for variant 'new_variant' in prompts/a.yaml"):
            self.worker.run_next()
        self.assertEqual(self.store.get_job(job['id'])['state'], 'failed')
        self.assertEqual(self.fake.requests, [])
        # prompt_override is the documented way to style an undeclared alias.
        self.create(variant='new_variant', style={'enabled': True, 'prompt_override': 'Painted toy kiosk'})
        review = self.worker.run_next()
        self.assertEqual(review['state'], 'review')
        self.assertEqual(len(self.fake.requests), 7)
        self.assertIn('Painted toy kiosk', self.fake.requests[0]['prompt'])

    def test_unconfigured_style_and_busy_timeout_fail_clearly(self):
        with patch.dict(os.environ, {'FORGE_STYLE_URL': ''}):
            self.worker = ForgeWorker(self.client, self.assets)
        job = self.create(style={'enabled': True})
        with self.assertRaisesRegex(ValueError, 'Styling engine not configured'):
            self.worker.run_next()
        self.assertEqual(self.store.get_job(job['id'])['state'], 'failed')
        self.worker = ForgeWorker(self.client, self.assets, style=FakeStyle(busy=100))
        self.worker.style_wait_s = 0
        self.create(style={'enabled': True})
        with self.assertRaisesRegex(StyleError, 'FORGE_STYLE_WAIT_S elapsed'):
            self.worker.run_next()

    def test_spec_alias_rewrites_asset_and_preserves_source(self):
        self.create(asset='renamed', spec_asset='a', palette_only='1')
        job = self.worker.run_next()
        self.assertEqual(job['state'], 'staged')
        data = self.store.blockout_file(job['id'], 'spec.yaml').read_bytes()
        self.assertEqual(yaml.safe_load(data)['asset'], 'renamed')
        self.assertEqual(yaml.safe_load((self.assets / 'specs/a.yaml').read_bytes())['asset'], 'a')
        ws = self.worker.workspace_root(job)
        self.assertEqual((ws / 'prompts/renamed.yaml').read_bytes(), (self.assets / 'prompts/a.yaml').read_bytes())
        for name in ('style_prompt', 'style_metrics', 'style_check', 'style_compose'):
            self.assertEqual(Path(load_tool(self.assets, name).__file__), self.assets / 'tools' / (name + '.py'))

    def test_authored_prop_without_plinth_and_undeclared_variant_alias(self):
        self.create(spec_asset='prop', variant='new_variant', palette_only='true')
        job = self.worker.run_next()
        self.assertEqual(job['state'], 'staged')
        params = job['generate']['blockout']['params']
        self.assertIsNone(params['plinth'])
        self.assertIsNone(params['tower'])
        self.assertEqual(params['footprint'], {'width': 8, 'depth': 6})
        self.assertEqual(params['height'], 12)
        spec = yaml.safe_load(self.store.blockout_file(job['id'], 'spec.yaml').read_bytes())
        self.assertEqual(spec['variants']['new_variant'], {})
        self.assertIn('v', spec['variants'])

    def test_authored_iteration_descendants_keep_workspace_spec_and_bakes(self):
        self.create(palette_only='true')
        parent = self.bake(self.worker.run_next())
        source = self.store.artifact_file('a', 'v', parent['number'], 'blockout/spec.yaml').read_bytes()
        for intent in ('iterate_params', 'iterate_views', 'iterate_blockout'):
            with self.subTest(intent=intent):
                child = self.store.create_job('a', 'v', {'turntable': 1}, intent=intent,
                    parent_job=parent['job_id'], parent_version=parent['number'],
                    generate={'edit': {'palette': {'body': 'abcdef'}}} if intent == 'iterate_blockout' else None)
                self.assertTrue(self.worker.generate_family(child))
                review = self.worker.run_next()
                self.assertEqual(review['state'], 'review')
                self.assertEqual(len(review['canonical_views']), 7)
                staged = self.submit(review)
                self.assertEqual(staged['state'], 'staged')
                version = self.bake(staged)
                self.assertIn('blockout/spec.yaml', version['artifacts'])
                if intent != 'iterate_blockout':
                    self.assertEqual(self.store.artifact_file('a', 'v', version['number'], 'blockout/spec.yaml').read_bytes(), source)
                self.assertFalse((self.store.root / 'locks/a__v.lock').exists())


class StyleClientTests(unittest.TestCase):
    def test_url_validation(self):
        for url in ('https://127.0.0.1', 'http://localhost', 'http://example.com',
                    'http://127.0.0.1/v1', 'http://u:p@127.0.0.1', 'http://127.0.0.1?x=1',
                    'http://127.0.0.1#x', 'http://127.0.0.1:bad'):
            with self.subTest(url=url), self.assertRaises(ValueError):
                StyleClient(url, 1)
        for url in ('http://127.0.0.1:8056/', 'http://[::1]:8056'):
            StyleClient(url, 1)
        for timeout in (0, -1, float('nan')):
            with self.assertRaises(ValueError):
                StyleClient('http://127.0.0.1', timeout)

    def test_http_roundtrip_busy_and_errors(self):
        requests = []

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                self.send_response(200); self.end_headers()
                self.wfile.write(b'{"loaded": false}')

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                requests.append((self.path, body))
                mode = body.get('mode')
                if mode == 'redirect':
                    self.send_response(302)
                    self.send_header('Location', 'http://example.com/forbidden')
                    self.end_headers()
                    return
                self.send_response(503 if mode else 200); self.end_headers()
                self.wfile.write(json.dumps({'error': mode, 'free_mb': 12, 'needed_mb': 4500} if mode else body).encode())

        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = Thread(target=server.serve_forever, daemon=True); thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(thread.join)
        self.addCleanup(server.shutdown)
        client = StyleClient(f'http://127.0.0.1:{server.server_port}', 2)
        self.assertEqual(client.status(), {'loaded': False})
        request = {'view': 'front', 'seeds': [1000], 'prompt': 'painted toy'}
        self.assertEqual(client.render(request), request)
        self.assertEqual(requests[-1], ('/v1/style/render', request))
        with self.assertRaises(StyleBusy) as raised:
            client.render({'mode': 'gpu_busy'})
        self.assertEqual((raised.exception.free_mb, raised.exception.needed_mb), (12, 4500))
        for mode in ('backend_error', 'redirect'):
            with self.assertRaises(StyleError):
                client.render({'mode': mode})
