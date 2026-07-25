"""
Lightweight unit tests for the pure/isolated logic in this project.

Run with:
    python -m unittest discover -s tests

No extra dependencies beyond the standard library (requests calls are mocked).
"""
import json
import shutil
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))

from fetch_asns import _extract_asns, _clean_asn  # noqa: E402
from utils.cache import JSONCache             # noqa: E402
from utils.rate_limit import Checkpoint       # noqa: E402
from utils.http import get_with_retry, FetchError  # noqa: E402
from classify_asn import classify_one, classify_batch, _chunks  # noqa: E402
from build_output import build_rows, build_full_report  # noqa: E402
from run_pipeline import run_step             # noqa: E402


class TestGetWithRetry(unittest.TestCase):
    def _resp(self, status_code):
        resp = MagicMock()
        resp.status_code = status_code
        if status_code < 400:
            resp.raise_for_status = MagicMock()
        else:
            import requests
            resp.raise_for_status = MagicMock(side_effect=requests.HTTPError(f"{status_code}"))
        return resp

    def test_succeeds_on_first_try_no_sleep_needed(self):
        session = MagicMock()
        session.get.return_value = self._resp(200)
        resp = get_with_retry(session, "http://example.test", max_retries=3)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(session.get.call_count, 1)

    @patch("utils.http.time.sleep")
    def test_retries_connection_error_then_succeeds(self, mock_sleep):
        session = MagicMock()
        session.get.side_effect = [ConnectionError("boom"), self._resp(200)]
        resp = get_with_retry(session, "http://example.test", max_retries=3)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(session.get.call_count, 2)
        mock_sleep.assert_called_once()

    @patch("utils.http.time.sleep")
    def test_retries_429_then_succeeds(self, mock_sleep):
        session = MagicMock()
        session.get.side_effect = [self._resp(429), self._resp(200)]
        resp = get_with_retry(session, "http://example.test", max_retries=3)
        self.assertEqual(resp.status_code, 200)

    @patch("utils.http.time.sleep")
    def test_raises_fetch_error_after_exhausting_retries(self, mock_sleep):
        session = MagicMock()
        session.get.side_effect = ConnectionError("still down")
        with self.assertRaises(FetchError):
            get_with_retry(session, "http://example.test", max_retries=2)
        self.assertEqual(session.get.call_count, 3)  # initial try + 2 retries

    def test_non_retryable_4xx_fails_immediately_no_sleep(self):
        import requests
        session = MagicMock()
        session.get.return_value = self._resp(404)
        with self.assertRaises(requests.HTTPError):
            get_with_retry(session, "http://example.test", max_retries=3)
        self.assertEqual(session.get.call_count, 1)  # never retried


class TestCleanAsn(unittest.TestCase):
    """
    Regression tests for a bug confirmed against the live RIPEstat API: at least some
    lod=1 entries come back wrapped as e.g. "AsnSingle(215040)" instead of a plain
    "215040", which broke every PeeringDB lookup downstream (malformed asn= URL param
    -> 404 for every ASN, then a 429 storm as the classify stage blasted through them
    with no backoff since 404s aren't retried).
    """
    def test_strips_asnsingle_wrapper(self):
        self.assertEqual(_clean_asn("AsnSingle(215040)"), "215040")

    def test_strips_wrapper_with_leading_brace(self):
        # the exact first-item shape observed in production, from splitting a
        # set-like "{AsnSingle(x), AsnSingle(y), ...}" string on commas
        self.assertEqual(_clean_asn("{AsnSingle(215040)"), "215040")

    def test_plain_value_passes_through_unchanged(self):
        self.assertEqual(_clean_asn("215040"), "215040")
        self.assertEqual(_clean_asn(215040), "215040")


class TestExtractAsns(unittest.TestCase):
    def test_comma_separated_string(self):
        self.assertEqual(_extract_asns("1103,1104,2914"), ["1103", "1104", "2914"])

    def test_list_of_ints(self):
        self.assertEqual(_extract_asns([1103, 1104]), ["1103", "1104"])

    def test_list_of_dicts(self):
        self.assertEqual(_extract_asns([{"asn": 1103}, {"asn": 1104}]), ["1103", "1104"])

    def test_empty_or_none(self):
        self.assertEqual(_extract_asns(None), [])
        self.assertEqual(_extract_asns(""), [])
        self.assertEqual(_extract_asns([]), [])

    def test_list_of_wrapped_values_matches_production_bug(self):
        # exact shape reconstructed from the real Actions log for KZ
        self.assertEqual(
            _extract_asns(["{AsnSingle(215040)", "AsnSingle(60930)", "AsnSingle(210435)"]),
            ["215040", "60930", "210435"],
        )

    def test_comma_string_of_wrapped_values(self):
        self.assertEqual(
            _extract_asns("AsnSingle(1103),AsnSingle(1104)"),
            ["1103", "1104"],
        )


class TestJSONCache(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_set_then_get_roundtrip(self):
        cache = JSONCache(self.tmpdir, ttl_days=30)
        cache.set("mykey", {"a": 1})
        self.assertEqual(cache.get("mykey"), {"a": 1})

    def test_missing_key_returns_none(self):
        cache = JSONCache(self.tmpdir, ttl_days=30)
        self.assertIsNone(cache.get("nope"))

    def test_expired_entry_returns_none(self):
        cache = JSONCache(self.tmpdir, ttl_days=30)
        cache.set("mykey", {"a": 1})
        path = cache._path("mykey")
        entry = json.loads(path.read_text())
        entry["_ts"] = time.time() - (31 * 86400)  # backdate past the 30-day TTL
        path.write_text(json.dumps(entry))
        self.assertIsNone(cache.get("mykey"))


class TestCheckpoint(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.path = str(Path(self.tmpdir) / "ckpt.json")

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_mark_and_check(self):
        ckpt = Checkpoint(self.path)
        self.assertFalse(ckpt.is_done("x"))
        ckpt.mark_done("x")
        self.assertTrue(ckpt.is_done("x"))

    def test_persists_across_instances(self):
        Checkpoint(self.path).mark_done("x")
        ckpt2 = Checkpoint(self.path)
        self.assertTrue(ckpt2.is_done("x"))


class TestClassifyOne(unittest.TestCase):
    def setUp(self):
        self.cfg = {
            "classification": {
                "accept_info_types": ["NSP", "Route Server"],
                "reject_info_types": ["Cable/DSL/ISP", "Enterprise", "Content"],
                "name_keywords_accept": ["backbone", "transit", "carrier"],
            }
        }

    def _mock_session(self, data=None):
        session = MagicMock()
        resp = MagicMock()
        resp.status_code = 200
        resp.raise_for_status = MagicMock()
        resp.json.return_value = {"data": data or []}
        session.get.return_value = resp
        return session

    def test_accept_by_info_type(self):
        session = self._mock_session([{"name": "Example Net", "info_type": "NSP"}])
        result = classify_one(session, "1234", self.cfg, 0)
        self.assertTrue(result["accepted"])
        self.assertEqual(result["reason"], "peeringdb_type")

    def test_accept_by_keyword(self):
        session = self._mock_session([{"name": "Example Transit Ltd", "info_type": "Government"}])
        result = classify_one(session, "1234", self.cfg, 0)
        self.assertTrue(result["accepted"])
        self.assertEqual(result["reason"], "name_keyword")

    def test_reject_by_info_type(self):
        session = self._mock_session([{"name": "Home Broadband Co", "info_type": "Cable/DSL/ISP"}])
        result = classify_one(session, "1234", self.cfg, 0)
        self.assertFalse(result["accepted"])
        self.assertEqual(result["reason"], "peeringdb_reject_type")

    def test_not_in_peeringdb(self):
        session = self._mock_session([])
        result = classify_one(session, "1234", self.cfg, 0)
        self.assertFalse(result["accepted"])
        self.assertEqual(result["reason"], "not_in_peeringdb")

    def test_default_reject(self):
        session = self._mock_session([{"name": "Some University", "info_type": "Educational/Research"}])
        result = classify_one(session, "1234", self.cfg, 0)
        self.assertFalse(result["accepted"])
        self.assertEqual(result["reason"], "default_reject")

    def test_keyword_match_requires_whole_word_not_substring(self):
        # Regression test: real PeeringDB data had orgs explicitly typed
        # Cable/DSL/ISP (i.e. not core, per PeeringDB itself) get incorrectly accepted
        # because "ix" or "core" happened to appear inside a longer word.
        for name in ["MATRONIX", "NCORE Sp. z o.o.", "WAWER - CePIX.pl",
                     "Orange Polska - Internet Optimum and TPIX Route Servers"]:
            session = self._mock_session([{"name": name, "info_type": "Cable/DSL/ISP"}])
            result = classify_one(session, "1234", self.cfg, 0)
            self.assertFalse(result["accepted"], f"{name!r} should not be accepted")
            self.assertEqual(result["reason"], "peeringdb_reject_type")

    def test_keyword_match_still_works_when_hyphenated(self):
        # A hyphen is still a word boundary — genuine hyphenated matches should still work.
        session = self._mock_session([{"name": "ABC-Carrier Networks", "info_type": "Cable/DSL/ISP"}])
        result = classify_one(session, "1234", self.cfg, 0)
        self.assertTrue(result["accepted"])
        self.assertEqual(result["reason"], "name_keyword")

    def test_404_from_single_lookup_is_not_in_peeringdb_not_an_error(self):
        # A single-ASN PeeringDB query 404s (not 200-with-empty-data) when nothing
        # matches — this must be treated as a valid "not found" result, not a
        # transient error to endlessly retry.
        import requests
        session = MagicMock()
        resp_404 = MagicMock(status_code=404)
        resp_404.raise_for_status.side_effect = requests.HTTPError(response=resp_404)
        session.get.return_value = resp_404
        result = classify_one(session, "1234", self.cfg, 0)
        self.assertEqual(result["reason"], "not_in_peeringdb")
        self.assertFalse(result["reason"].startswith("error:"))

    @patch("utils.http.time.sleep")
    def test_network_error_is_reported_not_rejected(self, mock_sleep):
        session = MagicMock()
        session.get.side_effect = ConnectionError("boom")
        result = classify_one(session, "1234", self.cfg, 0)
        self.assertFalse(result["accepted"])
        self.assertTrue(result["reason"].startswith("error:"))

    def test_score_type_and_keyword_both_match(self):
        session = self._mock_session([{"name": "Example Transit Ltd", "info_type": "NSP", "org_id": 555}])
        result = classify_one(session, "1234", self.cfg, 0)
        self.assertEqual(result["score"], 70)
        self.assertEqual(result["reason"], "peeringdb_type+name_keyword")
        self.assertEqual(result["org_id"], 555)

    def test_score_type_only(self):
        session = self._mock_session([{"name": "Example Net", "info_type": "NSP"}])
        result = classify_one(session, "1234", self.cfg, 0)
        self.assertEqual(result["score"], 60)

    def test_score_keyword_only(self):
        session = self._mock_session([{"name": "Example Transit Ltd", "info_type": "Government"}])
        result = classify_one(session, "1234", self.cfg, 0)
        self.assertEqual(result["score"], 45)

    def test_score_reject_type(self):
        session = self._mock_session([{"name": "Home Broadband Co", "info_type": "Cable/DSL/ISP"}])
        result = classify_one(session, "1234", self.cfg, 0)
        self.assertEqual(result["score"], 5)

    def test_score_not_in_peeringdb(self):
        session = self._mock_session([])
        result = classify_one(session, "1234", self.cfg, 0)
        self.assertEqual(result["score"], 10)

    @patch("utils.http.time.sleep")
    def test_score_is_none_on_error(self, mock_sleep):
        # None, not 0 — a failed lookup is a missing measurement, not evidence of low
        # confidence, so it must never be silently treated as a low score.
        session = MagicMock()
        session.get.side_effect = ConnectionError("boom")
        result = classify_one(session, "1234", self.cfg, 0)
        self.assertIsNone(result["score"])

    @patch("utils.http.time.sleep")
    @patch("classify_asn.polite_sleep")
    def test_paces_even_when_lookup_fails(self, mock_polite_sleep, mock_backoff_sleep):
        # Regression test: the per-item delay used to be skipped whenever a lookup
        # raised, which let a run of failing requests blast the API with zero pacing
        # — this is what turned one bad ASN value into a 429 storm in production.
        session = MagicMock()
        session.get.side_effect = ConnectionError("boom")
        classify_one(session, "1234", self.cfg, 2.5)
        mock_polite_sleep.assert_called_once_with(2.5)


class TestClassifyBatch(unittest.TestCase):
    def setUp(self):
        self.cfg = {
            "classification": {
                "accept_info_types": ["NSP", "Route Server"],
                "reject_info_types": ["Cable/DSL/ISP", "Enterprise", "Content"],
                "name_keywords_accept": ["backbone", "transit", "carrier"],
            }
        }

    def test_splits_batch_response_by_asn(self):
        session = MagicMock()
        resp = MagicMock(status_code=200)
        resp.raise_for_status = MagicMock()
        resp.json.return_value = {"data": [
            {"asn": 100, "name": "Big Carrier", "info_type": "NSP", "org_id": 1},
            {"asn": 200, "name": "Home Broadband Co", "info_type": "Cable/DSL/ISP", "org_id": 2},
        ]}
        session.get.return_value = resp

        results = classify_batch(session, ["100", "200", "300"], self.cfg, 0)

        self.assertEqual(len(results), 3)  # every requested ASN gets a result
        self.assertTrue(results["100"]["accepted"])
        self.assertFalse(results["200"]["accepted"])
        self.assertEqual(results["300"]["reason"], "not_in_peeringdb")  # not in the response at all
        self.assertEqual(session.get.call_count, 1)  # one request for the whole batch

    def test_sends_asn_list_as_asn_in_param(self):
        session = MagicMock()
        resp = MagicMock(status_code=200)
        resp.raise_for_status = MagicMock()
        resp.json.return_value = {"data": []}
        session.get.return_value = resp

        classify_batch(session, ["100", "200", "300"], self.cfg, 0)

        call_kwargs = session.get.call_args
        self.assertEqual(call_kwargs.kwargs["params"]["asn__in"], "100,200,300")

    @patch("utils.http.time.sleep")
    def test_whole_batch_failure_marks_every_asn_as_error(self, mock_sleep):
        session = MagicMock()
        session.get.side_effect = ConnectionError("boom")

        results = classify_batch(session, ["100", "200"], self.cfg, 0)

        self.assertEqual(len(results), 2)
        for asn in ["100", "200"]:
            self.assertTrue(results[asn]["reason"].startswith("error:"))


class TestChunks(unittest.TestCase):
    def test_splits_into_even_groups(self):
        self.assertEqual(list(_chunks([1, 2, 3, 4, 5, 6], 2)), [[1, 2], [3, 4], [5, 6]])

    def test_last_group_can_be_smaller(self):
        self.assertEqual(list(_chunks([1, 2, 3, 4, 5], 2)), [[1, 2], [3, 4], [5]])

    def test_empty_input(self):
        self.assertEqual(list(_chunks([], 5)), [])


class TestBuildRows(unittest.TestCase):
    def test_filters_out_low_prefix_count(self):
        store = {
            "classified_XX": {
                "100": {"accepted": True, "name": "Big Carrier", "info_type": "NSP"},
                "200": {"accepted": True, "name": "Tiny Net", "info_type": "NSP"},
                "300": {"accepted": False, "name": "Not Core", "info_type": "Enterprise"},
            },
            "prefixes_100": ["1.0.0.0/16", "2.0.0.0/16"],
            "prefixes_200": ["3.0.0.0/24"],
        }

        class FakeCache:
            def get(self, key):
                return store.get(key)

        rows, by_region, skipped, skipped_invalid = build_rows([("test_region", "XX")], FakeCache(), min_prefixes=2)
        self.assertEqual(len(rows), 2)          # both prefixes for ASN 100 only
        self.assertEqual(skipped, 1)             # ASN 200 had 1 prefix, below the threshold
        self.assertEqual(skipped_invalid, 0)
        self.assertEqual({r["asn"] for r in rows}, {"100"})

    def test_discards_malformed_prefixes(self):
        store = {
            "classified_XX": {
                "100": {"accepted": True, "name": "Big Carrier", "info_type": "NSP"},
            },
            "prefixes_100": ["1.0.0.0/16", "not-a-prefix", "2.0.0.0/16", ""],
        }

        class FakeCache:
            def get(self, key):
                return store.get(key)

        rows, by_region, skipped, skipped_invalid = build_rows([("test_region", "XX")], FakeCache(), min_prefixes=1)
        self.assertEqual(len(rows), 2)           # only the 2 valid CIDRs survive
        self.assertEqual(skipped_invalid, 2)     # "not-a-prefix" and ""
        self.assertEqual({r["prefix"] for r in rows}, {"1.0.0.0/16", "2.0.0.0/16"})


class TestBuildFullReport(unittest.TestCase):
    def test_includes_accepted_and_excluded(self):
        store = {
            "classified_XX": {
                "100": {"accepted": True, "name": "Big Carrier", "info_type": "NSP",
                        "org_id": 42, "score": 60, "reason": "peeringdb_type"},
                "300": {"accepted": False, "name": "Not Core", "info_type": "Enterprise",
                        "org_id": 7, "score": 5, "reason": "peeringdb_reject_type"},
            },
            "prefixes_100": ["1.0.0.0/16", "2.0.0.0/16"],
        }

        class FakeCache:
            def get(self, key):
                return store.get(key)

        rows = build_full_report([("test_region", "XX")], FakeCache())
        self.assertEqual(len(rows), 2)  # one row per evaluated ASN, accepted and excluded alike
        by_asn = {r["asn"]: r for r in rows}
        self.assertTrue(by_asn["100"]["accepted"])
        self.assertEqual(by_asn["100"]["prefix_count"], 2)
        self.assertFalse(by_asn["300"]["accepted"])
        self.assertEqual(by_asn["300"]["prefix_count"], 0)  # never fetched — not accepted


class TestRunStep(unittest.TestCase):
    @patch("run_pipeline.subprocess.run")
    def test_returns_zero_on_success(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0)
        code = run_step("src/fetch_asns.py", "all", True)
        self.assertEqual(code, 0)
        called_cmd = mock_run.call_args[0][0]
        self.assertIn("--resume", called_cmd)
        self.assertIn("all", called_cmd)

    @patch("run_pipeline.subprocess.run")
    def test_propagates_nonzero_exit_code(self, mock_run):
        mock_run.return_value = MagicMock(returncode=1)
        code = run_step("src/classify_asn.py", "central_asia", False)
        self.assertEqual(code, 1)
        called_cmd = mock_run.call_args[0][0]
        self.assertNotIn("--resume", called_cmd)


if __name__ == "__main__":
    unittest.main()
