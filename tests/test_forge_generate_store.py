from copy import deepcopy
from datetime import timedelta
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from open_sprite_pipeline.forge_store import ForgeStore, ForgeConflict, ForgeStoreError


def blockout_payload(views=None):
    return {'params': {'footprint': {'width': 8, 'depth': 6}, 'height': 12,
                       'plinth': {'floors': 4, 'floor_height': 3}, 'tower': None},
            'palette': {'body': {'hex': '778899'}}, 'confidence': {'height': 'low'},
            'assumptions': ['Height supplied by operator.'], 'next_view': 'roof',
            'synth_report': {'confidence': {'height': 'low'}}, 'views': views or ['front'],
            'spec_yaml': b'asset: a\n'}


class GenerateStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = ForgeStore(Path(self.tmp.name) / 'store')

    def claimed(self):
        job = self.store.create_job('a', 'v', intent='generate', canonical_views=['front'])
        return self.store.claim_job(stages=['matching'], job_id=job['id'])

    def reviewing(self):
        job = self.claimed()
        self.store.worker_blockout(job['id'], blockout_payload(), {'front': b'render'}, job['lease']['lease_id'])
        return self.store.worker_match(job['id'], {'panels': [], 'views_missing': ['front']}, job['lease']['lease_id'])

    def test_generate_defaults_and_input_copy(self):
        refs = [{'image_id': 'image1', 'segment_id': 'seg1'}] * 8
        job = self.store.create_job('a', 'v', intent='generate', generate={'segment_refs': refs})
        refs.clear()
        self.assertEqual(job['generate'], {'segment_refs': [{'image_id': 'image1', 'segment_id': 'seg1'}] * 8,
                         'height_hint': 12.0, 'floor_height': 3.0, 'blockout': None, 'regenerations': 0})
        self.assertIsNone(job['parent_job'])
        self.assertNotIn('generate', self.store.create_job('a', 'v'))

    def test_generate_validation_matrix(self):
        invalid = [[], {'unknown': 1}, {'segment_refs': {}}, {'segment_refs': [{}]},
                   {'segment_refs': [{'image_id': '../x', 'segment_id': 'a'}]},
                   {'segment_refs': [{'image_id': 'a', 'segment_id': 'b'}] * 9},
                   {'blockout': {}}, {'regenerations': 1}, {'regenerations': False}]
        for key, values in [('height_hint', [0, 301, float('nan'), True, '12']),
                            ('floor_height', [0, 11, float('inf'), False, None])]:
            invalid += [{key: value} for value in values]
        for settings in invalid:
            with self.subTest(settings=settings), self.assertRaises(ForgeStoreError):
                self.store.create_job('a', 'v', intent='generate', generate=settings)
        for kwargs in ({'intent': 'wrong'}, {'intent': 'fresh', 'generate': {}},
                       {'intent': 'generate', 'parent_job': 'parent'},
                       {'intent': 'generate', 'replacement_views': ['front']}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ForgeStoreError):
                self.store.create_job('a', 'v', **kwargs)
        self.store.create_job('a', 'v', intent='generate', generate={'height_hint': 300, 'floor_height': 10})

    def test_worker_blockout_requires_current_lease_state_and_intent(self):
        job = self.claimed()
        for lease in ('bad', ''):
            with self.assertRaises(ForgeConflict):
                self.store.worker_blockout(job['id'], blockout_payload(), {'front': b'x'}, lease)
        with patch.object(self.store, '_now', return_value=self.store._now() + timedelta(seconds=301)):
            with self.assertRaises(ForgeConflict):
                self.store.worker_blockout(job['id'], blockout_payload(), {'front': b'x'}, job['lease']['lease_id'])
        self.store.worker_blockout(job['id'], blockout_payload(), {'front': b'x'}, job['lease']['lease_id'])
        self.store.worker_match(job['id'], {'panels': []}, job['lease']['lease_id'])
        with self.assertRaises(ForgeConflict):
            self.store.worker_blockout(job['id'], blockout_payload(), {}, job['lease']['lease_id'])
        fresh = self.store.create_job('a', 'v')
        fresh = self.store.claim_job(stages=['matching'], job_id=fresh['id'])
        with self.assertRaises(ForgeConflict):
            self.store.worker_blockout(fresh['id'], blockout_payload(), {}, fresh['lease']['lease_id'])

    def test_blockout_payload_validation_and_artifact_round_trip(self):
        job = self.claimed(); lease = job['lease']['lease_id']
        for key, value in [('params', {}), ('palette', {'body': {'hex': 'red'}}), ('views', ['wrong']),
                           ('spec_yaml', 'text'), ('assumptions', [12]), ('next_view', None)]:
            payload = {**blockout_payload(), key: value}
            with self.subTest(key=key), self.assertRaises(ForgeStoreError):
                self.store.worker_blockout(job['id'], payload, {'front': b'x'}, lease)
        result = self.store.worker_blockout(job['id'], blockout_payload(), {'front': b'render'}, lease)
        self.assertNotIn('spec_yaml', result['generate']['blockout'])
        self.assertEqual(self.store.blockout_file(job['id'], 'spec.yaml').read_bytes(), b'asset: a\n')
        self.assertEqual(self.store.blockout_file(job['id'], 'renders/front.png').read_bytes(), b'render')
        for name in ('../job.json', 'renders/../../job.json.png', '/etc/passwd', 'renders/missing.png'):
            with self.assertRaises((ForgeStoreError, FileNotFoundError)):
                self.store.blockout_file(job['id'], name)

    def test_regenerate_merges_hints_clears_match_and_invalidates_old_lease(self):
        job = self.reviewing()
        first = self.store.regenerate_blockout(job['id'], {'height_hint': 20, 'tower_override': {'width': 2, 'location': 'center'}, 'palette_hex': {'body': '#abcdef'}})
        self.assertEqual(first['state'], 'matching')
        self.assertIsNone(first['lease'])
        self.assertIsNone(first['generate']['blockout'])
        self.assertEqual(first['match'], {'panels': [], 'decisions': [], 'views_missing': []})
        self.assertEqual(first['generate']['regenerations'], 1)
        claimed = self.store.claim_job(stages=['matching'], job_id=job['id'])
        self.store.worker_blockout(job['id'], blockout_payload(), {'front': b'x'}, claimed['lease']['lease_id'])
        self.store.worker_match(job['id'], {'panels': []}, claimed['lease']['lease_id'])
        second = self.store.regenerate_blockout(job['id'], {'floor_height': 4})
        self.assertEqual(second['generate']['height_hint'], 20)
        self.assertEqual(second['generate']['palette_hex'], {'body': '#abcdef'})

    def test_regenerate_legality_matrix_and_cap(self):
        for state in ('uploaded', 'matching', 'staged', 'queued_bake', 'baking', 'ready', 'failed'):
            job = self.reviewing(); job['state'] = state; self.store._save_job(job)
            with self.subTest(state=state), self.assertRaises(ForgeConflict):
                self.store.regenerate_blockout(job['id'], {})
        job = self.reviewing()
        self.store.review(job['id'], 'submit', [], ['front'])
        with self.assertRaises(ForgeConflict):
            self.store.regenerate_blockout(job['id'], {})
        job = self.reviewing(); job['intent'] = 'fresh'; self.store._save_job(job)
        with self.assertRaises(ForgeConflict):
            self.store.regenerate_blockout(job['id'], {})
        job = self.reviewing(); job['generate']['regenerations'] = 7; self.store._save_job(job)
        result = self.store.regenerate_blockout(job['id'], {})
        self.assertEqual(result['generate']['regenerations'], 8)
        result['state'] = 'review'; self.store._save_job(result)
        with self.assertRaises(ForgeConflict):
            self.store.regenerate_blockout(job['id'], {})

    def test_bad_hints_never_change_job(self):
        job = self.reviewing()
        for hints in ({'height_hint': 0}, {'floor_height': True}, {'tower_override': 'auto'},
                      {'tower_override': {'width': -1}}, {'tower_override': {'location': 'east'}},
                      {'palette_hex': {'unknown': 'abcdef'}}, {'palette_hex': {'body': 'gggggg'}},
                      {'segment_refs': []}):
            with self.subTest(hints=hints), self.assertRaises(ForgeStoreError):
                self.store.regenerate_blockout(job['id'], hints)
            self.assertEqual(self.store.get_job(job['id']), job)


class IterateBlockoutStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = ForgeStore(Path(self.tmp.name) / 'store')

    def parent(self, intent='generate', artifacts=None):
        job = self.store.create_job('a', 'v', intent=intent, canonical_views=['old'])
        self.store.record_upload(job['id'], 'one.png', 'image/png', b'original source')
        job = self.store.get_job(job['id'])
        job['state'] = 'baking'; self.store._save_job(job)
        version = self.store.create_version(job['id'], artifacts={'blockout/spec.yaml': b'asset: a\n'} if artifacts is None else artifacts)
        return job, version

    def child(self, job, version, **kwargs):
        return self.store.create_job('a', 'v', intent='iterate_blockout', parent_job=job['id'],
                                     parent_version=version['number'], **kwargs)

    def test_parent_intent_and_spec_artifact_required(self):
        for intent, artifacts in [('fresh', None), ('generate', {})]:
            parent, version = self.parent(intent, artifacts)
            with self.subTest(intent=intent), self.assertRaises(ForgeStoreError):
                self.child(parent, version, generate={'edit': {'height': 18}})
        for fields in ({}, {'parent_job': parent['id']}, {'parent_version': version['number']}):
            with self.subTest(fields=fields), self.assertRaises(ForgeStoreError):
                self.store.create_job('a', 'v', intent='iterate_blockout', generate={'edit': {'height': 18}}, **fields)

    def test_edit_validation_matrix(self):
        parent, version = self.parent()
        invalid = [None, [], {}, {'unknown': 2}, {'palette': {}}, {'palette': []},
                   {'palette': {'body': '#abcdef'}}, {'palette': {'body': 'gggggg'}},
                   {'palette': {'body': 123456}}, {'tower': {}}, {'tower': {'enabled': 1}},
                   {'tower': {'enabled': True, 'width': -1}}, {'tower': {'enabled': True, 'width': 301}},
                   {'tower': {'enabled': True, 'location': []}}, {'tower': {'enabled': True, 'location': 'east'}},
                   {'tower': {'enabled': False, 'width': 2}}, {'tower': {'enabled': True, 'secret': 2}}]
        for key, values in [('height', [0, 301, True, '18', float('nan')]),
                            ('floor_height', [0, 11, False, float('inf')]),
                            ('plinth_floors', [0, 41, 2.5, True])]:
            invalid.extend({key: value} for value in values)
        before = self.store.get_job(parent['id'])
        for edit in invalid:
            with self.subTest(edit=edit), self.assertRaises(ForgeStoreError):
                self.child(parent, version, generate={'edit': edit})
        self.assertEqual(self.store.get_job(parent['id']), before)
        for edit in ({'height': 1}, {'height': 300}, {'floor_height': 10}, {'plinth_floors': 40},
                     {'tower': {'enabled': True, 'width': 0.5, 'location': 'rear_center'}},
                     {'palette': {'body': 'ABCDEF'}}):
            self.child(parent, version, generate={'edit': edit})

    def test_edit_only_payload_parent_uploads_and_spec_owned_views(self):
        parent, version = self.parent()
        edit = {'height': 18}
        child = self.child(parent, version, generate={'edit': edit}, canonical_views=['new'])
        edit['height'] = 99
        self.assertEqual(child['generate']['edit'], {'height': 18})
        self.assertNotIn('edit', child['params'])
        self.assertEqual(child['uploads'], [])
        self.assertEqual(child['inputs'], {'parent_uploads': [0], 'parent_views_inherited': []})
        child = self.store.claim_job(stages=['matching'], job_id=child['id'])
        self.store.patch_canonical_views(child['id'], ['front'], child['lease']['lease_id'])
        self.store.worker_blockout(child['id'], blockout_payload(), {'front': b'render'}, child['lease']['lease_id'])
        self.assertEqual(self.store.blockout_file(child['id'], 'spec.yaml').read_bytes(), b'asset: a\n')
        self.assertEqual(self.store.upload_file(child['id'], 0)[0].read_bytes(), b'original source')
        with self.assertRaises(FileNotFoundError): self.store.upload_file(child['id'], 1)
        for settings in (None, {}, {'edit': {'height': 18}, 'height_hint': 20}):
            with self.assertRaises(ForgeStoreError): self.child(parent, version, generate=settings)
        with self.assertRaises(ForgeStoreError):
            self.child(parent, version, params={'edit': {'height': 18}}, generate={'edit': {'height': 18}})
        with self.assertRaises(ForgeStoreError):
            self.child(parent, version, generate={'edit': {'height': 18}}, replacement_views=['old'])
        with self.assertRaises(ForgeStoreError):
            self.store.create_job('a', 'v', intent='generate', generate={'edit': {'height': 18}})
        with self.assertRaises(ForgeStoreError):
            self.store.create_job('a', 'v', intent='iterate_params', parent_job=parent['id'],
                                  parent_version=version['number'], canonical_views=['new'])
