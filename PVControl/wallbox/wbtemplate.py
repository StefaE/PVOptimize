import math
from abc import ABC, abstractmethod

class WBTemplate(ABC):
    """
    Abstract class to implement a Wallbox provider
    """
    def __init__(self, config = None):
        self.status = []

    def round_current(self, I):                                                          # WB rounds to 1A
        """
        A wallbox may not allow arbitrarily granular charge current control. This method is called to
        round the calculated charging current I to something the wallbox can actually do. By default
        rounding down to integer value is done.
        """
        return(math.floor(I))

    @abstractmethod
    def readWB(self):
        """
        Reads wallbox status and returns a dictionary with (at least) following mandatory keys
            connected        : boolean - is a car connected?
            charge_completed : boolean - is charging completed?
            I_min            : float   - minimal charging current that can be provided [A]
            I_max            : float   - maximum charging current that can be provided [A]
            ctrl_current     : actually provided charging current [A]

            Additional keys can be provided and will be stored through PVControl._logInflux into measurement wbstatus
            See actual implementation for these additional fields
        """
        pass

    @abstractmethod
    def controlWB(self):
        """
        Set wallbox into desired state. For this, use is made of paramters read in readWB(),
        even though some of these parameters may not be used/modified outside this class.

        Paramters
        ---------
            I_new : new setpoint current [A]
        """
        pass

class DummyWB(WBTemplate):
    def __init__(self, config = None):
        self.status = []

    def readWB(self):
        pass

    def controlWB(self):
        pass
