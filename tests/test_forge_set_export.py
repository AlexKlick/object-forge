"""Factory F2: real repo B helpers, GLB bytes, and isolated worker/router store."""
import ast
from contextlib import redirect_stdout
from datetime import timedelta
from io import StringIO
import json
import os
from pathlib import Path
import struct
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from open_sprite_pipeline.forge_api import forge_router
from open_sprite_pipeline.forge_policy import Policy
from open_sprite_pipeline.forge_store import ForgeStore, ForgeConflict
from open_sprite_pipeline.forge_worker import ForgeWorker, DEFAULT_SPIKE_ASSETS, checked_path, main, publish_library_root
from test_forge_bake import FAKE, ApiClient

# Execute only the existing glb() fixture definition, never the fake bake script.
_fixture = ast.Module(body=[n for n in ast.parse(FAKE).body
                           if isinstance(n, ast.FunctionDef) and n.name == 'glb'], type_ignores=[])
_namespace = {'json': json, 'struct': struct}
exec(compile(_fixture, 'test_forge_bake.glb', 'exec'), _namespace)
glb = _namespace['glb']

MANIFEST = '''scene: block
description: Export fixture
requires:
  - {asset: a, variants: [v, blend, missing, uv, incomplete]}
  - {asset: loose, variant: v}
bindings:
  - {kind: bank, asset: a, variants: {default: v, closed: blend, vacant: missing}}
'''
REPORT = {'parts': [{'id': 'core', 'layer': 'core'}], 'views_missing': []}


class SetExportTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.assets = Path(os.getenv('FORGE_SPIKE_ASSETS', str(DEFAULT_SPIKE_ASSETS)))
        cls.before = cls.snapshot()

    @classmethod
    def snapshot(cls):
        return {str(p.relative_to(cls.assets)): (p.lstat().st_mode, p.lstat().st_size,
                p.lstat().st_mtime_ns, p.lstat().st_ctime_ns)
                for p in cls.assets.rglob('*')}

    @classmethod
    def tearDownClass(cls):
        if cls.snapshot() != cls.before:
            raise AssertionError('SPIKE TREE CHANGED')

    def setUp(self):
        temporary = TemporaryDirectory(prefix='factory-f2-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.store = ForgeStore(self.root, policy=Policy('off'))
        app = FastAPI()
        app.state.forge_store = self.store
        app.include_router(forge_router)
        self.http = TestClient(app)
        self.addCleanup(self.http.close)
        env = patch.dict(os.environ, {'FORGE_STORE_ROOT': str(self.root), 'FORGE_WORKER_TOKEN': 'test-bake'})
        env.start()
        self.addCleanup(env.stop)
        self.client = ApiClient(self.http)
        self.worker = ForgeWorker(self.client, self.assets)
        self.item = self.store.create_set(MANIFEST, [], {})
        self.id = self.item['id']
        self.base = f'/v1/forge/sets/{self.id}'
        self.library_root = self.root / 'sets' / self.id / 'library_root'

    def publish(self, asset='a', variant='v', *, accepted=False, alpha='OPAQUE', uv=1, optional=True, report=REPORT):
        job = self.store.create_job(asset, variant)
        job['state'] = 'baking'
        self.store._save_job(job)
        artifacts = {f'{asset}_{variant}.glb': glb(alpha, uv)}
        if report is not None:
            artifacts['bake_report.json'] = report
        if optional:
            artifacts[f'{asset}_{variant}_lod.glb'] = glb(alpha, uv)
            artifacts['blockout/build_plan.json'] = {'bbox': {'min': [-4, -3, 0], 'max': [4, 3, 12]}}
        version = self.store.complete(job['id'], artifacts=artifacts)
        if accepted:
            self.store.accept_version(asset, variant, version['number'])
        return self.store._version_dir(asset, variant, version['number']) / 'artifacts'

    def request(self):
        response = self.http.post(self.base + '/export')
        self.assertEqual(response.status_code, 202, response.text)
        return response.json()

    def run_export(self):
        self.request()
        output = StringIO()
        with redirect_stdout(output):
            result = self.worker.run_export_next()
        self.output = output.getvalue()
        return result

    def document(self, name):
        response = self.http.get(self.base + '/library/' + name + '.json')
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def test_real_library_layout_selection_reports_and_hardlinks(self):
        first = self.publish(accepted=True)
        self.publish(alpha='BLEND')  # Newer unaccepted must not replace accepted v1.
        self.publish(variant='blend', alpha='BLEND')
        self.publish(variant='uv', uv=2)
        self.publish(variant='incomplete', report=None)
        loose = self.publish(asset='loose', optional=False)
        before = {str(p): p.read_bytes() for p in self.root.glob('assets/**/artifacts/**/*') if p.is_file()}
        result = self.run_export()
        self.assertEqual(result['status'], 'done')
        self.assertEqual(self.output.strip(), f'EXPORT set={self.id} pairs=6 exported=2 skipped=4')
        self.assertEqual(before, {str(p): p.read_bytes() for p in self.root.glob('assets/**/artifacts/**/*') if p.is_file()})
        for name in ('a_v.glb', 'a_v_lod.glb', 'bake_report.json'):
            target = self.library_root / 'bakes/a/v' / name
            self.assertEqual(target.read_bytes(), (first / name).read_bytes())
            self.assertEqual(target.stat().st_ino, (first / name).stat().st_ino)
            self.assertEqual(target.stat().st_nlink, 2)
        self.assertEqual((self.library_root / 'blockouts/a/v/build_plan.json').read_bytes(),
                         (first / 'blockout/build_plan.json').read_bytes())
        self.assertEqual((self.library_root / 'bakes/loose/v/loose_v.glb').read_bytes(), (loose / 'loose_v.glb').read_bytes())
        self.assertFalse((self.library_root / 'bakes/loose/v/loose_v_lod.glb').exists())
        self.assertFalse((self.library_root / 'blockouts/loose').exists())
        library = self.document('asset_library')
        expected = self.worker.tool('asset_library').build_library(self.library_root,
            [('a', 'v'), ('a', 'blend'), ('a', 'uv'), ('a', 'incomplete'), ('loose', 'v')], digest=True)
        self.assertEqual({k: v for k, v in library.items() if k != 'generated'},
                         {k: v for k, v in expected.items() if k != 'generated'})
        self.assertEqual(library['version'], 1)
        self.assertEqual(library['totals'], {'assets': 2, 'variants': 2, 'views_complete': 2})
        self.assertEqual(library['assets']['a']['variants']['v']['glb'], 'bakes/a/v/a_v.glb')
        self.assertEqual(len(library['skipped']), 3)
        report = self.document('report')
        self.assertTrue(report['applied'])
        self.assertEqual(report['coverage']['percent'], 33.3)
        self.assertEqual(report['unplaced_assets'], ['loose'])
        self.assertEqual(report['unbound_kinds'], [{'kind': 'bank', 'asset': 'a', 'missing_variants': ['blend', 'missing']}])
        items = {i['asset'] + '/' + i['variant']: i for i in report['items']}
        self.assertEqual((items['a/v']['version'], items['a/v']['accepted']), (1, True))
        self.assertFalse(items['loose/v']['accepted'])
        self.assertEqual(items['a/missing'], {'asset': 'a', 'variant': 'missing', 'version': None,
                                           'accepted': False, 'status': 'skipped', 'reason': 'no version'})
        for skip in library['skipped']:
            self.assertEqual(items[skip['asset'] + '/' + skip['variant']]['reason'], skip['reason'])
        self.assertEqual(self.document('city_bindings'), self.worker.tool('scene_build').bindings_document(self.item['manifest']))
        self.assertEqual(result['result'], {k: report[k] for k in ('items', 'coverage', 'status_counts')} |
                         {k: library[k] for k in ('skipped', 'totals')})
        export = self.http.get(self.base + '/export').json()
        self.assertEqual(export['host_paths'], {'library_root': str(self.library_root), **{
            k: str(self.library_root / 'library' / (k + '.json')) for k in ('asset_library', 'city_bindings', 'report')}})
        self.assertEqual(self.store.get_set(self.id)['export']['coverage'], report['coverage'])
        self.assertEqual(self.store.list_sets()[0]['export_status'], 'done')

    def test_copy_fallback(self):
        source = self.publish()
        with patch('open_sprite_pipeline.forge_worker.os.link', side_effect=OSError('cross device')):
            self.assertEqual(self.run_export()['status'], 'done')
        target = self.library_root / 'bakes/a/v/a_v.glb'
        self.assertEqual(target.read_bytes(), (source / 'a_v.glb').read_bytes())
        self.assertEqual(target.stat().st_nlink, 1)
        self.assertNotEqual(target.stat().st_ino, (source / 'a_v.glb').stat().st_ino)

    def test_reexport_publishes_complete_pending_root_and_removes_obsolete_files(self):
        first = self.publish()
        self.assertEqual(self.run_export()['status'], 'done')
        (self.library_root / 'obsolete').write_text('old')
        second = self.publish(optional=False)
        original = publish_library_root
        publications = []
        def replace(source, target):
            if Path(target) == self.library_root:
                self.assertTrue(self.library_root.is_dir())
                self.assertEqual((self.library_root / 'obsolete').read_text(), 'old')
                self.assertTrue(Path(source).name.startswith('.pending-'))
                for name in ('asset_library', 'city_bindings', 'report'):
                    self.assertTrue((Path(source) / 'library' / (name + '.json')).is_file())
                self.assertEqual((Path(source) / 'bakes/a/v/a_v.glb').read_bytes(), (second / 'a_v.glb').read_bytes())
                publications.append((source, target))
            return original(source, target)
        with patch('open_sprite_pipeline.forge_worker.publish_library_root', side_effect=replace):
            self.assertEqual(self.run_export()['status'], 'done')
        self.assertEqual(len(publications), 1)
        self.assertEqual(self.document('report')['items'][0]['version'], 2)
        self.assertFalse((self.library_root / 'obsolete').exists())
        self.assertFalse((self.library_root / 'bakes/a/v/a_v_lod.glb').exists())
        self.assertEqual((first / 'a_v.glb').stat().st_nlink, 1)
        self.assertEqual(list(self.library_root.parent.glob('.pending-*')), [])

    def test_failure_records_error_removes_previous_and_pending_then_retries(self):
        self.publish()
        self.run_export()
        with patch.object(self.worker.tool('asset_library'), 'build_library', side_effect=ValueError('bad export')):
            result = self.run_export()
        self.assertEqual(result['status'], 'failed')
        self.assertEqual(result['error'], 'bad export')
        self.assertIn(f'EXPORT failed set={self.id}: bad export', self.output)
        self.assertFalse(self.library_root.exists())
        self.assertEqual(list(self.library_root.parent.glob('.pending-*')), [])
        self.assertEqual(self.run_export()['status'], 'done')

    def test_claim_payload_live_conflict_and_expiry(self):
        self.publish(accepted=True)
        self.publish()
        self.request()
        claim = self.client.request('POST', '/worker/claim', {'kind': 'export'})
        self.assertEqual(claim['set_id'], self.id)
        self.assertEqual(claim['manifest'], self.item['manifest'])
        self.assertEqual(claim['pairs']['a/v']['version'], 1)
        self.assertTrue(claim['pairs']['a/v']['accepted'])
        self.assertEqual(claim['pairs']['a/missing']['reason'], 'no version')
        self.assertEqual(self.http.post(self.base + '/export').status_code, 409)
        self.assertIsNone(self.store.claim_export())
        with patch.object(self.store, '_now', return_value=self.store._now() + timedelta(seconds=301)):
            reclaimed = self.store.claim_export()
            self.assertNotEqual(claim['export_lease']['lease_id'], reclaimed['export_lease']['lease_id'])
            with self.assertRaises(ForgeConflict):
                self.store.complete_export(self.id, lease_id=claim['export_lease']['lease_id'], result={})
            self.assertTrue(any(h['status'] == 'expired' for h in self.store.get_export(self.id)['history']))

    def test_oldest_requested_and_empty_poll(self):
        self.assertIsNone(self.worker.run_export_next())
        second = self.store.create_set('requires: [{asset: b, variant: v}]', [], {})
        self.store.request_export(second['id'])
        self.request()
        self.assertEqual(self.store.claim_export()['set_id'], second['id'])
        self.assertEqual(self.store.claim_export()['set_id'], self.id)
        self.assertIsNone(self.store.claim_export())

    def test_latest_accepted_and_latest_unaccepted_selection(self):
        self.publish(accepted=True)
        self.publish(accepted=True)
        self.publish()
        self.publish(asset='loose')
        self.publish(asset='loose')
        self.request()
        pairs = self.store.claim_export()['pairs']
        self.assertEqual((pairs['a/v']['version'], pairs['a/v']['accepted']), (2, True))
        self.assertEqual((pairs['loose/v']['version'], pairs['loose/v']['accepted']), (2, False))

    def test_get_whitelist_and_worker_auth(self):
        self.assertEqual(self.http.get(self.base + '/export').status_code, 404)
        for state in ('absent', 'requested', 'running', 'failed'):
            for name in ('asset_library.json', 'city_bindings.json', 'report.json', 'bad.json'):
                self.assertEqual(self.http.get(self.base + '/library/' + name).status_code, 404)
            if state == 'absent': self.request()
            elif state == 'requested': claim = self.store.claim_export()
            elif state == 'running': self.store.fail_export(self.id, lease_id=claim['export_lease']['lease_id'], error='fixture')
        for suffix, body in (('complete', {'lease_id': 'x', 'result': {}}), ('fail', {'lease_id': 'x', 'error': 'x'})):
            url = f'/v1/forge/worker/export/{self.id}/{suffix}'
            self.assertEqual(self.http.post(url, json=body).status_code, 403)
            self.assertEqual(self.http.post(url, json=body, headers={'X-Forge-Worker': 'test-bake'}).status_code, 409)
        self.assertEqual(self.run_export()['status'], 'done')
        self.assertEqual(self.http.get(self.base + '/library/export.json').status_code, 404)
        self.assertEqual(self.http.post('/v1/forge/sets/..%5Cescape/export').status_code, 422)
        self.assertEqual(self.http.post('/v1/forge/sets/%2E%2E/export').status_code, 422)
        self.assertEqual(self.http.post('/v1/forge/sets/..%2Fescape/export').status_code, 422)

    def test_host_paths_use_worker_metadata_but_files_use_api_mount(self):
        self.publish()
        self.request()
        claim = self.store.claim_export()
        # Simulate host materialization at the same bind mount in this process.
        with patch.object(self.client, 'request', wraps=self.client.request) as request:
            request.side_effect = lambda method, path, body=None, **kwargs: (
                claim if path == '/worker/claim' else
                ApiClient.request(self.client, method, path,
                    {**body, 'host_store_root': '/host/not-visible-in-container'}
                    if path.endswith('/complete') else body, **kwargs))
            self.assertEqual(self.worker.run_export_next()['status'], 'done')
        response = self.http.get(self.base + '/export').json()
        self.assertEqual(response['host_paths']['library_root'],
                         f'/host/not-visible-in-container/sets/{self.id}/library_root')
        self.assertEqual(self.document('asset_library')['totals']['variants'], 1)

    def test_expired_claim_cannot_publish_or_remove_existing_root(self):
        self.publish()
        self.run_export()
        original = self.document('asset_library')
        self.request()
        claim = self.store.claim_export()
        later = self.store._now() + timedelta(seconds=301)
        with patch.object(self.store, '_now', return_value=later):
            successor = self.store.claim_export()
        request = self.client.request
        def stale_request(method, path, body=None, **kwargs):
            return claim if path == '/worker/claim' else request(method, path, body, **kwargs)
        with patch.object(self.client, 'request', side_effect=stale_request):
            with self.assertRaises(Exception):  # The API refuses stale failure as well.
                self.worker.run_export_next()
        self.assertEqual(json.loads((self.library_root / 'library/asset_library.json').read_text()), original)
        self.assertEqual(self.store.get_export(self.id)['lease'], successor['export_lease'])

    def test_atomic_exchange_retains_old_root_until_complete_new_root_is_visible(self):
        self.publish()
        self.run_export()
        (self.library_root / 'old-only').write_text('old')
        original = __import__('shutil').rmtree
        observed = []
        def cleanup(path, *args, **kwargs):
            path = Path(path)
            if path.name.startswith('.pending-') and (path / 'old-only').exists():
                self.assertTrue((self.library_root / 'library/report.json').is_file())
                self.assertFalse((self.library_root / 'old-only').exists())
                observed.append(True)
            return original(path, *args, **kwargs)
        with patch('open_sprite_pipeline.forge_worker.shutil.rmtree', side_effect=cleanup):
            self.assertEqual(self.run_export()['status'], 'done')
        self.assertEqual(observed, [True])

    def test_source_symlink_is_rejected_and_external_bytes_preserved(self):
        source = self.publish()
        glb_path = source / 'a_v.glb'
        glb_path.unlink()
        outside = self.root / 'outside.glb'
        outside.write_bytes(b'protected')
        glb_path.symlink_to(outside)
        self.assertEqual(self.run_export()['status'], 'failed')
        self.assertEqual(outside.read_bytes(), b'protected')
        self.assertFalse(self.library_root.exists())

    def test_target_symlink_is_rejected_without_touching_external_tree(self):
        outside = self.root / 'external'
        outside.mkdir()
        (outside / 'protected').write_text('safe')
        self.library_root.symlink_to(outside, target_is_directory=True)
        self.assertEqual(self.run_export()['status'], 'failed')
        self.assertEqual((outside / 'protected').read_text(), 'safe')
        self.assertTrue(self.library_root.is_symlink())

    def test_checked_path_still_rejects_writable_hardlinks(self):
        source = self.publish()
        self.run_export()
        with self.assertRaisesRegex(ValueError, 'Hardlink forbidden'):
            checked_path(source, 'a_v.glb')
        self.assertEqual(checked_path(source, 'a_v.glb', allow_hardlinks=True), source / 'a_v.glb')

    def test_crashed_pending_root_is_removed_before_retry(self):
        stale = self.library_root.parent / '.pending-crashed'
        stale.mkdir()
        (stale / 'partial').write_text('incomplete')
        self.assertEqual(self.run_export()['status'], 'done')
        self.assertFalse(stale.exists())

    def test_once_loop_runs_pipeline_then_critic_then_export(self):
        calls = []
        with patch.dict(os.environ, {'FORGE_ONCE': '1'}), \
                patch('open_sprite_pipeline.forge_worker.ForgeWorker', return_value=self.worker), \
                patch('open_sprite_pipeline.forge_worker.signal.signal'), \
                patch.object(self.worker, 'init_critic'), patch.object(self.worker, 'publish_catalog'), \
                patch.object(self.worker, 'run_next', side_effect=lambda: calls.append('pipeline')), \
                patch.object(self.worker, 'run_critic_next', side_effect=lambda: calls.append('critic')), \
                patch.object(self.worker, 'run_export_next', side_effect=lambda: calls.append('export')):
            self.assertEqual(main(), 0)
        self.assertEqual(calls, ['pipeline', 'critic', 'export'])
