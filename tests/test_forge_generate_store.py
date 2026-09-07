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
