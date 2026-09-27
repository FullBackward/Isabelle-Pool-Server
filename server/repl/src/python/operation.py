class Success:
    msg: str
    data: str | dict | None = None
    def __init__(self, msg: str, data: str | dict | None = None) -> None:
        self.msg = msg
        self.data = data

class Failure:
    err: str
    def __init__(self, err: str) -> None:
        self.err = err