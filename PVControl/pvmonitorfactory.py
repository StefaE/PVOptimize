from .pvmonitor.solaranzeige import SolarAnzeige
from .pvmonitor.kostal       import Kostal

class PVMonitorFactory:
    '''
    Calls appropriate implementation of PVMonitorTemplate, based on parameter 'source'
    '''

    def getPVMonitor(self, source, config):
        pvmonitor   = None
        if   source == 'SolarAnzeige': pvmonitor = SolarAnzeige(config)
        elif source == 'Kostal'      : pvmonitor = Kostal(config)
        else: raise ValueError()
        return(pvmonitor)