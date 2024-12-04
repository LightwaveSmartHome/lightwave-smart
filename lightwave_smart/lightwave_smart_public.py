import asyncio
import aiohttp

import logging

from .link import LWLink2
from .device import LWRFFeatureSet, LWRFFeature


_LOGGER = logging.getLogger(__name__)

PUBLIC_API = "https://publicapi.lightwaverf.com/v1/"

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
