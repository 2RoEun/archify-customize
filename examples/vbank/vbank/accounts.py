from .store import Store


class Accounts:
    def __init__(self, root):
        self.store = Store(root)

    def deposit(self, name, amount):
        def operation():
            self.store.append({"type": "deposit", "name": name, "amount": amount})
            return amount
        return self.store.locked(operation)

    def withdraw(self, name, amount):
        if amount > self.balance(name):
            raise ValueError("insufficient funds")

        def operation():
            self.store.append({"type": "withdraw", "name": name, "amount": amount})
            return amount
        return self.store.locked(operation)

    def balance(self, name):
        total = 0
        for e in self.store.read_all():
            if e["name"] == name:
                total += e["amount"] if e["type"] == "deposit" else -e["amount"]
        return total
