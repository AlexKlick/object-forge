"""Factory F1 contract tests: pure manifests and isolated router/store fan-out."""
from copy import deepcopy
from io import BytesIO
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from PIL import Image
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from open_sprite_pipeline.forge_api import forge_router
from open_sprite_pipeline.forge_policy import Policy
from open_sprite_pipeline.forge_sets import load_manifest, pair_plan, coverage, unbound_kinds, unplaced_assets
from open_sprite_pipeline.forge_store import ForgeStore, ForgeStoreError


BASE = 'requires: [{asset: a, variant: v}]\n'


class ManifestTests(unittest.TestCase):
    def test_scene_defaults_and_expansion(self):
        scene = load_manifest('''scene: block
description: two buildings
requires:
  - {asset: bank_citadel, variants: [local, megabank], turntable: 8}
  - {asset: kiosk, variant: public}
bindings:
  - {kind: bank_branch, asset: bank_citadel, variants: {default: local}, lod: false}
''')
        self.assertEqual(scene['name'], 'block')
        self.assertEqual(scene['description'], 'two buildings')
        self.assertEqual([(p['asset'], p['variant']) for p in scene['requires']],
                         [('bank_citadel', 'local'), ('bank_citadel', 'megabank'), ('kiosk', 'public')])
        self.assertEqual([p['turntable'] for p in scene['requires']], [8, 8, 0])
        self.assertEqual(load_manifest(BASE)['name'], 'set')
        self.assertEqual(load_manifest('set: alias\n' + BASE)['name'], 'alias')
        self.assertFalse(scene['palette_only'])
        self.assertEqual(load_manifest('requires: [{asset: a, variants: v}]')['requires'][0]['variant'], 'v')

    def test_exact_scene_error_matrix(self):
        cases = [
            ('[]', 'set set must be a mapping'),
            ('requires: []', "set set: 'requires' must be a non-empty list"),
            ('requires: {}', "set set: 'requires' must be a non-empty list"),
            ('requires: [nope]', 'set set: requires[0] must be a mapping'),
            ('requires: [{variant: v}]', 'set set: requires[0] has no asset'),
            ('requires: [{asset: a}]', 'set set: a has no variants'),
            ('requires: [{asset: a, variants: []}]', 'set set: a has no variants'),
            ("requires: [{asset: a, variants: ['']}]", 'set set: a has an empty variant'),
            ('requires: [{asset: a, variants: [v, v]}]', 'set set: a/v listed twice'),
            (BASE + 'bindings: nope', "set set: 'bindings' must be a list"),
            (BASE + 'bindings: [nope]', 'set set: bindings[0] must be a mapping'),
            (BASE + 'bindings: [{asset: a}]', 'set set: bindings[0] has no kind'),
            (BASE + 'bindings: [{kind: k}]', 'set set: bindings[0] has no asset'),
            (BASE + 'bindings: [{kind: k, asset: a, variants: []}]', 'set set: bindings[0].variants must be a mapping'),
            (BASE + 'props: nope', "set set: 'props' must be a list"),
            (BASE + 'props: [nope]', 'set set: props[0] must be a mapping'),
            (BASE + 'props: [{}]', 'set set: props[0] has no asset'),
            (BASE + 'props: [{asset: a, anchor: edge}]', "set set: props[0] anchor 'edge' unsupported (tile) — props are decoration on an already-bound element"),
            (BASE + 'props: [{asset: a, count: 0}]', 'set set: props[0] count must be at least 1'),
        ]
        for body, message in cases:
            with self.subTest(body=body), self.assertRaises(ForgeStoreError) as caught:
                load_manifest(body)
            self.assertEqual(str(caught.exception), message)

    def test_malformed_yaml_and_numeric_errors_are_store_errors(self):
        for body in ('[', 'requires: [{asset: a, variant: v, turntable: nope}]',
                     BASE + 'props: [{asset: a, count: null}]',
                     BASE + 'props: [{asset: a, phase: 2026-09-07}]',
                     'requires: [{asset: a, variant: v, selfcheck_min: .nan}]'):
            with self.subTest(body=body), self.assertRaises(ForgeStoreError):
                load_manifest(body)

    def test_style_validation_uses_store_bounds(self):
        for key, bad in (('seeds_per_view', 7), ('seeds_per_view', True), ('strength', .1),
                         ('ip_scale', 2), ('control_scale', -1), ('prompt_override', 'x' * 401)):
            with self.subTest(key=key), self.assertRaises(ForgeStoreError) as caught:
                load_manifest(BASE + yaml.safe_dump({'style': {key: bad}}))
            with self.assertRaises(ForgeStoreError) as direct:
                ForgeStore.validate_style({key: bad})
            self.assertEqual(str(caught.exception), str(direct.exception))

    def test_factory_key_rejection_matrix(self):
        for style in ([], {'board': 'ref.png'}, {'board': [1]}, {'hero': 'a/v'}, {'hero': [1]}):
            with self.subTest(style=style), self.assertRaises(ForgeStoreError):
                load_manifest(BASE + yaml.safe_dump({'style': style}))
        for extra in ({'style': 'true'}, {'style': None}, {'style_refs': 'x'}, {'style_refs': [1]},
                      {'source': 'other'}, {'source': 'generate'}, {'source': 'generate', 'sources': []},
                      {'source': 'generate', 'sources': [f'{i}.png' for i in range(8)]}, {'height_hint': 301},
                      {'floor_height': 0}, {'sources': '../image.png'}):
            with self.subTest(extra=extra), self.assertRaises(ForgeStoreError):
                load_manifest(yaml.safe_dump({'requires': [{'asset': 'a', 'variant': 'v', **extra}]}))
        with self.assertRaises(ForgeStoreError) as caught:
            load_manifest(BASE + 'style: {hero: [b/v]}')
        self.assertEqual(str(caught.exception), 'set set: hero b/v is not required')

    def test_pair_plan_precedence_and_no_mutation(self):
        manifest = load_manifest('''palette_only: true
style: {board: [board.png], hero: [a/hero, a/off], strength: 0.7}
requires:
  - {asset: a, variants: [hero, plain]}
  - {asset: a, variant: 'off', style: false}
  - {asset: a, variant: own, style: true, style_refs: [own.png]}
  - {asset: a, variant: empty, style: true, style_refs: []}
  - {asset: a, variant: generated, source: generate, sources: [photo.png], style: true}
''')
        before = deepcopy(manifest)
        plans = [pair_plan(manifest, p) for p in manifest['requires']]
        self.assertEqual([p['palette_only'] for p in plans], [False, True, True, False, False, False])
        self.assertEqual([p['refs'] for p in plans], [['board.png'], [], [], ['own.png'], [], ['board.png']])
        self.assertEqual(plans[1], {'intent': 'from_spec', 'palette_only': True, 'style': None, 'refs': [], 'sources': []})
        self.assertEqual(plans[-1]['intent'], 'generate')
        self.assertEqual(plans[-1]['sources'], ['photo.png'])
        self.assertEqual(plans[0]['style']['strength'], .7)
        self.assertTrue(plans[0]['style']['enabled'])
        self.assertEqual(manifest, before)

    def test_scene_coverage_binding_and_prop_math(self):
        manifest = load_manifest('''requires:
  - {asset: a, variants: [v, missing]}
  - {asset: prop, variant: v}
  - {asset: unplaced, variant: v}
bindings: [{kind: bank, asset: a, variants: {default: v, dormant: missing}}]
props: [{asset: prop, variant: v, anchor: tile, count: 4, phase: 0.06}]
''')
        statuses = {'a/v': {'state': 'ready', 'version': 1, 'accepted': True, 'views_complete': True},
                    'a/missing': {'state': 'failed', 'version': 2, 'accepted': False, 'views_complete': False},
                    'prop/v': {'state': 'planned'}, 'unplaced/v': {}}
        self.assertEqual(coverage(statuses), {'coverage': {'requested_pairs': 4, 'baked_pairs': 2,
            'accepted_pairs': 1, 'view_complete_pairs': 1, 'percent': 50.0},
            'status_counts': {'failed': 1, 'planned': 2, 'ready': 1}})
        self.assertEqual(coverage({})['coverage']['percent'], 0.0)
        self.assertEqual(unbound_kinds(manifest, {'a/v'}),
                         [{'kind': 'bank', 'asset': 'a', 'missing_variants': ['missing']}])
        self.assertEqual(unbound_kinds(manifest, {('a', 'v'), ('a', 'missing')}), [])
        self.assertEqual(unplaced_assets(manifest), ['unplaced'])

    def test_names_are_confined(self):
        for body in ('scene: ../bad\n' + BASE, 'requires: [{asset: ../a, variant: v}]',
                     'requires: [{asset: a, variant: ../v}]', BASE + 'style: {board: [../x.png]}',
                     BASE + 'bindings: [{kind: k, asset: ../a}]', BASE + 'props: [{asset: ../a}]',
                     'requires: [{asset: a__b, variant: c}, {asset: a, variant: b__c}]'):
            with self.subTest(body=body), self.assertRaises(ForgeStoreError):
                load_manifest(body)


class SetApiTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.store = ForgeStore(self.root, policy=Policy('off'))
        app = FastAPI()
        app.state.forge_store = self.store
        app.include_router(forge_router)
        self.http = TestClient(app)
        self.addCleanup(self.http.close)
        output = BytesIO()
        Image.new('RGB', (8, 8), '#778899').save(output, format='PNG')
        self.png = output.getvalue()
        self.store.save_catalog({'specs': [{'asset': 'a', 'variants': ['v'], 'views': ['front']}],
            'prompts': [], 'assets_root': '/fake', 'published_at': '2026-09-07T00:00:00+00:00'})

    def create(self, manifest=BASE, files=(), as_file=False):
        if as_file:
            response = self.http.post('/v1/forge/sets', files=[('manifest', ('set.yaml', manifest, 'text/yaml')), *files])
        else:
            response = self.http.post('/v1/forge/sets', data={'manifest': manifest}, files=list(files))
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()

    def image(self, field, name):
        return field, (name, self.png, 'image/png')

    def launch(self, item, **body):
        response = self.http.post('/v1/forge/sets/' + item['id'] + '/launch', **({'json': body} if body else {}))
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def retry(self, item):
        response = self.http.post('/v1/forge/sets/' + item['id'] + '/retry')
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def finish(self, child, accepted=False, missing=()):
        job = self.store.get_job(child['job_id'])
        # Fixture a worker-owned terminal transition; this test targets set accounting.
        job['state'] = 'baking'
        self.store._save_job(job)
        version = self.store.complete(job['id'], artifacts={'bake_report.json': {'views_missing': list(missing)}})
        if accepted:
            self.store.accept_version(job['asset'], job['variant'], version['number'])
        return version

    def test_create_get_list_and_verbatim_manifest(self):
        text = '# keep this comment\nscene: block\n' + BASE
        item = self.create(text, as_file=True)
        self.assertRegex(item['id'], r'^[0-9a-f]{32}$')
        self.assertEqual(item['pairs']['a/v']['state'], 'planned')
        self.assertEqual(item['coverage']['requested_pairs'], 1)
        self.assertEqual(item['coverage']['percent'], 0)
        directory = self.root / 'sets' / item['id']
        self.assertEqual((directory / 'manifest.yaml').read_text(), text)
        saved = json.loads((directory / 'set.json').read_text())
        self.assertEqual(saved['pairs'], {})
        self.assertEqual(saved['launches'], [])
        self.assertEqual(self.http.get('/v1/forge/sets/' + item['id']).json(), item)
        self.assertEqual(self.http.get('/v1/forge/sets').json(), [{
            'id': item['id'], 'name': 'block', 'created_at': item['created_at'],
            'requested_pairs': 1, 'coverage': {'percent': 0.0}, 'attention': 0, 'export_status': None}])
        self.assertIsNone(item['export'])
        self.store.request_export(item['id'])
        self.assertEqual(self.store.get_set(item['id'])['export'], {
            'status': 'requested', 'finished_at': None, 'coverage': None,
            'library_root': f"sets/{item['id']}/library_root"})
        self.assertEqual(self.store.list_sets()[0]['export_status'], 'requested')

    def test_fanout_plans_refs_sources_and_idempotence(self):
        item = self.create('''palette_only: true
style: {board: [board.png], hero: [a/hero, a/own]}
requires:
  - {asset: a, variants: [plain, hero]}
  - {asset: a, variant: own, style_refs: [own.png]}
  - {asset: generated, variant: v, source: generate, sources: [photo.png], height_hint: 20, floor_height: 4}
''', [self.image('style[]', 'board.png'), self.image('pair_style[a/own][]', 'own.png'),
      self.image('pair_sources[generated/v][]', 'photo.png')])
        launched = self.launch(item)['launched']
        self.assertEqual(len(launched), 4)
        jobs = {j['variant'] if j['asset'] == 'a' else 'generated': j for j in self.store.list_jobs(set_id=item['id'])}
        self.assertEqual(jobs['plain']['intent'], 'from_spec')
        self.assertTrue(jobs['plain']['generate']['palette_only'])
        self.assertIsNone(jobs['plain']['generate']['style'])
        for variant, refs in (('hero', ['board.png']), ('own', ['own.png'])):
            self.assertFalse(jobs[variant]['generate']['palette_only'])
            self.assertTrue(jobs[variant]['generate']['style']['enabled'])
            self.assertEqual([r['filename'] for r in jobs[variant]['style_refs']], refs)
        self.assertEqual(jobs['generated']['intent'], 'generate')
        self.assertEqual(jobs['generated']['uploads'][0]['filename'], 'photo.png')
        self.assertEqual(jobs['generated']['generate']['height_hint'], 20)
        self.assertEqual(jobs['generated']['generate']['floor_height'], 4)
        self.assertEqual(self.launch(item)['launched'], [])
        self.assertEqual(len(self.store.list_jobs()), 4)
        self.assertEqual(len(self.store.get_set(item['id'])['launches']), 2)
        for path in (f"/sets/{item['id']}/style/0", f"/sets/{item['id']}/pairs/a/own/style/0"):
            response = self.http.get('/v1/forge' + path)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.content, self.png)
            self.assertEqual(response.headers['content-type'], 'image/png')
        self.assertEqual(self.http.get(f"/v1/forge/sets/{item['id']}/style/99").status_code, 404)

    def test_jobs_filter_and_version_summary_set_identity(self):
        one, two = self.create(), self.create()
        child = self.launch(one)['launched'][0]
        self.launch(two)
        self.store.create_job('a', 'v')
        response = self.http.get('/v1/forge/jobs', params={'set_id': one['id']})
        self.assertEqual([j['id'] for j in response.json()], [child['job_id']])
        self.assertEqual(response.json()[0]['set_id'], one['id'])
        self.finish(child)
        self.assertEqual(self.store.list_versions('a', 'v')[0]['set_id'], one['id'])

    def test_accepted_skip_force_and_coverage(self):
        item = self.create(BASE + 'bindings: [{kind: k, asset: a, variants: {default: v}}]')
        child = self.launch(item)['launched'][0]
        self.finish(child, accepted=True)
        state = self.store.get_set(item['id'])
        self.assertEqual(state['coverage'], {'requested_pairs': 1, 'baked_pairs': 1,
            'accepted_pairs': 1, 'view_complete_pairs': 1, 'percent': 100.0})
        self.assertEqual(state['unbound_kinds'], [])
        self.assertEqual(self.launch(item)['skipped'][0]['reason'], 'accepted version')
        self.assertEqual(len(self.launch(item, force=True)['launched']), 1)
        self.assertEqual(self.launch(item, force=True)['launched'], [])
        self.assertEqual(self.store.get_set(item['id'])['pairs']['a/v']['version'], 1)

    def test_any_accepted_version_prevents_relaunch(self):
        item = self.create()
        self.finish(self.launch(item)['launched'][0], accepted=True)
        self.finish(self.launch(item, force=True)['launched'][0], missing=['front'])
        state = self.store.get_set(item['id'])
        self.assertFalse(state['pairs']['a/v']['accepted'])
        self.assertEqual(state['coverage']['view_complete_pairs'], 0)
        self.assertEqual(self.launch(item)['skipped'][0]['reason'], 'accepted version')

    def test_retry_failed_attention_never_live_or_planned(self):
        item = self.create('requires: [{asset: a, variants: [failed, attention, live, done]}]')
        self.assertEqual(self.retry(item)['launched'], [])
        children = {c['variant']: c for c in self.launch(item)['launched']}
        self.store.set_state(children['failed']['job_id'], 'failed', error='fake failure')
        for variant in ('attention', 'done'):
            self.finish(children[variant])
        for variant in ('attention', 'live'):
            job = self.store.get_job(children[variant]['job_id'])
            job['attention'] = {'reason': 'critic', 'detail': ['fixture'], 'at': job['created_at']}
            self.store._save_job(job)
        state = self.store.get_set(item['id'])
        self.assertEqual(state['attention'], ['a/attention', 'a/live'])
        self.assertEqual(state['pairs']['a/failed']['reason'], 'fake failure')
        retried = self.retry(item)
        self.assertEqual({c['variant'] for c in retried['launched']}, {'failed', 'attention'})
        self.assertEqual({c['variant'] for c in retried['skipped']}, {'live', 'done'})
        self.assertEqual(self.retry(item)['launched'], [])

    def test_catalog_block_and_refresh(self):
        item = self.create('requires: [{asset: missing, variant: v}]')
        self.assertEqual(self.launch(item), {'launched': [], 'skipped': [
            {'asset': 'missing', 'variant': 'v', 'reason': 'no spec in catalog'}]})
        self.assertEqual(self.store.get_set(item['id'])['status_counts'], {'blocked': 1})
        self.assertEqual(self.retry(item)['launched'], [])
        self.assertEqual(self.store.get_set(item['id'])['status_counts'], {'blocked': 1})
        catalog = self.store.get_catalog()
        catalog['specs'].append({'asset': 'missing', 'variants': [], 'views': []})
        self.store.save_catalog(catalog)
        self.assertEqual(len(self.launch(item)['launched']), 1)

    def test_multipart_bad_manifest_missing_upload_and_path_rollback(self):
        for manifest, message in (('requires: []', "set set: 'requires' must be a non-empty list"),
                                  (BASE + 'style: {board: [Missing.png]}', 'set set: missing upload Missing.png'),
                                  ('requires: [{asset: ../a, variant: v}]', 'Invalid path identifier.')):
            response = self.http.post('/v1/forge/sets', data={'manifest': manifest},
                                      files=[self.image('style[]', 'missing.png')])
            self.assertEqual(response.status_code, 422, response.text)
            self.assertEqual(response.json()['detail'], message)
            self.assertEqual(list((self.root / 'sets').glob('*')), [])

    def test_pair_upload_names_are_confined(self):
        response = self.http.post('/v1/forge/sets', data={'manifest': BASE},
                                  files=[self.image('pair_style[../a/v][]', 'x.png')])
        self.assertEqual(response.status_code, 422)
        with self.assertRaises(ForgeStoreError):
            self.store.get_set('../outside')
        self.assertEqual(self.http.post('/v1/forge/sets/..bad/launch').status_code, 422)

    def test_store_image_validation_and_write_rollback(self):
        for media, data in (('text/plain', self.png), ('image/png', b''), ('image/png', b'bad'),
                            ('image/png', b'x' * (20 * 1024 * 1024 + 1))):
            with self.subTest(media=media, size=len(data)), self.assertRaises(ForgeStoreError):
                self.store.create_set(BASE, [('x.png', media, data)], {})
        original = self.store._write_json
        def fail_record(path, data):
            if path.name == 'set.json':
                raise OSError('fixture disk failure')
            return original(path, data)
        with patch.object(self.store, '_write_json', side_effect=fail_record), self.assertRaises(OSError):
            self.store.create_set(BASE, [], {})
        self.assertEqual(list((self.root / 'sets').glob('*')), [])

    def test_launch_upload_failure_rolls_back_all_new_children(self):
        item = self.create('requires: [{asset: a, variants: [v, two], style: true}]\nstyle: {board: [x.png]}',
                           [self.image('style[]', 'x.png')])
        record = self.store.record_style_ref
        calls = []
        def fail_second(*args):
            calls.append(args)
            if len(calls) == 2:
                raise ForgeStoreError('fixture write failure')
            return record(*args)
        with patch.object(self.store, 'record_style_ref', side_effect=fail_second):
            response = self.http.post('/v1/forge/sets/' + item['id'] + '/launch')
        self.assertEqual(response.status_code, 422)
        self.assertEqual(self.store.list_jobs(), [])
        self.assertEqual(self.store.get_set(item['id'])['launches'], [])

    def test_symlink_artifacts_cannot_escape(self):
        item = self.create(BASE + 'style: {board: [x.png], hero: [a/v]}', [self.image('style[]', 'x.png')])
        path = self.root / 'sets' / item['id'] / 'style/0.png'
        path.unlink()
        path.symlink_to('/etc/passwd')
        self.assertEqual(self.http.get(f"/v1/forge/sets/{item['id']}/style/0").status_code, 404)
        self.assertEqual(self.http.post(f"/v1/forge/sets/{item['id']}/launch").status_code, 422)
        self.assertEqual(self.store.list_jobs(), [])

    def test_launch_force_body_strictness(self):
        item = self.create()
        for value in ('true', 1, [], None):
            response = self.http.post(f"/v1/forge/sets/{item['id']}/launch", json={'force': value})
            self.assertEqual(response.status_code, 422)

    def test_generate_seven_sources_and_selfcheck_not_invented(self):
        # Seven is the synthesis worker's input ceiling; the manifest matches it.
        names = [f'{i}.png' for i in range(7)]
        manifest = yaml.safe_dump({'requires': [{'asset': 'new', 'variant': 'v', 'source': 'generate',
                                                'sources': names, 'selfcheck_min': .96, 'turntable': 8}]})
        item = self.create(manifest, [self.image('pair_sources[new/v][]', n) for n in names])
        job = self.store.get_job(self.launch(item)['launched'][0]['job_id'])
        self.assertEqual(len(job['uploads']), 7)
        self.assertEqual(job['params']['turntable'], 8)
        self.assertNotIn('selfcheck_min', job['params'])

    def test_multipart_limits_and_bad_files_leave_no_record(self):
        cases = [([self.image('style[]', f'{i}.png') for i in range(65)], BASE),
                 ([('style[]', ('bad.png', b'not png', 'image/png'))], BASE),
                 ([('manifest', ('bad.yaml', b'\xff', 'text/yaml'))], None)]
        for files, manifest in cases:
            with self.subTest(files=len(files), manifest=manifest):
                response = self.http.post('/v1/forge/sets',
                    data={'manifest': manifest} if manifest is not None else {}, files=files)
                self.assertEqual(response.status_code, 422, response.text)
                self.assertEqual(self.store.list_sets(), [])

    def test_set_board_and_pair_sources_resolve_original_basename(self):
        item = self.store.create_set(BASE + 'style: {board: [Board.png], hero: [a/v]}',
                                     [('folder/Board.png', 'image/png', self.png)], {})
        child = self.launch(item)['launched'][0]
        job = self.store.get_job(child['job_id'])
        self.assertEqual(job['style_refs'][0]['filename'], 'Board.png')
        self.assertEqual(self.store.style_ref_file(job['id'], 0)[0].read_bytes(), self.png)

    def test_missing_pair_source_and_duplicate_upload_rollback(self):
        for text, files in ((
                'requires: [{asset: a, variant: v, source: generate, sources: [photo.png]}]', {}),
                (BASE, {'a/v': [('photo.png', 'image/png', self.png)] * 2})):
            with self.subTest(text=text), self.assertRaises(ForgeStoreError):
                self.store.create_set(text, [], files)
            self.assertEqual(self.store.list_sets(), [])

    def test_concurrent_launch_does_not_duplicate_jobs(self):
        from concurrent.futures import ThreadPoolExecutor
        item = self.create('requires: [{asset: a, variants: [v, two]}]')
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: self.store.launch_set(item['id']), range(2)))
        self.assertEqual(sorted(len(r['launched']) for r in results), [0, 2])
        self.assertEqual(len(self.store.list_jobs(set_id=item['id'])), 2)

    def test_no_published_catalog_blocks_spec(self):
        (self.root / 'catalog.json').unlink()
        item = self.create()
        self.assertEqual(self.launch(item)['skipped'][0]['reason'], 'no spec in catalog')
