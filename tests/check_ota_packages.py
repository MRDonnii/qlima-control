"""Guard the two firmware channels before publishing a factory OTA image."""

from pathlib import Path
import re


root = Path(__file__).resolve().parents[1] / "esphome"
adopted = (root / "qlima-control-esp32c3.yaml").read_text()
shared = (root / "qlima-control.yaml").read_text()
factory = (root / "qlima-control-factory.yaml").read_text()

# The adopted import and everything it includes must never install the image
# whose Wi-Fi credentials are stored in flash rather than compiled into YAML.
assert not re.search(r"^\s*\w+:\s*!include\s+qlima-control-factory\.yaml", adopted + shared, re.M)
assert "platform: http_request" not in adopted + shared
assert "update.perform" not in adopted + shared
assert "qlima-control-esp32c3.yaml" in factory
assert re.search(r"lambda:\s*return\s*\{\s*\"factory\"\s*\};", factory)
assert "platform: http_request" in factory
assert "update.perform" in factory
print("OTA package separation OK")
