print("hello World")

class Socket():
    """blueprint for a socket"""
    def __init__(self, consumption_wh: str, id: int, is_on: bool, is_off: bool):
        """instantiates a socket object"""
        self.consumption_wh = consumption_wh
        self.id = id
        self.is_on = is_on
        self.is_off = is_off

    def turn_on(self, is_on: bool)-> bool:
        self.is_on = True
        return self.is_on


        




