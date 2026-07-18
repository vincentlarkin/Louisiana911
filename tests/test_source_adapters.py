import unittest

from sources import batonrouge, lafayette, neworleans


class SourceAdapterTests(unittest.TestCase):
    def test_lafayette_location_keeps_cross_street_out_of_city(self):
        street, cross_streets, municipality = lafayette._split_location(
            "3142 AMBASSADOR CAFFERY PKWY/CURRAN PKWY LAFAYETTE, LA"
        )

        self.assertEqual("3142 AMBASSADOR CAFFERY PKWY", street)
        self.assertEqual("CURRAN PKWY", cross_streets)
        self.assertEqual("LAFAYETTE", municipality)

    def test_baton_rouge_intersection_location_is_split_once(self):
        street, cross_streets = batonrouge._split_location(
            "HOOPER RD / SULLIVAN RD",
            "HOOPER RD / SULLIVAN RD",
        )

        self.assertEqual("HOOPER RD", street)
        self.assertEqual("SULLIVAN RD", cross_streets)

    def test_baton_rouge_numbered_address_is_preserved(self):
        street, cross_streets = batonrouge._split_location(
            "14200 S HARRELL'S FERRY RD",
            "WOODBROOK DR / MILLERVILLE RD",
        )

        self.assertEqual("14200 S HARRELL'S FERRY RD", street)
        self.assertEqual("WOODBROOK DR / MILLERVILLE RD", cross_streets)

    def test_baton_rouge_merges_the_official_traffic_map_point(self):
        incidents = [
            {
                "description": "TRAFFIC CONGESTION BLOCKAGE",
                "street": "281 W INTERSTATE 12 HWY",
                "cross_streets": "SHERWOOD FOREST / DRUSILLA-JEFFERSON",
            },
            {
                "description": "HIT AND RUN",
                "street": "6601 - 6799 KLEINPETER RD",
                "cross_streets": "SAINT CLAUDE AVE / SAINT PETER AVE",
            },
        ]
        features = [{
            "attributes": {
                "INCIDENT_TYPE_DESC": "TRAFFIC CONGESTION BLOCKAGE",
                "ADDRESS": "281 W INTERSTATE 12 HWY",
                "CROSS_STREET": "SHERWOOD FOREST/DRUSILLA-JEFFERSON",
                "LAT": 30.42386,
                "LON": -91.07526,
            },
            "geometry": {
                "x": -91.07529920339584,
                "y": 30.42394156754017,
            },
        }]

        batonrouge._merge_published_points(incidents, features)

        self.assertAlmostEqual(30.42394156754017, incidents[0]["latitude"])
        self.assertAlmostEqual(-91.07529920339584, incidents[0]["longitude"])
        self.assertTrue(incidents[0]["coordinates_published"])
        self.assertTrue(incidents[0]["location_is_approximate"])
        self.assertNotIn("latitude", incidents[1])
        self.assertTrue(incidents[1]["location_is_approximate"])

    def test_new_orleans_uses_only_published_coordinates(self):
        incident = neworleans._normalize_row(
            {
                "nopd_item": "G1791526",
                "typetext": "AUTO THEFT",
                "timecreate": "2026-07-17T23:42:38.077000",
                "block_address": "French St & Vicksburg St",
                "location": {
                    "type": "Point",
                    "coordinates": [-90.10700406, 30.00271204],
                },
            },
            is_recent=True,
        )

        self.assertIsNotNone(incident)
        self.assertEqual("neworleans", incident["source"])
        self.assertEqual("G1791526", incident["source_id"])
        self.assertEqual("French St & Vicksburg St", incident["street"])
        self.assertAlmostEqual(30.00271204, incident["latitude"])
        self.assertAlmostEqual(-90.10700406, incident["longitude"])
        self.assertTrue(incident["is_active"])

    def test_new_orleans_does_not_invent_redacted_coordinates(self):
        incident = neworleans._normalize_row(
            {
                "nopd_item": "G0000126",
                "typetext": "SENSITIVE CALL TYPE",
                "timecreate": "2026-07-17T12:00:00",
                "block_address": "REDACTED BLOCK",
            },
            is_recent=False,
        )

        self.assertIsNone(incident["latitude"])
        self.assertIsNone(incident["longitude"])
        self.assertFalse(incident["coordinates_published"])
        self.assertFalse(incident["is_active"])

    def test_new_orleans_treats_zero_point_as_missing_and_preserves_approx_label(self):
        incident = neworleans._normalize_row(
            {
                "nopd_item": "G1790626",
                "typetext": "SHOTSPOTTER- REPORTED GUNSHOTS",
                "timecreate": "2026-07-17T23:26:47.473000",
                "block_address": "Approx Loc: 015XX Iberville St",
                "location": {
                    "type": "Point",
                    "coordinates": [0, 0],
                },
            },
            is_recent=True,
        )

        self.assertIsNone(incident["latitude"])
        self.assertIsNone(incident["longitude"])
        self.assertFalse(incident["coordinates_published"])
        self.assertTrue(incident["location_is_approximate"])
        self.assertEqual("Approx Loc: 015XX Iberville St", incident["street"])

    def test_new_orleans_excludes_requested_generic_call_types(self):
        for call_type in neworleans.EXCLUDED_GENERIC_CALL_TYPES:
            with self.subTest(call_type=call_type):
                incident = neworleans._normalize_row(
                    {
                        "nopd_item": f"NOISE-{call_type}",
                        "typetext": call_type,
                        "initialtypetext": call_type,
                        "timecreate": "2026-07-17T12:00:00",
                        "block_address": "010XX Example St",
                    },
                    is_recent=False,
                )
                self.assertIsNone(incident)

    def test_new_orleans_excludes_uninformative_medical_type(self):
        incident = neworleans._normalize_row(
            {
                "nopd_item": "G-MEDICAL",
                "typetext": "MEDICAL",
                "initialtypetext": "MEDICAL",
                "timecreate": "2026-07-17T12:00:00",
                "block_address": "010XX Example St",
            },
            is_recent=True,
        )

        self.assertIsNone(incident)

    def test_new_orleans_preserves_material_initial_classification(self):
        incident = neworleans._normalize_row(
            {
                "nopd_item": "G-RECLASSIFIED-CRIME",
                "typetext": "COMPLAINT OTHER",
                "initialtypetext": "AGGRAVATED BURGLARY",
                "timecreate": "2026-07-17T12:00:00",
                "block_address": "010XX Example St",
            },
            is_recent=True,
        )

        self.assertIsNotNone(incident)
        self.assertEqual(
            "AGGRAVATED BURGLARY (initial classification)",
            incident["description"],
        )
        self.assertEqual("COMPLAINT OTHER", incident["final_description"])
        self.assertEqual("AGGRAVATED BURGLARY", incident["initial_description"])


if __name__ == "__main__":
    unittest.main()
