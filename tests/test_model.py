import json
import tempfile
import unittest
from pathlib import Path

from fearless.model import DraftForecaster, train
from fearless.model_features import ChampionCatalog, DraftContext, History, split_for_date
from fearless.rules import schedule


def game(day, number=1, *, series=None, champions=None, players=None):
    series = series or day
    champions = champions or [f'Champion {index:02d}' for index in range(20)]
    actions = [
        {'team': team, 'kind': kind, 'champion': champion}
        for (team, kind), champion in zip(schedule('A', 'B'), champions, strict=True)
    ]
    return {
        'date': day, 'series_id': series, 'game_number': number, 'patch': '16.1',
        'region': 'EMEA', 'first': 'A', 'second': 'B', 'fearless': True, 'eligible': True,
        'actions': actions, 'rosters': players or {},
    }


class ModelTest(unittest.TestCase):
    def test_released_pool_and_draft_prefix_exclude_unavailable_choices(self):
        catalog = ChampionCatalog({'Alpha': '', 'Beta': '', 'Gamma': '', 'Later': '2026-03-29'})
        history = History(catalog)
        context = DraftContext('2026-03-28', '16.1', 'EMEA', 'A', 'B', 2, True)
        prefix = [{'team': 'A', 'kind': 'ban', 'champion': 'Alpha'}]
        turn = history.features(context, prefix, {'Beta'})

        self.assertEqual(turn.champions, ['Gamma'])
        self.assertEqual((turn.team, turn.kind), ('B', 'ban'))

    def test_history_uses_only_completed_previous_days_and_their_lineups(self):
        old = game('2026-03-27', players={
            'A': [{'player': 'prior-player', 'champion': 'Champion 06', 'role': 'top'}],
        })
        same_day = game('2026-03-28', players={
            'A': [{'player': 'today-player', 'champion': 'Champion 06', 'role': 'top'}],
        })
        catalog = ChampionCatalog({action['champion']: '' for action in old['actions']})
        forecaster = DraftForecaster(catalog, [same_day, old])
        result = forecaster.forecast(date='2026-03-28', patch='16.1', region='EMEA', first='A',
                                     second='B', game_number=1, current=old['actions'][:6], limit=20)
        self.assertEqual(result['as_of'], '2026-03-27')
        self.assertEqual(result['roster_players'], {'A': 1, 'B': 0})
        self.assertEqual(result['roster_evidence']['A']['source'], 'previous_observed_lineup')
        self.assertEqual(result['legal_candidates'], 14)
        choice = next(row for row in result['choices'] if row['champion'] == 'Champion 06')
        self.assertTrue(any('historical player' in line for line in choice['evidence']))
        self.assertEqual((choice['games'], choice['roles']), (1, ['top']))
        self.assertAlmostEqual(sum(row['probability'] for row in result['choices']), 1)

    def test_overnight_game_enters_evidence_only_after_completion_day(self):
        row = game('2026-03-27', players={
            'A': [{'player': 'overnight-player', 'champion': 'Champion 06', 'role': 'top'}],
        })
        row['finished_at'] = '2026-03-28T00:30:00Z'
        catalog = ChampionCatalog({action['champion']: '' for action in row['actions']})
        forecaster = DraftForecaster(catalog, [row])
        draft = dict(patch='16.1', region='EMEA', first='A', second='B', game_number=1, current=[])
        self.assertEqual(forecaster.forecast(date='2026-03-28', **draft)['roster_players']['A'], 0)
        self.assertEqual(forecaster.forecast(date='2026-03-29', **draft)['roster_players']['A'], 1)

    def test_patch_catalog_keeps_a_disabled_champion_out_and_honors_lock_aliases(self):
        catalog = ChampionCatalog.from_json({'15.1': ['Aatrox', 'Wukong'], '15.2': ['Wukong', 'Rell']})
        forecaster = DraftForecaster(catalog, [])
        result = forecaster.forecast(date='2025-02-01', patch='25.2', region='EMEA', first='A',
                                     second='B', game_number=2, current=[], locked=['MonkeyKing'],
                                     pool=['Aatrox', 'MonkeyKing', 'Rell', 'Unknown'])
        self.assertEqual([row['champion'] for row in result['choices']], ['Rell'])
        self.assertEqual(result['probability_condition'], 'named_champion_action')

    def test_split_boundaries_leave_later_play_as_a_separate_report(self):
        expected = {'2024-12-31': 'history', '2025-01-01': 'train', '2025-12-31': 'train',
                    '2026-01-01': 'validation', '2026-03-27': 'validation', '2026-03-28': 'test',
                    '2026-06-14': 'test', '2026-06-15': 'later'}
        self.assertEqual({day: split_for_date(day) for day in expected}, expected)

    def test_train_publish_metrics_and_reload_model_on_cpu(self):
        rows = [game(day) for day in ('2025-02-01', '2025-03-01', '2026-01-02',
                                      '2026-02-03', '2026-03-28', '2026-06-15')]
        champions = {action['champion']: '' for action in rows[0]['actions']}
        with tempfile.TemporaryDirectory() as temporary:
            bundle = Path(temporary) / 'bundle'
            metrics = train(rows, bundle, catalog=ChampionCatalog(champions), iterations=3,
                            negatives=8, threads=2, bootstrap_samples=20, joint_experiment=True)
            self.assertEqual(json.loads((bundle / 'metrics.json').read_text()), metrics)
            self.assertEqual(metrics['splits']['test']['pick']['counts']['turns'], 10)
            self.assertEqual(metrics['splits']['later']['ban']['counts']['legal_targets'], 10)
            self.assertEqual(metrics['splits']['test']['pick']['methods']['baseline']['illegal_recommendations'], 0)
            self.assertIn('learned_joint', metrics['splits']['test']['ban']['methods'])
            self.assertNotEqual(metrics['promotion']['ban']['method'], 'learned_joint')
            forecaster = DraftForecaster.load(bundle)
            result = forecaster.forecast(date='2026-03-28', patch='16.1', region='EMEA', first='A',
                                         second='B', game_number=1, current=rows[0]['actions'][:6])

        self.assertEqual(result['status'], 'scored')
        self.assertTrue(result['calibrated'])
        self.assertEqual(result['calibration_through'], '2026-03-27')
        self.assertEqual(len(result['choices']), 3)
        self.assertTrue(all(row['champion'] not in {f'Champion {i:02d}' for i in range(6)}
                            for row in result['choices']))


if __name__ == '__main__':
    unittest.main()
