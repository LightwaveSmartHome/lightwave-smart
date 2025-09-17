from lightwave_smart import lightwave_smart
import asyncio
import logging
logging.basicConfig(level=logging.DEBUG)

USER = None
PASSWORD = None
user = USER if USER else input("Enter username: ")
password = PASSWORD if PASSWORD else input("Enter password: ")

EXAMPLE_FEATURESET = 'your-feature-set-id'

async def main():
    link = lightwave_smart.LWLink2()
    link.auth.set_auth_method(auth_method="password", username=user, password=password)
    await link.async_activate()
    await link.async_get_hierarchy()
    
    # print("link.featuresets:", link.featuresets)
    for i in link.featuresets.values():
        print("Feature Set:", i.name, i.featureset_id, i.features.keys())
        
    for i in link.featuresets[EXAMPLE_FEATURESET].features.values():
        print("Feature:", i.name, i.id, i.state)
        
    print("Featureset name:",link.featuresets[EXAMPLE_FEATURESET].name)
    print("is_switch:", link.featuresets[EXAMPLE_FEATURESET].is_switch())
    print("is_light:",link.featuresets[EXAMPLE_FEATURESET].is_light())
    print("is_climate:", link.featuresets[EXAMPLE_FEATURESET].is_climate())
    print("is_energy:", link.featuresets[EXAMPLE_FEATURESET].is_energy())
    print("is_gen2:", link.featuresets[EXAMPLE_FEATURESET].is_gen2())
    print("Features:", link.featuresets[EXAMPLE_FEATURESET].features)

    print("List of switches", link.get_switches())
    print("List of lights:", link.get_lights())
    print("List of climate devices:", link.get_climates())
    print("List of energy sensors:", link.get_energy())
    
    await link.async_deactivate()
    
asyncio.run(main())