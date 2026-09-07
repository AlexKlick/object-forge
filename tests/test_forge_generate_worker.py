from datetime import timedelta
import hashlib
from io import BytesIO
import json
import os
from pathlib import Path
import shlex
import sys
import tempfile
from threading import Event
import unittest
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from PIL import Image, ImageDraw
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from open_sprite_pipeline.forge_api import forge_router
from open_sprite_pipeline.forge_store import ForgeStore
from open_sprite_pipeline.forge_worker import ForgeWorker, DEFAULT_SPIKE_ASSETS, png, checked_path
import test_forge_bake as bake_fixture

VIEWS = ['front_left', 'front_right', 'rear_left', 'rear_right', 'roof']
SYNTH = r'''
import argparse, json
from pathlib import Path
import yaml
p = argparse.ArgumentParser()
p.add_argument('--image', action='append', default=[])
p.add_argument('--force-single', action='store_true')
p.add_argument('--asset'); p.add_argument('--out', type=Path)
p.add_argument('--height-hint', type=float); p.add_argument('--floor-height', type=float)
p.add_argument('--report', type=Path); p.add_argument('--override', nargs='+')
p.add_argument('--edit-in', type=Path); p.add_argument('--edit')
a = p.parse_args()
if a.edit_in:
    spec = yaml.safe_load(a.edit_in.read_text()); edits = json.loads(a.edit)
    for role, color in edits.get('palette', {}).items(): spec['palette'][role]['hex'] = color
    if edits.get('tower'): spec['massing']['tower'].update(edits['tower'])
else:
    assert 1 <= len(a.image) <= 7
    assert len(a.image) > 1 or a.force_single
    for image in a.image: assert Path(image).is_file()
    spec = {'asset': a.asset, 'palette': {'body': {'hex': '778899'}},
            'massing': {'footprint': {'width': 8, 'depth': 6},
                        'plinth': {'floors': 4, 'floor_height': a.height_hint / 4}},
            'variants': {'default': {}}, 'views': {v: {} for v in ['front_left', 'front_right', 'rear_left', 'rear_right', 'roof']}}
    count = a.report.parent / 'synth_count'
    count.write_text(str(int(count.read_text()) + 1 if count.exists() else 1))
a.out.write_text(yaml.safe_dump(spec))
a.report.write_text(json.dumps({'confidence': {'height': 'low'}, 'assumptions': ['Supplied height hint.'],
                                'next_view': 'roof', 'params': {'floor_height': a.floor_height}}))
'''
BLOCKOUT = r'''
import sys, yaml
from pathlib import Path
from PIL import Image, ImageDraw
spec, root = yaml.safe_load(Path(sys.argv[1]).read_text()), Path(sys.argv[2])
for index, view in enumerate(spec['views']):
    image = Image.new('RGBA', (128, 128), 'white')
    draw = ImageDraw.Draw(image)
    if index == 0: draw.polygon([(24, 108), (64, 20), (104, 108)], fill='#778899')
    else: draw.rectangle((20, 20, 30 + index * 15, 100), fill='#778899')
    out = root / 'renders' / spec['asset'] / 'default'
    out.mkdir(parents=True, exist_ok=True)
    image.save(out / (view + '.png'))
'''


class GenerateWorkerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='forge-gl4-')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.assets = self.root / 'spike'
        (self.assets / 'tools').mkdir(parents=True)
        for name in ('sheet_match.py', 'spec_synth.py', 'blockout.py', 'view_validate.py', 'atlas_layout.py'):
            (self.assets / 'tools' / name).write_bytes((DEFAULT_SPIKE_ASSETS / 'tools' / name).read_bytes())
        (self.assets / 'tools/bake_views.py').write_text('SELFCHECK_THRESHOLD = 0.9\n')
        self.before = self.snapshot()
        self.addCleanup(lambda: self.assertEqual(self.snapshot(), self.before, 'SPIKE TREE CHANGED'))
        app = FastAPI(); app.state.forge_store = ForgeStore(self.root / 'store'); app.include_router(forge_router)
        self.store = app.state.forge_store
        self.http = TestClient(app); self.addCleanup(self.http.close)
        self.client = bake_fixture.ApiClient(self.http)
        synth = self.root / 'fake synth.py'; synth.write_text(SYNTH)
        render = self.root / 'fake blockout.py'; render.write_text(BLOCKOUT)
        bake = self.root / 'fake bake.py'
        # Reuse the existing nontrivial GLB fixture, preserving its P7 gate.
        fake = bake_fixture.FAKE.replace('assert sorted(p.name for p in views.glob("*.png")) == ["front.png"]', 'assert sorted(p.name for p in views.glob("*.png")) in ([], ["front_left.png"])')
        fake = fake.replace('assert (views / "front.png").read_bytes() == b"staged front"', 'assert not list(views.glob("*.png")) or Image.open(views / "front_left.png").mode == "RGBA"')
        fake = fake.replace('out = root / "bakes/a/v"', 'out = root / "bakes/a/v"\n(out / "turntable").mkdir(parents=True, exist_ok=True)')
        bake.write_text(fake)
        self.env = patch.dict(os.environ, {
            'FORGE_WORKER_TOKEN': 'test-bake', 'FORGE_BAKE_PYTHON': sys.executable,
            'FORGE_STORE_ROOT': str(self.store.root),
            'FORGE_SYNTH_CMD': f'{shlex.quote(sys.executable)} {shlex.quote(str(synth))} {{inputs}} --asset {{asset}} --out {{out}} --height-hint {{height_hint}} --floor-height {{floor_height}} --report {{report}}',
            'FORGE_BLOCKOUT_CMD': f'{shlex.quote(sys.executable)} {shlex.quote(str(render))} {{spec}} {{out}}',
            'FORGE_BAKE_CMD': f'{shlex.quote(sys.executable)} {shlex.quote(str(bake))} {{assets_root}} ok {{spec}}',
        })
        self.env.start(); self.addCleanup(self.env.stop)
        self.worker = ForgeWorker(self.client, self.assets)
        self.image = Image.new('RGBA', (128, 128), 'white')
        ImageDraw.Draw(self.image).polygon([(24, 108), (64, 20), (104, 108)], fill='#778899')

    def snapshot(self):
        return {str(p.relative_to(self.assets)): (p.stat().st_mtime_ns, hashlib.sha256(p.read_bytes()).hexdigest() if p.is_file() else None)
                for p in [self.assets, *sorted(self.assets.rglob('*'))]}

    def upload(self, **settings):
        response = self.http.post('/v1/forge/jobs', data={'asset': 'a', 'variant': 'v', 'intent': 'generate',
             'params': json.dumps({'turntable': 1}), **settings}, files={'files': ('photo.png', png(self.image), 'image/png')})
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()

    def reviewing(self, **settings):
        self.upload(**settings)
        job = self.worker.run_next()
        self.assertEqual(job['state'], 'review')
        self.assertEqual(job['canonical_views'], VIEWS)
        return job

    def stage(self, job):
        panel = job['match']['panels'][0]
        self.client.request('POST', f"/jobs/{job['id']}/review", {'mode': 'submit',
            'panels': [{'panel_id': panel['panel_id'], 'decision': 'repin', 'view': 'front_left'}],
            'views_missing': VIEWS[1:]})
        result = self.worker.run_next()
        self.assertEqual(result['state'], 'staged')
        return result

    def test_full_lifecycle_workspace_staging_spec_metrics_and_glb(self):
        job = self.reviewing()
        ws = self.worker.workspace_root(job)
        self.assertEqual(ws, self.store.root / 'assets/a/workspace/v')
        cutout = Image.open(ws / 'in/u0.png')
        self.assertEqual(cutout.getpixel((0, 0))[3], 0)
        job = self.stage(job)
        self.assertEqual((ws / 'styled/a/v/views/front_left.png').read_bytes(), self.store.staged_view_file(job['id'], 'front_left').read_bytes())
        self.client.request('POST', f"/jobs/{job['id']}/approve")
        self.assertEqual(self.worker.run_next()['state'], 'ready')
        version = self.store.get_version('a', 'v', 1)
        self.assertEqual(version['origin'], 'bake')
        self.assertIn('blockout/spec.yaml', version['artifacts'])
        self.assertEqual(self.store.artifact_file('a', 'v', 1, 'blockout/spec.yaml').read_bytes(), self.store.blockout_file(job['id'], 'spec.yaml').read_bytes())
        self.assertEqual(version['metrics']['synth'], {k: job['generate']['blockout'][k] for k in ('confidence', 'params')})
        self.assertTrue(version['metrics']['glb']['textured'])
        self.assertFalse(list(self.store.root.rglob('snapshot.json')))
        self.assertTrue((self.store.root / 'locks/a__v.workspace.lock').is_file())
        self.assertFalse((self.store.root / 'locks/a__v.lock').exists())

    def test_regeneration_reruns_synth_changes_spec_and_resets_decisions(self):
        job = self.reviewing()
        self.client.request('POST', f"/jobs/{job['id']}/blockout/regenerate", {'height_hint': 24, 'floor_height': 4, 'tower_override': 'none', 'palette_hex': {'body': '#abcdef'}})
        job = self.worker.run_next()
        self.assertEqual(job['state'], 'review')
        self.assertEqual(job['generate']['blockout']['params']['height'], 24)
        self.assertEqual(job['generate']['blockout']['palette']['body']['hex'], 'abcdef')
        self.assertEqual(job['generate']['regenerations'], 1)
        self.assertEqual(job['match']['decisions'], [])
        self.assertEqual((self.worker.workspace_root(job) / 'synth_count').read_text(), '2')

    def test_same_pair_jobs_restore_own_spec_and_renders_before_staging_and_bake(self):
        first = self.reviewing(height_hint='12')
        saved = self.store.blockout_file(first['id'], 'spec.yaml').read_bytes()
        second = self.reviewing(height_hint='24')
        self.stage(first)
        ws = self.worker.workspace_root(first)
        self.assertEqual((ws / 'specs/a.yaml').read_bytes(), saved)
        self.client.request('POST', f"/jobs/{second['id']}/blockout/regenerate", {'height_hint': 36})
        self.worker.run_next()
        self.client.request('POST', f"/jobs/{first['id']}/approve")
        self.worker.run_next()
        self.assertEqual(self.store.artifact_file('a', 'v', 1, 'blockout/spec.yaml').read_bytes(), saved)

    def test_workspace_lock_is_distinct_and_blocks_claim_before_lease(self):
        job = self.upload()
        with self.worker.bake_lock(job, 'workspace'):
            with self.worker.bake_lock(job, 'spike') as spike:
                self.assertEqual(spike, self.store.root)
            self.assertIsNone(ForgeWorker(self.client, self.assets).run_next())
            self.assertEqual(self.store.get_job(job['id'])['state'], 'uploaded')
            self.assertIsNone(self.store.get_job(job['id'])['lease'])

    def test_workspace_rejects_symlink_and_hardlink_inputs(self):
        job = self.upload(); ws = self.worker.workspace_root(job)
        outside = self.root / 'outside'; outside.write_bytes(b'untouched')
        for kind in ('symlink', 'hardlink'):
            target = ws / 'in/u0.png'
            if kind == 'symlink': target.symlink_to(outside)
            else: os.link(outside, target)
            with self.assertRaises(ValueError): checked_path(ws, 'in/u0.png')
            target.unlink()
        self.assertEqual(outside.read_bytes(), b'untouched')

    def test_generate_bake_command_adds_palette_only_when_no_staged_views(self):
        job = self.reviewing()
        ws = self.worker.workspace_root(job)
        views = ws / 'styled/a/v/views'; views.mkdir(parents=True, exist_ok=True)
        env = dict(os.environ); env.pop('FORGE_BAKE_CMD', None)
        with patch.dict(os.environ, env, clear=True):
            command = self.worker.bake_command(job)
        self.assertIn('--allow-palette-only', command)
        staged = views / 'front_left.png'; staged.write_bytes(png(self.image))
        with patch.dict(os.environ, env, clear=True):
            self.assertNotIn('--allow-palette-only', self.worker.bake_command(job))
        staged.unlink()

    def test_generate_job_with_zero_staged_views_bakes_palette_only(self):
        job = self.reviewing()
        panels = [{'panel_id': p['panel_id'], 'decision': 'reject'} for p in job['match']['panels']]
        self.client.request('POST', f"/jobs/{job['id']}/review",
                            {'mode': 'submit', 'panels': panels, 'views_missing': VIEWS})
        self.assertEqual(self.worker.run_next()['state'], 'staged')
        self.assertFalse(list((self.worker.workspace_root(job) / 'styled/a/v/views').glob('*.png')))
        self.client.request('POST', f"/jobs/{job['id']}/approve")
        self.assertEqual(self.worker.run_next()['state'], 'ready')
        version = self.store.get_version('a', 'v', 1)
        self.assertEqual(version['origin'], 'bake')
        self.assertIn('blockout/spec.yaml', version['artifacts'])

    def test_subprocess_failure_and_stale_success_fail_closed(self):
        for code in ('import sys; sys.exit(2)', 'pass'):
            self.upload()
            with patch.dict(os.environ, {'FORGE_SYNTH_CMD': shlex.join([sys.executable, '-c', code])}):
                with self.assertRaises((ValueError, FileNotFoundError)): self.worker.run_next()
            latest = self.store.list_jobs()[-1]
            # Missing fresh output can retain a recoverable matching lease;
            # neither path can publish a blockout or reach review.
            self.assertNotEqual(latest['state'], 'review')
            self.assertIsNone(latest['generate']['blockout'])

    def test_heartbeats_cover_blockout_subprocess(self):
        self.upload(); observed = Event(); stopped = Event(); intervals = []
        original = self.worker.progress
        run = self.worker.run_workspace_command
        class FastEvent:
            def wait(self, interval):
                intervals.append(interval)
                return stopped.wait(0.005)
            def set(self): stopped.set()
        def progress(job, *markers, **fields):
            result = original(job, *markers, **fields)
            if not markers: observed.set()
            return result
        def command(job, argv, label):
            if label == 'blockout':
                observed.clear()
                self.assertTrue(observed.wait(5), 'No heartbeat during render command')
            return run(job, argv, label)
        with patch('open_sprite_pipeline.forge_worker.Event', FastEvent), patch.object(self.worker, 'progress', side_effect=progress), patch.object(self.worker, 'run_workspace_command', side_effect=command):
            self.assertEqual(self.worker.run_next()['state'], 'review')
        self.assertEqual(set(intervals), {30})

    def test_cutout_only_panels_use_proxy_and_c_prefix(self):
        ref = {'image_id': 'im1', 'segment_id': 'seg1'}
        path = self.root / 'cutout.png'; path.write_bytes(png(self.image))
        from unittest.mock import Mock
        self.http.app.state.store = Mock()
        self.http.app.state.store.segment_file.return_value = path
        response = self.http.post('/v1/forge/jobs', data={'asset': 'a', 'variant': 'v', 'intent': 'generate', 'segment_refs': json.dumps([ref])})
        self.assertEqual(response.status_code, 201, response.text)
        job = self.worker.run_next()
        self.assertEqual(job['match']['panels'][0]['panel_id'], 'c0-p0')
        self.assertEqual(job['match']['panels'][0]['cutout_index'], 0)
        self.assertNotIn('upload_index', job['match']['panels'][0])
        self.assertEqual((self.worker.workspace_root(job) / 'in/c0.png').read_bytes(), path.read_bytes())
        self.http.app.state.store.segment_file.assert_called_with('im1', 'seg1', 'cutout')

    def test_default_commands_and_placeholder_tokenization(self):
        job = self.upload()
        ws = self.worker.workspace_root(job)
        with patch.dict(os.environ, {'FORGE_BAKE_CMD': ''}):
            argv = self.worker.bake_command(job)
            # A fresh workspace has no staged views yet, so the palette-only
            # degraded-mode flag rides last.
            self.assertEqual(argv[-5:], ['--assets-root', str(ws), '--spec', str(ws / 'specs/a.yaml'),
                                         '--allow-palette-only'])
        self.assertEqual(self.worker.command_override('fake {inputs} --out={out} literal{"x":1}',
            {'inputs': ['--image', '/a path/input.png'], 'out': '/a path/out.yaml'}),
            ['fake', '--image', '/a path/input.png', '--out=/a path/out.yaml', 'literal{x:1}'])
        with patch.dict(os.environ, {'FORGE_BAKE_CMD': 'python "untouched command.py" --legacy'}):
            self.assertEqual(self.worker.bake_command(job), ['python', 'untouched command.py', '--legacy'])

    def test_real_cli_regeneration_for_asset_named_edited_and_default_variant(self):
        with patch.dict(os.environ, {'FORGE_SYNTH_CMD': ''}):
            self.upload(asset='edited', variant='default')
            job = self.worker.run_next()
            self.assertEqual(job['state'], 'review')
            self.client.request('POST', f"/jobs/{job['id']}/blockout/regenerate", {'palette_hex': {'body': 'abcdef'}})
            job = self.worker.run_next()
        self.assertEqual(job['state'], 'review')
        self.assertEqual(job['generate']['blockout']['palette']['body']['hex'], 'abcdef')
        self.assertEqual(job['canonical_views'], VIEWS)

    def test_real_companion_synthesis_cli_and_edit_contract(self):
        # Run the current real synthesis CLI from a copied, read-only fixture;
        # rendering remains fake, so this lane has no Blender or GPU dependency.
        with patch.dict(os.environ, {'FORGE_SYNTH_CMD': ''}):
            job = self.reviewing()
            self.assertEqual(set(job['generate']['blockout']['views']), set(VIEWS))
            self.client.request('POST', f"/jobs/{job['id']}/blockout/regenerate", {'tower_override': 'none', 'palette_hex': {'body': 'abcdef'}})
            job = self.worker.run_next()
        self.assertEqual(job['generate']['blockout']['palette']['body']['hex'], 'abcdef')
        self.assertIsNone(job['generate']['blockout']['params']['tower'])
