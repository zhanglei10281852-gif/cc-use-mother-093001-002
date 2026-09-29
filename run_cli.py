import json, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent / "src"))
from drainage_dispatch.contracts import CommandAction, DrainageDevice, StormSnapshot

storm = StormSnapshot("ST-01", 72.5, ("BASIN-A",))
device = DrainageDevice("PUMP-3", "BASIN-A", 18.0)
print(json.dumps({"storm": storm.storm_id, "device": device.device_id, "action": CommandAction.START.value}, ensure_ascii=False))
