# Lightwave Smart Python Library

[![PyPI version](https://badge.fury.io/py/lightwave_smart.svg)](https://badge.fury.io/py/lightwave_smart)
[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/downloads/)
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

Requires Python 3.11 or newer.

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

# Thermostats (degrees; 22.5 is sent as 225)
await link.async_set_temperature_by_featureset_id(featureset_id, 22.5)

# Covers/Blinds
await link.async_cover_open_by_featureset_id(featureset_id)
await link.async_cover_close_by_featureset_id(featureset_id)
await link.async_cover_stop_by_featureset_id(featureset_id)
```

### Feature Set Type Detection
```python
featureset = link.featuresets['featureset-id']
print(f"Is light: {featureset.is_light()}")
print(f"Is climate: {featureset.is_climate()}")
print(f"Is switch: {featureset.is_switch()}")
print(f"Is hub: {featureset.is_hub()}")
print(f"Is gen2: {featureset.is_gen2()}")
```

`is_light()`, `is_climate()`, `is_switch()`, `is_cover()`, `is_energy()`, and `is_trv()` are feature set methods. `is_hub()` and `is_gen2()` exist on the feature set and on the device. The feature set forwards those two calls to its device.

### Event Callbacks
```python
def feature_changed(**kwargs):
    # example output for a switch. feature_id is the feature id, not the feature set id:
    # {'feature': 'switch', 'feature_id': 'structure-thing-link+1', 'prev_value': 0, 'new_value': 1}
    print(f"Feature changed: {kwargs}")

# Register callback
await link.async_register_feature_callback(featureset_id, feature_changed)
```

## API Reference

### Core Methods
- `async_activate()` - Connect to Lightwave servers
- `async_deactivate()` - Disconnect and stop background work
- `async_get_hierarchy()` - Load devices and feature sets. The first feature-state, firmware, and device-status reads run in the background

`async_get_hierarchy` returns when the structure is loaded. Feature states known at that time are then updated from websocket events. The first state read may still be in progress.

### Device Control
- `async_turn_on_by_featureset_id(id)` - Turn the switch feature on
- `async_turn_off_by_featureset_id(id)` - Turn the switch feature off
- `async_set_brightness_by_featureset_id(id, level)` - Write `level` to the `dimLevel` feature
- `async_set_temperature_by_featureset_id(id, temp)` - Set temperature in degrees. The value is sent as tenths of a degree
- `async_cover_open_by_featureset_id(id)` - Open a cover
- `async_cover_close_by_featureset_id(id)` - Close a cover
- `async_cover_stop_by_featureset_id(id)` - Stop a cover

### Feature Set Information
- `is_light()`, `is_climate()`, `is_switch()`, `is_cover()`, `is_energy()`, `is_trv()` - Feature set type checks
- `is_gen2()`, `is_hub()` - On the feature set and on the device

## Examples

- `example_readme.py` - Async script that lists feature sets and their types
- `example_async.py` - Async script that stays running to receive callbacks

## Contributing

Contributions welcome! Fork, create a feature branch, commit changes, and submit a PR.

## License

MIT License - see [LICENSE](LICENSE) file.

## Thanks

Credit to Bryan Blunt for the original version: https://github.com/bigbadblunt/lightwave2
