import asyncio
import logging
import time
import traceback

from .websocket import LWWebsocket
from .message import LW_WebsocketMessage
from .device import LWRFDevice, LWRFFeatureSet, LWRFFeature, LWRFUiIOMapFeature, LWRFUiButtonFeature
from .auth import LWAuth

_LOGGER = logging.getLogger(__name__)

RGB_FLOOR = int("0x0", 16) #Previously the Lightwave app floored RGB values, but it doesn't any more

# A Link can authenticate before its devices answer feature reads.
_LINK_REFRESH_DEBOUNCE_SECS = 2
_LINK_REFRESH_ATTEMPTS = 3
_LINK_REFRESH_RETRY_SECS = 5
_LINK_OFFLINE_GRACE_SECS = 30
# While the socket is settling or latched, device status reads and events wait.
_LINK_STATUS_HELD = ("grace", "offline")


def connected_since_ms(value):
    """Socket generation in milliseconds. None when the event cannot be ordered."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        if value != value:
            return None
        return float(value)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            return float(text)
        except ValueError:
            return None
    return None


def link_id_from_combined_id(combined_id):
    """Return the link id from structureId-thingId-linkId+factoryResets."""
    if combined_id is None:
        return None
    parts = str(combined_id).split("-")
    if len(parts) < 3 or not parts[2]:
        return None
    link_id = parts[2].split("+", 1)[0]
    return link_id or None

class LWLink2:
    def __init__(self, device_id=None, auth=None, client_version=None):
        self.auth = auth if auth else LWAuth()
        
        self._ws = LWWebsocket(self.auth, device_id, client_version=client_version)        
        
        self.structures = {}    # structureId -> {linkPlus_featureset_id, name}
        
        self.devices = {}
        self.featuresets = {}
        self.features = {}
        self._group_ids = []

        self._callbacks = []
        
        self._closing = False
        self._background_tasks = set()
        self._link_refresh_ready_at = {}
        self._link_refresh_tasks = {}
        self._link_grace_tasks = {}
        self._link_socket_state = {}
        self._link_status_epoch = {}
        self._link_connected_since = {}
        self._link_restore_owed = set()
        self._link_feature_cycle_seen = set()

        self._ws.register_event_handler("feature", self._feature_event_handler)
        self._ws.register_event_handler("group", self.async_get_hierarchy)
        # self._ws.register_event_handler("device", self.async_get_hierarchy)
        self._ws.register_event_handler("deviceStatus", self._device_status_event_handler)
        
        self._ws.register_event_handler("server", self._firmware_event_handler_started, "sendingFirmwareStatus")
        self._ws.register_event_handler("server", self._firmware_event_handler_complete, "sendingFirmwareComplete")
        self._ws.register_event_handler("system", self._link_socket_event_handler, "event")

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
                    continue

                prev_value = feature.state

                feature.update_feature_state(value)

                for gen_func in self._callbacks:
                    gen_func(feature=feature.name, feature_id=feature.id, prev_value = prev_value, new_value = value)

                extra = ""
                if feature.name in ("uiButton", "uiButtonPair"):
                    decoded = feature.decode_ui_button_value(value, feature.name) if value else {}
                    extra = f" - eventType: {decoded.get('eventType')} - presses: {decoded.get('presses')} - upDown: {decoded.get('upDown')}"
                    if value and "eventType" not in decoded:
                        _LOGGER.warning(
                            f"_feature_event_handler: button value did not decode to an event type - feature_id: {feature.id} - type: {feature.name} - value: {value} - decoded: {decoded}"
                        )
                _LOGGER.debug(
                    f"_feature_event_handler: feature_id: {feature.id} - type: {feature.name} - value: {value}{extra}"
                )

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

    def _track_background(self, task):
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)

    def _clear_link_socket_state(self):
        """Drop per-link socket memory so a reused link can connect cleanly."""
        self._link_socket_state.clear()
        self._link_status_epoch.clear()
        self._link_connected_since.clear()
        self._link_restore_owed.clear()
        self._link_feature_cycle_seen.clear()
        self._link_refresh_ready_at.clear()
        for task in list(self._link_grace_tasks.values()):
            if not task.done():
                task.cancel()
        self._link_grace_tasks.clear()

    async def _link_socket_event_handler(self, message):
        """Track one Link's cloud socket and refresh features when it comes back."""
        if self._closing:
            return
        for item in message.get("items") or []:
            payload = item.get("payload") if isinstance(item, dict) else None
            if not isinstance(payload, dict):
                continue
            online = payload.get("online")
            if online is not True and online is not False:
                continue

            link_id = payload.get("linkId")
            if link_id is None:
                link_id = link_id_from_combined_id(payload.get("deviceId"))
            if link_id is None:
                _LOGGER.warning("Link socket event missing linkId - payload: %s", payload)
                continue
            link_id = str(link_id)
            if not self._accept_link_socket_generation(link_id, connected_since_ms(payload.get("connectedSince"))):
                continue
            if online is True:
                self._note_link_online(link_id)
            else:
                self._note_link_offline(link_id)

    def _accept_link_socket_generation(self, link_id, connected_since):
        """False when this event is older than a socket generation already applied.

        A missing generation cannot be ordered and is applied as it arrives.
        """
        if connected_since is None:
            return True
        newest = self._link_connected_since.get(link_id)
        if newest is not None and connected_since < newest:
            _LOGGER.debug(
                "Link socket event ignored - older generation for linkId: %s (%s < %s)",
                link_id, connected_since, newest,
            )
            return False
        self._link_connected_since[link_id] = connected_since
        return True

    def _note_link_online(self, link_id):
        """Rising edge into online. Heartbeats while already online do nothing."""
        if self._closing:
            return
        state = self._link_socket_state.get(link_id)
        if state == "online":
            return

        grace = self._link_grace_tasks.get(link_id)
        if grace is not None and not grace.done():
            grace.cancel()
        if state in _LINK_STATUS_HELD:
            self._link_status_epoch[link_id] = self._link_status_epoch.get(link_id, 0) + 1
            self._link_restore_owed.add(link_id)
            _LOGGER.debug("Link socket online - leaving %s for linkId: %s", state, link_id)
        else:
            _LOGGER.debug("Link socket online - refreshing features for linkId: %s", link_id)

        self._link_socket_state[link_id] = "online"
        self._link_feature_cycle_seen.discard(link_id)
        self._arm_link_feature_refresh(link_id)
        if state == "offline":
            self._notify_hub_firmware_callbacks(link_id)

    def _note_link_offline(self, link_id):
        """Start the disconnect grace once. A waiting grace or an existing latch stays put."""
        if self._closing:
            return
        grace = self._link_grace_tasks.get(link_id)
        if self._link_socket_state.get(link_id) == "offline":
            return
        if grace is not None and not grace.done():
            return

        _LOGGER.debug("Link socket offline - grace for linkId: %s", link_id)
        self._link_socket_state[link_id] = "grace"
        self._link_refresh_ready_at.pop(link_id, None)
        refresh = self._link_refresh_tasks.get(link_id)
        if refresh is not None and not refresh.done():
            refresh.cancel()
        task = asyncio.create_task(self._disconnect_link_after_grace(link_id))
        self._link_grace_tasks[link_id] = task
        self._track_background(task)
        task.add_done_callback(self._on_link_grace_done(link_id, task))

    async def _disconnect_link_after_grace(self, link_id):
        await asyncio.sleep(_LINK_OFFLINE_GRACE_SECS)
        if self._closing or self._link_socket_state.get(link_id) != "grace":
            return
        self._link_status_epoch[link_id] = self._link_status_epoch.get(link_id, 0) + 1
        self._link_socket_state[link_id] = "offline"
        self._mark_link_devices_disconnected(link_id)
        self._notify_hub_firmware_callbacks(link_id)
        _LOGGER.debug("Link socket grace elapsed - disconnected linkId: %s", link_id)

    def _on_link_grace_done(self, link_id, task):
        def done(_task):
            if self._link_grace_tasks.get(link_id) is task:
                self._link_grace_tasks.pop(link_id, None)
        return done

    def _mark_link_devices_disconnected(self, link_id):
        for device in self._devices_for_connection_status():
            if link_id_from_combined_id(device.device_id) == link_id:
                device.set_connection_status("disconnected")

    def link_is_latched_offline(self, device_id):
        """True after the disconnect grace has elapsed for this device's link."""
        link_id = link_id_from_combined_id(device_id)
        if not link_id:
            return False
        return self._link_socket_state.get(link_id) == "offline"

    def _notify_hub_firmware_callbacks(self, link_id):
        """The hub has no heartbeat status. Listeners still need the latch change."""
        for device in self.devices.values():
            try:
                if not device.is_hub():
                    continue
            except Exception:
                continue
            if link_id_from_combined_id(device.device_id) != link_id:
                continue
            device._call_connection_status_callbacks()

    def _link_status_held(self, link_id):
        return bool(link_id) and self._link_socket_state.get(link_id) in _LINK_STATUS_HELD

    def _arm_link_feature_refresh(self, link_id):
        if self._closing:
            return
        link_id = str(link_id)
        self._link_refresh_ready_at[link_id] = time.monotonic() + _LINK_REFRESH_DEBOUNCE_SECS
        task = self._link_refresh_tasks.get(link_id)
        if task is not None and not task.done():
            return

        task = asyncio.create_task(self._refresh_link_after_online(link_id))
        self._link_refresh_tasks[link_id] = task
        self._track_background(task)
        task.add_done_callback(self._on_link_refresh_done(link_id, task))

    def _features_for_link(self, link_id):
        target = str(link_id)
        return [
            feature for feature in self.features.values()
            if feature.can_read and link_id_from_combined_id(feature.id) == target
        ]

    async def _read_features(self, features, no_cache=False):
        """Read features and return those whose items failed or never arrived."""
        if not features:
            return []

        read_message = LW_WebsocketMessage("feature", "read")
        id_map = {}
        for feature in features:
            payload = {"featureId": feature.id}
            if no_cache:
                payload["noCache"] = True
            item_id = read_message.add_item(payload)
            id_map[item_id] = feature

        responses = await self._ws.async_sendmessage(read_message)
        failed = []
        seen = set()
        for response in responses or []:
            if not isinstance(response, dict):
                continue
            feature = id_map.get(response.get("itemId"))
            if feature is None:
                continue
            seen.add(feature.id)
            if response.get("success") is not True:
                failed.append(feature)
            try:
                feature.process_feature_read(response)
            except Exception as e:
                _LOGGER.error(f"_read_features: Error processing message - response: {response} - {str(e)} - {traceback.format_exc()}")
                if feature not in failed:
                    failed.append(feature)
        for feature in features:
            if feature.id not in seen and feature not in failed:
                failed.append(feature)
        return failed

    async def _refresh_link_after_online(self, link_id):
        restore_task = None
        saw_features = False
        try:
            # A cancel that arrives before the first await must still abort the read.
            await asyncio.sleep(0)
            if link_id in self._link_restore_owed and not self._closing:
                restore_task = asyncio.create_task(self._restore_link_connection_status(link_id))
                self._track_background(restore_task)

            while True:
                ready_at = self._link_refresh_ready_at.get(link_id)
                if ready_at is None:
                    break
                delay = ready_at - time.monotonic()
                if delay > 0:
                    await asyncio.sleep(delay)
                    continue

                self._link_refresh_ready_at.pop(link_id, None)
                pending = None
                for attempt in range(_LINK_REFRESH_ATTEMPTS):
                    if link_id in self._link_refresh_ready_at:
                        break

                    if pending is None:
                        features = self._features_for_link(link_id)
                    else:
                        features = pending
                    if not features:
                        _LOGGER.debug(f"_refresh_link_after_online: No readable features yet for linkId: {link_id}")
                        if attempt + 1 < _LINK_REFRESH_ATTEMPTS:
                            await asyncio.sleep(_LINK_REFRESH_RETRY_SECS)
                            continue
                        break

                    saw_features = True
                    try:
                        failed = await self._read_features(features, no_cache=True)
                    except asyncio.CancelledError:
                        raise
                    except Exception:
                        _LOGGER.exception(f"_refresh_link_after_online: feature read failed for linkId: {link_id}")
                        failed = list(features)

                    if not failed:
                        _LOGGER.debug(f"_refresh_link_after_online: Refreshed {len(features)} features for linkId: {link_id}")
                        break

                    _LOGGER.debug(f"_refresh_link_after_online: {len(failed)} features unread for linkId: {link_id}")
                    pending = failed
                    if attempt + 1 < _LINK_REFRESH_ATTEMPTS:
                        await asyncio.sleep(_LINK_REFRESH_RETRY_SECS)

                if link_id not in self._link_refresh_ready_at:
                    break

            if saw_features and self._link_socket_state.get(link_id) == "online":
                self._link_feature_cycle_seen.add(link_id)
            if restore_task is not None:
                await restore_task
        except asyncio.CancelledError:
            if restore_task is not None and not restore_task.done():
                restore_task.cancel()
                await asyncio.gather(restore_task, return_exceptions=True)
            raise

    def _on_link_refresh_done(self, link_id, task):
        """Drop the mapping when the cycle ends, including a cancel before it starts."""
        def done(_task):
            if self._link_refresh_tasks.get(link_id) is task:
                self._link_refresh_tasks.pop(link_id, None)
            if (
                not self._closing
                and link_id in self._link_refresh_ready_at
                and link_id not in self._link_refresh_tasks
            ):
                self._arm_link_feature_refresh(link_id)
        return done

    async def _restore_link_connection_status(self, link_id):
        """Re-read status once the socket is online again. A failed attempt leaves the current status."""
        for attempt in range(_LINK_REFRESH_ATTEMPTS):
            if self._closing or self._link_socket_state.get(link_id) != "online":
                return
            devices = [
                device for device in self._devices_for_connection_status()
                if link_id_from_combined_id(device.device_id) == link_id
            ]
            if not devices:
                return

            try:
                ok = await self._async_read_device_statuses(
                    devices,
                    clear_on_failure=False,
                    on_send=lambda: self._link_restore_owed.discard(link_id),
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                _LOGGER.exception(f"_restore_link_connection_status: status read failed for linkId: {link_id}")
                ok = False
            if ok:
                return
            if attempt + 1 < _LINK_REFRESH_ATTEMPTS:
                await asyncio.sleep(_LINK_REFRESH_RETRY_SECS)

    async def _device_status_event_handler(self, message):
        """Apply a hub deviceStatus event. Payload is deviceId and status."""
        items = message.get("items") or []
        for item in items:
            payload = item.get("payload") if isinstance(item, dict) else None
            if not isinstance(payload, dict):
                continue
            device_id = payload.get("deviceId")
            device = self.devices.get(device_id)
            if device is None or device.is_hub():
                continue
            link_id = link_id_from_combined_id(device.device_id)
            if self._link_status_held(link_id):
                continue
            device.set_connection_status(payload.get("status"))

    def _status_result_current(self, device, captured_epoch):
        """False once this link was held or its epoch moved after the read was sent."""
        link_id = link_id_from_combined_id(device.device_id)
        if self._link_status_held(link_id):
            return False
        current = self._link_status_epoch.get(link_id, 0) if link_id else 0
        return captured_epoch == current

    def _apply_device_status_response(self, device, response, clear_on_failure=True):
        """Store a deviceStatus.read item. A failed startup item clears the status."""
        if not isinstance(response, dict) or response.get("success") is not True:
            if response is not None:
                _LOGGER.warning(f"deviceStatus.read failed for {device.device_id} - {response}")
            if clear_on_failure:
                device.set_connection_status(None)
                return None
            return device.connection_status
        payload = response.get("payload") if isinstance(response.get("payload"), dict) else {}
        return device.set_connection_status(payload.get("status"))

    def _devices_for_connection_status(self):
        """Gen2 devices whose online state comes from the hub heartbeat cache."""
        devices = []
        for device in self.devices.values():
            try:
                if device.is_gen2() and not device.is_hub():
                    devices.append(device)
            except Exception:
                _LOGGER.debug(f"_devices_for_connection_status: skipped {getattr(device, 'device_id', None)}")
        return devices

    async def _async_read_device_statuses(self, devices=None, clear_on_failure=True, on_send=None):
        """Read deviceStatus for gen2 devices. Each device keeps the epoch from send time."""
        if devices is None:
            devices = self._devices_for_connection_status()
        if not devices:
            return True

        readmess = LW_WebsocketMessage("deviceStatus", "read")
        id_map = {}
        epochs = {}
        for device in devices:
            link_id = link_id_from_combined_id(device.device_id)
            if self._link_status_held(link_id):
                continue
            item_id = readmess.add_item({"deviceId": device.device_id})
            id_map[item_id] = device
            epochs[device.device_id] = self._link_status_epoch.get(link_id, 0) if link_id else 0

        if not id_map:
            return True

        if on_send is not None:
            on_send()

        try:
            responses = await self._ws.async_sendmessage(readmess)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            _LOGGER.warning(f"_async_read_device_statuses: {e}")
            if clear_on_failure:
                for device in id_map.values():
                    if self._status_result_current(device, epochs[device.device_id]):
                        device.set_connection_status(None)
            return False

        seen = set()
        ok = True
        for response in responses or []:
            device = id_map.get(response.get("itemId")) if isinstance(response, dict) else None
            if device is None:
                continue
            if not self._status_result_current(device, epochs[device.device_id]):
                ok = False
                continue
            seen.add(device.device_id)
            if response.get("success") is True:
                self._apply_device_status_response(device, response, clear_on_failure=False)
            else:
                ok = False
                self._apply_device_status_response(device, response, clear_on_failure=clear_on_failure)
        for device in id_map.values():
            if device.device_id in seen:
                continue
            if not self._status_result_current(device, epochs[device.device_id]):
                ok = False
                continue
            ok = False
            if clear_on_failure:
                device.set_connection_status(None)
        return ok

    async def async_refresh_device_connection_status(self, device_id):
        """Re-read one device before an install. A latched hub follows its socket."""
        device = self.devices.get(device_id)
        if device is None:
            return None
        if device.is_hub():
            if not self.connected or self.link_is_latched_offline(device.device_id):
                return None
            return "connected"
        link_id = link_id_from_combined_id(device.device_id)
        if self._link_status_held(link_id):
            return device.connection_status
        await self._async_read_device_statuses([device], clear_on_failure=True)
        return device.connection_status

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

        self._track_background(asyncio.create_task(self.async_update_featureset_states()))
        self._track_background(asyncio.create_task(self._async_read_firmware()))
        self._track_background(asyncio.create_task(self._async_read_device_statuses()))
        self._track_background(asyncio.create_task(self._kick_online_link_feature_refreshes()))

    async def _kick_online_link_feature_refreshes(self):
        """One refresh for an online link whose earlier cycle ran before its features existed."""
        if self._closing:
            return
        seen = set()
        for feature in self.features.values():
            if not feature.can_read:
                continue
            link_id = link_id_from_combined_id(feature.id)
            if not link_id or link_id in seen:
                continue
            seen.add(link_id)
            if self._link_socket_state.get(link_id) != "online":
                continue
            if link_id in self._link_feature_cycle_seen:
                continue
            task = self._link_refresh_tasks.get(link_id)
            if task is not None and not task.done():
                continue
            self._arm_link_feature_refresh(link_id)
        

    def _room_names_by_featureset(self, hierarchy_payload):
        """Map a featureset id to its Lightwave room name.

        Rooms come from the hierarchy payload. The structure root is not a room.
        Missing room data leaves the map empty rather than using a device name.
        """
        rooms = hierarchy_payload.get("room") if isinstance(hierarchy_payload, dict) else None
        if not isinstance(rooms, list):
            return {}
        names = {}
        for room in rooms:
            if not isinstance(room, dict):
                continue
            name = room.get("name")
            if not isinstance(name, str) or not name.strip():
                continue
            for featureset_id in room.get("featureSets") or []:
                names.setdefault(featureset_id, name.strip())
        return names

    async def _async_read_groups(self):
        self.featuresets = {}
        for device in self.devices.values():
            device.room_name = None
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
                hierarchy_payload = hierarchy_responses[0]["payload"]
                featuresets = list(hierarchy_payload["featureSet"])
                room_names = self._room_names_by_featureset(hierarchy_payload)
                
                self.get_featuresets(featuresets, devices, features, room_names)
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

    async def async_register_connection_status_callback(self, device_id, callback):
        if device_id in self.devices:
            self.devices[device_id].register_connection_status_callback(callback)
        else:
            _LOGGER.warning(f"async_register_connection_status_callback: Device not found: {device_id}")

    def unregister_connection_status_callback(self, device_id, callback):
        device = self.devices.get(device_id)
        if device is not None:
            device.unregister_connection_status_callback(callback)

    @property
    def connected(self):
        """True while the websocket session is authenticated."""
        return self._ws.connected

    def register_connect_callback(self, callback):
        self._ws.register_connect_callback(callback)

    def unregister_connect_callback(self, callback):
        self._ws.unregister_connect_callback(callback)

    def register_disconnect_callback(self, callback):
        """Register a callback for a drop after the link has been up."""
        self._ws.register_disconnect_callback(callback)

    def unregister_disconnect_callback(self, callback):
        self._ws.unregister_disconnect_callback(callback)

    async def async_connect(self, max_tries=None, force_keep_alive_secs=0, source="link-async_connect"):
        return await self._ws.async_connect(max_tries, force_keep_alive_secs, source)

    async def async_force_reconnect(self, secs):
        self._ws.async_force_reconnect(secs)

    async def async_deactivate(self, source="link-deactivate"):
        self._closing = True
        self._link_refresh_ready_at.clear()
        if self._background_tasks:
            _LOGGER.debug(f"async_deactivate ({source}): Cancelling {len(self._background_tasks)} LWLink2 background tasks")
            tasks_to_cancel = []
            while self._background_tasks:
                task = self._background_tasks.pop()
                tasks_to_cancel.append(task)
                task.cancel()
            
            if tasks_to_cancel:
                await asyncio.gather(*tasks_to_cancel, return_exceptions=True)
        
        await self.auth.close()
        
        await self._ws.async_deactivate(source)
        
    async def async_activate(self, max_tries=None, force_keep_alive_secs=0, source="link-activate", connect_callback=None):
        self._closing = False
        self._clear_link_socket_state()
        self._ws.activate(source=source)
        
        if connect_callback:
            self._ws.register_connect_callback(connect_callback)
        
        connected = await self.async_connect(max_tries, force_keep_alive_secs, source)
        if not connected:
            await self._ws.async_deactivate(f'link-activate-not-connected-{source}')
        return connected

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

    def get_featuresets(self, featuresets, devices, features, room_names=None):
        if room_names is None:
            room_names = {}
        for y in featuresets:
            new_featureset = LWRFFeatureSet()
            new_featureset.link = self
            new_featureset.featureset_id = y["groupId"]
            room_name = room_names.get(new_featureset.featureset_id)
            if room_name:
                new_featureset.room_name = room_name
            
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
            if new_featureset.room_name and not device.room_name:
                device.room_name = new_featureset.room_name
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