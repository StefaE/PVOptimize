'''
Copyright (C) 2021    Stefan Eichenberger   se_misc ... hotmail.com
This file is part of the PVOptimize project, licensed under Gnu General Public License v3
'''
import pandas as pd
from influxdb import InfluxDBClient
from .pvmonitortemplate import PVMonitorTemplate

class SolarAnzeige(PVMonitorTemplate):
    """
    This class implements the abstract Class PVMonitorTemplate for data stored in a 
    SolarAnzeige Influx database.

    The current implementation assumes that SolarAnzeige is configured for a Kostal inverter
    (hardware ID = 17). Other inverters store some corresponding information in Influx fields
    named differently.

    Methods are further described in abstract Class PVMonitorTemplate

    Methods
    -------
        getStatus(start = None, stop = None)
            If start and stop are not defined, returns last (most current) record in Influx
            Else, extracts data betweeen start and stop (used by Class PVServer)
        setBatCharge()
            Dummy procedure, since SolarAnzeige cannot control inverter battery charging

    Public methods are described in abstract Class PVMonitorTemplate
    """
    def __init__(self, config):
        self.config           = config
        host                  = self.config['SolarAnzeige'].get('host')
        port                  = self.config['SolarAnzeige'].getint('port', 8086)
        database              = self.config['SolarAnzeige'].get('database')
        self._influx          = InfluxDBClient(host=host, port=port, database=database)


    def getStatus(self, start = None, stop = None):
        pv       = self._query('PV',       start, stop)
        ac       = self._query('AC',       start, stop)
        bat      = self._query('Batterie', start, stop)
        pvDict   = []
        acDict   = []
        batDict  = []
        for row in pv.get_points():
            pvDict.append( { 'datetime'     : row['time'],
                             'dc_power'     : row['Gesamtleistung']})
        pvDF           = pd.DataFrame.from_dict(pvDict)
        for row in ac.get_points():
            if 'KSEM_Leistung' in row: grid_power =  row['KSEM_Leistung']                # not an 'official' solaranzeige field
            else:                      grid_power = -row['Einspeisung']                  # >0 = grid consumption, <0 = feed-in
            acDict.append( { 'datetime'              : row['time'],
                             'grid_voltage'          : (row['Spannung_R'] + row['Spannung_S'] + row['Spannung_T'])/3,
                             'home_consumption'      : row['Verbrauch'],
                             'home_consumption_bat'  : row['Verbrauch_Batterie'],
                             'home_consumption_grid' : row['Verbrauch_Netz'],
                             'home_consumption_pv'   : row['Verbrauch_PV'],
                             'grid_power'            : grid_power })
        acDF           = pd.DataFrame.from_dict(acDict)
        for row in bat.get_points():
            batDict.append( { 'datetime'     : row['time'],
                              'soc'          : row['SOC']/100,
                              'bat_power'    : row['Spannung']*row['Strom'],             # >0 = battery charge, <0 = discharge
                              'bat_voltage'  : row['Spannung'],
                              'bat_current'  : row['Strom']})
        batDF              = pd.DataFrame.from_dict(batDict)

        pvData             = pd.merge(pvDF,   acDF,  on='datetime', how='inner')         # may drop a row if not both tables were already updated 
        pvData             = pd.merge(pvData, batDF, on='datetime', how='inner')
        pvData['datetime'] = pd.to_datetime(pvData['datetime'])
        pvData.set_index('datetime', inplace=True)
        if start is None and stop is None:                                               # we are in active controller context
            pvData = pvData.iloc[0]
        return(pvData)

    def setBatCharge(self):
        '''
        Dummy procedure - SolarAnzeige is unable to control home battery charging
        (SolarAnzeige 4.7.2 can be extended with custom .php code, but that's not considered here)
        '''
        pass

    def _query(self, table, start = None, stop = None):
        if start is None and stop is None:
            sql  = 'SELECT * FROM "' + table + '" ORDER BY time DESC LIMIT 2'            # return two, so that after merge we have at least one
        else:
            sql  = 'SELECT * FROM "' + table + '"' + "WHERE time > '" + start + "' AND time < '" + stop + "'"
        data = self._influx.query(sql)
        return(data)
