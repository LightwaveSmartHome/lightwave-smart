import asyncio
import uuid
import datetime
import aiohttp
import logging
import traceback
from .message import LW_WebsocketMessage, LW_WebsocketMessageBatch
from .utils import LWConnectionException

_LOGGER = logging.getLogger(__name__)
# _LOGGER.setLevel(logging.INFO)

MAX_PENDING_ITEMS = 2000
CLIENT_ID_PREFIX = "7"
TRANS_SERVER = "wss://v1-linkplus-app.lightwaverf.com"


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
    def __init__(self, auth, device_id=None, client_version=None):
        self._auth = auth
        self._client_version = client_version
        
        self._active = None
        self._session = None

        self._eventHandlers = {}

        # Websocket only variables:
        self._device_id = (device_id or ("PLW2-" + CLIENT_ID_PREFIX + ":")) + str(uuid.uuid4())
        self._websocket = None
        self._connectingTS = None
        self._connect_callbacks = []
        self._disconnect_callbacks = []
        self._authenticated = False
        self._suppress_disconnect = False

        self._transaction_queue = asyncio.PriorityQueue()
        self._pending_items_manager = PendingItemsManager()

        # Start background tasks
        self.background_tasks = set()
        
        self.next_message_event = asyncio.Event()

    async def async_sendmessage(self, message, redact = False, immediate = False):
        socket = self._websocket
        if socket is None or socket.closed:
            _LOGGER.warning(f"async_sendmessage ({id(self)}): Websocket closed, reconnecting")
            # Claim before the first await so the consumer cannot also own this socket.
            if self._claim_closed_socket(socket):
                await self._notify_disconnect()
                try:
                    await self.clean_up()
                except Exception:
                    _LOGGER.exception(f"async_sendmessage ({id(self)}): clean_up failed")
            connected = await self.async_connect(source="async_sendmessage")
            if not connected:
                _LOGGER.error(f"async_sendmessage ({id(self)}): Cannot connect, aborting")
                raise LWConnectionException(f"Cannot connect, aborting", retry=False)
            
            _LOGGER.debug(f"async_sendmessage ({id(self)}): Connection reopened")

        if redact:
            _LOGGER.debug(f"async_sendmessage ({id(self)}): [contents hidden for security]")
        else:
            _LOGGER.debug(f"async_sendmessage ({id(self)}): Sending: %s", message.json())

        return await self._queue_or_send_message(message, immediate)


    async def _send_message_to_websocket(self, message):
        try:
            self._pending_items_manager.add_message(message)

            json = message.send_message()
            await self._websocket.send_str(json)
            
            transaction_id = message._message["transactionId"]
            _LOGGER.debug(f"_send_message_to_websocket ({id(self)}): Message sent - TranId: {transaction_id}")
        
        except Exception as exp:
            _LOGGER.error(f"_send_message_to_websocket ({id(self)}): Exception: {str(exp)}")

            
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
                _LOGGER.warning(f"{logPre}:  Task cancelled - active: {self._active}")
                if message and self._active:
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

            try:
                await asyncio.sleep(0.01)
            except asyncio.CancelledError:
                _LOGGER.warning(f"{logPre}:  Sleep cancelled")
                
        _LOGGER.debug(f"{logPre}: Ending")                

    async def _consumer_handler(self):
        logPre = f"consumer_handler ({id(self)})"
        _LOGGER.debug(f"{logPre}: Starting consumer handler")
        while self._active:
            websocket = self._websocket
            if websocket is None:
                # websocket is None if not set up, just wait for a while
                _LOGGER.debug(f"{logPre}: Websocket not ready, sleeping for 3 seconds")
                await asyncio.sleep(3)
                continue
            try:
                mess = await websocket.receive()
            except AttributeError:
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
                    
            elif mess.type in (
                aiohttp.WSMsgType.CLOSED,
                aiohttp.WSMsgType.CLOSING,
                aiohttp.WSMsgType.ERROR,
            ):
                # A send may already have replaced this socket and reported the drop.
                if not self._claim_closed_socket(websocket):
                    _LOGGER.debug(
                        f"{logPre}: Ignoring {mess.type.name} for a socket that is no longer current"
                    )
                    continue
                # We're not going to get a response, so clear response flag to allow _send_message to unblock
                _LOGGER.warning(
                    f"{logPre}: Websocket {mess.type.name}, reconnecting"
                )
                try:
                    await self.clean_up()
                except Exception:
                    _LOGGER.exception(f"{logPre}: clean_up failed during {mess.type.name}")
                await self._notify_disconnect()

                if self._active:
                    asyncio.ensure_future(self.async_connect(source="consumer_handler"))
                    _LOGGER.debug(f"{logPre}: Websocket reconnect requested")

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
        if callback not in self._connect_callbacks:
            self._connect_callbacks.append(callback)

    def unregister_connect_callback(self, callback):
        try:
            self._connect_callbacks.remove(callback)
        except ValueError:
            return

    def register_disconnect_callback(self, callback):
        if callback not in self._disconnect_callbacks:
            self._disconnect_callbacks.append(callback)

    def unregister_disconnect_callback(self, callback):
        try:
            self._disconnect_callbacks.remove(callback)
        except ValueError:
            return

    def _claim_closed_socket(self, socket):
        """Detach socket if it is still current. Only one caller can own the close."""
        if socket is None or self._websocket is not socket:
            return False
        self._websocket = None
        return True

    @property
    def connected(self):
        """True after authentication until the socket closes or the link is stopped."""
        websocket = self._websocket
        if not self._authenticated or websocket is None:
            return False
        return not websocket.closed

    async def _notify_disconnect(self):
        """Tell listeners a session that was up has dropped.

        Startup, before the first successful authentication, does not notify.
        async_deactivate suppresses this so an intentional stop is not an outage.
        """
        if self._suppress_disconnect or not self._authenticated:
            self._authenticated = False
            return
        self._authenticated = False
        for callback in list(self._disconnect_callbacks):
            try:
                result = callback()
                if asyncio.iscoroutine(result):
                    await result
            except Exception:
                _LOGGER.exception(f"_notify_disconnect ({id(self)}): callback failed")

    async def _notify_connect(self):
        for callback in list(self._connect_callbacks):
            try:
                result = callback()
                if asyncio.iscoroutine(result):
                    await result
            except Exception:
                _LOGGER.exception(f"_notify_connect ({id(self)}): callback failed")

    #########################################################
    # Connection
    #########################################################
    async def async_deactivate(self, source=None):
        if self._active:
            _LOGGER.info(f"async_deactivate ({id(self)}/{source}): Deactivating - {len(self.background_tasks)} background tasks will be cancelled")
        else:
            _LOGGER.warning(f"async_deactivate ({id(self)}/{source}): Not active")
            return
        
        self._suppress_disconnect = True
        self._authenticated = False
        self._active = None
        await self.clean_up()
        self._connect_callbacks = []
        self._disconnect_callbacks = []
        
        tasks_to_cancel = []
        while self.background_tasks:
            task = self.background_tasks.pop()
            tasks_to_cancel.append(task)
            task.cancel()
        
        if tasks_to_cancel:
            await asyncio.gather(*tasks_to_cancel, return_exceptions=True)
                        
        if self._websocket:
            await self._websocket.close()
        self._websocket = None
        
        if self._session:
            await self._session.close()
        self._session = None
        
        
    def activate(self, source=None):
        if self._active:
            _LOGGER.warning(f"activate ({source}/{id(self)}): Already active")
            return
        
        _LOGGER.info(f"activate ({source}/{id(self)}): Activating")
        self._active = True
        self._suppress_disconnect = False
        self._authenticated = False
        
        self._session = aiohttp.ClientSession()

        self._start_background_task(self._consumer_handler, "consumer")
        self._start_background_task(self._process_transaction_queue, "transaction_queue")

    def _start_background_task(self, coro_fn, name):
        task = asyncio.create_task(coro_fn(), name=name)
        self.background_tasks.add(task)
        task.add_done_callback(self._on_background_task_done)

    def _on_background_task_done(self, task):
        self.background_tasks.discard(task)
        if task.cancelled() or not self._active:
            return

        name = task.get_name()
        exc = task.exception()
        _LOGGER.error(
            f"background task {name} ({id(self)}) exited while active - exception: {exc!r}"
        )
        if name == "consumer":
            _LOGGER.info(f"Restarting consumer handler ({id(self)})")
            self._start_background_task(self._consumer_handler, "consumer")

    async def clean_up(self):
        already_finished = []
        try:
            seen = set()
            for tran_message in self._pending_items_manager._pending_items.values():
                if id(tran_message) in seen:
                    continue
                seen.add(id(tran_message))
                if tran_message.future is not None and tran_message.future.done():
                    already_finished.append(tran_message.transcation_id)
                try:
                    tran_message.complete()
                except Exception:
                    _LOGGER.exception(
                        f"clean_up ({id(self)}): complete failed - tranId: {tran_message.transcation_id}"
                    )
        finally:
            while not self._transaction_queue.empty():
                try:
                    self._transaction_queue.get_nowait()
                    self._transaction_queue.task_done()
                except asyncio.QueueEmpty:
                    break
            self._pending_items_manager.clear()

        if already_finished:
            _LOGGER.info(
                f"clean_up ({id(self)}): {len(already_finished)} already-finished futures - tranIds: {already_finished}"
            )

    async def async_connect(self, max_tries=None, force_keep_alive_secs=0, source=None):
        logPre = f"async_connect ({id(self)}/{source}):"
        if not self._active:
            _LOGGER.error(f"{logPre} Not active")
            return False
        
        if self._connectingTS:
            _LOGGER.warning(f"{logPre} Connecting already in progress as of {self._connectingTS} - skipping")
            return False
        
        _LOGGER.info(f"{logPre} - Connecting...")
        self._connectingTS = datetime.datetime.now()
        
        connected = False
        authenticated = False
        try:
            connected = await self._connect_to_server(max_tries, source)
            if not connected:
                _LOGGER.error(f"{logPre} Cannot connect - aborting after: {datetime.datetime.now() - self._connectingTS}")
            else:
                authenticated = await self._authenticate(max_tries, force_keep_alive_secs, source)

                if authenticated:
                    elapsed = datetime.datetime.now() - self._connectingTS
                    if source in ("consumer_handler", "async_sendmessage"):
                        _LOGGER.warning(f"{logPre} Websocket reconnected after: {elapsed}")
                    else:
                        _LOGGER.info(f"{logPre} Connected and Authenticated - after: {elapsed}")
                    self._authenticated = True
                    await self._notify_connect()
                else:
                    _LOGGER.error(f"{logPre} Cannot authenticate - aborting after: {datetime.datetime.now() - self._connectingTS}")
                
        except Exception as exp:
            _LOGGER.error(f"{logPre} Connected: {connected} - Authenticated: {authenticated} - Exception: {repr(exp)} - {traceback.format_exc()}")
            
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
                
            except LWConnectionException as exp:
                if exp.retry == False:
                    _LOGGER.warning(f"_authenticate ({source}): Unrecoverable error - Attempt {attempt} - exception: '{repr(exp)}'")
                    break
                    
            except Exception as exp:
                retryMsg = 'Exception'
                details = f" - exception: '{repr(exp)} - {traceback.format_exc()}'"
            
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
                await self._session.close()
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
        
        access_token = await self._auth.async_get_access_token()
            
        if access_token:
            authmess = LW_WebsocketMessage("user", "authenticate")
            auth_item = {"token": access_token, "clientDeviceId": self._device_id}
            if self._client_version:
                auth_item["clientVersion"] = self._client_version
            authmess.add_item(auth_item)
            
            responses = await self.async_sendmessage(authmess, redact = True, immediate = True)
            if responses and responses[0] and not responses[0]["success"]:
                error = responses[0]["error"]
                error_code = error["code"]
                error_message = error["message"]
                _LOGGER.warning(f"authenticate_websocket ({id(self)}): Authentication error: {error}")
                
                retry_reason = None
                if error_code == "200":
                    # "Channel is already authenticated" - Do nothing
                    pass
                
                elif error_code == 405:
                    # "Access denied" - bogus token, let's reauthenticate
                    # Lightwave seems to return a string for 200 but an int for 405!  TODO - fix
                    retry_reason = "rejected (405)"
                    
                elif error_message == "user-msgs: Token not valid/expired.":
                    retry_reason = "expired"
                    
                else:
                    _LOGGER.warning(f"authenticate_websocket: Unhandled authentication error")
                    return False

                if retry_reason:
                    retry_allowed = self._auth.invalidate_access_token()
                    if retrying or not retry_allowed:
                        return False
                    
                    _LOGGER.info(f"authenticate_websocket ({id(self)}): Authentication token {retry_reason}, regenerating and reauthenticating")
                    return await self._authenticate_websocket(source=retry_reason, retrying=True)
                    
            return True
        else:
            return None

    #########################################################
    # Convenience methods for non-async calls
    #########################################################

    def _sendmessage(self, message):
        return asyncio.get_event_loop().run_until_complete(self.async_sendmessage(message))

    def connect(self):
        return asyncio.get_event_loop().run_until_complete(self.async_connect(source="websocket-connect"))

