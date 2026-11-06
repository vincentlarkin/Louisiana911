import os
import tempfile
import unittest
from unittest.mock import patch

os.environ.setdefault('LOUISIANA911_DB_PATH', os.path.join(tempfile.gettempdir(), f'identity-tests-{os.getpid()}.db'))
import app


GEO = dict(lat=32.5, lng=-93.7, source='arcgis', quality='street', query='test')


def caddo_call(**changes):
    call = dict(source='caddo', agency='SPD', time='1833', units=2, description='DISTURBANCE',
                street='MAIN', cross_streets='OAK', municipality='SHV')
    call.update(changes)
    return call


def lafayette_call(**changes):
    call = dict(source='lafayette', agency='POLICE', time='2350', units=1, description='CRASH',
                street='MAIN', cross_streets='OAK', municipality='LAFAYETTE',
                occurred_at='2026-09-02T04:50:00+00:00')
    call.update(changes)
    return call


def baton_rouge_call(**changes):
    call = dict(source='batonrouge', agency='LAW', time='2350', units=1, description='CRASH',
                street='MAIN', cross_streets='OAK', municipality='Baton Rouge')
    call.update(changes)
    return call


class IncidentIdentityTests(unittest.TestCase):
    """A call whose mutable fields change at the source stays one record."""

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        for patcher in (
            patch.object(app, 'DB_PATH', os.path.join(directory.name, 'live.db')),
            patch.object(app, '_incident_geocode_result', return_value=GEO),
            patch.object(app, 'meta_set'),
            patch.object(app, 'log'),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        app.init_db()

    def snapshot(self, source, *calls):
        app.process_incidents([dict(call) for call in calls], source=source, allow_empty=True)

    def rows(self):
        conn = app.db_connect(row_factory=True)
        try:
            return [dict(row) for row in conn.execute('SELECT * FROM incidents ORDER BY id')]
        finally:
            conn.close()

    def test_lafayette_call_keeps_one_record_as_assisting_agencies_join(self):
        original = lafayette_call()
        joined = lafayette_call(agency='POLICE / FIRE')
        self.snapshot('lafayette', original)
        self.snapshot('lafayette', joined)
        self.snapshot('lafayette', joined)
        self.snapshot('lafayette')
        rows = self.rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['hash'], app.hash_incident(original))
        self.assertEqual(rows[0]['agency'], 'POLICE / FIRE')
        self.assertEqual(rows[0]['is_active'], 0)

    def test_reclassified_caddo_call_keeps_one_record_and_its_unit_peak(self):
        original = caddo_call(units=2)
        self.snapshot('caddo', original)
        self.snapshot('caddo', caddo_call(description='FIGHT IN PROGRESS', units=5))
        self.snapshot('caddo', caddo_call(description='ASSAULT', units=3))
        self.snapshot('caddo', caddo_call(description='ASSAULT', units=3))
        rows = self.rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['hash'], app.hash_incident(original))
        self.assertEqual((rows[0]['description'], rows[0]['units'], rows[0]['peak_units']), ('ASSAULT', 3, 5))
        self.assertEqual(rows[0]['is_active'], 1)

    def test_reclassified_baton_rouge_call_keeps_one_record(self):
        original = baton_rouge_call()
        self.snapshot('batonrouge', original)
        self.snapshot('batonrouge', baton_rouge_call(description='CRASH WITH INJURIES'))
        rows = self.rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['hash'], app.hash_incident(original))
        self.assertEqual(rows[0]['description'], 'CRASH WITH INJURIES')

    def test_different_caddo_or_baton_rouge_agency_is_a_different_call(self):
        # These feeds list one row per agency, so agency is part of identity.
        self.snapshot('caddo', caddo_call())
        self.snapshot('caddo', caddo_call(agency='CFD3'))
        self.snapshot('batonrouge', baton_rouge_call())
        self.snapshot('batonrouge', baton_rouge_call(agency='OSS'))
        rows = self.rows()
        self.assertEqual(len(rows), 4)
        self.assertEqual([row['is_active'] for row in rows], [0, 1, 0, 1])

    def test_new_call_at_same_place_and_minute_does_not_absorb_a_continuing_call(self):
        first = caddo_call()
        self.snapshot('caddo', first)
        self.snapshot('caddo', first, caddo_call(description='WELFARE CHECK'))
        rows = self.rows()
        self.assertEqual(len(rows), 2)
        self.assertEqual([row['description'] for row in rows], ['DISTURBANCE', 'WELFARE CHECK'])
        self.assertEqual([row['is_active'] for row in rows], [1, 1])

    def test_ambiguous_changes_are_not_merged(self):
        # Two calls share a place and minute and both change at once; there is
        # no way to tell which became which, so neither is guessed.
        self.snapshot('caddo', caddo_call(), caddo_call(description='WELFARE CHECK'))
        self.snapshot('caddo', caddo_call(description='ASSAULT'), caddo_call(description='SUSPICIOUS PERSON'))
        rows = self.rows()
        self.assertEqual(len(rows), 4)
        self.assertEqual([row['is_active'] for row in rows], [0, 0, 1, 1])

    def test_one_vanished_call_is_not_split_between_two_new_ones(self):
        self.snapshot('caddo', caddo_call())
        self.snapshot('caddo', caddo_call(description='ASSAULT'), caddo_call(description='WELFARE CHECK'))
        rows = self.rows()
        self.assertEqual(len(rows), 3)
        self.assertEqual([row['is_active'] for row in rows], [0, 1, 1])

    def test_adopted_record_stays_matched_when_a_lookalike_call_appears_later(self):
        original = caddo_call()
        reclassified = caddo_call(description='ASSAULT')
        self.snapshot('caddo', original)
        self.snapshot('caddo', reclassified)
        self.snapshot('caddo', reclassified, caddo_call(description='WELFARE CHECK'))
        rows = self.rows()
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]['hash'], app.hash_incident(original))
        self.assertEqual(rows[0]['description'], 'ASSAULT')
        self.assertEqual([row['is_active'] for row in rows], [1, 1])

    def test_closed_call_is_not_adopted_by_a_changed_lookalike(self):
        # Only calls that were open until this snapshot can be continued.
        self.snapshot('caddo', caddo_call())
        self.snapshot('caddo')
        self.snapshot('caddo', caddo_call(description='ASSAULT'))
        rows = self.rows()
        self.assertEqual(len(rows), 2)
        self.assertEqual([row['is_active'] for row in rows], [0, 1])


if __name__ == '__main__':
    unittest.main()
