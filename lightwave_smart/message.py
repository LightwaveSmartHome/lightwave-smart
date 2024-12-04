import asyncio
import uuid
import json
import logging
import traceback

_LOGGER = logging.getLogger(__name__)
# _LOGGER.setLevel(logging.INFO)


class LW_WebsocketMessageBatch:
    def __init__(self):
        self.message_count = 0
        self.complete_messages = []
        self.combined_future = asyncio.Future()
    
    def add_message(self):
        self.message_count += 1

    def message_complete(self, message):
        self.complete_messages.append(message)
        
        _LOGGER.debug("BATCH - message_complete - %s - %s", self.message_count, len(self.complete_messages))
        
        if len(self.complete_messages) == self.message_count:
            results = []
            for message in self.complete_messages:
                results.extend(message.get_item_responses())
            self.combined_future.set_result(results)

class LW_WebsocketMessage:
    _tran_id = 0
    # TODO - format sender_id better
    _sender_id = str(uuid.uuid4())

    def __init__(self, opclass, operation, item_received_cb = None, transcation_id = None, message_batch = None, priority = 1):
        if transcation_id is None:
            LW_WebsocketMessage._tran_id += 1
        
        self.transcation_id = transcation_id or LW_WebsocketMessage._tran_id
        self.opclass = opclass
        self.operation = operation
        
        self.priority = priority
        self.item_received_cb = item_received_cb
        
        self.pending_item_ids = []
        self.item_responses = []

        self._message = {
            "version": 1,
            "senderId": LW_WebsocketMessage._sender_id,
            "transactionId": LW_WebsocketMessage._tran_id,
            "direction": "request",
            "class": opclass,
            "operation": operation,
        }
        self._message["items"] = []
        
        self._waitflag = asyncio.Event()
        self.response_task = None

        # either / or - todo - improve
        self.message_batch = message_batch
        self.future = asyncio.Future()
        
    def __lt__(self, other: object) -> bool:
        return self.priority < other.priority        

    def add_item(self, payload = None, item_id = None):
        new_item = LW_WebsocketMessageItem(payload, item_id)
        self.pending_item_ids.append(new_item._item["itemId"])
        self._message["items"].append(new_item._item)
        return new_item._item["itemId"]
    
    def add_item_as_is(self, item = None):
        if item is not None:
            item_id = item["itemId"] if "itemId" in item else None
            payload = item["payload"] if "payload" in item else None
        
        return self.add_item(payload, item_id)
    
    def add_item_response(self, item):
        try:
            self.item_responses.append(item)
            
            if item["itemId"] in self.pending_item_ids:
                self.pending_item_ids.remove(item["itemId"])
            
            if self.item_received_cb:
                try:
                    self.item_received_cb(item)
                except Exception as e:
                    _LOGGER.error(f"add_item_response: Error processing item_received_cb for message - tranId: {self.transcation_id} - item: {item} - Error: {str(e)}  Trackback: {traceback.format_exc()}")
            
            pending_items_count = len(self.pending_item_ids)
            if pending_items_count <= 0:
                self.complete()
                
        except Exception as e:
            _LOGGER.error(f"add_item_response: Error processing message - tranId: {self.transcation_id} - item: {item} - Error: {str(e)}  Trackback: {traceback.format_exc()}")

        return pending_items_count

    def get_items(self):
        return self._message["items"]
    
    def get_item_responses(self):
        return self.item_responses
    
    def get_responses_and_timeouts(self):
        for item_id in self.pending_item_ids:
            if not any(item['itemId'] == item_id for item in self.item_responses):
                timeout_item = {
                    "itemId": item_id,
                    "success": False,
                    "error": {
                        "group": "HA",
                        "code": "TIMEOUT",
                        "message": "Request timeout"
                    }
                }
                self.item_responses.append(timeout_item)
        
        self.pending_item_ids.clear()
        self.complete()
        
        return self.item_responses
    
    
    def _response_timeout(self):
        for item_id in self.pending_item_ids:
            if not any(item['itemId'] == item_id for item in self.item_responses):
                timeout_item = {
                    "itemId": item_id,
                    "success": False,
                    "error": {
                        "group": "HA",
                        "code": "TIMEOUT",
                        "message": "Request timeout"
                    }
                }
                self.item_responses.append(timeout_item)
        
        self.pending_item_ids.clear()
        self.complete()
    
    async def wait_for_response(self):
        self._waitflag.clear()
        try:
            timeout = len(self.get_items()) * 60.0
            await asyncio.wait_for(self._waitflag.wait(), timeout=timeout)
            _LOGGER.debug(f"All responses received: TranId: {self.transcation_id} - {self._message['class']}/{self._message['operation']} - Received items: {len(self.get_items())}")
        except asyncio.TimeoutError:
            _LOGGER.error(f"Timeout waiting for responses: TranId: {self.transcation_id} - {self._message['class']}/{self._message['operation']} - Received/Expected: {len(self.get_item_responses())} / {len(self.get_items())}")
            self._response_timeout()

    
    # TODO - rename
    def send_message(self):
        # should not be needed
        if self.response_task is not None:
            _LOGGER.warning("send_message: response_task already exists - cancelling")
            self.response_task.cancel()
            
        self.response_task = asyncio.create_task(self.wait_for_response())
        # self.response_task.add_done_callback(self.response_task.discard)
        
        return self.json()
        # return self._waitflag
    
    
    def complete(self):
        # if self._waitflag:
        if self.future:
            self.future.set_result(self.item_responses)
        if self.message_batch:
            self.message_batch.message_complete(self)
        
        self._waitflag.set()
        # self._waitflag = None

    def json(self):
        return json.dumps(self._message)
    
    def get_future(self):
        return self.future

class LW_WebsocketMessageItem:
    _item_id = 0

    def __init__(self, payload=None, item_id=None):
        if item_id is None:
            LW_WebsocketMessageItem._item_id += 1
            item_id = LW_WebsocketMessageItem._item_id
            
        if payload is None:
            payload = {}
            
        self._item = {
            "itemId": item_id,
            "payload": payload
        }

