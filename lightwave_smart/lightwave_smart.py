import asyncio
import uuid
import json
import datetime
import aiohttp

import logging

from .products import get_product


_LOGGER = logging.getLogger(__name__)

AUTH_SERVER = "https://auth.lightwaverf.com/v2/lightwaverf/autouserlogin/lwapps"
TRANS_SERVER = "wss://v1-linkplus-app.lightwaverf.com"
VERSION = "1.6.8"
MAX_RETRIES = 5
PUBLIC_AUTH_SERVER = "https://auth.lightwaverf.com/token"
PUBLIC_API = "https://publicapi.lightwaverf.com/v1/"
RGB_FLOOR = int("0x0", 16) #Previously the Lightwave app floored RGB values, but it doesn't any more

#TODO adapt async_connect calls to respond to connection failure

class _LWRFWebsocketMessage:
    _tran_id = 0
    _sender_id = str(uuid.uuid4())

    def __init__(self, opclass, operation, item_received_cb=None):
        _LWRFWebsocketMessage._tran_id += 1
        self.transcation_id = _LWRFWebsocketMessage._tran_id
        self.opclass = opclass
        self.operation = operation
        self.item_received_cb = item_received_cb
        
        self.pending_item_ids = []
        self.item_responses = []

        self._waitflag = None
        self._message = {
            "direction": "request",
            "class": opclass, 
            "operation": operation, 
            "version": 1, 
            "senderId": self._sender_id,
            "transactionId": _LWRFWebsocketMessage._tran_id
        }
        self._message["items"] = []

    def additem(self, newitem):
        self.pending_item_ids.append(newitem._item["itemId"])
        self._message["items"].append(newitem._item)
        return newitem._item["itemId"]

    def add_item_responses(self, message):
        items = message["items"]
        for item in items:
            self.item_responses.append(item)
            if self.item_received_cb:
                self.item_received_cb(item)
            idx = self.pending_item_ids.index(item["itemId"])
            if idx is not None:
                self.pending_item_ids.pop(idx)
            # else:
            #     # TODO - problem?

        return len(self.pending_item_ids)

    def set_item_received_cb(self, item_received_cb):
        self.item_received_cb = item_received_cb        

    def get_items(self):
        return self._message["items"]
    
    def get_item_responses(self):
        return self.item_responses
    
    def send_items(self):
        self._waitflag = asyncio.Event()
        self._waitflag.clear()
        return self._waitflag
    
    def set_waitflag(self):
        return self._waitflag.set()

    def json(self):
        return json.dumps(self._message)

class _LWRFWebsocketMessageItem:
    _item_id = 0

    def __init__(self, payload=None):
        _LWRFWebsocketMessageItem._item_id += 1
        if payload is None:
            payload = {}
        self._item = {
            "itemId": _LWRFWebsocketMessageItem._item_id,
            "payload": payload
        }


class LWRFFeatureSet:

    def __init__(self):
        self.link = None

        self.featureset_id = None
        self.name = None
        self.product_code = None
        self.virtual_product_code = None
        self.firmware_version = None
        self.manufacturer_code = None
        self.serial = None

        self.features = {}

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

class LWRFFeature:

    def __init__(self, id, lw_feature, link):
        self.id = id
        self.lw_feature = lw_feature
        self.name = lw_feature["attributes"]["type"]
        self.link = link
        
        self.feature_sets = []
        self._state = None


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
        self._state = state

    def process_feature_read(self, response):
        if response["success"] == True:
            state = response["payload"]["value"]
            self.update_feature_state(state)
        else:
            _LOGGER.warning("process_feature_read: failed to read feature ({}), returned {}".format(self.id, response))
        
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

    def update_feature_state(self, state):
        super().update_feature_state(state)
        if (self._state):
            mapping = self.decode_io_mapping_value(self._state, self._channel_count, self.channel_zero_position)
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
        self.decoded_obj = {}

    def update_feature_state(self, state):
        super().update_feature_state(state)
        if (self._state):
            self.decoded_obj = self.decode_ui_button_value(self._state, self.name)


class LWWebsocket:

    def __init__(self, username=None, password=None, auth_method="username", api_token=None, refresh_token=None, device_id=None):

        self._authtoken = None

        self._username = username
        self._password = password

        self._auth_method = auth_method
        self._api_token = api_token
        self._refresh_token = refresh_token

        self._session = aiohttp.ClientSession()
        self._token_expiry = None

        self._eventHandlers = {}

        # Websocket only variables:
        self._device_id = (device_id or "PLW2:") + str(uuid.uuid4())
        self._websocket = None

        # Next two variables are used to synchronise responses to requests
        self._pending_transactions = {}

        self.background_tasks = set()

        task = asyncio.create_task(self._consumer_handler())
        self.background_tasks.add(task)
        task.add_done_callback(self.background_tasks.discard)

    async def _async_sendmessage(self, message, _retry = 1, redact = False):

        if not self._websocket or self._websocket.closed:
            _LOGGER.info("async_sendmessage: Websocket closed, reconnecting")
            await self.async_connect()
            _LOGGER.info("async_sendmessage: Connection reopened")

        if redact:
            _LOGGER.debug("async_sendmessage: [contents hidden for security]")
        else:
            _LOGGER.debug("async_sendmessage: Sending: %s", message.json())

        transaction_id = message._message["transactionId"]
        self._pending_transactions[transaction_id] = message
        waitflag = message.send_items()

        await self._websocket.send_str(message.json())
        _LOGGER.debug("async_sendmessage: Message sent, waiting for acknowledgement from server: %s ", transaction_id)

        responses = None
        try:
            timeout = 60.0 if len(message.get_items()) > 12 else len(message.get_items()) * 5.0
            await asyncio.wait_for(waitflag.wait(), timeout=timeout)
            responses = message.get_item_responses()
            _LOGGER.debug("async_sendmessage: Response received for: %s ", transaction_id)
        except asyncio.TimeoutError:
            responses = message.get_item_responses()
            items = message.get_items()
            _LOGGER.warning("async_sendmessage: Timeout waiting for responses to: %s (items: %s , responses: %s)  %s - %s", transaction_id, len(items), len(responses), message._message["class"], message._message["operation"])
            if len(responses) == 0:
                # retry only possible if no responses received
                responses = None

        self._pending_transactions.pop(transaction_id)

        if responses:
            return responses
        elif _retry >= MAX_RETRIES:
            if redact:
                _LOGGER.warning("async_sendmessage: Exceeding MAX_RETRIES, abandoning send. Failed message %s", message.json())
            else:
                _LOGGER.warning("async_sendmessage: Exceeding MAX_RETRIES, abandoning send. Failed message not shown as contains sensitive info")
            
            _LOGGER.warning("async_sendmessage: Exceeding MAX_RETRIES, abandoning send. Failed tran %s", transaction_id)
            return None
        else:
            _LOGGER.info("async_sendmessage: Send failed for tran %s , resending message (attempt %s)", transaction_id, _retry + 1)
            return await self._async_sendmessage(message, _retry + 1, redact)

    async def _consumer_handler(self):
        while True:
            _LOGGER.debug("consumer_handler: Starting consumer handler")
            try:
                mess = await self._websocket.receive()
                _LOGGER.debug("consumer_handler: Received %s", mess)
            except AttributeError:  # websocket is None if not set up, just wait for a while
                _LOGGER.debug("consumer_handler: websocket not ready, sleeping for 0.1 sec")
                await asyncio.sleep(0.1)
            except Exception as exp:
                _LOGGER.warning("consumer_handler: unhandled exception ('{}')".format(exp))
            else:
                if mess.type == aiohttp.WSMsgType.TEXT:
                    message = mess.json()
                    # now parse the message
                    if message["transactionId"] in self._pending_transactions:
                        tran_message = self._pending_transactions[message["transactionId"]]
                        items_remaining = tran_message.add_item_responses(message)
                        _LOGGER.debug("consumer_handler: Response for transaction %s - %s - %s - %s ", message["transactionId"], tran_message.opclass, tran_message.operation, items_remaining)

                        if items_remaining <= 0:
                            tran_message.set_waitflag()

                    elif message["direction"] == "notification":
                        if message["operation"] == "event":
                            if (message["class"] in self._eventHandlers):
                                _LOGGER.debug("consumer_handler: handling notification event of class: %s ", message["class"])
                                fns = self._eventHandlers[message["class"]]
                                
                                async with asyncio.TaskGroup() as tg:
                                    for func in fns:
                                        tg.create_task(func(message))

                            # if "featureId" in message["items"][0]["payload"]:
                            #     feature_id = message["items"][0]["payload"]["featureId"]
                            #     feature = self.get_feature_by_featureid(feature_id)
                            #     value = message["items"][0]["payload"]["value"]
                                
                            #     if feature is None:
                            #         _LOGGER.debug("consumer_handler: feature is None: %s)", feature_id)
                            #     else:
                            #         prev_value = feature.state

                            #         feature._state = value
                            #         cblist = [c.__name__ for c in self._callback]
                            #         _LOGGER.debug("consumer_handler: Event received (%s %s %s), calling callbacks %s", feature_id, feature, value, cblist)
                            #         for func in self._callback:
                            #             func(feature=feature.name, feature_id=feature.id, prev_value = prev_value, new_value = value)
                            else:
                                _LOGGER.warning("consumer_handler: Unhandled event message: %s", message)
                        else:
                            _LOGGER.warning("consumer_handler: Received unhandled notification: %s", message)
                    else:
                        _LOGGER.warning("consumer_handler: Received unhandled message: %s - %s - %s", message["transactionId"], message["class"], message["operation"])
                elif mess.type == aiohttp.WSMsgType.CLOSED:
                    # We're not going to get a response, so clear response flag to allow _send_message to unblock
                    _LOGGER.info("consumer_handler: Websocket closed in message handler")
                    self._websocket = None
                    for key, tran_message in self._pending_transactions.items():
                        tran_message.set_waitflag()
                    self._pending_transactions = {}
                    #self._authtoken = None
                    asyncio.ensure_future(self.async_connect())
                    _LOGGER.info("consumer_handler: Websocket reconnect requested by message handler")

    def register_event_handler(self, eventClass, callback):
        if (eventClass not in self._eventHandlers):
            self._eventHandlers[eventClass] = []
        self._eventHandlers[eventClass].append(callback)


    #########################################################
    # Connection
    #########################################################

    async def async_connect(self, max_tries=5, force_keep_alive_secs=0):
        retry_delay = 2
        for x in range(0, max_tries):
            try:
                result = await self._connect_to_server()
                if force_keep_alive_secs > 0:
                    asyncio.ensure_future(self.async_force_reconnect(force_keep_alive_secs))
                
                return result
            except Exception as exp:
                if x < max_tries-1:
                    _LOGGER.warning("async_connect: Cannot connect (exception '{}'). Waiting {} seconds to retry".format(repr(exp), retry_delay))
                    await asyncio.sleep(retry_delay)
                    retry_delay = min(2 * retry_delay, 120)
                else:
                    _LOGGER.warning("async_connect: Cannot connect (exception '{}'). No more retry".format(repr(exp), retry_delay))
        _LOGGER.warning("async_connect: Cannot connect, max_tries exceeded, aborting")
        return False

    async def async_force_reconnect(self, secs):
        while True:
            await asyncio.sleep(secs)
            _LOGGER.debug("async_force_reconnect: time elapsed, forcing a reconnection")
            await self._websocket.close()


    async def _connect_to_server(self):
        if (not self._websocket) or self._websocket.closed:
            _LOGGER.debug("connect_to_server: Connecting to websocket")
            self._websocket = await self._session.ws_connect(TRANS_SERVER, heartbeat=10)
        return await self._authenticate_websocket()

    async def _authenticate_websocket(self):
        if not self._authtoken:
            await self._get_access_token()
        if self._authtoken:
            authmess = _LWRFWebsocketMessage("user", "authenticate")
            authpayload = _LWRFWebsocketMessageItem({"token": self._authtoken, "clientDeviceId": self._device_id})
            authmess.additem(authpayload)
            responses = await self._async_sendmessage(authmess, redact = True)

            if not responses[0]["success"]:
                if responses[0]["error"]["code"] == "200":
                    # "Channel is already authenticated" - Do nothing
                    pass
                elif responses[0]["error"]["code"] == 405:
                    # "Access denied" - bogus token, let's reauthenticate
                    # Lightwave seems to return a string for 200 but an int for 405!
                    _LOGGER.info("authenticate_websocket: Authentication token rejected, regenerating and reauthenticating")
                    self._authtoken = None
                    await self._authenticate_websocket()
                elif responses[0]["error"]["message"] == "user-msgs: Token not valid/expired.":
                    _LOGGER.info("authenticate_websocket: Authentication token expired, regenerating and reauthenticating")
                    self._authtoken = None
                    await self._authenticate_websocket()
                else:
                    _LOGGER.warning("authenticate_websocket: Unhandled authentication error {}".format(responses[0]["error"]))
            return responses
        else:
            return None

    async def _get_access_token(self):
        if self._auth_method == "username":
            await self._get_access_token_username()
        elif self._auth_method == "api":
            await self._get_access_token_api()
        else:
            raise ValueError("auth_method must be 'username' or 'api'")

    async def _get_access_token_username(self):
        _LOGGER.debug("get_access_token_username: Requesting authentication token (using username and password)")
        authentication = {"email": self._username, "password": self._password, "version": VERSION}
        async with self._session.post(AUTH_SERVER, headers={"x-lwrf-appid": "ios-01"}, json=authentication) as req:
            if req.status == 200:
                _LOGGER.debug("get_access_token_username: Received response: [contents hidden for security]")
                #_LOGGER.debug("get_access_token_username: Received response: {}".format(await req.json()))
                self._authtoken = (await req.json())["tokens"]["access_token"]
            elif req.status == 404:
                _LOGGER.warning("get_access_token_username: Authentication failed - if network is ok, possible wrong username/password")
                self._authtoken = None
            else:
                _LOGGER.warning("get_access_token_username: Authentication failed, : status {}".format(req.status))
                self._authtoken = None

    # TODO check for token expiry
    async def _get_access_token_api(self):
        _LOGGER.debug("get_access_token_api: Requesting authentication token (using API key and refresh token)")
        authentication = {"grant_type": "refresh_token", "refresh_token": self._refresh_token}
        async with self._session.post(PUBLIC_AUTH_SERVER,
                            headers={"authorization": "basic " + self._api_token},
                            json=authentication) as req:
            _LOGGER.debug("get_access_token_api: Received response: [contents hidden for security]")
            #_LOGGER.debug("get_access_token_api: Received response: {}".format(await req.text()))
            if req.status == 200:
                self._authtoken = (await req.json())["access_token"]
                self._refresh_token = (await req.json())["refresh_token"]
                self._token_expiry = datetime.datetime.now() + datetime.timedelta(seconds=(await req.json())["expires_in"])
            else:
                _LOGGER.warning("get_access_token_api: No authentication token (status_code '{}').".format(req.status))
                raise ConnectionError("No authentication token: {}".format(await req.text()))

    #########################################################
    # Convenience methods for non-async calls
    #########################################################

    def _sendmessage(self, message):
        return asyncio.get_event_loop().run_until_complete(self._async_sendmessage(message))

    def connect(self):
        return asyncio.get_event_loop().run_until_complete(self.async_connect())


class LWLink2:

    def __init__(self, username=None, password=None, auth_method="username", api_token=None, refresh_token=None, device_id=None):

        self._ws = LWWebsocket(username, password, auth_method, api_token, refresh_token, device_id)

        self.featuresets = {}
        self.features = {}
        self._group_ids = []

        self._callbacks = []
        self._feature_set_event_callbacks = {}

        self._ws.register_event_handler("feature", self._feature_event_handler)
        self._ws.register_event_handler("group", self.async_get_hierarchy)


    async def _feature_event_handler(self, message):
        _LOGGER.debug("_feature_event_handler: Event received - items: %s ", len(message["items"]))
        
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
                    _LOGGER.warning("_feature_event_handler: feature is None: %s", feature_id)
                else:
                    prev_value = feature.state
                    
                    feature.update_feature_state(value)

                    #  call any callbacks registered for this featues featureSet
                    for feature_set in feature.feature_sets:
                        if feature_set.featureset_id in self._feature_set_event_callbacks:
                            for func in self._feature_set_event_callbacks[feature_set.featureset_id]:
                                func(feature=feature.name, feature_id=feature.id, prev_value = prev_value, new_value = value)
                
                return feature


    async def async_get_hierarchy(self):
        _LOGGER.debug("async_get_hierarchy: Reading hierarchy")
        readmess = _LWRFWebsocketMessage("user", "rootGroups")
        readitem = _LWRFWebsocketMessageItem()
        readmess.additem(readitem)
        responses = await self._ws._async_sendmessage(readmess)

        self._group_ids = []
        for item in responses:
            self._group_ids = self._group_ids + item["payload"]["groupIds"]

        _LOGGER.debug("async_get_hierarchy: Reading groups {}".format(self._group_ids))
        await self._async_read_groups()

        await self.async_update_featureset_states()

    async def _async_read_groups(self):
        self.featuresets = {}
        for groupId in self._group_ids:
            readmess = _LWRFWebsocketMessage("group", "hierarchy")
            readitem = _LWRFWebsocketMessageItem({"groupId": groupId})

            readmess.additem(readitem)
            hierarchy_responses = await self._ws._async_sendmessage(readmess)

            readmess2 = _LWRFWebsocketMessage("group", "read")
            readitem2 = _LWRFWebsocketMessageItem({"groupId": groupId,
                                         "devices": True,
                                         "devicesDetail": True,
                                         "features": True,
                                        #  "automations": True,
                                         "subgroups": True,
                                         "subgroupDepth": 10,
                                        })
            
            readmess2.additem(readitem2)
            group_read_responses = await self._ws._async_sendmessage(readmess2)

            devices = list(group_read_responses[0]["payload"]["devices"].values())
            features = list(group_read_responses[0]["payload"]["features"].values())

            featuresets = list(hierarchy_responses[0]["payload"]["featureSet"])
            
            self.get_featuresets(featuresets, devices, features)

    async def async_update_featureset_states(self):
        # async with asyncio.TaskGroup() as tg:
        #     for feature in self.features.values():
        #         tg.create_task(feature.async_read_feature_state())
        
        id_map = {}
        
        def process_read_item_cb(response):
            item_id = response["itemId"]
            feature = id_map[item_id]
            feature.process_feature_read(response)

        message = _LWRFWebsocketMessage("feature", "read", process_read_item_cb)

        for feature in self.features.values():
            # readitem = _LWRFWebsocketMessageItem({ "featureId": feature.id, "noCache": True })
            readitem = _LWRFWebsocketMessageItem({ "featureId": feature.id })
            item_id = message.additem(readitem)
            id_map[item_id] = feature
            
        await self._ws._async_sendmessage(message, process_read_item_cb)

    async def async_write_feature(self, feature_id, value):
        readmess = _LWRFWebsocketMessage("feature", "write")
        readitem = _LWRFWebsocketMessageItem({"featureId": feature_id, "value": value})
        readmess.additem(readitem)
        await self._ws._async_sendmessage(readmess)

    async def async_write_feature_by_name(self, featureset_id, featurename, value):
        await self.featuresets[featureset_id].features[featurename].set_state(value)

    async def async_read_feature(self, feature_id):
        readmess = _LWRFWebsocketMessage("feature", "read")
        readitem = _LWRFWebsocketMessageItem({"featureId": feature_id})
        readmess.additem(readitem)
        return await self._ws._async_sendmessage(readmess)

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

    def get_hubs(self):
        return [(x.featureset_id, x.name) for x in self.featuresets.values() if x.is_hub()]

    #########################################################
    # WS Interface
    #########################################################
    async def async_register_callback________REMOVE________(self, callback):
        _LOGGER.debug("async_register_callback: Register callback '%s'", callback.__name__)
        self._callbacks.append(callback)

    async def async_register_feature_callback(self, featureset_id, callback):
        _LOGGER.debug("async_register_feature_callback: Register callback %s - '%s'", featureset_id, callback.__name__)
        if (featureset_id not in self._feature_set_event_callbacks):
            self._feature_set_event_callbacks[featureset_id] = []
        self._feature_set_event_callbacks[featureset_id].append(callback)

    async def async_connect(self, max_tries=5, force_keep_alive_secs=0):
        return await self._ws.async_connect(max_tries, force_keep_alive_secs)

    async def async_force_reconnect(self, secs):
        self._ws.async_force_reconnect(secs)


    #########################################################
    # Convenience methods for non-async calls
    #########################################################

    def _sendmessage(self, message):
        return asyncio.get_event_loop().run_until_complete(self._ws._async_sendmessage(message))

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
        return asyncio.get_event_loop().run_until_complete(self._ws.async_connect())

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
            
            device = self.get_from_lw_ar_by_id(devices, y["deviceId"], "deviceId")

            new_featureset.product_code = device["productCode"]
            if "virtualProductCode" in device:
                new_featureset.virtual_product_code = device["virtualProductCode"]
            new_featureset.firmware_version = device["firmwareVersion"]
            new_featureset.manufacturer_code = device["manufacturerCode"]
            new_featureset.serial = device["serial"]

            new_featureset.name = y["name"]

            for feature_id in y["features"]:
                if feature_id in self.features:
                    feature = self.features[feature_id]
                    featureType = feature.name
                else:
                    _lw_Feature = self.get_from_lw_ar_by_id(features, feature_id, 'featureId')
                    featureType = _lw_Feature["attributes"]["type"]

                    if featureType == "uiIOMap":
                        count = 0
                        for featureId in device["featureIds"]:
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

            self.featuresets[y["groupId"]] = new_featureset



class LWLink2Public(LWLink2):

    def __init__(self, username=None, password=None, auth_method="username", api_token=None, refresh_token=None):

        self.featuresets = {}
        self._authtoken = None

        self._username = username
        self._password = password

        self._auth_method = auth_method
        self._api_token = api_token
        self._refresh_token = refresh_token

        self._session = aiohttp.ClientSession()
        self._token_expiry = None

        self._callback = []

    # TODO add retries/error checking to public API requests
    async def _async_getrequest(self, endpoint, _retry=1):
        _LOGGER.debug("async_getrequest: Sending API GET request to {}".format(endpoint))
        async with self._session.get(PUBLIC_API + endpoint,
                                     headers= {"authorization": "bearer " + self._authtoken}
                                      ) as req:
            _LOGGER.debug("async_getrequest: Received API response {} {} {}".format(req.status, req.raw_headers, await req.text()))
            if (req.status == 429): #Rate limited
                _LOGGER.debug("async_getrequest: rate limited, wait and retry")
                await asyncio.sleep(1)
                await self._async_getrequest(endpoint, _retry)

            return await req.json()

    async def _async_postrequest(self, endpoint, body="", _retry=1):
        _LOGGER.debug("async_postrequest: Sending API POST request to {}: {}".format(endpoint, body))
        async with self._session.post(PUBLIC_API + endpoint,
                                      headers= {"authorization": "bearer " + self._authtoken},
                                      json=body) as req:
            _LOGGER.debug("async_postrequest: Received API response {} {} {}".format(req.status, req.raw_headers, await req.text()))
            if (req.status == 429): #Rate limited
                _LOGGER.debug("async_postrequest: rate limited, wait and retry")
                await asyncio.sleep(1)
                await self._async_postrequest(endpoint, body, _retry)
            if not(req.status == 401 and (await req.json())['message'] == 'Unauthorized'):
                return await req.json()
        try:
            _LOGGER.info("async_postrequest: POST failed due to unauthorized connection, retrying connect")
            await self.async_connect()
            async with self._session.post(PUBLIC_API + endpoint,
                                          headers={
                                              "authorization": "bearer " + self._authtoken},
                                          json=body) as req:
                _LOGGER.debug("async_postrequest: Received API response {} {} {}".format(req.status, await req.text(), await req.json(content_type=None)))
                return await req.json()
        except:
            return False

    async def _async_deleterequest(self, endpoint, _retry=1):
        _LOGGER.debug("async_deleterequest: Sending API DELETE request to {}".format(endpoint))
        async with self._session.delete(PUBLIC_API + endpoint,
                                     headers= {"authorization": "bearer " + self._authtoken}
                                      ) as req:
            _LOGGER.debug("async_deleterequest: Received API response {} {} {}".format(req.status, req.raw_headers, await req.text()))
            if (req.status == 429): #Rate limited
                _LOGGER.debug("async_deleterequest: rate limited, wait and retry")
                await asyncio.sleep(1)
                await self._async_deleterequest(endpoint, _retry)
            return await req.json()

    async def async_get_hierarchy(self):

        self.featuresets = {}
        req = await self._async_getrequest("structures")
        for struct in req["structures"]:
            response = await self._async_getrequest("structure/" + struct)

            for x in response["devices"]:
                for y in x["featureSets"]:
                    _LOGGER.debug("async_get_hierarchy: Creating device {}".format(y))
                    new_featureset = LWRFFeatureSet()
                    new_featureset.link = self
                    new_featureset.featureset_id = y["featureSetId"]
                    new_featureset.product_code = x["productCode"]
                    new_featureset.name = y["name"]

                    for z in y["features"]:
                        _LOGGER.debug("async_get_hierarchy: Adding device features {}".format(z))
                        feature = LWRFFeature()
                        feature.id = z["featureId"]
                        feature.featureset = new_featureset
                        feature.name = z["type"]
                        new_featureset.features[z["type"]] = feature

                    self.featuresets[y["featureSetId"]] = new_featureset

        await self.async_update_featureset_states()

    async def async_register_webhook(self, url, feature_id, ref, overwrite = False):
        if overwrite:
            req = await self._async_deleterequest("events/" + ref)
        payload = {"events": [{"type": "feature", "id": feature_id}],
                    "url": url,
                    "ref": ref}
        req = await self._async_postrequest("events", payload)
        #TODO: test for req = 200

    async def async_register_webhook_list(self, url, feature_id_list, ref, overwrite = False):
        if overwrite:
            req = await self._async_deleterequest("events/" + ref)
        feature_list = []
        for feat in feature_id_list:
            feature_list.append({"type": "feature", "id": feat})
        payload = {"events": feature_list,
                    "url": url,
                    "ref": ref}
        req = await self._async_postrequest("events", payload)
        #TODO: test for req = 200

    async def async_register_webhook_all(self, url, ref, overwrite = False):
        if overwrite:
            webhooks = await self._async_getrequest("events")
            for wh in webhooks:
                if ref in wh["id"]:
                    await self._async_deleterequest("events/" + wh["id"])
        feature_list = []
        for x in self.featuresets.values():
            for y in x.features.values():
                feature_list.append(y.id)
        MAX_REQUEST_LENGTH = 200
        feature_list_split = [feature_list[i:i + MAX_REQUEST_LENGTH] for i in range(0, len(feature_list), MAX_REQUEST_LENGTH)]
        index = 1
        for feat_list in feature_list_split:
            f_list = []
            for feat in feat_list:
                f_list.append({"type": "feature", "id": feat})
            payload = {"events": f_list,
                "url": url,
                "ref": ref+str(index)}
            req = await self._async_postrequest("events", payload)
            index += 1
        #TODO: test for req = 200

    async def async_get_webhooks(self):
        webhooks = await self._async_getrequest("events")
        wh_list = []
        for wh in webhooks:
            wh_list.append(wh["id"])
        return wh_list

    async def delete_all_webhooks(self):
        webhooks = await self._async_getrequest("events")
        for wh in webhooks:
            await self._async_deleterequest("events/" + wh["id"])

    async def async_delete_webhook(self, ref):
        req = await self._async_deleterequest("events/" + ref)
        #TODO: test for req = 200

    def process_webhook_received(self, body):

        featureid = body['triggerEvent']['id']
        feature = self.get_feature_by_featureid(featureid)
        value = body['payload']['value']
        prev_value = feature.state
        feature._state = value
        
        cblist = [c.__name__ for c in self._callback]
        _LOGGER.debug("process_webhook_received: Event received (%s %s %s), calling callbacks %s", featureid, feature, value, cblist)
        for func in self._callback:
            func(feature=feature.name, feature_id=feature.id, prev_value = prev_value, new_value = value)

    async def async_update_featureset_states(self):
        feature_list = []

        for x in self.featuresets.values():
            for y in x.features.values():
                feature_list.append({"featureId": y.id})

        #split up the feature list into chunks as the public API doesn't like requests that are too long
        #if the request is too long, will get 404 response {"message":"Structure not found"} or a 500 Internal Server Error
        #a value of 200 used to work, but for at least one user this results in a 500 error now, so setting it to 150
        MAX_REQUEST_LENGTH = 150
        feature_list_split = [feature_list[i:i + MAX_REQUEST_LENGTH] for i in range(0, len(feature_list), MAX_REQUEST_LENGTH)]
        for feat_list in feature_list_split:
            body = {"features": feat_list}
            req = await self._async_postrequest("features/read", body)

            for featuresetid in self.featuresets:
                for featurename in self.featuresets[featuresetid].features:
                    if self.featuresets[featuresetid].features[featurename].id in req:
                        self.featuresets[featuresetid].features[featurename]._state = req[self.featuresets[featuresetid].features[featurename].id]

    async def async_write_feature(self, feature_id, value):
        payload = {"value": value}
        await self._async_postrequest("feature/" + feature_id, payload)

    async def async_read_feature(self, feature_id):
        req = await self._async_getrequest("feature/" + feature_id)
        return req["value"]

    #########################################################
    # Connection
    #########################################################

    async def _connect_to_server(self):
        await self._get_access_token()
        return True

    async def async_force_reconnect(self, secs):
        _LOGGER.debug("async_force_reconnect: not implemented for public API, skipping")




