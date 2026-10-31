import importlib
import os
import tempfile
import time
import unittest


class SumoBackendTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        os.environ["KINLY_DB_PATH"] = os.path.join(cls.tmp, "t.sqlite3")
        os.environ["SUMO_PUBLIC_MAP"] = "0"
        import app as module
        cls.m = importlib.reload(module)

    def setUp(self):
        self.m._hits.clear()
        with self.m.connect_db() as db:
            db.execute("DELETE FROM device_locations")
            db.execute("DELETE FROM devices")
        self.c = self.m.app.test_client()

    def register(self, name="Hero", **kw):
        return self.c.post("/api/device/register", json={"name": name, **kw}).get_json()

    def post_loc(self, token, lat=17.0, lon=78.0, ts=None, acc=5):
        body = {"latitude": lat, "longitude": lon, "accuracy": acc,
                "timestamp": ts if ts is not None else int(time.time() * 1000)}
        return self.c.post("/api/location", json=body, headers={"Authorization": "Bearer " + token})

    def people(self, token):
        return self.c.get("/api/people", headers={"Authorization": "Bearer " + token}).get_json()

    # --- the takeover bug
    def test_cannot_steal_token_with_leaked_id(self):
        victim = self.register("Victim")
        self.post_loc(victim["token"])
        listed = self.people(victim["token"])["members"][0]
        self.assertNotEqual(listed["id"], victim["device_id"])  # list exposes only the public id
        # attacker presents the real device id but no token
        atk = self.register("Hacked", device_id=victim["device_id"])
        self.assertNotEqual(atk["token"], victim["token"])
        self.assertNotEqual(atk["device_id"], victim["device_id"])
        self.assertEqual(self.people(victim["token"])["members"][0]["name"], "Victim")
        # attacker presents the id plus a wrong token
        r = self.c.post("/api/device/register", json={"name": "X1", "device_id": victim["device_id"]},
                        headers={"Authorization": "Bearer wrong"}).get_json()
        self.assertNotEqual(r["token"], victim["token"])

    def test_owner_can_rename_and_keep_token(self):
        a = self.register("Aaron")
        r = self.c.post("/api/device/register", json={"name": "Aaron R", "device_id": a["device_id"]},
                        headers={"Authorization": "Bearer " + a["token"]}).get_json()
        self.assertEqual((r["token"], r["device_id"]), (a["token"], a["device_id"]))
        self.assertEqual(r["device_name"], "Aaron R")

    # --- stop sharing
    def test_stop_removes_marker_and_drops_in_flight_samples(self):
        a = self.register("Stopper")
        self.post_loc(a["token"])
        self.assertEqual(len(self.people(a["token"])["members"]), 1)
        stop_ms = int(time.time() * 1000)
        r = self.c.post("/api/location/stop", json={"timestamp": stop_ms}, headers={"Authorization": "Bearer " + a["token"]})
        self.assertTrue(r.get_json()["ok"])
        self.assertEqual(self.people(a["token"])["members"], [])
        late = self.post_loc(a["token"], ts=stop_ms - 500).get_json()  # sample taken before Stop arrives late
        self.assertFalse(late["applied"])
        self.assertEqual(self.people(a["token"])["members"], [])
        self.post_loc(a["token"], ts=stop_ms + 5000)  # user started sharing again
        self.assertEqual(len(self.people(a["token"])["members"]), 1)

    # --- map access / validation
    def test_map_requires_token(self):
        self.assertEqual(self.c.get("/api/people").status_code, 401)
        self.assertEqual(self.c.get("/api/people", headers={"Authorization": "Bearer nope"}).status_code, 401)

    def test_invalid_payloads(self):
        a = self.register("Valid")
        h = {"Authorization": "Bearer " + a["token"]}
        for body in ({"latitude": 91, "longitude": 0}, {"latitude": "x", "longitude": 0}, {"latitude": 0},
                     {"latitude": float("nan"), "longitude": 0}):
            self.assertEqual(self.c.post("/api/location", json=body, headers=h).status_code, 400, body)
        self.assertEqual(self.c.post("/api/location", json={"latitude": 1, "longitude": 1}).status_code, 401)
        self.assertEqual(self.c.post("/api/device/register", json={"name": "A"}).status_code, 400)

    def test_out_of_order_sample_ignored(self):
        a = self.register("Order")
        now = int(time.time() * 1000)
        self.post_loc(a["token"], lat=10, ts=now)
        late = self.post_loc(a["token"], lat=20, ts=now - 20000).get_json()
        self.assertFalse(late["applied"])
        self.assertEqual(self.people(a["token"])["members"][0]["latitude"], 10)

    def test_status_age_and_me_flag(self):
        a = self.register("Fresh"); b = self.register("Old")
        now = int(time.time() * 1000)
        self.post_loc(a["token"], ts=now)
        self.post_loc(b["token"], lat=1, lon=1, ts=now - 20 * 60_000)
        by_name = {m["name"]: m for m in self.people(a["token"])["members"]}
        self.assertEqual(by_name["Fresh"]["status"], "live")
        self.assertTrue(by_name["Fresh"]["is_me"])
        self.assertEqual(by_name["Old"]["status"], "stale")
        self.assertFalse(by_name["Old"]["is_me"])
        self.assertGreaterEqual(by_name["Old"]["age_seconds"], 1199)

    def test_rate_limit_register(self):
        codes = [self.c.post("/api/device/register", json={"name": "Spam"}).status_code for _ in range(32)]
        self.assertEqual(codes.count(429), 2)

    def test_page_and_assets(self):
        r = self.c.get("/")
        self.assertEqual(r.status_code, 200)
        self.assertIn(b"Sumo live map", r.data)
        self.assertEqual(self.c.get("/static/app.js").status_code, 200)
        self.assertEqual(self.c.get("/static/vendor/leaflet.js").status_code, 200)
        self.assertEqual(self.c.get("/health").get_json()["ok"], True)
        self.assertEqual(self.c.get("/api/login").status_code, 404)  # legacy phone/login removed


if __name__ == "__main__":
    unittest.main(verbosity=2)
