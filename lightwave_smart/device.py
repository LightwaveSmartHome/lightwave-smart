import logging
from .products import get_product
from .message import LW_WebsocketMessage
from .utils import get_highest_version

_LOGGER = logging.getLogger(__name__)
# _LOGGER.setLevel(logging.INFO)


class LWRFDevice:
    
    def __init__(self):
        self.link = None
        
        self.device_id = None
        
        self.featuresets = []
        self.featureIds = []
        self._firmware_event_callbacks = []
        
        self.product_code = None
        self.virtual_product_code = None
        self.firmware_version = '0.00.0'
        self.manufacturer_code = None
        self.serial = None

        self.latest_firmware_version = '0.00.0'
        self.latest_firmware_release_summary = None
        # self.latest_firmware_release_url = None
        
        self._latest_firmware_release_id = None
        
        self.firmware_update_in_progress = False
        
    @property
    def name(self):
        return " - ".join([featureset.name for featureset in self.featuresets])
        
    def is_gen2(self): 
        if self.manufacturer_code == 'LightwaveRF':
            if self.product_code[0] == 'L' and self.product_code[:2] != 'LW':
                return True
            
        return False
    
    def add_featureset(self, new_featureset):
        self.featuresets.append(new_featureset)

    async def update_firmware(self, version):
        if version != self.latest_firmware_version:
            _LOGGER.warning(f"update_firmware: Firmware version given '{version}' is not the latest version '{self.latest_firmware_version}', latest release version will be used, release id: {self._latest_firmware_release_id}")
        
        writemess = LW_WebsocketMessage("firmware", "update")
        writemess.add_item({
            "deviceId": self.device_id, 
            "productCode": self.product_code, 
            "releaseId": self._latest_firmware_release_id
        })
        response = await self.link._ws.async_sendmessage(writemess)
        if response[0]["success"] == True:
            self.update_firmware_in_progress(True)
        else:
            _LOGGER.warning(f"update_firmware: Firmware update failed - error: {response[0]['error']}")
            
        return self.firmware_update_in_progress
    
    def register_firmware_event_callback(self, callback):
        self._firmware_event_callbacks.append(callback)
        
    def _call_firmware_event_callbacks(self):
        for callback in self._firmware_event_callbacks:
            try:    
                callback(device_id = self.device_id)
            except Exception as exp:
                _LOGGER.error(f"_call_firmware_event_callbacks - callback: {callback.__name__} - error: {str(exp)}")

    def update_firmware_in_progress(self, state):
        self.firmware_update_in_progress = state
        self._call_firmware_event_callbacks()
            
    def update_latest_firmware_info(self, version=None, release_id=None, release_summary=None):
        if version is not None:
            self.latest_firmware_version = version
        if release_id is not None:
            self._latest_firmware_release_id = release_id
        if release_summary is not None:
            self.latest_firmware_release_summary = release_summary
        
        if version is not None or release_id is not None or release_summary is not None:
            self._call_firmware_event_callbacks()

    async def async_update_latest_firmware_info(self, releases):
        # releases.versionPaths, get last array item, this is the latest FM version
        # for now use the release for the highest FM version found
        
        release_id = None
        new_version = None
        for release in releases:
            to_version = release["versionPaths"][-1]
            compare_versions = [to_version, self.latest_firmware_version]
            
            highest_version = get_highest_version(compare_versions)
            
            if highest_version == to_version:
                new_version = to_version
                release_id = release["_id"]
                
        firmware_description = None
        if self.latest_firmware_version != '0.00.0':
            firmware_notes = LW_WebsocketMessage("firmware", "readForProductCode")
            firmware_notes.add_item({"productCode": self.product_code, "specificFirmwareVersion": self.latest_firmware_version})
            
            notes_responses = await self.link._ws.async_sendmessage(firmware_notes)
            
            firmware_versions = notes_responses[0]["payload"]["firmwareVersions"]
            for firmware_version in firmware_versions:
                firmware_description = firmware_version["description"]
            
        self.update_latest_firmware_info(new_version, release_id, firmware_description)

    

class LWRFFeatureSet:

    def __init__(self):
        self.link = None

        self.featureset_id = None
        self.primary_feature_type = None
        self.name = None
        
        self.device = None
        
        self.features = {}
        
        self._event_callbacks = []
        
    @property
    def product_code(self):
        return self.device.product_code
    
    @property
    def virtual_product_code(self):
        return self.device.virtual_product_code
    
    @property
    def firmware_version(self):
        return self.device.firmware_version
    
    @property
    def manufacturer_code(self):
        return self.device.manufacturer_code
    
    @property
    def serial(self):
        return self.device.serial
    
    @property
    def latest_firmware_version(self):
        return self.device.latest_firmware_version
    
    @property
    def latest_firmware_release_summary(self):
        return self.device.latest_firmware_release_summary
    

    def has_feature(self, feature): return feature in self.features.keys()

    def is_switch(self): return (self.has_feature('switch')) and not (self.has_feature('dimLevel')) and not (self.has_feature('socketSetup'))
    def is_outlet(self): return self.has_feature('socketSetup')
    def is_light(self): return self.has_feature('dimLevel')
    def is_climate(self): return self.has_feature('targetTemperature')
    def is_trv(self): return self.has_feature('valveSetup')
    def is_cover(self): return self.has_feature('threeWayRelay')
    def is_energy(self): return (self.has_feature('energy')) and (self.has_feature('rssi'))
    def is_windowsensor(self): return self.has_feature('windowPosition')
    def is_motionsensor(self): return self.has_feature('movement')
    def is_hub(self): return self.has_feature('buttonPress')
    def is_remote(self): return (self.has_feature('uiButton') or self.has_feature('uiButtonPair')) and self.has_feature('batteryLevel')

    def is_gen2(self): return (self.has_feature('upgrade') or self.has_feature('uiButton') or self.is_hub())
    def reports_power(self): return self.has_feature('power')
    def has_led(self): return self.has_feature('rgbColor') and not self.virtual_product_code
    def has_uiIndicator(self): return self.has_feature('uiIndicator')
    
    def is_uiButtonPair_producer(self): return (self.has_feature('uiButtonPair') and (self.is_remote() or self.is_light()))
    def is_uiButton_producer(self): return (self.has_feature('uiButton') and not self.is_uiButtonPair_producer())

    def get_feature_by_type(self, type):
        feature = None
        if type in self.features:
            feature = self.features[type]
        return feature

    def register_event_callback(self, callback):
        self._event_callbacks.append(callback)
        
    def call_event_callbacks(self, feature, prev_value, new_value):
        for callback in self._event_callbacks:
            callback(feature=feature.name, feature_id=feature.id, prev_value=prev_value, new_value=new_value)
            

class LWRFFeature:

    def __init__(self, id, lw_feature, link):
        self.id = id
        self.lw_feature = lw_feature
        self.name = lw_feature["attributes"]["type"]
        self.link = link
        
        self.feature_sets = []
        self._state = None
        self.can_read = True


    def add_feature_set(self, feature_set):
        self.feature_sets.append(feature_set)

    def get_feature_set_names(self):
        names = ""
        for fs in self.feature_sets:
            if len(names) > 0:
                names += ", "
            names += fs.name
        return names
    
    def get_feature_set_product_code(self):
        fs = self.feature_sets[0]
        return fs.product_code
        

    @property
    def state(self):
        return self._state

    async def set_state(self, value):
        await self.link.async_write_feature(self.id, value)

    def update_feature_state(self, state):
        prev_value = self._state
        self._state = state
        
        # Add hook for subclass processing
        self._process_state_update(state)
        
        # Call callbacks after subclass processing is complete
        for feature_set in self.feature_sets:
            feature_set.call_event_callbacks(self, prev_value, state)
            
    def _process_state_update(self, state):
        """Hook method for subclasses to process state updates before callbacks are called."""
        pass

    def process_feature_read(self, response):
        if response["success"] == True:
            state = response["payload"]["value"]
            self.update_feature_state(state)
        else:
            _LOGGER.warning(f"process_feature_read: failed to read feature: {self.id}  returned {response}")
        
    async def async_read_feature_state(self):
        responses = await self.link.async_read_feature(self.id)
        self.process_feature_read(responses[0])


class UiIOMapEncoderDecoder:
    def _encode(self, io_mapping):
        inputs = list(io_mapping["inputs"])
        input_str = "".join(map(str, inputs)).rjust(8, "0")
        outputs = list(io_mapping["outputs"])
        output_str = "".join(map(str, outputs)).rjust(8, "0")
        io_mapping_bin = f"{output_str}{input_str}".rjust(32, "0")
        return int(io_mapping_bin, 2)

    def _decode(self, value):
        bin_str = bin(value)[2:].rjust(32, "0")
        inputs = list(map(int, bin_str[24:32]))
        outputs = list(map(int, bin_str[16:24]))
        return {"inputs": inputs, "outputs": outputs}  

    def decode_io_mapping_value(self, value, channel_count, channel_zero_position):
        #  left order of channels as given by uiIOMap feature
        decoded = self._decode(value)
        reversed_channels = channel_zero_position != 'left'

        inputs = decoded['inputs'][-channel_count:]
        outputs = decoded['outputs'][-channel_count:]
        if reversed_channels:
            inputs.reverse()
            outputs.reverse()
        return { 'inputs': inputs, 'outputs': outputs }
    
    # def get_ui_io_data(self, array, channel_count, reversed_channels):
    def get_ui_io_data(self, array, channel_count):
        return [
            {
                'index': index,
                'channelNumber': index + 1 if False else channel_count - index,
                # 'channelNumber': index + 1 if reversed_channels else self.channel_count - index,
                'selected': bool(value)
            }
            for index, value in enumerate(array)
        ]    

class LWRFUiIOMapFeature(LWRFFeature, UiIOMapEncoderDecoder):

    def __init__(self, id, name, link, channel_count):
        super().__init__(id, name, link)
        self._channel = self.lw_feature["attributes"]["channel"]
        self._channel_count = channel_count

        self.channel_zero_position = "right"
        self.channel_input_mapped = True

    def add_feature_set(self, feature_set):
        super().add_feature_set(feature_set)

        product_code = self.get_feature_set_product_code()
        product_details = get_product(product_code)
        if product_details:
            if "channel_zero_position" in product_details:
                self.channel_zero_position = product_details["channel_zero_position"]

    def _process_state_update(self, state):
        if state:
            mapping = self.decode_io_mapping_value(state, self._channel_count, self.channel_zero_position)
            self.channel_input_mapped = bool(mapping["inputs"][self._channel])
            # _in = self.get_ui_io_data(mapping["inputs"], self._channel_count)
            # _out = self.get_ui_io_data(mapping["outputs"], self._channel_count)

class UiButtonEncoderDecoder:
    def decode_ui_button_value(self, value, type):
        decoded_obj = {}

        if type == 'uiButtonPair':
            decoded_obj['upDown'] = 'Up' if (value & 0xf000) >> 12 == 0 else 'Down'

        press = (value & 0x0f00) >> 8
        if press == 1:
            decoded_obj['eventType'] = 'Short'
        elif press == 2:
            decoded_obj['eventType'] = 'Long'
        elif press == 3:
            decoded_obj['eventType'] = 'Long-Release'

        decoded_obj['presses'] = value & 0x00ff

        return decoded_obj

class LWRFUiButtonFeature(LWRFFeature, UiButtonEncoderDecoder):

    def __init__(self, id, name, link):
        super().__init__(id, name, link)
        self.can_read = False
        self.decoded_obj = None

    def _process_state_update(self, state):
        if state:
            self.decoded_obj = self.decode_ui_button_value(state, self.name)
