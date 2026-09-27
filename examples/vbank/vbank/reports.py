from .accounts import Accounts


def summary(root, names):
    acc = Accounts(root)
    return {n: acc.balance(n) for n in names}


def audit(root):
    """Never called anywhere."""
    return len(Accounts(root).store.read_all())
