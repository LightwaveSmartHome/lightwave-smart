import json


def get_highest_version(versions):
    def version_key(v):
        return tuple(map(int, v.split(".")))

    return max(versions, key=version_key)

def pretty_print_json(json_data):
    return json.dumps(json_data, indent=4)

class LWConnectionException(ConnectionError):
    def __init__(self, message, code=None, retry=None):
        super().__init__(message)
        self.code = code
        self.retry = retry