import logging
import datetime
import aiohttp
import json
import traceback
from .utils import pretty_print_json, LWConnectionException

_LOGGER = logging.getLogger(__name__)
_LOGGER.setLevel(logging.DEBUG)

VERSION = "1.6.9"

PUBLIC_AUTH_SERVER = "https://auth.lightwaverf.com"
LOGIN_ENDPOINT = "/v2/lightwaverf/autouserlogin/lwapps"


class LWAuth:
    def __init__(self, auth_method="refresh", api_key=None, access_token=None, refresh_token=None):
        self._session = None

        self._auth_method = auth_method

        self._api_key = api_key
        self._access_token = access_token
        self._refresh_token = refresh_token
        self._token_expiry = None

        self._username = None   # TODO - to be removed
        self._password = None   # TODO - to be removed

        self._token_refresh_callback = None


    def set_auth_method(self, auth_method, api_key=None, access_token=None, refresh_token=None, token_expiry=None, username=None, password=None):
        self._auth_method = auth_method
        
        self._api_key = api_key
        
        self._access_token = access_token
        self._refresh_token = refresh_token
        self._token_expiry = token_expiry

        self._username = username
        self._password = password

    async def async_get_access_token(self):
        if not self._access_token:
            await self._renew_access_token()
        return self._access_token

    def invalidate_access_token(self):
        self._access_token = None
        self._token_expiry = None

        return True # Retry allowed

    # Not useful if get_access_token is overridden
    def set_token_refresh_callback(self, callback):
        self._token_refresh_callback = callback

    def get_tokens(self):
        return {
            "access_token": self._access_token,
            "refresh_token": self._refresh_token,
            "token_expiry": self._token_expiry
        }
        
    async def _renew_access_token(self):
        if self._auth_method == "refresh":
            await self._get_access_token_refresh()
        elif self._auth_method == "password":
            await self._get_access_token_username()
        elif self._auth_method == "api_key":
            await self._get_access_token_api_key()
        else:
            raise ValueError(f"auth_method must be 'refresh', 'password' or 'api_key' - got: '{self._auth_method}'")

    async def _get_access_token_refresh(self):
        _LOGGER.debug("get_access_token_refresh: Requesting tokens (using refresh token)")
        
        if not self._refresh_token:
            raise LWConnectionException("No refresh token", retry=False)
        
        authentication = { "grant_type": "refresh_token", "refresh_token": self._refresh_token }
        session = await self.get_session()
        async with session.post(PUBLIC_AUTH_SERVER + LOGIN_ENDPOINT, headers={"x-lwrf-appid": "ios-01"}, json=authentication) as req:
            _LOGGER.debug(f"get_access_token_refresh: Received response with status: {req.status} - [contents hidden for security]")
            if req.status == 200:
                self._set_tokens_from_response(await req.json())
            else:
                text = await req.text()
                _LOGGER.warning(f"get_access_token_refresh: Authentication failed (status_code '{req.status}') - refresh token: '{self._refresh_token[:1]}...{self._refresh_token[-1:]}' - {text}")

                try:
                    text_as_json = json.loads(text) 
                    if text_as_json["error"] == "invalid_token" and text_as_json["error_description"] == "Unknown refresh token":
                        self._refresh_token = None
                        raise ConnectionError(f"Invalid refresh token: {text}")
                except json.JSONDecodeError:
                    pass
                
                raise ConnectionError(f"Authentication failed: {text}")

    async def _get_access_token_api_key(self):
        _LOGGER.debug("get_access_token_api: Requesting tokens (using API key and refresh token)")
        
        if not self._refresh_token:
            raise LWConnectionException("No refresh token", retry=False)
        
        authentication = { "grant_type": "refresh_token", "refresh_token": self._refresh_token }
        session = await self.get_session()
        async with session.post(PUBLIC_AUTH_SERVER + "/token",
                            headers={"authorization": "basic " + self._api_key},
                            json=authentication) as req:
            _LOGGER.debug(f"get_access_token_api: Received response with status: {req.status} - [contents hidden for security]")
            if req.status == 200:
                self._set_tokens_from_response(await req.json())
            else:
                _LOGGER.warning(f"get_access_token_api: No authentication token (status_code '{req.status}') - refresh token: '{self._refresh_token[:1]}...{self._refresh_token[-1:]}'")
                raise ConnectionError(f"No authentication token: {await req.text()}")

    async def _get_access_token_username(self):
        _LOGGER.debug("get_access_token_username: Requesting tokens (using username and password)")
        
        if not self._username or not self._password:
            raise LWConnectionException("No username or password", retry=False)
        
        authentication = { "email": self._username, "password": self._password, "version": VERSION }
        session = await self.get_session()
        async with session.post(PUBLIC_AUTH_SERVER + LOGIN_ENDPOINT, headers={"x-lwrf-appid": "ios-01"}, json=authentication) as req:
            _LOGGER.debug(f"get_access_token_username: Received response with status: {req.status} - [contents hidden for security]")
            if req.status == 200:
                self._set_tokens_from_response(await req.json())
            elif req.status == 404:
                _LOGGER.warning("get_access_token_username: Authentication failed - if network is ok, possible wrong username/password")
            else:
                _LOGGER.warning(f"get_access_token_username: Authentication failed - status {req.status}")

    def _set_tokens_from_response(self, response):
        tokens = response["tokens"] if "tokens" in response else response
        
        self._access_token = tokens["access_token"]
        self._refresh_token = tokens["refresh_token"]
        self._token_expiry = datetime.datetime.now() + datetime.timedelta(seconds=tokens["expires_in"])
        
        if self._token_refresh_callback:
            try:
                self._token_refresh_callback(self._access_token, self._refresh_token, self._token_expiry)
            except Exception as e:
                _LOGGER.error(f"set_tokens: Error calling token refresh callback (continuing) - {str(e)} - {traceback.format_exc()}")

    #########################################################
    # Session management
    #########################################################
    async def close(self):
        if self._session:
            await self._session.close()
        self._session = None
        
    async def open(self):
        if not self._session:
            self._session = aiohttp.ClientSession()
            
    async def get_session(self):
        if not self._session:
            await self.open()
        return self._session

