import os
import tempfile
import unittest

_IMPORT_DB_PATH = os.path.join(
    tempfile.gettempdir(), f"caddo911-geocoding-tests-{os.getpid()}.db"
)
os.environ["CADDO911_DB_PATH"] = _IMPORT_DB_PATH
import app


def tearDownModule():
    for suffix in ("", "-shm", "-wal"):
        try:
            os.remove(_IMPORT_DB_PATH + suffix)
        except FileNotFoundError:
            pass


class FakeLocation:
    def __init__(self, address, latitude, longitude, *, address_type, score=100.0):
        self.address = address
        self.latitude = latitude
        self.longitude = longitude
        self.raw = {
            "address": address,
            "score": score,
            "attributes": {
                "Addr_type": address_type,
                "Score": score,
                "Match_addr": address,
            },
        }


class FakeArcGIS:
    def __init__(self, results):
        self.results = results
        self.calls = []

    def geocode(self, query, **kwargs):
        self.calls.append((query, kwargs))
        return self.results.get(query, [])


class FakeOSM:
    def geocode(self, query, **kwargs):
        return None


class FailingArcGIS:
    def geocode(self, query, **kwargs):
        raise TimeoutError("provider unavailable")


class GeocodingTests(unittest.TestCase):
    def setUp(self):
        self.original_arcgis = app.geolocator_arcgis
        self.original_osm = app.geolocator_osm
        app.geocode_cache.clear()
        app.geocode_intersection_cache.clear()
        app.geolocator_osm = FakeOSM()

    def tearDown(self):
        app.geolocator_arcgis = self.original_arcgis
        app.geolocator_osm = self.original_osm
        app.geocode_cache.clear()
        app.geocode_intersection_cache.clear()

    def test_two_cross_streets_use_named_street_segment_midpoint(self):
        jump_query = (
            "E BERT KOUNS INDUSTRIAL & JUMP RUN, "
            "Shreveport, Caddo Parish, LA"
        )
        youree_query = (
            "E BERT KOUNS INDUSTRIAL & YOUREE DR, "
            "Shreveport, Caddo Parish, LA"
        )
        fake = FakeArcGIS({
            jump_query: [FakeLocation(
                "E Bert Kouns Industrial Loop & Jump Run Dr, Shreveport, Louisiana, 71105",
                32.429491017214,
                -93.716106002087,
                address_type="StreetInt",
                score=99.6,
            )],
            youree_query: [FakeLocation(
                "E Bert Kouns Industrial Loop & Youree Dr, Shreveport, Louisiana, 71105",
                32.431747006453,
                -93.711973975278,
                address_type="StreetInt",
                score=100.0,
            )],
        })
        app.geolocator_arcgis = fake

        result = app.geocode_address(
            "E BERT KOUNS INDUSTRIAL",
            "JUMP RUN & YOUREE DR",
            "SHV",
            source="caddo",
        )

        self.assertEqual("street-segment", result["quality"])
        self.assertAlmostEqual(32.4306190118335, result["lat"], places=8)
        self.assertAlmostEqual(-93.7140399886825, result["lng"], places=8)
        called_queries = [query for query, _ in fake.calls]
        self.assertIn(jump_query, called_queries)
        self.assertIn(youree_query, called_queries)
        self.assertNotIn(
            "JUMP RUN & YOUREE DR, Shreveport, Caddo Parish, LA",
            called_queries,
        )

    def test_fuzzy_street_name_is_not_accepted_as_an_intersection(self):
        fuzzy_query = "JUMP RUN & YOUREE DR, Shreveport, Caddo Parish, LA"
        fake = FakeArcGIS({
            fuzzy_query: [FakeLocation(
                "Youree Dr, Shreveport, Louisiana, 71105",
                32.455029276747,
                -93.722094241535,
                address_type="StreetName",
                score=93.55,
            )],
        })
        app.geolocator_arcgis = fake

        result = app.geocode_address(
            "E BERT KOUNS INDUSTRIAL",
            "JUMP RUN & YOUREE DR",
            "SHV",
            source="caddo",
        )

        self.assertEqual("unresolved", result["quality"])
        self.assertIsNone(result["lat"])
        self.assertIsNone(result["lng"])
        self.assertTrue(result["provider_responded"])

    def test_provider_outage_is_not_cached_as_a_confirmed_unresolved_result(self):
        app.geolocator_arcgis = FailingArcGIS()

        result = app.geocode_address(
            "E BERT KOUNS INDUSTRIAL",
            "JUMP RUN & YOUREE DR",
            "SHV",
            source="caddo",
        )

        self.assertEqual("unresolved", result["quality"])
        self.assertFalse(result["provider_responded"])
        self.assertEqual({}, app.geocode_cache)

    def test_cross_pair_is_used_when_street_field_is_a_place_label(self):
        cross_query = (
            "CEDARWOOD LN & EASTWOOD DR, Shreveport, Caddo Parish, LA"
        )
        fake = FakeArcGIS({
            cross_query: [FakeLocation(
                "Cedarwood Ln & Eastwood Dr, Shreveport, Louisiana, 71105",
                32.47818,
                -93.704673,
                address_type="StreetInt",
            )],
        })
        app.geolocator_arcgis = fake

        result = app.geocode_address(
            "EASTLAKE",
            "CEDARWOOD LN & EASTWOOD DR",
            "SHV",
            source="caddo",
        )

        self.assertEqual("intersection-2", result["quality"])
        self.assertAlmostEqual(32.47818, result["lat"])
        self.assertAlmostEqual(-93.704673, result["lng"])

    def test_one_cross_street_requires_a_real_named_street_intersection(self):
        query = "YOUREE & E 70TH ST, Shreveport, Caddo Parish, LA"
        fake = FakeArcGIS({
            query: [FakeLocation(
                "Youree Dr & E 70th St, Shreveport, Louisiana, 71105",
                32.443362019231,
                -93.722291595173,
                address_type="StreetInt",
            )],
        })
        app.geolocator_arcgis = fake

        result = app.geocode_address(
            "YOUREE",
            "E 70TH ST",
            "SHV",
            source="caddo",
        )

        self.assertEqual("street+cross", result["quality"])
        self.assertAlmostEqual(32.443362019231, result["lat"])

    def test_road_matching_respects_conflicting_directions(self):
        self.assertTrue(app._road_name_matches(
            "E BERT KOUNS INDUSTRIAL",
            "E Bert Kouns Industrial Loop",
        ))
        self.assertFalse(app._road_name_matches(
            "E BERT KOUNS INDUSTRIAL",
            "W Bert Kouns Industrial Loop",
        ))
        self.assertTrue(app._road_name_matches("ST VINCENT AV", "Saint Vincent Avenue"))

    def test_stale_active_row_self_heals_to_current_geocoder_version(self):
        jump_query = (
            "E BERT KOUNS INDUSTRIAL & JUMP RUN, "
            "Shreveport, Caddo Parish, LA"
        )
        youree_query = (
            "E BERT KOUNS INDUSTRIAL & YOUREE DR, "
            "Shreveport, Caddo Parish, LA"
        )
        app.geolocator_arcgis = FakeArcGIS({
            jump_query: [FakeLocation(
                "E Bert Kouns Industrial Loop & Jump Run Dr, Shreveport, Louisiana, 71105",
                32.429491017214,
                -93.716106002087,
                address_type="StreetInt",
            )],
            youree_query: [FakeLocation(
                "E Bert Kouns Industrial Loop & Youree Dr, Shreveport, Louisiana, 71105",
                32.431747006453,
                -93.711973975278,
                address_type="StreetInt",
            )],
        })
        incident = {
            "source": "caddo",
            "agency": "SPD",
            "time": "1513",
            "units": 1,
            "description": "THEFT",
            "street": "E BERT KOUNS INDUSTRIAL",
            "cross_streets": "JUMP RUN & YOUREE DR",
            "municipality": "SHV",
        }

        original_db_path = app.DB_PATH
        try:
            with tempfile.TemporaryDirectory() as tmp_dir:
                app.DB_PATH = os.path.join(tmp_dir, "caddo911.db")
                app.init_db()
                incident_hash = app.hash_incident(incident)
                conn = app.db_connect()
                conn.execute(
                    """INSERT INTO incidents (
                        hash, agency, time, units, description, street,
                        cross_streets, municipality, source, latitude, longitude,
                        first_seen, last_seen, is_active, geocode_source,
                        geocode_quality, geocode_query, geocoded_at, geocode_version
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        incident_hash, "SPD", "1513", 1, "THEFT",
                        "E BERT KOUNS INDUSTRIAL", "JUMP RUN & YOUREE DR",
                        "SHV", "caddo", 32.455029276747, -93.722094241535,
                        "2026-07-17T20:17:34+00:00", "2026-07-17T20:17:34+00:00",
                        1, "arcgis", "intersection-2", "JUMP RUN & YOUREE DR",
                        "2026-07-17T20:17:34+00:00", 1,
                    ),
                )
                conn.commit()
                conn.close()

                app.process_incidents([dict(incident)], source="caddo")

                conn = app.db_connect(row_factory=True)
                row = conn.execute(
                    "SELECT latitude, longitude, geocode_quality, geocode_version "
                    "FROM incidents WHERE hash = ?",
                    (incident_hash,),
                ).fetchone()
                conn.close()
                self.assertAlmostEqual(32.4306190118335, row["latitude"], places=8)
                self.assertAlmostEqual(-93.7140399886825, row["longitude"], places=8)
                self.assertEqual("street-segment", row["geocode_quality"])
                self.assertEqual(app.GEOCODER_VERSION, row["geocode_version"])
        finally:
            app.DB_PATH = original_db_path


if __name__ == "__main__":
    unittest.main()
