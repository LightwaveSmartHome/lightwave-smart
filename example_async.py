import asyncio
from lightwave_smart import lightwave_smart
from logging import basicConfig, INFO
from logging import getLogger

basicConfig(level=INFO) # Set logging level to INFO
_LOGGER = getLogger(__name__)

def feature_changed(**kwargs):
    _LOGGER.info(f"Feature changed: {kwargs}")

async def main():
    link = lightwave_smart.LWLink2()
    link.auth.set_auth_method(auth_method="password", username="your_email@example.com", password="your_password")
    await link.async_activate()
    await link.async_get_hierarchy()
    
    # Register callback for state changes
    # only register the last featureset for this example
    last_featureset = None
    for featureset in link.featuresets.values():
        last_featureset = featureset
    if last_featureset:
        _LOGGER.info(f"Registering callback for Feature Set: {last_featureset.name} (ID: {last_featureset.featureset_id}, Device ID: {last_featureset.device.device_id}, Product Code: {last_featureset.device.product_code})")
        await link.async_register_feature_callback(last_featureset.featureset_id, feature_changed)
    else:
        _LOGGER.warning("No featuresets found")

    # Keep running to receive updates
    while True:
        await asyncio.sleep(1)

asyncio.run(main())