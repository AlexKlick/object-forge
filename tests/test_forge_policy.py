from dataclasses import replace
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from open_sprite_pipeline.forge_policy import Policy, decide_review, decide_bake, decide_version


def style(panel_id='s', view='front', *, passed=True, checks=True, drift=.1):
    return {'panel_id': panel_id, 'source': 'style', 'style_view': view, 'auto_view': view,
            'iou': 1., 'metrics': {'pass': passed, 'palette_drift': drift}, 'checks': {'pass': checks}}


def ordinary(panel_id='p', view='front', iou=.85, **fields):
    return {'panel_id': panel_id, 'auto_view': view, 'iou': iou, **fields}


class PolicyTests(unittest.TestCase):
    def setUp(self):
        self.policy = Policy(mode='enforce')
        self.job = {'intent': 'upload', 'canonical_views': ['front'], 'inputs': {}}

    def decide(self, *panels, policy=None):
        return decide_review(policy or self.policy, self.job, {'panels': list(panels)})

    def test_style_preference_ranking_and_complete_rejections(self):
        result = self.decide(ordinary(iou=1), style('s1', drift=.2), style('s2', drift=.05),
                             style('bad', passed=False, drift=0))
        self.assertEqual(result['action'], 'submit')
        self.assertEqual([p for p in result['panels'] if p['decision'] == 'accept'],
                         [{'panel_id': 's2', 'decision': 'accept', 'view': 'front', 'iou': 1.}])
        self.assertEqual(sum(p['decision'] == 'reject' for p in result['panels']), 3)
        self.assertEqual(result['thresholds'], self.policy.thresholds)

    def test_style_checks_and_ordinary_fallback(self):
        for checks, require, expected in [(False, True, 'escalate'), (False, False, 'submit'),
                                           (True, True, 'submit')]:
            with self.subTest(checks=checks, require=require):
                result = self.decide(style(checks=checks), policy=replace(self.policy, require_checks=require))
                self.assertEqual(result['action'], expected)
        self.assertEqual(self.decide(style(passed=False), ordinary())['action'], 'submit')
        self.assertIn('failed metrics', self.decide(style(passed=False))['reasons'][0])
        self.assertEqual(self.decide(style(passed=False), policy=replace(self.policy, require_checks=False))['action'], 'escalate')

    def test_iou_edges_reasons_and_best_ordinary(self):
        for iou, expected in [(.849999, 'escalate'), (.85, 'submit'), (.850001, 'submit'),
                              (float('nan'), 'escalate')]:
            with self.subTest(iou=iou):
                self.assertEqual(self.decide(ordinary(iou=iou))['action'], expected)
        self.assertEqual(self.decide(ordinary(reason='ambiguous'))['action'], 'escalate')
        result = self.decide(ordinary('p1'), ordinary('p2', iou=.9))
        self.assertEqual(result['panels'][1]['decision'], 'accept')
        self.assertEqual(self.decide()['reasons'], ['front: no candidate ≥ 0.85 IoU'])

    def test_palette_only_and_inherited_views(self):
        self.job.update(intent='from_spec', generate={'palette_only': True})
        result = self.decide()
        self.assertEqual((result['action'], result['views_missing']), ('submit', ['front']))
        self.job['intent'] = 'generate'
        self.assertEqual(self.decide()['action'], 'escalate')
        self.job['inputs']['parent_views_inherited'] = ['front']
        self.assertEqual(self.decide()['views_missing'], [])
        self.assertEqual(self.decide()['action'], 'submit')

    def test_bake_auto_switch_for_human_and_policy(self):
        for actor in ('human', 'policy'):
            job = {'policy': {'review': {'actor': actor}}}
            self.assertEqual(decide_bake(self.policy, job), 'approve')
            self.assertEqual(decide_bake(replace(self.policy, auto_bake=False), job), 'escalate')

    def test_critic_matrix_and_last_style_gate(self):
        for status, score, origin, warn, expected in [
            ('pass', 20, 'bake', True, 'accept'), ('warn', 85, 'bake', True, 'accept'),
            ('warn', 70, 'bake', True, 'accept'), ('warn', 60, 'bake', True, 'flag'),
            ('warn', 85, 'bake', False, 'flag'), ('fail', 85, 'bake', True, 'flag'),
            ('error', None, 'bake', True, 'flag'), ('skipped', None, 'trellis', True, 'accept'),
            ('skipped', None, 'bake', True, 'flag'), ('pending', None, 'bake', True, 'flag')]:
            with self.subTest(status=status, score=score, origin=origin, warn=warn):
                version = {'origin': origin, 'critic': {'status': status, 'score': score}}
                result = decide_version(replace(self.policy, accept_on_warn=warn), version)
                self.assertEqual(result['action'], expected)
                self.assertIn('thresholds', result)
                version['metrics'] = {'style': {'views': {'front': {'pass': False}}}}
                self.assertEqual(decide_version(self.policy, version)['action'], 'flag')

    def test_env_defaults_and_valid_edges(self):
        self.assertEqual(Policy.from_env({}), Policy())
        env = {'FORGE_POLICY': 'enforce', 'FORGE_POLICY_MIN_IOU': '1', 'FORGE_POLICY_MIN_CRITIC': '100',
               'FORGE_POLICY_REQUIRE_CHECKS': 'false', 'FORGE_POLICY_ACCEPT_ON_WARN': '0',
               'FORGE_POLICY_AUTO_BAKE': 'off'}
        self.assertEqual(Policy.from_env(env), Policy('enforce', 1., False, 100, False, False))
        self.assertEqual(Policy.from_env({'FORGE_POLICY_MIN_IOU': '0', 'FORGE_POLICY_MIN_CRITIC': '0'}).min_iou, 0)

    def test_invalid_env_values(self):
        for key, values in {'FORGE_POLICY': ['on', '', 'ENFORCE'],
                            'FORGE_POLICY_MIN_IOU': ['nan', 'inf', '-.1', '1.1', 'bad'],
                            'FORGE_POLICY_MIN_CRITIC': ['-1', '101', '70.5', 'nan'],
                            'FORGE_POLICY_REQUIRE_CHECKS': ['maybe', ''],
                            'FORGE_POLICY_ACCEPT_ON_WARN': ['2'],
                            'FORGE_POLICY_AUTO_BAKE': ['enable']}.items():
            for value in values:
                with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                    Policy.from_env({key: value})
