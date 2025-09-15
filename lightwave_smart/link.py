import asyncio
import logging
import traceback

from .websocket import LWWebsocket
from .message import LW_WebsocketMessage
from .device import LWRFDevice, LWRFFeatureSet, LWRFFeature, LWRFUiIOMapFeature, LWRFUiButtonFeature

_LOGGER = logging.getLogger(__name__)

RGB_FLOOR = int("0x0", 16) #Previously the Lightwave app floored RGB values, but it doesn't any more

class LWLink2:
    def __init__(self, username=None, password=None, auth_method="username", api_token=None, refresh_token=None, device_id=None):
        self._ws = LWWebsocket(username, password, auth_method, api_token, refresh_token, device_id)
        
        self.structures = {}    # structureId -> {linkPlus_featureset_id, name}
        
        self.devices = {}
        self.featuresets = {}
        self.features = {}
        self._group_ids = []

        self._callbacks = []
        
        self._background_tasks = set()

        self._ws.register_event_handler("feature", self._feature_event_handler)
        self._ws.register_event_handler("group", self.async_get_hierarchy)
        # self._ws.register_event_handler("device", self.async_get_hierarchy)
        
        self._ws.register_event_handler("server", self._firmware_event_handler_started, "sendingFirmwareStatus")
        self._ws.register_event_handler("server", self._firmware_event_handler_complete, "sendingFirmwareComplete")

    async def _feature_event_handler(self, message):
        _LOGGER.debug(f"_feature_event_handler: Event received - items: {len(message['items'])}")
        
        items = message["items"]
        for item in items:
            if "featureId" in item["payload"]:
                feature_id = item["payload"]["featureId"]
                value = item["payload"]["value"]

                # feature = self.get_feature_by_featureid(feature_id)
                feature = None
                if feature_id in self.features:
                    feature = self.features[feature_id]
                
                if feature is None:
                    _LOGGER.warning(f"_feature_event_handler: feature is None - feature_id: {feature_id}")
                else:
                    prev_value = feature.state
                    
                    feature.update_feature_state(value)

                    for gen_func in self._callbacks:
                        gen_func(feature=feature.name, feature_id=feature.id, prev_value = prev_value, new_value = value)

                
                return feature

    async def _firmware_event_handler_started(self, message):
        _LOGGER.debug("_firmware_event_handler_started: Event received - message: %s ", message)
        if "deviceId" in message["payload"]:    
            device_id = message["payload"]["deviceId"]
            if device_id in self.devices:
                device = self.devices[device_id]
                device.firmware_update_in_progress = True

    async def _firmware_event_handler_complete(self, message):
        _LOGGER.debug("_firmware_event_handler_complete: Event received - message: %s ", message)
        if "deviceId" in message["payload"]:    
            device_id = message["payload"]["deviceId"]
            if device_id in self.devices:
                device = self.devices[device_id]
                
                if (message["payload"]["success"] == True):
                    device.firmware_version = message["payload"]["firmwareVersion"]
                    
                device.update_firmware_in_progress(False)

    async def async_get_hierarchy(self):
        _LOGGER.debug("async_get_hierarchy: Reading hierarchy")
        
        readmess = LW_WebsocketMessage("user", "rootGroups")
        readmess.add_item()
        responses = await self._ws.async_sendmessage(readmess)

        self._group_ids = []
        for item in responses:
            if "success" in item and item["success"] != True:
                _LOGGER.warning(f"async_get_hierarchy: Error reading user.rootGroups - item: {item} - all responses: {responses}")
                continue
            
            if "groupIds" not in item["payload"]:
                _LOGGER.error(f"async_get_hierarchy: No groupIds in user.rootGroups response - item: {item} - all responses: {responses}")
                continue
                
            self._group_ids = self._group_ids + item["payload"]["groupIds"]

        _LOGGER.debug(f"async_get_hierarchy: Reading groups: {self._group_ids}")
        await self._async_read_groups()

        task = asyncio.create_task(self.async_update_featureset_states())
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)
        
        task = asyncio.create_task(self._async_read_firmware())
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)
        

    async def _async_read_groups(self):
        self.featuresets = {}
        for groupId in self._group_ids:
            read_hierarchy = LW_WebsocketMessage("group", "hierarchy")
            read_hierarchy.add_item({"groupId": groupId})
            
            hierarchy_responses = await self._ws.async_sendmessage(read_hierarchy)
            
            if "success" in hierarchy_responses[0] and hierarchy_responses[0]["success"] != True:
                _LOGGER.warning(f"_async_read_groups: Error reading group hierarchy - groupId: {groupId} - {hierarchy_responses}")
                
            self.set_structure_data_from_hierarchy(groupId, hierarchy_responses[0])
            
            read_group = LW_WebsocketMessage("group", "read")
            read_group.add_item({"groupId": groupId,
                                         "devices": True,
                                         "devicesDetail": True,
                                         "features": True,
                                        #  "automations": True,
                                         "subgroups": True,
                                         "subgroupDepth": 10,
                                        })
            
            group_read_responses = await self._ws.async_sendmessage(read_group)
            
            try:
                devices = list(group_read_responses[0]["payload"]["devices"].values())
                features = list(group_read_responses[0]["payload"]["features"].values())
                featuresets = list(hierarchy_responses[0]["payload"]["featureSet"])
                
                self.get_featuresets(featuresets, devices, features)
            except Exception as e:
                _LOGGER.warning(f"_async_read_groups: groupId: {groupId} - does not have devices/features/featuresets - {group_read_responses} - {str(e)}")

    async def _async_read_firmware(self):
        def process_firmware_item_cb(response):
            try:
                item_id = response["itemId"]
                device = id_map[item_id]
                
                if response["success"] != True:
                    _LOGGER.warning(f"Success not True reading firmware response for device: {device.device_id} - {response}")
                    return
            
                releases = response["payload"]["releases"]
                
                task = asyncio.create_task(device.async_update_latest_firmware_info(releases))
                self._background_tasks.add(task)
                task.add_done_callback(self._background_tasks.discard)
                
            except Exception as e:
                _LOGGER.error(f"_async_read_firmware - process_firmware_item_cb: Error processing message - id_map: {id_map}   response: {response} - {str(e)}")
            
        readmess = LW_WebsocketMessage("firmware", "readForDevice", process_firmware_item_cb)
        id_map = {}
        for device in list(self.devices.values()):
            item_id = readmess.add_item({"deviceId": device.device_id})
            id_map[item_id] = device
            
        firmware_responses = await self._ws.async_sendmessage(readmess)

    async def async_update_featureset_states(self):
        def process_read_item_cb(response):
            try:
                item_id = response["itemId"]
                feature = id_map[item_id]
                feature.process_feature_read(response)
                
            except Exception as e:
                _LOGGER.error(f"async_update_featureset_states - process_read_item_cb: Error processing message - id_map: {id_map}   response: {response} - {str(e)} - {traceback.format_exc()}")

        update_featureset_states = LW_WebsocketMessage("feature", "read", process_read_item_cb)
        id_map = {}
        for feature in self.features.values():
            if feature.can_read:
                item_id = update_featureset_states.add_item({"featureId": feature.id})
                id_map[item_id] = feature
        
        feature_responses = await self._ws.async_sendmessage(update_featureset_states)
        

    async def async_write_feature(self, feature_id, value):
        write_message = LW_WebsocketMessage("feature", "write")
        write_message.add_item({"featureId": feature_id, "value": value})
        # handle write as a priority message
        await self._ws.async_sendmessage(write_message, False, True)

    async def async_write_feature_by_name(self, featureset_id, featurename, value):
        await self.featuresets[featureset_id].features[featurename].set_state(value)

    async def async_read_feature(self, feature_id):
        readmess = LW_WebsocketMessage("feature", "read")
        readmess.add_item({"featureId": feature_id})
        return await self._ws.async_sendmessage(readmess)

    def get_featuresets_by_featureid(self, feature_id):
        if feature_id in self.features:
            return self.features[feature_id].feature_sets
        return None

    def get_feature_by_featureid(self, feature_id):
        for x in self.featuresets.values():
            for y in x.features.values():
                if y.id == feature_id:
                    return y
        return None

    async def async_turn_on_by_featureset_id(self, featureset_id):
        await self.async_write_feature_by_name(featureset_id, "switch", 1)

    async def async_turn_off_by_featureset_id(self, featureset_id):
        await self.async_write_feature_by_name(featureset_id, "switch", 0)

    async def async_set_brightness_by_featureset_id(self, featureset_id, level):
        await self.async_write_feature_by_name(featureset_id, "dimLevel", level)

    async def async_set_temperature_by_featureset_id(self, featureset_id, level):
        await self.async_write_feature_by_name(featureset_id, "targetTemperature", int(level * 10))

    async def async_set_valvelevel_by_featureset_id(self, featureset_id, level):
        await self.async_write_feature_by_name(featureset_id, "valveLevel", int(level * 20))

    async def async_cover_open_by_featureset_id(self, featureset_id):
        await self.async_write_feature_by_name(featureset_id, "threeWayRelay", 1)

    async def async_cover_close_by_featureset_id(self, featureset_id):
        await self.async_write_feature_by_name(featureset_id, "threeWayRelay", 2)

    async def async_cover_stop_by_featureset_id(self, featureset_id):
        await self.async_write_feature_by_name(featureset_id, "threeWayRelay", 0)

    async def async_set_led_rgb_by_featureset_id(self, featureset_id, color, feature_type="rgbColor"):
        red = (color & int("0xFF0000", 16)) >> 16
        if red != 0:
            red = min(max(red, RGB_FLOOR), 255)
        green = (color & int("0xFF00", 16)) >> 8
        if green != 0:
            green = min(max(green, RGB_FLOOR), 255)
        blue = (color & int("0xFF", 16))
        if blue != 0:
            blue = min(max(blue , RGB_FLOOR), 255)
        newcolor = (red << 16) + (green << 8) + blue
        await self.async_write_feature_by_name(featureset_id, feature_type, newcolor)

    def get_device_ids(self):
        return [x.device_id for x in self.devices.values()]

    def get_switches(self):
        return [(x.featureset_id, x.name) for x in self.featuresets.values() if x.is_switch()]
    
    def get_sockets(self):
        return [(x.featureset_id, x.name) for x in self.featuresets.values() if x.is_outlet()]
    
    def get_lights(self):
        return [(x.featureset_id, x.name) for x in self.featuresets.values() if x.is_light()]

    def get_remotes(self):
        return [(x.featureset_id, x.name) for x in self.featuresets.values() if x.is_remote()]

    def get_uiButtonPair_producers(self):
        return [(x.featureset_id, x.name) for x in self.featuresets.values() if x.is_uiButtonPair_producer()]

    def get_uiButton_producers(self):
        return [(x.featureset_id, x.name) for x in self.featuresets.values() if x.is_uiButton_producer()]

    def get_climates(self):
        return [(x.featureset_id, x.name) for x in self.featuresets.values() if x.is_climate()]

    def get_covers(self):
        return [(x.featureset_id, x.name) for x in self.featuresets.values() if x.is_cover()]

    def get_energy(self):
        return [(x.featureset_id, x.name) for x in self.featuresets.values() if x.is_energy()]

    def get_windowsensors(self):
        return [(x.featureset_id, x.name) for x in self.featuresets.values() if x.is_windowsensor()]

    def get_motionsensors(self):
        return [(x.featureset_id, x.name) for x in self.featuresets.values() if x.is_motionsensor()]

    def get_with_feature(self, feature):
        return [(x.featureset_id, x.name) for x in self.featuresets.values() if x.has_feature(feature)]

    def get_buttons(self):
        return [(x.featureset_id, x.name) for x in self.featuresets.values() if x.is_light()]

    def get_hubs(self):
        return [(x.featureset_id, x.name) for x in self.featuresets.values() if x.is_hub()]

    #########################################################
    # WS Interface
    #########################################################
    
    # Warning using async_register_general_callback will result in lots of callbacks
    async def async_register_general_callback(self, callback):
        _LOGGER.debug(f"async_register_general_callback: Register callback: '{callback.__name__}'")
        self._callbacks.append(callback)

    async def async_register_feature_callback(self, featureset_id, callback):
        if featureset_id in self.featuresets:
            self.featuresets[featureset_id].register_event_callback(callback)
        else:
            _LOGGER.warning(f"async_register_feature_callback: Featureset not found: {featureset_id}")    
        
    async def async_register_firmware_event_callback(self, device_id, callback):
        if (device_id in self.devices):
            self.devices[device_id].register_firmware_event_callback(callback)
        else:
            _LOGGER.warning(f"async_register_firmware_event_callback: Device not found: {device_id}")

    async def async_connect(self, max_tries=None, force_keep_alive_secs=0, source="link-async_connect"):
        return await self._ws.async_connect(max_tries, force_keep_alive_secs, source)

    async def async_force_reconnect(self, secs):
        self._ws.async_force_reconnect(secs)


    #########################################################
    # Convenience methods for non-async calls
    #########################################################

    def _sendmessage(self, message):
        return asyncio.get_event_loop().run_until_complete(self._ws.async_sendmessage(message))

    def get_hierarchy(self):
        return asyncio.get_event_loop().run_until_complete(self.async_get_hierarchy())

    def update_featureset_states(self):
        return asyncio.get_event_loop().run_until_complete(self.async_update_featureset_states())

    def write_feature(self, feature_id, value):
        return asyncio.get_event_loop().run_until_complete(self.async_write_feature(feature_id, value))

    def write_feature_by_name(self, featureset_id, featurename, value):
        return asyncio.get_event_loop().run_until_complete(self.async_write_feature_by_name(self, featureset_id, featurename, value))

    def read_feature(self, feature_id):
        return asyncio.get_event_loop().run_until_complete(self.async_read_feature(feature_id))

    def turn_on_by_featureset_id(self, featureset_id):
        return asyncio.get_event_loop().run_until_complete(self.async_turn_on_by_featureset_id(featureset_id))

    def turn_off_by_featureset_id(self, featureset_id):
        return asyncio.get_event_loop().run_until_complete(self.async_turn_off_by_featureset_id(featureset_id))

    def set_brightness_by_featureset_id(self, featureset_id, level):
        return asyncio.get_event_loop().run_until_complete(self.async_set_brightness_by_featureset_id(featureset_id, level))

    def set_temperature_by_featureset_id(self, featureset_id, level):
        return asyncio.get_event_loop().run_until_complete(self.async_set_temperature_by_featureset_id(featureset_id, level))

    def cover_open_by_featureset_id(self, featureset_id):
        return asyncio.get_event_loop().run_until_complete(self.async_cover_open_by_featureset_id(featureset_id))

    def cover_close_by_featureset_id(self, featureset_id):
        return asyncio.get_event_loop().run_until_complete(self.async_cover_close_by_featureset_id(featureset_id))

    def cover_stop_by_featureset_id(self, featureset_id):
        return asyncio.get_event_loop().run_until_complete(self.async_cover_stop_by_featureset_id(featureset_id))

    def connect(self):
        return asyncio.get_event_loop().run_until_complete(self.async_connect(source="link-connect"))

    def get_from_lw_ar_by_id(self, ar, id, label):
        for x in ar:
            if x[label] == id:
                return x
        return None

    def get_featuresets(self, featuresets, devices, features):
        for y in featuresets:
            new_featureset = LWRFFeatureSet()
            new_featureset.link = self
            new_featureset.featureset_id = y["groupId"]
            
            device = None
            if y["deviceId"] in self.devices:
               device = self.devices[y["deviceId"]]
            
            if device is None:
                device = LWRFDevice()
                device.link = self
                device.device_id = y["deviceId"]

                _device = self.get_from_lw_ar_by_id(devices, y["deviceId"], "deviceId")
                
                device.featureIds = _device["featureIds"]

                device.product_code = _device["productCode"]
                if "virtualProductCode" in _device:
                    device.virtual_product_code = _device["virtualProductCode"]
                device.firmware_version = _device["firmwareVersion"]
                
                if "manufacturerCode" in _device:
                    device.manufacturer_code = _device["manufacturerCode"]
                if "serial" in _device:
                    device.serial = _device["serial"]
                
                self.devices[device.device_id] = device
                
                if device.is_hub():
                    self.set_structure_data(device.device_id, new_featureset.featureset_id)
                    
            new_featureset.device = device
            device.add_featureset(new_featureset)
                    
            new_featureset.name = y["name"]
            
            primaryFeatureId = None
            if "primaryFeatureId" in y:
                primaryFeatureId = y["primaryFeatureId"]
            else:
                # We can try to guess the primary feature
                for feature_id in y["features"]:
                    _lw_Feature = self.get_from_lw_ar_by_id(features, feature_id, 'featureId')
                    featureType = _lw_Feature["attributes"]["type"]
                    if featureType == "switch":
                        primaryFeatureId = feature_id
                        break

            for feature_id in y["features"]:
                if feature_id in self.features:
                    feature = self.features[feature_id]
                    featureType = feature.name
                else:
                    _lw_Feature = self.get_from_lw_ar_by_id(features, feature_id, 'featureId')
                    featureType = _lw_Feature["attributes"]["type"]

                    if featureType == "uiIOMap":
                        count = 0
                        # for featureId in device["featureIds"]:
                        for featureId in device.featureIds:
                            __feature = self.get_from_lw_ar_by_id(features, featureId, 'featureId')
                            __featureType = __feature["attributes"]["type"]
                            if __featureType == "uiIOMap":
                                count += 1

                        feature = LWRFUiIOMapFeature(feature_id, _lw_Feature, self, count)
                        
                    elif featureType == "uiButton" or featureType == "uiButtonPair":
                        feature = LWRFUiButtonFeature(feature_id, _lw_Feature, self)
                        
                    else:
                        feature = LWRFFeature(feature_id, _lw_Feature, self)

                    self.features[feature_id] = feature


                feature.add_feature_set(new_featureset)
                new_featureset.features[featureType] = feature
                
                if primaryFeatureId == feature_id:
                    new_featureset.primary_feature_type = featureType


            self.featuresets[y["groupId"]] = new_featureset

    def get_structure_id(self, thing_id):
        # thing_id = [structureId]-[group id] OR [structureId]-[deviceId]-[link id]+[reset id]
        return thing_id.split("-")[0]
    
    def get_linkPlus_featureset_id(self, thing_id):
        structure_id = self.get_structure_id(thing_id)
        if structure_id in self.structures:
            return self.structures[structure_id]["linkPlus_featureset_id"]
        return None
    
    def get_structure_name(self, thing_id):
        structure_id = self.get_structure_id(thing_id)
        if structure_id in self.structures:
            return self.structures[structure_id]["name"]
        return None
    
    def set_structure_data(self, thing_id, linkPlus_featureset_id = None, name = None):
        structure_id = self.get_structure_id(thing_id)
        if structure_id in self.structures:
            if linkPlus_featureset_id is not None:
                self.structures[structure_id]["linkPlus_featureset_id"] = linkPlus_featureset_id
            if name is not None:
                self.structures[structure_id]["name"] = name
        else:
            self.structures[structure_id] = {
                "linkPlus_featureset_id": linkPlus_featureset_id,
                "name": name
            }

    def set_structure_data_from_hierarchy(self, groupId, hierarchy_response):
        linkFeatureSetId, rootName = None, None
        
        try:
            payload = hierarchy_response["payload"]
            if (payload["link"] is not None and len(payload["link"]) > 0):
                link = payload["link"][0]
                
                if "featureSets" in link:
                    linkFeatureSets = link["featureSets"]
                    if linkFeatureSets is not None and len(linkFeatureSets) > 0:
                        linkFeatureSetId = linkFeatureSets[0]
                
                if "root" in payload:
                    rootName = payload["root"][0]["name"]
                
        except Exception as e:
            _LOGGER.error(f"set_structure_data_from_hierarchy: Error setting structure data - groupId: {groupId} - {hierarchy_response} - {str(e)} - {traceback.format_exc()}")
            
        self.set_structure_data(groupId, linkFeatureSetId, rootName)