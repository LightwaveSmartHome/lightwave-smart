import asyncio
import uuid
import datetime
import aiohttp
import logging
from .message import LW_WebsocketMessage, LW_WebsocketMessageBatch

_LOGGER = logging.getLogger(__name__)
# _LOGGER.setLevel(logging.INFO)

MAX_PENDING_ITEMS = 2000

VERSION = "1.6.9"
CLIENT_ID_PREFIX = "3"

PUBLIC_AUTH_SERVER = "https://auth.lightwaverf.com/token"
AUTH_SERVER = "https://auth.lightwaverf.com/v2/lightwaverf/autouserlogin/lwapps"
TRANS_SERVER = "wss://v1-linkplus-app.lightwaverf.com"

#TODO adapt async_connect calls to respond to connection failure


class PendingItemsManager:
    def __init__(self):
        self._pending_items = {}
        self._pending_items_event = asyncio.Event()
        self._pending_items_event.set()

    def add_message(self, message):
        items = message.get_items()        
        for item in items:
            self._pending_items[item["itemId"]] = message
        
    def add_item(self, item_id, message):
        self._pending_items[item_id] = message

    def pop_item(self, item_id):
        item = self._pending_items.pop(item_id, None)
        if item and len(self._pending_items) < MAX_PENDING_ITEMS:
            self._pending_items_event.set()
        return item

    def clear(self):
        self._pending_items.clear()
        self._pending_items_event.set()

    @property
    def count(self):
        return len(self._pending_items)

    @property
    def start_event(self):
        self._pending_items_event.clear()
        return self._pending_items_event.wait


class LWWebsocket:
    def __init__(self, username=None, password=None, auth_method="username", api_token=None, refresh_token=None, device_id=None):
        self._active = None
        
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
        self._device_id = (device_id or ("PLW2-" + CLIENT_ID_PREFIX + ":")) + str(uuid.uuid4())
        self._websocket = None
        self._connectingTS = None
        self._connect_callbacks = []

        self._transaction_queue = asyncio.PriorityQueue()
        self._pending_items_manager = PendingItemsManager()

        # Start background tasks
        self.background_tasks = set()
        
        self.next_message_event = asyncio.Event()

    async def async_sendmessage(self, message, redact = False, immediate = False):
        if not self._websocket or self._websocket.closed:
            _LOGGER.info("async_sendmessage: Websocket closed, reconnecting")
            await self.async_connect(source="async_sendmessage")
            _LOGGER.info("async_sendmessage: Connection reopened")

        if redact:
            _LOGGER.debug("async_sendmessage: [contents hidden for security]")
        else:
            _LOGGER.debug("async_sendmessage: Sending: %s", message.json())

        return await self._queue_or_send_message(message, immediate)


    async def _send_message_to_websocket(self, message):
        try:
            self._pending_items_manager.add_message(message)

            json = message.send_message()
            await self._websocket.send_str(json)
            
            transaction_id = message._message["transactionId"]
            _LOGGER.debug(f"_send_message_to_websocket: Message sent - TranId: {transaction_id}")
        
        except Exception as exp:
            _LOGGER.error(f"_send_message_to_websocket: Exception: {str(exp)}")

            
    async def _queue_or_send_message(self, message, immediate = False):
        message_items_count = len(message.get_items())

        if message_items_count <= MAX_PENDING_ITEMS:
            future = message.get_future()
            message.priority = 0 if immediate else 1
            await self._transaction_queue.put(message)
            
        else:
            # Split the message into batches
            message_batch = LW_WebsocketMessageBatch()
                        
            batches = []
            for i in range(0, message_items_count, MAX_PENDING_ITEMS):
                batch = LW_WebsocketMessage(message.opclass, message.operation, message.item_received_cb, message._message["transactionId"], message_batch)
                batch.priority = 0 if immediate else 1
                batch_items = message.get_items()[i:i+MAX_PENDING_ITEMS]
                for item in batch_items:
                    batch.add_item_as_is(item)
                batches.append(batch)
                message_batch.add_message()

            # Queue all batches
            for batch in batches:
                await self._transaction_queue.put(batch)

            future = message_batch.combined_future

        return await future

    
    async def _process_transaction_queue(self):
        logPre = f"_process_transaction_queue ({id(self)})"
        _LOGGER.debug(f"{logPre}: Starting")
        while self._active:
            if self._pending_items_manager.count >= MAX_PENDING_ITEMS:
                _LOGGER.info(f"{logPre} - MAX:  SLEEPING max items reached - pending items: {self._pending_items_manager.count}")
                
                try:
                    waiter = self._pending_items_manager.start_event()
                    await asyncio.wait_for(
                        waiter,
                        timeout=10
                    )
                    
                    _LOGGER.debug(f"{logPre} - MAX:  CONTINUE - pending items: {self._pending_items_manager.count}")
                    
                except asyncio.TimeoutError:
                    _LOGGER.debug(f"{logPre} - MAX:  TIMEOUT - pending items: {self._pending_items_manager.count}")
                    
                except asyncio.CancelledError:
                    _LOGGER.warning(f"{logPre} - MAX:  Wait cancelled")
                
                except Exception as exp:
                    _LOGGER.error(f"{logPre} - MAX:  ERROR - {str(exp)}")
                
                continue
            
            
            message = None
            try:
                message = await asyncio.wait_for(
                    self._transaction_queue.get(),                          
                    timeout=5.0
                )
                
            except asyncio.TimeoutError:
                if self._pending_items_manager.count > 0:
                    _LOGGER.info(f"{logPre}:  No new messages last 5 seconds - pending items: {self._pending_items_manager.count}")
                continue
            
            except asyncio.CancelledError:
                _LOGGER.warning(f"{logPre}:  Task cancelled")
                if message:
                    # If a message was retrieved before cancellation, put it back in the queue
                    await self._transaction_queue.put(message)
                    message = None
                continue
            
            except Exception as exp:
                _LOGGER.error(f"{logPre}:  Unhandled exception: {str(exp)}")
                await asyncio.sleep(1)  # Avoid rapid retries in case of persistent errors
                continue
            
            finally:
                if message:
                    self._transaction_queue.task_done()

            _LOGGER.debug(f"{logPre} (priority: {message.priority}) - tranId: {message._message['transactionId']} - cl/op: {message.opclass}/{message.operation} - pending items: {self._pending_items_manager.count}, queue size: {self._transaction_queue.qsize()}")
            await self._send_message_to_websocket(message)

            await asyncio.sleep(0.01)
                

    async def _consumer_handler(self):
        logPre = f"consumer_handler ({id(self)})"
        _LOGGER.debug(f"{logPre}: Starting consumer handler")
        while self._active:
            try:
                mess = await self._websocket.receive()
                _LOGGER.debug(f"{logPre}: Received: {mess}")
            except AttributeError:  
                # websocket is None if not set up, just wait for a while
                _LOGGER.debug(f"{logPre}: Websocket not ready, sleeping for 3 seconds")
                await asyncio.sleep(3)
                continue
            except Exception as exp:
                _LOGGER.warning(f"{logPre}: Unhandled exception: {str(exp)}")
                continue
                
            _LOGGER.debug(f"{logPre}: Received: {str(mess)}")
            
            if mess.type == aiohttp.WSMsgType.TEXT:
                try:
                    # now parse the message
                    message = mess.json()
                    _LOGGER.debug(f"{logPre}: Received message - TranId: {message['transactionId']} - direction: {message['direction']} - cl/op: {message['class']}/{message['operation']}")
                    
                    if message["direction"] == "response":
                        items_handled = 0
                        if message["items"]:
                            for item in message["items"]:
                                if "itemId" in item:
                                    tran_message = self._pending_items_manager.pop_item(item["itemId"])
                                    if tran_message:
                                        items_remaining = tran_message.add_item_response(item)
                                        _LOGGER.debug(f"{logPre}: Response - TranId: {message['transactionId']} - cl/op: {tran_message.opclass}/{tran_message.operation} - tran items_remaining: {items_remaining}")
                                        
                                        items_handled += 1

                        if items_handled > 0:
                            _LOGGER.debug(f"{logPre}: {items_handled} items handled for TranId: {message['transactionId']}")

                    elif message["direction"] == "notification":
                        if message["operation"] == "event":
                            if message["class"] in self._eventHandlers:
                                _LOGGER.debug(f"{logPre}: handling notification event of cl/op: {message['class']}/{message['operation']}")
                                
                                event_handlers = self._eventHandlers[message["class"]]
                                
                                if message["operation"] in event_handlers:
                                    fns = event_handlers[message["operation"]]
                                    async with asyncio.TaskGroup() as tg:
                                        for func in fns:
                                            tg.create_task(func(message))
                                
                                if None in event_handlers:
                                    fns = event_handlers[None]
                                    async with asyncio.TaskGroup() as tg:
                                        for func in fns:
                                            tg.create_task(func(message))
                            else:
                                _LOGGER.info(f"{logPre}: Unhandled event message - cl/op: {message['class']}/{message['operation']}")
                        else:
                            _LOGGER.info(f"{logPre}: Unhandled notification - cl/op: {message['class']}/{message['operation']}")
                            
                    else:
                        _LOGGER.info(f"{logPre}: Unhandled message - cl/op: {message['class']}/{message['operation']}")
                        
                except Exception as exp:
                    _LOGGER.warning(f"{logPre}: Error parsing message: {str(mess)} - Exception: {str(exp)}")
                    
            elif mess.type == aiohttp.WSMsgType.CLOSED:
                # We're not going to get a response, so clear response flag to allow _send_message to unblock
                _LOGGER.warning(f"{logPre}: Websocket closed")
                self._websocket = None
                await self.clean_up()
                
                #self._authtoken = None
                asyncio.ensure_future(self.async_connect(source="consumer_handler"))
                # self.connect()
                _LOGGER.info(f"{logPre}: Websocket reconnect requested")

    def register_event_handler(self, eventClass, callback, eventOperation=None):
        if eventClass not in self._eventHandlers:
            self._eventHandlers[eventClass] = {}
        if eventOperation:
            if eventOperation not in self._eventHandlers[eventClass]:
                self._eventHandlers[eventClass][eventOperation] = []
            self._eventHandlers[eventClass][eventOperation].append(callback)
        else:
            if None not in self._eventHandlers[eventClass]:
                self._eventHandlers[eventClass][None] = []
            self._eventHandlers[eventClass][None].append(callback)

    def register_connect_callback(self, callback):
        self._connect_callbacks.append(callback)

    #########################################################
    # Connection
    #########################################################
    async def async_deactivate(self):
        _LOGGER.info(f"async_deactivate ({id(self)}): Deactivating - {len(self.background_tasks)} background tasks will be cancelled")
        
        self._active = None
        await self.clean_up()
        self._connect_callbacks = []
        
        if self._websocket:
            await self._websocket.close()
            self._websocket = None
        
        if self._session:
            await self._session.close()
        self._session = None
        
        while self.background_tasks:
            task = self.background_tasks.pop()
            task.cancel()
        
    def activate(self):
        if self._active:
            _LOGGER.warning(f"activate ({id(self)}): Already active")
            return
        
        _LOGGER.info(f"activate ({id(self)}): Activating")
        self._active = True
        
        task = asyncio.create_task(self._consumer_handler())
        self.background_tasks.add(task)
        # task.add_done_callback(self.background_tasks.discard)
        
        task = asyncio.create_task(self._process_transaction_queue())
        self.background_tasks.add(task)
        # task.add_done_callback(self.background_tasks.discard)
    
    async def clean_up(self):
        while not self._transaction_queue.empty():
            try:
                message = await self._transaction_queue.get_nowait()
                self._transaction_queue.task_done()
            except asyncio.QueueEmpty:
                break

        for key, tran_message in self._pending_items_manager._pending_items.items():
            tran_message.complete()                 # will be called potentially multiple times for the same message
        self._pending_items_manager.clear()

    async def async_connect(self, max_tries=None, force_keep_alive_secs=0, source=None, connect_callback=None):
        if not self._active:
            _LOGGER.error(f"async_connect ({id(self)}/{source}): Not active")
            return False
        
        if self._connectingTS:
            _LOGGER.warning(f"async_connect ({id(self)}/{source}): Connecting already in progress as of {self._connectingTS} - skipping")
            return False
        
        _LOGGER.info(f"async_connect ({id(self)}/{source}): - Connecting...")
        self._connectingTS = datetime.datetime.now()
        
        connected = False
        authenticated = False
        try:
            if connect_callback:
                self.register_connect_callback(connect_callback)
            
            connected = await self._connect_to_server(max_tries, source)
            if not connected:
                _LOGGER.error(f"async_connect ({source}): Cannot connect - aborting after: {datetime.datetime.now() - self._connectingTS}")
            else:
                authenticated = await self._authenticate(max_tries, force_keep_alive_secs, source)

                if authenticated:
                    _LOGGER.info(f"async_connect ({source}): Connected and Authenticated - after: {datetime.datetime.now() - self._connectingTS}")
                    if self._connect_callbacks:
                        for callback in self._connect_callbacks:
                            await callback()
                else:
                    _LOGGER.error(f"async_connect ({source}): Cannot authenticate - aborting after: {datetime.datetime.now() - self._connectingTS}")
                
        except Exception as exp:
            _LOGGER.error(f"async_connect ({source}): Connected: {connected} - Authenticated: {authenticated} - Exception: {str(exp)}")
            
        if not connected or not authenticated:
            if self._websocket:
                await self._websocket.close()
                self._websocket = None
        
        self._connectingTS = None
        return connected and authenticated
        
    async def _authenticate(self, max_tries=None, force_keep_alive_secs=0, source=None):
        max_auth_retries = 3
        attempt_delay = 20
        if max_tries is not None:
            max_auth_retries = max_tries
            attempt_delay = 5

        authenticated = False
        attempt = 0
        while self._active and attempt < max_auth_retries:
            attempt += 1
            retryMsg = ''
            details = ''
            
            try:
                authenticated = await self._authenticate_websocket('async_connect')                
                if authenticated:
                    if force_keep_alive_secs > 0:
                        asyncio.ensure_future(self.async_force_reconnect(force_keep_alive_secs))
                    break
                    
                else:
                    retryMsg = 'Not authenticated'
                
            except Exception as exp:
                retryMsg = 'Exception'
                details = f" - exception: '{repr(exp)}'"
            
            if attempt >= max_auth_retries:
                break
            
            _LOGGER.warning(f"_authenticate ({source}): {retryMsg} - Attempt {attempt} - retrying at {datetime.datetime.now() + datetime.timedelta(seconds=attempt * attempt_delay)}{details}")
            await asyncio.sleep(attempt * attempt_delay)

        return authenticated

    async def async_force_reconnect(self, secs):
        while self._active:
            await asyncio.sleep(secs)
            _LOGGER.warning(f"async_force_reconnect ({id(self)}): time elapsed, forcing a reconnection")
            await self._websocket.close()


    async def _connect_to_server(self, max_tries=None, source=None):
        _LOGGER.info(f"connect_to_server: Starting ({source})")
        await self.clean_up()
        
        network_retry_delay = 30        # retry every 30 seconds, changing to 300 after 20 attempts (10 mins)
        if max_tries is not None:
            network_retry_delay = 5
            
        attempt = 0
        while self._active and (max_tries is None or attempt < max_tries):
            attempt += 1
            
            retryMsg = ''
            details = ''
            retryDelay = network_retry_delay
            if attempt > 20 and max_tries is None:
                retryDelay = 300
            
            try:
                _LOGGER.info(f"connect_to_server: Connecting to websocket ({source}) - Attempt {attempt}")
                self._websocket = await self._session.ws_connect(TRANS_SERVER, heartbeat=10)
                _LOGGER.info(f"connect_to_server: Connected to websocket ({source}) - Attempt {attempt}")
                break

            except asyncio.InvalidStateError as exp:
                # Session state error - recreate session and continue
                retryMsg = f"InvalidStateError"
                details = f" - recreating session"
                self._session = aiohttp.ClientSession()
            
            except (aiohttp.ClientError, aiohttp.ClientConnectorError, aiohttp.ClientConnectionError, ConnectionRefusedError, OSError) as exp:
                # Network-related errors
                retryMsg = f"Network error"
                details = f" - exception: '{repr(exp)}'"
                
            except Exception as exp:
                retryMsg = f"Unknown exception"
                details = f" - exception: '{repr(exp)}'"
                
            max_tries_msg = f" of {max_tries}" if max_tries is not None else ""
            _LOGGER.warning(f"connect_to_server ({source}): {retryMsg} - Attempt: {attempt}{max_tries_msg} - Retrying at: {datetime.datetime.now() + datetime.timedelta(seconds=retryDelay)}{details}")
            
            await asyncio.sleep(retryDelay)
            
        return self._websocket is not None and not self._websocket.closed

    async def _authenticate_websocket(self, source = None, retrying = False):
        # return True if authenticated, False if failed, None if no authtoken
        
        if not self._authtoken:
            await self._get_access_token()
            
        if self._authtoken:
            authmess = LW_WebsocketMessage("user", "authenticate")
            authmess.add_item({"token": self._authtoken, "clientDeviceId": self._device_id})
            
            responses = await self.async_sendmessage(authmess, redact = True, immediate = True)
            if responses and responses[0] and not responses[0]["success"]:
                if responses[0]["error"]["code"] == "200":
                    # "Channel is already authenticated" - Do nothing
                    pass
                
                elif responses[0]["error"]["code"] == 405:
                    # "Access denied" - bogus token, let's reauthenticate
                    # Lightwave seems to return a string for 200 but an int for 405!  TODO - fix
                    self._authtoken = None
                    if retrying:
                        return False
                    
                    _LOGGER.info("authenticate_websocket: Authentication token rejected, regenerating and reauthenticating")
                    return await self._authenticate_websocket('405', True)
                    
                elif responses[0]["error"]["message"] == "user-msgs: Token not valid/expired.":
                    self._authtoken = None
                    if retrying:
                        return False
                    
                    _LOGGER.info("authenticate_websocket: Authentication token expired, regenerating and reauthenticating")
                    return await self._authenticate_websocket('expired', True)
                    
                else:
                    _LOGGER.warning("authenticate_websocket: Unhandled authentication error {}".format(responses[0]["error"]))
                    return False
                    
            return True
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
                _LOGGER.warning("get_access_token_username: Authentication failed - status {}".format(req.status))
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
        return asyncio.get_event_loop().run_until_complete(self.async_sendmessage(message))

    def connect(self):
        return asyncio.get_event_loop().run_until_complete(self.async_connect(source="websocket-connect"))

