import unittest

from app.code_detail.vnext_service import VNextCodeDetailService


class IdentityCacheIndexBudgetTest(unittest.TestCase):
    def _service(self, **kwargs):
        defaults = {
            "max_identity_entries": 2,
            "max_identity_bytes": 1024 * 1024,
            "max_cache_bytes": 2 * 1024 * 1024,
        }
        defaults.update(kwargs)
        return VNextCodeDetailService(None, None, None, **defaults)

    def test_identity_index_is_evicted_with_lru_entry(self):
        service = self._service()
        entries = []
        for number in range(3):
            base_key = ("scan", number)
            full_key = (base_key, 1, "asset-{}".format(number))
            with service._cache_lock:
                stored = service._store_service_entry_locked(
                    service._identity_cache, full_key,
                    {"number": number}, "identity",
                    service.max_identity_entries,
                    service.max_identity_bytes,
                )
                if stored:
                    service._identity_index[base_key] = full_key
            entries.append((base_key, full_key))

        self.assertEqual(len(service._identity_cache), 2)
        self.assertEqual(
            set(service._identity_index.values()),
            set(service._identity_cache.keys()),
        )
        self.assertNotIn(entries[0][0], service._identity_index)
        metrics = service.metrics()
        self.assertEqual(metrics["identity_index_entries"], 2)
        self.assertEqual(metrics["identity_index_orphan_entries"], 0)

    def test_identity_entry_removal_cleans_exact_index_mapping(self):
        service = self._service()
        base_key = ("scan", 1)
        full_key = (base_key, 1, "asset")
        with service._cache_lock:
            self.assertTrue(service._store_service_entry_locked(
                service._identity_cache, full_key, {"value": 1}, "identity",
                service.max_identity_entries, service.max_identity_bytes,
            ))
            service._identity_index[base_key] = full_key
            service._remove_service_entry_locked(
                service._identity_cache, full_key, "identity"
            )
        self.assertNotIn(base_key, service._identity_index)
        self.assertEqual(service.metrics()["identity_index_entries"], 0)


if __name__ == "__main__":
    unittest.main()
