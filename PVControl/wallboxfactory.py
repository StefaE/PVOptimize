from .wallbox.hardybarth import HardyBarth
from .wallbox.wbtemplate import DummyWB

class WallBoxFactory:
    '''
    Calls appropriate implementation of WBTemplate, based on parameter 'source'
    '''
    
    def getWallBox(self, name, config):
        wallbox   = None
        if   name == 'HardyBarth': wallbox = HardyBarth(config)
        elif name == 'dummy'     : wallbox = DummyWB()
        else: raise ValueError()
        return(wallbox)