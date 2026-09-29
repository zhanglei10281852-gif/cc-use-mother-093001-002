import sys, unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parents[1] / "src"))
from drainage_dispatch.contracts import DrainageDevice, StormSnapshot


class DispatchContractTests(unittest.TestCase):
    def test_snapshot_retains_basins(self):
        item = StormSnapshot("S-1", 10, ("B-1", "B-2"))
        self.assertEqual(len(item.basin_ids), 2)

    def test_device_capacity_is_positive(self):
        with self.assertRaises(ValueError):
            DrainageDevice("D-1", "B-1", 0)


if __name__ == "__main__": unittest.main()
