class Store:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def save(self):
        return 1

    def load(self):
        return 2


class Other:
    def save(self):
        return 3


class Service:
    def __init__(self, backup: Store):
        self.store = Store()
        self.backup = backup

    def run(self):
        return self.store.save() + self.backup.load()


def local_var():
    s = Store()
    return s.load()


def annotated(s: Store):
    return s.save()


def ambiguous(flag):
    x = Store()
    if flag:
        x = Other()
    return x.save()


def ctx():
    with Store() as s:
        return s.load()


def unknown(obj):
    return obj.save()


def reassigned_none():
    y = Store()
    y = None
    return y.load()
