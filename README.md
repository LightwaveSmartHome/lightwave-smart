# Lightwave Smart Python Library

[![PyPI version](https://badge.fury.io/py/lightwave_smart.svg)](https://badge.fury.io/py/lightwave_smart)
[![Python 3.7+](https://img.shields.io/badge/python-3.7+-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

A Python library for controlling Lightwave (https://lightwaverf.com) Smart Series (second generation) devices as well as Connect Series (first generation) devices connected to a Link Plus hub.
Control and monitor lights (dimmers), power outlets (sockets), smart switches (wirefrees), PIRs, thermostats, TRVs, magnetic switches, relays, energy monitors and other device types.

Supported devices include:
+ [Link Plus hub](https://shop.lightwaverf.com/collections/all/products/link-plus) (required)
+ [Light Switches / Dimmers](https://shop.lightwaverf.com/collections/smart-lighting)
+ [Power Outlets](https://shop.lightwaverf.com/collections/smart-sockets)
+ [Smart Switches](https://shop.lightwaverf.com/collections/scene-selectors)
+ [TRVs and Thermostats](https://shop.lightwaverf.com/collections/smart-heating)
+ [Relays and LED Drivers](https://shop.lightwaverf.com/collections/relays-and-led-drivers)
+ And more...

## Updates

**Important**: The `LWLink2Public` class has been removed as of version 1.0.0.

## Installation

```bash
pip install lightwave_smart
```

## Quick Start

```python
import asyncio
from lightwave_smart import lightwave_smart

async def main():
    # Create and authenticate
    link = lightwave_smart.LWLink2()
    link.auth.set_auth_method(auth_method="password", username="your_email@example.com", password="your_password")
    await link.async_activate()
    
    # Get devices and feature sets
    await link.async_get_hierarchy()
    
    # List feature sets
    for featureset in link.featuresets.values():
        device = featureset.device
        print(f"{featureset.name} (ID: {featureset.featureset_id}), Device Product Code: {device.product_code}")
    
    # Control a light
    featureset_id = "your-feature-set-id"
    await link.async_turn_on_by_featureset_id(featureset_id)
    await link.async_set_brightness_by_featureset_id(featureset_id, 75)
    
    await link.async_deactivate()

asyncio.run(main())
```

## Key Concepts

- **Devices**: Physical devices (light switches, thermostats, etc.)
- **Feature Sets**: Logical groupings within a device (e.g. a 2-circuit switch has 2 feature sets)

## Device Control

### Basic Control
```python
# Lights/Switches
await link.async_turn_on_by_featureset_id(featureset_id)
await link.async_turn_off_by_featureset_id(featureset_id)
await link.async_set_brightness_by_featureset_id(featureset_id, 50)

# Thermostats
await link.async_set_temperature_by_featureset_id(featureset_id, 22.5)

# Covers/Blinds
await link.async_cover_open_by_featureset_id(featureset_id)
await link.async_cover_close_by_featureset_id(featureset_id)
```

### Device Type Detection
```python
device = link.featuresets['featureset-id'].device
print(f"Is light: {device.is_light()}")
print(f"Is climate: {device.is_climate()}")
print(f"Is switch: {device.is_switch()}")
```

### Event Callbacks
```python
def feature_changed(**kwargs):
    # example output for a switch:
    # {'feature': 'switch', 'feature_id': 'your-feature-set-id', 'prev_value': 0, 'new_value': 1}
    print(f"Feature changed: {kwargs}")

# Register callback
await link.async_register_feature_callback(featureset_id, feature_changed)
```

## API Reference

### Core Methods
- `async_activate()` - Connect to Lightwave servers
- `async_get_hierarchy()` - Get all devices and feature sets, reads and updates all features states in the background

After calling `async_get_hierarchy` all feature known at that time will have their states updated as they change based on events received from the websocket.

### Device Control
- `async_turn_on_by_featureset_id(id)` - Turn device on
- `async_turn_off_by_featureset_id(id)` - Turn device off
- `async_set_brightness_by_featureset_id(id, level)` - Set brightness (0-100)
- `async_set_temperature_by_featureset_id(id, temp)` - Set temperature

### Device Information
- `is_light()`, `is_climate()`, `is_switch()`, `is_cover()`, `is_energy()` - Device type checks
- `is_gen2()`, `is_hub()`, `is_trv()` - Specific device checks

## Examples

- `example_readme.py` - Basic synchronous usage
- `example_async.py` - Advanced async usage with callbacks

## Contributing

Contributions welcome! Fork, create a feature branch, commit changes, and submit a PR.

## License

MIT License - see [LICENSE](LICENSE) file.

## Thanks

Credit to Bryan Blunt for the original version: https://github.com/bigbadblunt/lightwave2