import threading


def leaf():
    return 1


def deco(fn):
    def wrapper(*a, **k):
        return fn(*a, **k)
    return wrapper


@deco
def decorated():
    return leaf()


class Box:
    def __init__(self):
        self.v = leaf()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def get_value(self):
        return self.v


def use_box():
    with Box() as b:
        return b.get_value()


def via_lambda():
    f = lambda: leaf()
    return f()


def via_genexpr():
    return sum(leaf() for _ in range(3))


def dynamic():
    return getattr(Box(), "get_value")()


def threaded():
    out = []
    t = threading.Thread(target=lambda: out.append(leaf()))
    t.start()
    t.join()
    return out


def never_called():
    return leaf()
