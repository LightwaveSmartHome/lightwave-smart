def get_highest_version(versions):
    def version_key(v):
        return tuple(map(int, v.split(".")))

    return max(versions, key=version_key)