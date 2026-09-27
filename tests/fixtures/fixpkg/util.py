CONST = 1
DATA_FILE = "state/cache.json"


def helper(x):
    return x


def save_cache(text):
    open(DATA_FILE, "w").write(text)


class Worker:
    def step(self):
        return helper(3)


LOCK_FILE = ".state.lock"
